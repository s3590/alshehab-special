importScripts('https://www.gstatic.com/firebasejs/8.10.1/firebase-app.js' );
importScripts('https://www.gstatic.com/firebasejs/8.10.1/firebase-messaging.js' );
importScripts('/static/localforage.min.js'); // 🌟 المسار المحلي الصحيح

// 🌟 الرابط الأساسي للسيرفر (مهم جداً لتوجيه طلبات الأوفلاين)
const SERVER_URL = 'https://my-bot-ehio.onrender.com';

// ================= 1. إعدادات Firebase =================
firebase.initializeApp({
  apiKey: "AIzaSyBBYxCDcGilfl_xAiAFzaYH-G-7L_jR7Zo",
  authDomain: "alshehab-pro.firebaseapp.com",
  projectId: "alshehab-pro",
  storageBucket: "alshehab-pro.firebasestorage.app",
  messagingSenderId: "356161376498",
  appId: "1:356161376498:web:6b76713ca26e4e3c76c06b"
} );

const messaging = firebase.messaging();

messaging.onBackgroundMessage(function(payload) {
  if (payload.data && payload.data.is_silent === 'true') {
      syncOfflineQueue();
      return;
  }
  const notificationTitle = payload.notification ? payload.notification.title : (payload.data ? payload.data.title : 'إشعار جديد');
  const notificationOptions = {
    body: payload.notification ? payload.notification.body : (payload.data ? payload.data.body : ''),
    icon: '/static/logo.png',
    badge: '/static/logo.png'
  };
  return self.registration.showNotification(notificationTitle, notificationOptions);
});

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    if ('clearAppBadge' in navigator) navigator.clearAppBadge().catch(e=>e);
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
            for (let i = 0; i < clientList.length; i++) {
                let client = clientList[i];
                if (client.url.includes('/') && 'focus' in client) return client.focus();
            }
            if (clients.openWindow) return clients.openWindow('/');
        })
    );
});

// ================= 2. نظام الأوفلاين الفولاذي (Cache First Strategy) =================
const CACHE_NAME = 'shehab-offline-vCACHE_VERSION_PLACEHOLDER'; // 🌟 السيرفر سيضع رقم الإصدار هنا تلقائياً

const CORE_ASSETS = [
    '/', 
    '/static/manifest.json',
    '/static/logo.png',
    'https://fonts.googleapis.com/css2?family=Cairo:wght@400;700;900&display=swap',
    '/static/localforage.min.js', 
    'https://www.gstatic.com/firebasejs/8.10.1/firebase-app.js',
    'https://www.gstatic.com/firebasejs/8.10.1/firebase-messaging.js'
];

self.addEventListener('install', (event ) => {
    self.skipWaiting(); // إجبار التنصيب الفوري
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return Promise.all(
                CORE_ASSETS.map(url => {
                    return cache.add(url).catch(err => console.log('⚠️ فشل حفظ الملف مؤقتاً:', url, err));
                })
            );
        })
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cache) => {
                    if (cache !== CACHE_NAME) return caches.delete(cache);
                })
            );
        })
    );
    self.clients.claim(); // السيطرة الفورية على كل الصفحات
});

// 🚀 السحر هنا: استراتيجية (الكاش أولاً) لمنع ظهور الديناصور نهائياً
self.addEventListener('fetch', (event) => {
    const url = event.request.url;

    // تجاهل طلبات الـ API لكي لا نخزن بيانات قديمة
    if (url.includes('/api/') || url.includes('/webhook/') || event.request.method !== 'GET') {
        return; 
    }

    event.respondWith(
        caches.match(event.request, {ignoreSearch: true}).then((cachedResponse) => {
            // 1. إذا وجدنا الصفحة في الكاش، نرجعها فوراً
            if (cachedResponse) {
                // 🌟 الإصلاح الأمني: لا نحدث الكاش في الخلفية لملف HTML الأساسي لمنع حلقة إعادة التحميل اللانهائية
                if (!event.request.url.endsWith('/') && !event.request.url.includes('index.html')) {
                    event.waitUntil(
                        fetch(event.request).then((networkResponse) => {
                            if (networkResponse && networkResponse.status === 200) {
                                caches.open(CACHE_NAME).then((cache) => {
                                    cache.put(event.request, networkResponse.clone());
                                });
                            }
                        }).catch(() => {}) 
                    );
                }
                return cachedResponse;
            }

            // 2. إذا لم تكن في الكاش، نحاول جلبها من النت
            return fetch(event.request).then((networkResponse) => {
                // حفظ الملفات الجديدة في الكاش (فقط الملفات الصالحة)
                if (networkResponse && networkResponse.status === 200 && networkResponse.type === 'basic') {
                    let responseToCache = networkResponse.clone();
                    caches.open(CACHE_NAME).then((cache) => {
                        cache.put(event.request, responseToCache);
                    });
                }
                return networkResponse;
            }).catch(() => {
                // 3. خط الدفاع الأخير: إذا انقطع النت فجأة
                if (event.request.mode === 'navigate' || (event.request.headers.get('accept') && event.request.headers.get('accept').includes('text/html'))) {
                    return caches.match('/', {ignoreSearch: true});
                }
            });
        })
    );
});

// ================= 3. المزامنة الشبحية في الخلفية =================
self.addEventListener('sync', (event) => {
    if (event.tag === 'sync-offline-data') {
        event.waitUntil(syncOfflineQueue());
    }
});

// 🌟 الإصلاح الأمني: تفويض المزامنة للنافذة المفتوحة (Client) لأنها تملك وصولاً آمناً لـ localforage
async function syncOfflineQueue() {
    try {
        const clientsList = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
        if (clientsList && clientsList.length > 0) {
            // إرسال أمر للنافذة المفتوحة لتقوم هي بالمزامنة
            clientsList.forEach(client => {
                client.postMessage({ type: 'TRIGGER_SYNC' });
            });
        } else {
            console.log("⚠️ لا توجد نافذة مفتوحة لتنفيذ المزامنة. سيتم التأجيل.");
        }
    } catch (err) { 
        console.error("Sync Error:", err); 
    }
}

self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});
