import asyncio
import logging
import os
import traceback
from logging.handlers import RotatingFileHandler
import html 
import aiohttp 
import time # 🌟 [جديد]

from aiohttp import web 
from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# 🌟 [جديد] ضبط توقيت السيرفر الأساسي على توقيت اليمن/السعودية
os.environ['TZ'] = 'Asia/Aden'
try:
    time.tzset( )
except AttributeError:
    # نظام ويندوز لا يدعم tzset، نتجاهل الخطأ إذا كنت تجرب على جهازك المحلي
    pass

# =====================================================================
# 🌟 تحميل ملفات النظام الأساسية (الأوفلاين والخطوط ) تلقائياً (بدون تجميد السيرفر)
# =====================================================================
async def download_initial_assets():
    os.makedirs("static", exist_ok=True)
    file_path = "static/localforage.min.js"
    font_path = "Amiri-Regular.ttf"

    # 🌟 الحماية 1: مسح الملفات المعطوبة إذا كان حجمها صغيراً جداً (غير مكتملة التحميل)
    if os.path.exists(file_path) and os.path.getsize(file_path) < 20000:
        os.remove(file_path)
    if os.path.exists(font_path) and os.path.getsize(font_path) < 50000: # الخط عادة حجمه أكبر من 50KB
        os.remove(font_path)

    # 🌟 الحماية 2: إضافة مهلة زمنية (Timeout) لمنع تعليق السيرفر للأبد عند الإقلاع
    timeout = aiohttp.ClientTimeout(total=30 )
    
    async with aiohttp.ClientSession(timeout=timeout ) as session:
        # 1. تحميل مكتبة الأوفلاين
        if not os.path.exists(file_path):
            try:
                print("⏳ جاري تحميل مكتبة الأوفلاين (localforage)...")
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                async with session.get("https://cdn.jsdelivr.net/npm/localforage@1.10.0/dist/localforage.min.js", headers=headers ) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        # 🌟 الحماية 3: الكتابة الآمنة (نكتب المحتوى فقط إذا اكتمل التحميل بنجاح)
                        with open(file_path, 'wb') as f:
                            f.write(content)
                        print("✅ تم تحميل المكتبة بنجاح!")
                    else:
                        print(f"❌ فشل تحميل المكتبة، كود الخطأ: {resp.status}")
            except Exception as e:
                print(f"❌ فشل تحميل المكتبة: {e}")

        # 2. تحميل الخط العربي
        if not os.path.exists(font_path):
            try:
                print("⏳ جاري تحميل الخط العربي للفواتير...")
                async with session.get("https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Regular.ttf" ) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        # 🌟 الحماية 3: الكتابة الآمنة
                        with open(font_path, 'wb') as f:
                            f.write(content)
                        print("✅ تم تحميل الخط بنجاح!")
                    else:
                        print(f"❌ فشل تحميل الخط، كود الخطأ: {resp.status}")
            except Exception as e:
                print(f"❌ فشل تحميل الخط: {e}")
                print("⚠️ تنبيه: سيتم استخدام الخط الافتراضي للنظام في الفواتير حتى يتم تحميل الخط العربي بنجاح.")

# =====================================================================
# 🌟 إعدادات النظام والموجهات
# =====================================================================
from config import BOT_TOKEN, ADMIN_ID
import database
from scheduler import start_scheduler

import client_panel 
import unified_main
import tickets 
import ai_chat_panel
from web_api import setup_web_api

# إعداد سجل الأخطاء (Logs) لمراقبة عمل النظام
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler("system_logs.log", maxBytes=5*1024*1024, backupCount=3, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

print(f"🔍 التوكن الذي يقرأه البوت الآن يبدأ بـ: {str(BOT_TOKEN)[:10]}... وطوله: {len(str(BOT_TOKEN))}")

# تفعيل وضع HTML لكي تظهر النصوص منسقة بشكل احترافي
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) 
dp = Dispatcher() 

# ================= صائد الأخطاء البرمجية العام =================
@dp.error()
async def global_error_handler(event: ErrorEvent):
    error_details = traceback.format_exc()
    # 🌟 حماية من تجاوز حد تليجرام (4096 حرف)
    truncated_details = error_details[-2500:] if len(error_details) > 2500 else error_details
    
    # 🛡️ الحماية الفولاذية: تحويل الرموز البرمجية (< > &) لكي لا يرفضها تليجرام كـ HTML خاطئ
    safe_details = html.escape(truncated_details)
    safe_error_msg = html.escape(str(event.exception)[:500])
    
    error_msg = (
        "🚨 <b>تنبيه: حدث خطأ في البوت!</b>\n\n"
        f"<b>نوع الخطأ:</b> {type(event.exception).__name__}\n"
        f"<b>رسالة الخطأ:</b> {safe_error_msg}\n\n"
        f"<b>التفاصيل البرمجية:</b>\n<code>{safe_details}</code>"
    )
    try:
        await bot.send_message(ADMIN_ID, error_msg)
    except Exception as e:
        logging.error(f"فشل إرسال رسالة الخطأ للمدير: {e}")

# ================= دالة التشغيل الأساسية =================
async def main():
    print("🚀 جاري تشغيل النظام المحاسبي وتطبيق الويب...")
    
    # 0. 🌟 [جديد] تحميل الملفات الأساسية بشكل آمن وسريع
    await download_initial_assets()
    
    # 1. تشغيل قاعدة البيانات
    await database.init_db()
    
    # 🌟 [جديد] تشغيل مهمة تنظيف الذاكرة في الخلفية (لمنع انفجار قاعدة البيانات)
    import core_accounting
    asyncio.create_task(core_accounting.cleanup_idempotency_keys(database.pool))
    
    # 2. استدعاء الموجهات (Routers) بالترتيب الصحيح
    await ai_chat_panel.init_ai_session()
    
    dp.include_router(client_panel.router) 
    dp.include_router(unified_main.router)
    dp.include_router(tickets.router) 
    dp.include_router(ai_chat_panel.router) # 👈 الذكاء الاصطناعي يكون الأخير دائماً

    await bot.delete_webhook(drop_pending_updates=True)

    # 3. (تم نقل تشغيل المنبه للأسفل مع إقلاع السيرفر)
    
    # 4. إعداد سيرفر الويب (aiohttp   )

    app = setup_web_api(bot) 
    runner = web.AppRunner(app)
    await runner.setup()
    
    # استخدام المنفذ الديناميكي لكي يعمل على سيرفرات Render وغيرها
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    
    print(f"🌐 تم تجهيز سيرفر الويب على البورت {port}")

    # 5. تشغيل البوت وسيرفر الويب معاً في نفس الوقت
    try:
        await site.start()  # تشغيل سيرفر الويب في الخلفية
        
        start_scheduler(bot)
        print("⏰ تم تشغيل المراقب الليلي للنسخ الاحتياطي بنجاح.")
        
        # 👇 هذا هو السطر الجديد الذي سيرسل لك رسالة عند كل تشغيل 👇
        try:
            await bot.send_message(ADMIN_ID, "✅ <b>تم تشغيل النظام بنجاح!</b> 🚀\nالسيرفر يعمل الآن بأقصى كفاءة ومستعد لاستقبال الطلبات.")
        except: pass
        
        print("🤖 البوت يعمل الآن ويستقبل الرسائل...")
        await dp.start_polling(bot)  # تشغيل البوت

    finally:
        # إغلاق آمن وهرمي عند إيقاف النظام (Graceful Shutdown)
        print("🛑 جاري إغلاق النظام وتنظيف الموارد...")
        
        await runner.cleanup()
        print("🌐 تم إيقاف سيرفر الويب.")
        
        try:
            await ai_chat_panel.close_ai_session()
            print("🧠 تم إغلاق جلسات الذكاء الاصطناعي.")
        except: 
            pass
            
        await bot.session.close()
        print("🤖 تم إغلاق جلسة البوت.")
            
        await database.close_db()
        print("🗄️ تم إغلاق قاعدة البيانات بأمان.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 تم إيقاف النظام بالكامل.")
