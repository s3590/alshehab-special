import asyncio
import json
import re
import random
import base64
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta

from aiohttp import web
import orjson
import jwt
from cryptography.fernet import Fernet
from aiogram import Bot

import aiohttp_cors  # 👈 هذا هو السطر الجديد الذي أضفناه

import time
import database
from unified_main import send_whatsapp_message
from config import BOT_TOKEN, ADMIN_ID, NETWORK_OWNER_ID, ENCRYPTION_KEY, API_SECRET_KEY, JWT_SECRET
import traceback
from core_accounting import to_decimal # 🌟 استدعاء دالة التحويل المالي الآمنة
from collections import defaultdict
import psutil

# 🌟 تم إبقاء هذه المتغيرات كطبقة حماية أولى، لكن الاعتماد الحقيقي أصبح على قاعدة البيانات
active_telecom_requests = set()
active_pos_requests = set()

# =====================================================================
# 🚨 قاطع الطوارئ المالي (Auto-Kill Switch) والتوجيه الذكي (Smart Routing)
# =====================================================================
# يدعم مزود أساسي ومزود احتياطي (حتى لو كان الاحتياطي غير مفعل حالياً، البنية التحتية جاهزة)
TELECOM_PROVIDERS = {
    'primary': {'id': 'om_drahem', 'name': 'أم دراهم', 'failures': 0, 'last_failure': 0, 'status': 'online', 'is_active': True},
    'backup': {'id': 'yemen_robot', 'name': 'يمن روبوت (احتياطي)', 'failures': 0, 'last_failure': 0, 'status': 'online', 'is_active': False}
}

network_circuit_breaker = {
    'yemen_mobile': {'status': 'online'}, 'you': {'status': 'online'},
    'sabafon': {'status': 'online'}, 'y': {'status': 'online'}, 'adsl': {'status': 'online'}
}

def check_circuit_breaker(network: str) -> bool:
    """دالة توافقية لكي لا تتعطل الواجهة الأمامية"""
    return True

def get_active_provider() -> str:
    """التوجيه الذكي: يختار المزود الأساسي، وإذا كان منهاراً يحول للاحتياطي تلقائياً"""
    primary = TELECOM_PROVIDERS['primary']
    backup = TELECOM_PROVIDERS['backup']
    
    if primary['status'] in ['online', 'half_open'] and primary['is_active']:
        return 'primary'
    if backup['status'] in ['online', 'half_open'] and backup['is_active']:
        return 'backup'
        
    return None # 🚨 قاطع الطوارئ نزل بالكامل! (لا يوجد مزود يعمل)

async def record_provider_result(provider_key: str, is_success: bool):
    """يسجل النتيجة، وإذا فشل 5 مرات في دقيقة ينزل القاطع (Kill Switch)"""
    if provider_key not in TELECOM_PROVIDERS: return
    provider = TELECOM_PROVIDERS[provider_key]
    
    if is_success:
        provider['failures'] = 0
        provider['status'] = 'online'
    else:
        # إذا كان الفشل السابق قبل أكثر من دقيقة، نصفر العداد (نحسب الفشل المتتالي في نفس الدقيقة فقط)
        if time.time() - provider['last_failure'] > 60:
            provider['failures'] = 0
            
        provider['failures'] += 1
        provider['last_failure'] = time.time()
        
        # 🚨 قاطع الطوارئ: 5 إخفاقات متتالية = إغلاق المزود لحماية الأموال
        if provider['failures'] >= 5 and provider['status'] != 'offline':
            provider['status'] = 'offline'
            try:
                await smart_notify_web(ADMIN_ID, f"🚨 **قاطع الطوارئ المالي (Kill Switch):**\nتم إيقاف المزود ({provider['name']}) تلقائياً بسبب فشل 5 عمليات متتالية خلال دقيقة!\nتم حماية أموالك من الاستنزاف. سيعيد النظام اختباره بحذر بعد 5 دقائق.")
            except: pass

def check_circuit_breaker_recovery():
    """يفتح القاطع جزئياً بعد 5 دقائق من الانهيار لاختبار المزود بطلب واحد"""
    for p_key, provider in TELECOM_PROVIDERS.items():
        if provider['status'] == 'offline' and (time.time() - provider['last_failure'] > 300):
            provider['status'] = 'half_open' 
            
 # 🌟 سجل أخطاء المزود (API Logs) لإبهار المدققين
provider_api_logs = []
SANDBOX_MODE = False # وضع الاختبار (True = لا يتم الخصم الحقيقي)

def add_api_log(network, phone, status, response_time_ms):
    """يسجل حركات الـ API لعرضها في لوحة المدقق التقني"""
    log_entry = {
        "time": datetime.now().strftime('%H:%M:%S'),
        "network": network,
        "phone": phone,
        "status": status,
        "ms": response_time_ms
    }
    provider_api_logs.insert(0, log_entry)
    if len(provider_api_logs) > 50: # نحتفظ بآخر 50 عملية فقط
        provider_api_logs.pop()
           
# قاموس لتتبع اتصالات الـ WebSockets النشطة {user_id: set(ws1, ws2, ...)}
connected_ws_clients = defaultdict(set)

# تهيئة التشفير باستخدام المفتاح المستورد من config
cipher_suite = Fernet(ENCRYPTION_KEY )

# نسخة من البوت لإرسال الرسائل
bot_instance = Bot(token=BOT_TOKEN)

async def safe_task(coro, task_name="مهمة خلفية"):
    """درع المهام الخلفية: يمنع الموت الصامت ويرسل الخطأ للتليجرام فوراً"""
    try:
        await coro
    except Exception as e:
        error_details = traceback.format_exc()
        error_msg = (
            f"🔴 **انهيار صامت (Background Task)!**\n"
            f"⚙️ **المهمة:** {task_name}\n"
            f"⚠️ **الخطأ:** {type(e).__name__}: {str(e)}\n\n"
            f"🔍 **التفاصيل:**\n`{error_details[-600:]}`"
        )
        try:
            await smart_notify_web(ADMIN_ID, error_msg)
        except: pass

# ================= دالة الإشعارات الذكية للويب =================
async def smart_notify_web(user_id: int, text: str, document=None, photo=None, reply_markup=None):
    """
    دالة ذكية لملف الويب: ترسل الإشعار للتليجرام والتطبيق معاً.
    إذا كان هناك ملف، ترسل تنبيهاً للتطبيق وتترك الملف للتليجرام.
    """
    global bot_instance
    if not bot_instance:
        return

    # 1. الإرسال إلى التليجرام
    try:
        if document:
            await bot_instance.send_document(user_id, document=document, caption=text, reply_markup=reply_markup)
        elif photo:
            await bot_instance.send_photo(user_id, photo=photo, caption=text, reply_markup=reply_markup)
        else:
            await bot_instance.send_message(user_id, text, reply_markup=reply_markup)
    except Exception as e:
        print(f"Telegram send error in web_api: {e}")

    # 2. الإرسال إلى تطبيق الويب (Web Push)
    try:
        # تنظيف النص من علامات التليجرام
        clean_text = text.replace('**', '').replace('`', '').replace('_', '')
        
        if document or photo:
            file_type = "ملف (PDF/Excel)" if document else "صورة"
            push_title = f"📎 إشعار بـ {file_type} جديد"
            push_body = f"تم إرسال {file_type} في التليجرام:\n{clean_text[:100]}..."
            # استدعاء دالة الإرسال الموجودة أسفل الملف
            await send_web_push(user_id, push_title, push_body)
        else:
            lines = clean_text.split('\n')
            title = lines[0][:50] if lines else "🔔 إشعار جديد"
            body = '\n'.join(lines[1:])[:200] if len(lines) > 1 else clean_text[:200]
            await send_web_push(user_id, title, body)
    except Exception as e:
        print(f"Web Push error in web_api: {e}")

# =====================================================================
# 1. دوال الحماية والمساعدة
# =====================================================================

def encrypt_pin(pin: str) -> str:
    """تشفير الرقم السري قبل حفظه في قاعدة البيانات"""
    return cipher_suite.encrypt(pin.encode()).decode()

def decrypt_pin(encrypted_pin: str) -> str:
    """فك تشفير الرقم السري عند سحبه للعميل"""
    try:
        return cipher_suite.decrypt(encrypted_pin.encode()).decode()
    except:
        # في حال كان الكرت قديماً وغير مشفر، نرجعه كما هو
        return encrypted_pin
        
def custom_json_encoder(obj):
    """🌟 التعديل الأمني: تحويل الأرقام المحاسبية لنصوص لحماية الدقة في الجافاسكريبت"""
    if isinstance(obj, Decimal):
        return str(obj) 
    if isinstance(obj, datetime):
        return obj.strftime('%Y-%m-%d %H:%M')
    raise TypeError

def fast_json_response(data):
    """دالة مساعدة لتسريع إرسال البيانات للويب باستخدام orjson"""
    return web.Response(
        body=orjson.dumps(data, default=custom_json_encoder),
        content_type="application/json"
    )

# 🌟 إصلاح أمني: تمرير الصلاحيات (perms) داخل التوكن المشفر لكي لا يتمكن الهكر من تزويرها
def generate_jwt(user_id, device_id="", cashier_name=None, perms=None):
    """توليد بطاقة مرور مشفرة صالحة لمدة 30 يوماً ومربوطة ببصمة الهاتف وتدعم حسابات العمال"""
    payload = {
        'user_id': user_id,
        'device_id': device_id, 
        'cashier_name': cashier_name, 
        'perms': perms, # 🌟 حفظ الصلاحيات داخل التوكن
        'exp': datetime.utcnow() + timedelta(days=30)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')

def check_jwt(request: web.Request, return_full_payload=False):
    """حارس البوابة الجديد: يفحص بطاقة المرور وبصمة الهاتف معاً"""
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        raise web.HTTPUnauthorized(reason="Missing or Invalid Token")

    token = auth_header.split(' ')[1]
    client_device_id = request.headers.get('X-Device-ID', '') 
    
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        
        # 🚨 الجدار الأمني: طرد المخترق إذا اختلف الجهاز (باستثناء الإدارة والزوار)
        if payload['user_id'] not in [0, ADMIN_ID, NETWORK_OWNER_ID, 888888, 999999]:
            if payload.get('device_id') and payload.get('device_id') != client_device_id:
                raise web.HTTPUnauthorized(reason="Session Hijacking Detected! تم اكتشاف محاولة سرقة جلسة.")
                
        # 🌟 التعديل: إرجاع البيانات كاملة إذا طلبنا ذلك (لمعرفة اسم الكاشير)
        if return_full_payload:
            return payload
            
        return payload['user_id']
    except jwt.ExpiredSignatureError:
        raise web.HTTPUnauthorized(reason="Token Expired")
    except jwt.InvalidTokenError:
        raise web.HTTPUnauthorized(reason="Invalid Token")

async def enforce_cashier_perms(request: web.Request, required_perm: str):
    """🌟 الإصلاح الأمني: فحص الصلاحيات لحظياً من قاعدة البيانات لمنع العمال المطرودين"""
    payload = check_jwt(request, return_full_payload=True)
    user_id = payload['user_id']
    cashier_name = payload.get('cashier_name')
    
    if cashier_name:
        async with database.pool.acquire() as conn:
            user = await conn.fetchrow("SELECT cashiers FROM users WHERE user_id = $1", user_id)
            if user and user['cashiers']:
                import json
                cashiers = json.loads(user['cashiers']) if isinstance(user['cashiers'], str) else user['cashiers']
                # البحث عن العامل الحالي
                current_cashier = next((c for c in cashiers if c.get('name') == cashier_name), None)
                
                # إذا تم حذف العامل أو سحب الصلاحية منه
                if not current_cashier or not current_cashier.get('perms', {}).get(required_perm, False):
                    raise web.HTTPForbidden(reason=f"⛔ عذراً يا {cashier_name}، تم سحب صلاحيتك للوصول لقسم ({required_perm}) من قبل صاحب البقالة!")
            else:
                raise web.HTTPForbidden(reason="⛔ تم حذف حسابك كعامل!")
                
    return user_id

def require_admin(request: web.Request):
    """حارس أمني صارم: يسمح فقط للمدير العام أو الوكيل بالمرور"""
    user_id = check_jwt(request) # ✅ التصحيح: استدعاء check_jwt بدلاً من استدعاء الدالة لنفسها
    if user_id not in [ADMIN_ID, NETWORK_OWNER_ID]:
        raise web.HTTPForbidden(reason="⛔ غير مصرح لك بإجراء هذه العملية! هذا القسم للإدارة فقط.")
    return user_id

def require_agent_only(request: web.Request):
    """حارس أمني صارم جداً: يسمح للوكيل فقط بالمرور (ويطرد المدير العام)"""
    user_id = check_jwt(request)
    if user_id != ADMIN_ID:
        raise web.HTTPForbidden(reason="⛔ غير مصرح لك! هذا القسم خاص بالوكيل فقط ولا يظهر للإدارة.")
    return user_id

async def send_security_alert(request: web.Request, user_id: int, threat_type: str, details: str):
    """نظام كشف الاختراقات (IDS) - يرسل إنذاراً فورياً للتليجرام"""
    try:
        ip = request.remote
        alert_msg = (
            f"🚨 **إنذار أمني خطير (محاولة اختراق)** 🚨\n\n"
            f"⚠️ **نوع التهديد:** {threat_type}\n"
            f"👤 **المشتبه به (ID):** `{user_id}`\n"
            f"🌐 **الآيبي (IP):** `{ip}`\n"
            f"📝 **التفاصيل:** {details}\n"
            f"🔗 **الرابط المستهدف:** `{request.path}`\n\n"
            f"🛡️ *(النظام قام بصد الهجوم تلقائياً)*"
        )
        asyncio.create_task(smart_notify_web(ADMIN_ID, alert_msg))
    except:
        pass
        
def convert_decimals_to_float(obj):
    """تم الإبقاء على اسم الدالة لكي لا يتعطل الكود القديم، لكنها الآن تحول لنصوص (Strings) لحماية الدقة"""
    if isinstance(obj, Decimal):
        return str(obj) # 🌟 التعديل الأمني: إرسال الأرقام كنصوص لمنع أخطاء الجافاسكريبت
    elif isinstance(obj, dict):
        return {k: convert_decimals_to_float(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_decimals_to_float(i) for i in obj]
    elif isinstance(obj, datetime):
        return obj.strftime('%Y-%m-%d %H:%M')
    return obj

async def api_login(request: web.Request):
    data = await request.json()
    
    try:
        user_id = int(data.get('user_id', 0))
    except ValueError:
        return fast_json_response({"status": "error", "message": "رقم الحساب غير صالح. يرجى إدخال أرقام فقط."})
        
    password = data.get('password', '').strip()
    device_id = data.get('device_id', '').strip() # 🌟 استقبال بصمة الهاتف من الواجهة
    
    if not password:
        return fast_json_response({"status": "error", "message": "كلمة المرور مطلوبة!"})
    
    try:
        # 1. حالة الإدارة (المدير أو الوكيل)
        if user_id in [ADMIN_ID, NETWORK_OWNER_ID]:
            if password != API_SECRET_KEY:
                await send_security_alert(request, user_id, "محاولة اختراق حساب الإدارة", "كلمة مرور خاطئة!")
                return fast_json_response({"status": "error", "message": "كلمة المرور غير صحيحة!"})
            token = generate_jwt(user_id, device_id)
            return fast_json_response({"status": "success", "token": token, "role": "admin"}) # 👈 إضافة role

        # 🌟 التعديل الجديد: حساب المدقق التقني (Auditor)
        if user_id == 888888:
            if password != 'audit2026':
                return fast_json_response({"status": "error", "message": "كلمة المرور غير صحيحة!"})
            return fast_json_response({"status": "success", "token": generate_jwt(user_id, device_id), "role": "auditor"}) # 👈 إضافة role
            
        # 🌟 التعديل الجديد: حساب التجربة (Demo Account)
        if user_id == 999999:
            if password != 'demo':
                return fast_json_response({"status": "error", "message": "كلمة المرور غير صحيحة!"})
            return fast_json_response({"status": "success", "token": generate_jwt(user_id, device_id), "role": "client"}) # 👈 إضافة role

        # 2. حالة العملاء (البقالات)
        async with database.pool.acquire() as conn:
            # 🌟 جلب بصمة الهاتف من قاعدة البيانات مع الباسورد وقائمة العمال وحالة الحساب
            user = await conn.fetchrow("SELECT role, web_password, device_id, cashiers, account_status FROM users WHERE user_id = $1", user_id)
            
            if user and user['role'] == 'client' and user_id != 0:
                # 🌟 التعديل: السماح للعميل المجمد بالدخول للمشاهدة، ومنع المحذوف فقط
                if user.get('account_status') == 'deleted':
                    return fast_json_response({"status": "error", "message": "⛔ حسابك محذوف! يرجى مراجعة الإدارة."})
                    
                db_password = user['web_password']
                db_device_id = user['device_id']
                
                # 🚨 الجدار الأمني 1: هل الحساب مفعل؟
                if not db_password or db_password == '1234':
                    return fast_json_response({"status": "error", "message": "حسابك غير مفعل! يرجى الضغط على زر (تفعيل حسابي) بالأسفل."})
                
                # 🟢 1. محاولة دخول صاحب البقالة (المدير)
                if password == db_password:
                    # 🚨 الجدار الأمني 2: فحص بصمة الهاتف (هل يحاول الدخول من هاتف آخر؟)
                    if db_device_id and db_device_id != device_id:
                        return fast_json_response({"status": "error", "message": "⛔ عذراً، هذا الحساب مربوط بهاتف آخر! للدخول من هذا الهاتف، يرجى طلب رمز تفعيل جديد عبر الواتساب."})
                        
                    token = generate_jwt(user_id, device_id)
                    
                    import random
                    scrambled_pass = str(random.randint(1000000, 9999999)) # 🌟 توليد رقم عشوائي لحرق الرمز القديم
                    
                    # 🌟 حفظ بصمة الهاتف الجديد (إذا كان يدخل لأول مرة) وحرق الباسورد القديم
                    await conn.execute("UPDATE users SET web_password = $1, device_id = $2 WHERE user_id = $3", scrambled_pass, device_id, user_id)
                    
                    return fast_json_response({"status": "success", "token": token, "role": "client"})
                
                # 🟢 2. محاولة دخول عامل (كاشير)
                cashiers_json = user.get('cashiers')
                if cashiers_json:
                    import json
                    cashiers = json.loads(cashiers_json) if isinstance(cashiers_json, str) else cashiers_json
                    for cashier in cashiers:
                        if password == cashier.get('pin'):
                            # 🚨 فحص بصمة الهاتف للعامل أيضاً (يجب أن يدخل من نفس هاتف البقالة المعتمد)
                            if db_device_id and db_device_id != device_id:
                                return fast_json_response({"status": "error", "message": "⛔ عذراً، يجب الدخول من هاتف البقالة المعتمد فقط!"})
                                
                            # 🌟 الإصلاح الأمني الخطير: حقن اسم العامل وصلاحياته داخل التوكن المشفر
                            perms = cashier.get('perms', {"ecard": True, "physical": True, "telecom": True}) 
                            token = generate_jwt(user_id, device_id, cashier_name=cashier.get('name'), perms=perms)
                            return fast_json_response({
                                "status": "success", 
                                "token": token, 
                                "role": "cashier", 
                                "cashier_name": cashier.get('name'),
                                "perms": perms 
                            })
                
                return fast_json_response({"status": "error", "message": "كلمة المرور غير صحيحة أو منتهية الصلاحية!"})
                
        return fast_json_response({"status": "error", "message": "رقم الحساب غير مسجل"})
    except Exception as e:
        print(f"Login Error: {e}")
        return fast_json_response({"status": "error", "message": "خطأ في تسجيل الدخول"})

# ================= دالة إدارة حسابات العمال (لصاحب البقالة فقط) =================
async def api_manage_cashiers(request: web.Request):
    # 🌟 حماية الصلاحيات: منع الكاشير من إدارة العمال
    jwt_payload = check_jwt(request, return_full_payload=True)
    user_id = jwt_payload['user_id']
    if jwt_payload.get('cashier_name'):
        return fast_json_response({"status": "error", "message": "⛔ غير مصرح للعمال بإدارة الحسابات!"})
    
    # 🌟 الإصلاح الجذري: دعم GET و POST معاً لمنع خطأ الاتصال
    if request.method == 'GET':
        action = request.query.get('action', 'get')
        data = {}
    else:
        try:
            if request.content_type == 'application/json':
                data = await request.json()
            else:
                data = await request.post()
        except:
            data = await request.post()
        action = data.get('action')
    
    if user_id in [0, ADMIN_ID, NETWORK_OWNER_ID, 888888, 999999]:
        return fast_json_response({"status": "error", "message": "غير مصرح."})
        
    try:
        async with database.pool.acquire() as conn:
            user = await conn.fetchrow("SELECT cashiers FROM users WHERE user_id = $1", user_id)
            if not user:
                return fast_json_response({"status": "error", "message": "المستخدم غير موجود."})
                
            cashiers_json = user.get('cashiers')
            import json
            cashiers = json.loads(cashiers_json) if isinstance(cashiers_json, str) else (cashiers_json or [])
            
            if action == 'get':
                return fast_json_response({"status": "success", "cashiers": cashiers})
                
            elif action == 'add':
                name = data.get('name', '').strip()
                pin = data.get('pin', '').strip()
                perms_raw = data.get('perms', '{"ecard": true, "physical": true, "telecom": true}')
                perms = json.loads(perms_raw) if isinstance(perms_raw, str) else perms_raw
                
                if not name or not pin:
                    return fast_json_response({"status": "error", "message": "الاسم والرمز السري مطلوبان."})
                if len(pin) < 4:
                    return fast_json_response({"status": "error", "message": "الرمز السري يجب أن يكون 4 أرقام على الأقل."})
                
                if any(c.get('name') == name for c in cashiers):
                    return fast_json_response({"status": "error", "message": "يوجد عامل بهذا الاسم مسبقاً."})
                if any(c.get('pin') == pin for c in cashiers):
                    return fast_json_response({"status": "error", "message": "هذا الرمز السري مستخدم لعامل آخر!"})
                    
                cashiers.append({"name": name, "pin": pin, "perms": perms})
                await conn.execute("UPDATE users SET cashiers = $1::jsonb WHERE user_id = $2", json.dumps(cashiers), user_id)
                return fast_json_response({"status": "success", "message": f"تم إضافة العامل ({name}) بنجاح."})
                
            elif action == 'delete':
                name = data.get('name', '').strip()
                cashiers = [c for c in cashiers if c.get('name') != name]
                await conn.execute("UPDATE users SET cashiers = $1::jsonb WHERE user_id = $2", json.dumps(cashiers), user_id)
                return fast_json_response({"status": "success", "message": f"تم إيقاف وحذف العامل ({name}) بنجاح."})
                
        return fast_json_response({"status": "error", "message": "إجراء غير معروف."})
    except Exception as e:
        print(f"Manage Cashiers Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء معالجة الطلب."})

async def api_change_my_password(request: web.Request):
    """العميل يغير كلمة مروره بنفسه من داخل التطبيق"""
    # 🌟 حماية الصلاحيات: منع الكاشير من تغيير كلمة المرور الرئيسية
    jwt_payload = check_jwt(request, return_full_payload=True)
    user_id = jwt_payload['user_id']
    if jwt_payload.get('cashier_name'):
        return fast_json_response({"status": "error", "message": "⛔ غير مصرح للعمال بتغيير كلمة المرور الرئيسية!"})
    data = await request.post()
    old_pass = data.get('old_password', '')
    new_pass = data.get('new_password', '')

    if not new_pass: return fast_json_response({"status": "error", "message": "كلمة المرور الجديدة مطلوبة!"})

    try:
        async with database.pool.acquire() as conn:
            current_pass = await conn.fetchval("SELECT web_password FROM users WHERE user_id = $1", user_id)
            current_pass = current_pass if current_pass else '1234'

            if old_pass != current_pass:
                return fast_json_response({"status": "error", "message": "كلمة المرور الحالية غير صحيحة!"})

            await conn.execute("UPDATE users SET web_password = $1 WHERE user_id = $2", new_pass, user_id)

        return fast_json_response({"status": "success", "message": "تم تغيير كلمة المرور بنجاح! 🔐"})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تغيير كلمة المرور."})

async def api_set_client_password(request: web.Request):
    """الوكيل يقوم بتغيير كلمة مرور البقالة (في حال طلب العميل ذلك)"""
    user_id = require_admin(request)
    data = await request.post()
    client_id = int(data.get('client_id', 0))
    new_password = data.get('new_password', '').strip()
    
    if not new_password: return fast_json_response({"status": "error", "message": "كلمة المرور لا يمكن أن تكون فارغة."})
        
    try:
        async with database.pool.acquire() as conn:
            await conn.execute("UPDATE users SET web_password = $1 WHERE user_id = $2", new_password, client_id)
        return fast_json_response({"status": "success", "message": "تم تغيير كلمة المرور للعميل بنجاح!"})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تغيير كلمة المرور."})
        
async def api_logout(request: web.Request):
    """دالة لمسح الكوكي عند تسجيل الخروج"""
    response = fast_json_response({"status": "success"})
    response.del_cookie('auth_token')
    return response
    
async def notify_clients(target_user_id=None, action="update_needed", message=""):
    """إرسال تحديث لحظي عبر WebSockets للأجهزة المتصلة"""
    data = orjson.dumps({"action": action, "message": message}).decode('utf-8')
    
    if target_user_id is not None:
        # إرسال لمستخدم محدد (مثل البقالة أو الوكيل)
        sockets = connected_ws_clients.get(target_user_id, set())
        for ws in list(sockets):
            try:
                await ws.send_str(data)
            except:
                sockets.discard(ws) # 🌟 تنظيف فوري للاتصال الميت من الذاكرة
        if not sockets and target_user_id in connected_ws_clients:
            del connected_ws_clients[target_user_id]
    else:
        # إرسال للجميع (Broadcast) - لتحديث لوحة المراقبة مثلاً
        for uid, sockets in list(connected_ws_clients.items()):
            for ws in list(sockets):
                try:
                    await ws.send_str(data)
                except:
                    sockets.discard(ws) # 🌟 تنظيف فوري للاتصال الميت من الذاكرة
            if not sockets and uid in connected_ws_clients:
                del connected_ws_clients[uid]
                    
import hmac
import hashlib

def verify_request_signature(data_dict, signature: str) -> bool:
    """🌟 الحماية الفولاذية 3: التحقق من التوقيع الرقمي للطلب لمنع تزوير الأسعار (Parameter Tampering)"""
    if not signature:
        return False # رفض الطلب إذا لم يكن مشفراً
        
    # ترتيب البيانات أبجدياً لضمان تطابق النص قبل التشفير
    sorted_items = sorted([(k, str(v)) for k, v in data_dict.items() if k != 'signature'])
    payload_str = "&".join([f"{k}={v}" for k, v in sorted_items])
    
    # تشفير البيانات باستخدام المفتاح السري للسيرفر
    expected_sig = hmac.new(API_SECRET_KEY.encode(), payload_str.encode(), hashlib.sha256).hexdigest()
    
    # مقارنة التوقيع القادم من الهاتف مع توقيع السيرفر
    return hmac.compare_digest(expected_sig, signature)
                 
# =====================================================================
# 2. روابط جلب البيانات (GET Endpoints)
# =====================================================================
async def api_get_user_data(request: web.Request):
    requested_id = int(request.match_info.get('id', 0))
    requester_id = None # للضمان
    
    # 🌟 التعديل هنا: إعفاء الزائر (0) من فحص التوكن 🌟
    if requested_id != 0:
        requester_id = check_jwt(request) 
        # 🚨 حماية IDOR: منع العميل من التجسس على حسابات العملاء الآخرين
        if requester_id not in [ADMIN_ID, NETWORK_OWNER_ID] and requester_id != requested_id:
            return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك بالوصول لبيانات هذا الحساب!"})
            
    user_id = requested_id
        
    if not database.pool: return fast_json_response({"status": "error", "message": "قاعدة البيانات غير متصلة"})
        
    try:
        async with database.pool.acquire() as conn:
            # 🌟 جلب بيانات لوحة العروض (Promo Banner) لجميع المستخدمين
            promo_text = await conn.fetchval("SELECT value FROM settings WHERE key = 'promo_text'")
            promo_image = await conn.fetchval("SELECT value FROM settings WHERE key = 'promo_image'")
            global_lite = await conn.fetchval("SELECT value FROM settings WHERE key = 'global_lite_mode'")
            
            promo_data = {
                "promo_text": promo_text if promo_text and promo_text != 'off' else None,
                "promo_image": promo_image if promo_image == 'on' else None,
                "global_lite_mode": True if global_lite == 'on' else False
            }

            if user_id == 0: # زائر
                cards = await conn.fetch("SELECT card_type, retail_price FROM inventory")
                pos_list = await conn.fetch("SELECT name, region FROM users WHERE role = 'client'")
                data = {"status": "success", "role": "visitor", "cards": [dict(c) for c in cards], "pos_list": [dict(p) for p in pos_list], **promo_data}
            
            # 🌟 التعديل الجديد: حساب المدقق التقني (Auditor)
            elif user_id == 888888: 
                data = {
                    "status": "success", 
                    "role": "auditor", 
                    "name": "المدقق التقني (Auditor)",
                    "circuit_breaker": network_circuit_breaker,
                    "api_logs": provider_api_logs,
                    "sandbox_mode": SANDBOX_MODE,
                    "telecom_balance": 100000.0,
                    "telecom_debt": 0.0,
                    "telecom_credit_limit": 100000.0,
                    "telecom_status": "on",
                    "ecard_status": "on",
                    "physical_status": "on",
                    "account_status": "active",
                    "suspend_reason": "",
                    **promo_data
                }
                
            # 🌟 التعديل الجديد: حساب التجربة (Demo)
            elif user_id == 999999: 
                data = {
                    "status": "success", 
                    "name": "بقالة التجربة (Demo)", 
                    "role": "client", 
                    "debt": 0.0, 
                    "credit_limit": 100000.0, 
                    "ecard_status": "on", 
                    "telecom_status": "on", 
                    "physical_status": "on",
                    "telecom_balance": 5000.0, 
                    "telecom_debt": 0.0, 
                    "telecom_credit_limit": 20000.0,
                    "is_vip": True, 
                    "special_discount": 0.0, 
                    "debt_schedule_pct": 0.0,
                    "inventory": [],
                    "today_profit": 150.0, 
                    "current_month_profit": 4500.0, 
                    "history_profits": [],
                    "account_status": "active",
                    "suspend_reason": "",
                    "circuit_breaker": network_circuit_breaker,
                    **promo_data
                }

            else: # عميل مسجل حقيقي
                # 🌟 جلب الأرباح المعلقة لعرض الدين الصافي ونسبة الجدولة وحالة التجميد
                user = await conn.fetchrow("SELECT name, role, debt, pending_profit, credit_limit, ecard_status, telecom_balance, telecom_debt, telecom_status, telecom_credit_limit, physical_status, phone, wa_status, is_vip, special_discount, debt_schedule_pct, account_status, suspend_reason FROM users WHERE user_id = $1", user_id)
                if not user: return fast_json_response({"status": "error", "message": "المستخدم غير موجود"})
                
                c_limit = float(user['credit_limit']) if user['credit_limit'] is not None else 50000.0
                # 🌟 إصلاح: العميل يرى دينه الإجمالي كاملاً
                c_debt = float(Decimal(user['debt'] or 0))

                # 🌟 التعديل الأمني: إخفاء بيانات التسديدات تماماً إذا كان الطالب هو المدير العام
                if requester_id == NETWORK_OWNER_ID:
                    t_balance = 0.0
                    t_debt = 0.0
                    t_limit = 0.0
                    telecom_status = 'off'
                else:
                    t_balance = float(user['telecom_balance']) if user['telecom_balance'] is not None else 0.0
                    t_debt = float(user['telecom_debt']) if user['telecom_debt'] is not None else 0.0
                    t_limit = float(user['telecom_credit_limit']) if user['telecom_credit_limit'] is not None else 20000.0
                    telecom_status = user.get('telecom_status', 'off')

                data = {
                    "status": "success", 
                    "name": user['name'], 
                    "role": user['role'], 
                    "debt": c_debt, 
                    "credit_limit": c_limit,
                    "ecard_status": user.get('ecard_status', 'on'),
                    "telecom_status": telecom_status,
                    "physical_status": user.get('physical_status', 'on'),
                    "telecom_balance": t_balance,
                    "telecom_debt": t_debt,
                    "telecom_credit_limit": t_limit,
                    "is_vip": user.get('is_vip', False),
                    "special_discount": float(user.get('special_discount') or 0),
                    "debt_schedule_pct": float(user.get('debt_schedule_pct') or 0),
                    "inventory": [],
                    "circuit_breaker": network_circuit_breaker,
                    "account_status": user.get('account_status', 'active'),
                    "suspend_reason": user.get('suspend_reason', ''),
                    **promo_data
                }

                if user['role'] == 'client':
                    inv = await conn.fetch("SELECT card_type, quantity FROM client_inventory WHERE user_id = $1 AND quantity > 0", user_id)
                    data["inventory"] = [dict(i) for i in inv]
                    
                    try:
                        today_profit = await conn.fetchval("SELECT COALESCE(SUM(profit), 0) FROM client_sales WHERE client_id = $1 AND sale_date >= CURRENT_DATE", user_id)
                        
                        monthly_profits_records = await conn.fetch("""
                            SELECT to_char(sale_date, 'YYYY-MM') as month, COALESCE(SUM(profit), 0) as total_profit
                            FROM client_sales
                            WHERE client_id = $1
                            GROUP BY month
                            ORDER BY month DESC
                        """, user_id)
                        
                        current_month_str = datetime.now().strftime('%Y-%m')
                        current_month_profit = 0.0
                        history_profits = []
                        
                        for mp in monthly_profits_records:
                            if mp['month'] == current_month_str:
                                current_month_profit = float(mp['total_profit'])
                            else:
                                history_profits.append({"month": mp['month'], "profit": float(mp['total_profit'])})
                                
                        data["today_profit"] = float(today_profit) if today_profit else 0.0
                        data["current_month_profit"] = current_month_profit
                        data["history_profits"] = history_profits
                    except Exception as e:
                        print(f"Profit fetch error: {e}")
                        data["today_profit"] = 0.0
                        data["current_month_profit"] = 0.0
                        data["history_profits"] = []
                        
        return fast_json_response(convert_decimals_to_float(data))
    except Exception as e:
        print(f"Get User Data Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب البيانات."})

async def api_network_data(request: web.Request):
    requester_id = check_jwt(request) 
    if not database.pool: return fast_json_response({"status": "error"})
    try:
        async with database.pool.acquire() as conn:
            inventory = await conn.fetch("SELECT card_type, quantity FROM inventory WHERE is_active = TRUE")
            data = {"status": "success", "inventory": [dict(i) for i in inventory]}
            
            # 🚨 حماية: إرسال قائمة العملاء وديونهم للإدارة فقط
            if requester_id in [ADMIN_ID, NETWORK_OWNER_ID]:
                try:
                    clients = await conn.fetch("SELECT user_id, name, debt, region, credit_limit, limit_set_by FROM users WHERE role = 'client' ORDER BY debt DESC")
                except:
                    # في حال لم يتم تحديث قاعدة البيانات بعد
                    clients = await conn.fetch("SELECT user_id, name, debt, region FROM users WHERE role = 'client' ORDER BY debt DESC")
                data["clients"] = [dict(c) for c in clients]
            else:
                data["clients"] = [] # إخفاء بيانات العملاء عن البقالات
                
        return fast_json_response(convert_decimals_to_float(data))
    except Exception as e:
        print(f"Network Data Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب بيانات الشبكة."})

async def api_manager_data(request: web.Request):
    """جلب بيانات المدير (شاشة الرقابة ورأس المال فقط)"""
    requester_id = require_admin(request) # 🚨 حماية الإدارة فقط
    try:
        from core_accounting import FinancialEngine
        from decimal import Decimal
        
        # 🌟 التوحيد المحاسبي: الاعتماد الكلي على المطبخ المركزي
        engine = FinancialEngine(database.pool)
        
        async with database.pool.acquire() as conn:
            # 🌟 الإصلاح الأمني: تمرير الاتصال للدالة لمنع استنزاف الـ Pool
            stats = await engine.get_summary(conn)
            
            # 1. ديون السوق (رأس المال فقط للمدير)
            # 🌟 إصلاح جديد: إضافة user_id لكي يعمل زر (عمر الدين) بشكل صحيح
            clients = await conn.fetch("SELECT user_id, name, (debt - COALESCE(pending_profit, 0)) AS debt, region FROM users WHERE role = 'client' ORDER BY (debt - COALESCE(pending_profit, 0)) DESC")
            inventory = await conn.fetch("SELECT card_type, quantity FROM inventory")
            
            is_report_ready = await conn.fetchval("""
                SELECT EXISTS(
                    SELECT 1 FROM reports_archive 
                    WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '3 days'
                )
            """)
            
            # 2. أداء الشهر الحالي
            # 🌟 إصلاح جديد: تجاهل التراجعات وتحديد المحفظة لكي تكون إحصائيات المدير دقيقة 100%
            monthly_paid = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'تسديد_للشبكة' AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)")
            monthly_expenses = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'مصروفات' AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)")
            
            # 3. تجميع الأصول (إجمالي رأس المال المحفوظ للمدير)
            # 🌟 الإصلاح الجذري: حساب رأس مال المدير من إجمالي الداخل ناقص إجمالي الخارج (يشمل الأرصدة الافتتاحية)
            total_in = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE AND wallet_type = 'manager'")
            total_out = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسديد_للشبكة', 'مصروفات', 'كروت_تالفة', 'مرتجع_للشبكة', 'نسبة_الوكيل') AND is_reverted = FALSE AND wallet_type = 'manager'")
            
            total_capital = Decimal(total_in or 0) - Decimal(total_out or 0)
            
            market_debt_cost = stats["debt_cost"]
            agent_inv_cost = stats["inv_value"]
            
            # 🌟 التوحيد: استخدام الكاش الصافي من الدالة المركزية مباشرة (نفس معادلة التليجرام)
            agent_cash = max(Decimal('0.0'), stats["cash"] - stats["realized"])
            
            data = {
                "status": "success", 
                "total_capital": float(total_capital),
                "market_debt": float(market_debt_cost),
                "agent_cash": float(agent_cash),
                "agent_inventory": float(agent_inv_cost),
                "monthly_paid": float(monthly_paid),
                "monthly_expenses": float(monthly_expenses),
                "is_report_ready": is_report_ready,
                "clients_list": [dict(c) for c in clients], 
                "inventory_list": [dict(i) for i in inventory]
            }
        return fast_json_response(convert_decimals_to_float(data))
    except Exception as e:
        print(f"Manager Data Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب بيانات المدير."})

async def api_ghost_mode(request: web.Request):
    requester_id = require_admin(request) # 🚨 حماية الإدارة فقط
    target_id = int(request.match_info.get('id', 0))
    try:
        async with database.pool.acquire() as conn:
            # 👇 التعديل: جلب بيانات الرصيد والباقات في وضع التخفي 👇
            user = await conn.fetchrow("SELECT name, role, debt, credit_limit, ecard_status, telecom_balance, telecom_debt, telecom_status, telecom_credit_limit FROM users WHERE user_id = $1", target_id)
            # 🌟 الإصلاح المحاسبي: جلب الأرباح المعلقة لحساب الدين الصافي
            user = await conn.fetchrow("SELECT name, role, debt, pending_profit, credit_limit, ecard_status, telecom_balance, telecom_debt, telecom_status, telecom_credit_limit FROM users WHERE user_id = $1", target_id)
            if user:
                # 🌟 الجدار الناري: إخفاء بيانات التسديدات تماماً عن المدير العام في وضع الشبح
                if requester_id == NETWORK_OWNER_ID:
                    t_balance, t_debt, t_limit, telecom_status = 0.0, 0.0, 0.0, 'off'
                else:
                    t_balance = float(user['telecom_balance']) if user['telecom_balance'] is not None else 0.0
                    t_debt = float(user['telecom_debt']) if user['telecom_debt'] is not None else 0.0
                    t_limit = float(user['telecom_credit_limit']) if user['telecom_credit_limit'] is not None else 20000.0
                    telecom_status = user.get('telecom_status', 'off')
                    
                c_limit = float(user['credit_limit']) if user['credit_limit'] is not None else 50000.0
                pending = float(user.get('pending_profit') or 0.0)
                c_debt = float(user['debt'] or 0.0) # 🌟 إصلاح: العميل يرى دينه الإجمالي كاملاً

                data = {
                    "status": "success", 
                    "name": user['name'], 
                    "role": user['role'], 
                    "debt": c_debt, 
                    "credit_limit": c_limit,
                    "ecard_status": user.get('ecard_status', 'on'),
                    "telecom_status": user.get('telecom_status', 'off'),
                    "telecom_balance": t_balance,
                    "telecom_debt": t_debt,
                    "telecom_credit_limit": t_limit
                }
                if user['role'] == 'client':
                    inv = await conn.fetch("SELECT card_type, quantity FROM client_inventory WHERE user_id = $1 AND quantity > 0", target_id)
                    data["inventory"] = [dict(i) for i in inv]
                return fast_json_response(convert_decimals_to_float(data))
        return fast_json_response({"status": "error", "message": "الحساب غير موجود"})
    except Exception as e:
        print(f"Ghost Mode Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تفعيل وضع الشبح."})

async def api_client_transactions(request: web.Request):
    requester_id = check_jwt(request) 
    user_id = int(request.match_info.get('id', 0))
    
    if requester_id not in [ADMIN_ID, NETWORK_OWNER_ID] and requester_id != user_id:
        return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك بالوصول لبيانات هذا الحساب!"})

    try:
        async with database.pool.acquire() as conn:
            # 🌟 التعديل الأمني: إخفاء عمليات التسديدات من كشف الحساب إذا كان الطالب هو المدير
            if requester_id == NETWORK_OWNER_ID:
                txs = await conn.fetch("""
                    SELECT type, amount, details, date FROM transactions 
                    WHERE user_id = $1 
                    AND wallet_type = 'manager'
                    ORDER BY date DESC LIMIT 20
                """, user_id)
            else:
                txs = await conn.fetch("SELECT type, amount, details, date FROM transactions WHERE user_id = $1 ORDER BY date DESC LIMIT 20", user_id)
                
            data = {"status": "success", "transactions": [dict(t) for t in txs]}
        return fast_json_response(convert_decimals_to_float(data))
    except Exception as e:
        print(f"Transactions Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب المعاملات."})

async def api_client_telecom_history(request: web.Request):
    """جلب سجل عمليات الشحن الفوري والباقات للعميل (البقالة)"""
    requester_id = check_jwt(request) 
    client_id = int(request.match_info.get('id', 0))
    
    # 🚨 الجدار الناري: منع المدير العام من رؤية سجل تسديدات البقالة
    if requester_id == NETWORK_OWNER_ID:
        return fast_json_response({"status": "error", "message": "⛔ هذا القسم خاص بالوكيل فقط!"})
        
    # 🚨 حماية IDOR: منع بقالة من رؤية أرقام وتسديدات بقالة أخرى
    if requester_id != ADMIN_ID and requester_id != client_id:
        return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك بالوصول لبيانات هذا الحساب!"})

    try:
        async with database.pool.acquire() as conn:
            # جلب آخر 50 عملية شحن للعميل
            records = await conn.fetch("""
                SELECT network, phone_number, package_name, selling_price, status, created_at 
                FROM telecom_transactions 
                WHERE client_id = $1 
                ORDER BY created_at DESC LIMIT 50
            """, client_id)
            
            history = []
            for r in records:
                history.append({
                    "network": r['network'],
                    "phone": r['phone_number'],
                    "package": r['package_name'],
                    "price": float(r['selling_price']),
                    "status": r['status'], # 'success', 'failed', 'pending'
                    "date": r['created_at'].strftime('%Y-%m-%d %H:%M')
                })
                
        return fast_json_response({"status": "success", "history": history})
    except Exception as e:
        print(f"Telecom History Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب سجل الشحن."})
        
async def api_agent_daily_profit(request: web.Request):
    requester_id = require_admin(request) # 🚨 حماية الإدارة فقط
    try:
        async with database.pool.acquire() as conn:
            profit = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM agent_profits WHERE date >= CURRENT_DATE")
        return fast_json_response({"status": "success", "profit": float(profit or 0)})
    except Exception as e:
        print(f"Profit Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب الأرباح."})

async def api_agent_dashboard_stats(request: web.Request):
    """جلب إحصائيات الوكيل الشاملة (مع فصل كاش الكروت عن كاش التسديدات)"""
    requester_id = require_admin(request) # 🚨 حماية الإدارة فقط
    try:
        # ✅ التعديل: الاستدعاء من المطبخ المركزي
        from core_accounting import FinancialEngine
        import re
        from decimal import Decimal
        
        engine = FinancialEngine(database.pool)
        
        async with database.pool.acquire() as conn:
            # 🌟 الإصلاح الأمني: تمرير الاتصال للدالة لمنع استنزاف الـ Pool وانهيار السيرفر
            stats = await engine.get_summary(conn)
            
            # 1. خزينتي
            # 🌟 العزل التام: فصل كاش الكروت عن كاش التسديدات
            cards_cash = stats["cash"]
            telecom_cash = stats.get("telecom", Decimal('0.0'))
            
            # 🌟 التعديل المحاسبي: حساب المخزون بسعر التكلفة (cost_price) وليس سعر البيع
            agent_inventory_value = await conn.fetchval("SELECT COALESCE(SUM(quantity * cost_price), 0) FROM inventory")
            
            # 🌟 [جديد] حساب الأرباح المتوقعة من المخزون الحالي
            expected_profits = await conn.fetchval("SELECT COALESCE(SUM(quantity * (price - cost_price)), 0) FROM inventory")

            # 2. أموالي في السوق
            market_debt = stats["debt_market"]
            
            # 👇 التعديل الجذري: قراءة الكاش الجاهز من العداد المباشر فقط 👇
            try: await conn.execute("ALTER TABLE users ADD COLUMN pos_cash_collected NUMERIC DEFAULT 0.0")
            except: pass
            
            # 🌟 دمج كاش الكروت وكاش التسديدات في لوحة التحكم مع الحفاظ على العزل التام
            clients = await conn.fetch("SELECT name, pos_cash_collected, telecom_pos_cash FROM users WHERE role = 'client' AND (COALESCE(pos_cash_collected, 0) > 0 OR COALESCE(telecom_pos_cash, 0) > 0)")
            ready_cash_list = []
            total_ready_cards_cash = Decimal('0.0')
            total_ready_telecom_cash = Decimal('0.0')
            
            for c in clients:
                c_ready = Decimal(c['pos_cash_collected'] or 0)
                t_ready = Decimal(c['telecom_pos_cash'] or 0)
                
                # نرسل التفاصيل مفصولة للواجهة الأمامية
                ready_cash_list.append({
                    "name": c['name'], 
                    "cash": float(c_ready),           # كاش الكروت (للمدير)
                    "telecom_cash": float(t_ready),   # كاش التسديدات (للوكيل)
                    "total_cash": float(c_ready + t_ready) # الإجمالي للتحصيل
                })
                total_ready_cards_cash += c_ready
                total_ready_telecom_cash += t_ready

            # 3. أرباحي والتزاماتي
            agent_total_profit = stats["realized"]
            agent_pending_profit = await conn.fetchval("SELECT COALESCE(SUM(pending_profit), 0) FROM users WHERE role = 'client'")
            
            pending_clients = await conn.fetch("SELECT name, pending_profit FROM users WHERE role = 'client' AND COALESCE(pending_profit, 0) > 0")
            pending_profits_list = [{"name": c['name'], "pending": float(c['pending_profit'])} for c in pending_clients]
            
            # 🌟 الإصلاح المحاسبي: المطلوب توريده للمدير يُحسب من (كاش الكروت فقط)
            required_to_remit = max(Decimal('0.0'), cards_cash - agent_total_profit)

            # 4. الأداء الشهري وتحليل الفئات
            # 🌟 إصلاح جديد: تجاهل التراجعات (is_reverted = FALSE) لكي لا تتضخم المبيعات بأرقام وهمية
            monthly_clients_sales = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE) AND type = 'تسليم_لعميل'")
            monthly_walkin_sales = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE) AND type = 'بيع_مباشر'")
            
            # تحليل الكروت المباعة هذا الشهر (مع خصم المرتجعات)
            txs = await conn.fetch("SELECT type, details FROM transactions WHERE type IN ('تسليم_لعميل', 'بيع_مباشر', 'مرتجع_من_عميل', 'مرتجع_بيع_مباشر') AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)")
            category_counts = {}
            for tx in txs:
                match = re.search(r"(\d+)\s*كرت\s*([^\(]+)", tx['details'])
                if match:
                    qty = int(match.group(1))
                    ctype = match.group(2).strip()
                    
                    # إذا كانت العملية مرتجع، نقوم بخصم الكمية بدلاً من جمعها
                    if tx['type'] in ['مرتجع_من_عميل', 'مرتجع_بيع_مباشر']:
                        category_counts[ctype] = category_counts.get(ctype, 0) - qty
                    else:
                        category_counts[ctype] = category_counts.get(ctype, 0) + qty
            
            # تنظيف الفئات التي أصبحت كميتها صفر بسبب المرتجعات
            category_counts = {k: v for k, v in category_counts.items() if v > 0}
            
            category_sales_list = [{"card_type": k, "quantity": v} for k, v in category_counts.items()]

            data = {
                "status": "success",
                "cards_cash": float(cards_cash),         
                "telecom_cash": float(telecom_cash),     
                "agent_inventory_value": float(agent_inventory_value),
                "market_debt": float(market_debt),
                
                "detail_mgr_debt": float(Decimal(market_debt) - Decimal(agent_pending_profit or 0)),
                "detail_agent_debt": float(agent_pending_profit or 0),
                "detail_mgr_inv": float(agent_inventory_value or 0),
                "detail_agent_inv": float(expected_profits or 0),
                
                # 👇 هنا تم التحديث ليتوافق مع العزل التام 👇
                "total_ready_cards_cash": float(total_ready_cards_cash), 
                "total_ready_telecom_cash": float(total_ready_telecom_cash),
                "ready_cash_list": ready_cash_list,
                
                "agent_total_profit": float(agent_total_profit),
                "agent_pending_profit": float(agent_pending_profit), 
                "pending_profits_list": pending_profits_list,
                "required_to_remit": float(required_to_remit), 
                "monthly_clients_sales": float(monthly_clients_sales),
                "monthly_walkin_sales": float(monthly_walkin_sales),
                "category_sales_list": category_sales_list
            }
        return fast_json_response(convert_decimals_to_float(data))
    except Exception as e:
        print(f"Agent Stats Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب إحصائيات الوكيل."})

async def api_live_activity(request: web.Request):
    """جلب أحدث العمليات لعرضها في شريط النبض الحي"""
    requester_id = require_admin(request) # 🚨 حماية الإدارة فقط
    try:
        async with database.pool.acquire() as conn:
            # 🌟 التعديل الذكي: فصل ما يراه الوكيل عما يراه المدير 🌟
            
            if requester_id == ADMIN_ID:
                # 1. إذا كان الطالب هو الوكيل (وليد): يرى كل العمليات (كروت + تسديدات) لليوم الحالي (آخر 10 عمليات)
                query = """
                    SELECT t.type, t.amount, t.details, u.name 
                    FROM transactions t
                    LEFT JOIN users u ON t.user_id = u.user_id
                    WHERE DATE(t.date) = CURRENT_DATE
                    ORDER BY t.date DESC LIMIT 10
                """
            else:
                # 2. إذا كان الطالب هو المدير (رهيب): يتم إخفاء عمليات التسديدات عنه تماماً عبر الجدار الناري
                query = """
                    SELECT t.type, t.amount, t.details, u.name 
                    FROM transactions t
                    LEFT JOIN users u ON t.user_id = u.user_id
                    WHERE DATE(t.date) = CURRENT_DATE 
                    AND t.wallet_type = 'manager'
                    ORDER BY t.date DESC LIMIT 10
                """
            
            rows = await conn.fetch(query)
            
            activities = []
            for r in rows:
                user_name = r['name'] if r['name'] else "الوكيل"
                t_type = r['type']
                amount = float(r['amount'])
                
                # تنسيق الرسالة بذكاء بناءً على نوع العملية
                if 'مبيع' in t_type:
                    activities.append(f"🟢 {user_name} باع بقيمة {amount} ريال")
                elif t_type == 'شحن_رصيد_بوابة':
                    activities.append(f"💵 {user_name} أودع رصيد مقدم ({amount} ريال)")
                elif t_type in ['تسديد_باقة', 'تسديد_باقة_وكيل']:
                    activities.append(f"⚡ {user_name} سدد باقة/رصيد ({amount} ريال)")
                elif 'تسديد' in t_type or 'تحصيل' in t_type:
                    activities.append(f"💰 {user_name} سدد {amount} ريال")
                elif 'مرتجع' in t_type:
                    activities.append(f"↩️ {user_name} أرجع كروت بقيمة {amount} ريال")
                elif 'استلام' in t_type:
                    activities.append(f"📦 {user_name} استلم بضاعة بقيمة {amount} ريال")
                else:
                    activities.append(f"⚡ {user_name}: {r['details']} ({amount} ريال)")
                    
            # إذا كان اليوم جديداً ولم تحدث أي عمليات بعد
            if not activities:
                activities.append("لا توجد عمليات جديدة اليوم 🌟")
                
            return fast_json_response({"status": "success", "activities": activities})
    except Exception as e:
        print(f"Live Activity Error: {e}")
        return fast_json_response({"status": "error", "message": "خطأ في جلب النبض الحي"})

# =====================================================================
# 🌟 نمط المحول الذكي (Adapter Pattern) للاتصال بالمزودين
# =====================================================================
async def call_provider_primary(phone: str, package_name: str, txn_uuid: str):
    """محول المزود الأساسي (أم دراهم)"""
    # 💡 هنا سيوضع كود الـ API الحقيقي لاحقاً
    await asyncio.sleep(random.uniform(0.5, 1.5))
    has_loan = True if phone.startswith("7772") else False
    
    if phone == "777000000": return {"status": "failed", "message": "الرقم مفصول من الشركة."}
    if phone == "777111111": return {"status": "failed", "message": "رصيد الوكيل غير كافٍ."}
    
    rand_val = random.random()
    if rand_val < 0.70: return {"status": "success", "ref_id": f"TXN_A_{random.randint(10000, 99999)}", "has_loan": has_loan}
    elif rand_val < 0.90: return {"status": "failed", "message": "نظام الشركة مشغول حالياً.", "is_busy": True}
    return {"status": "failed", "message": "خطأ غير معروف من المزود."}

async def call_provider_backup(phone: str, package_name: str, txn_uuid: str):
    """محول المزود الاحتياطي (يمن روبوت)"""
    await asyncio.sleep(1)
    return {"status": "success", "ref_id": f"TXN_B_{random.randint(10000, 99999)}", "has_loan": False}

# 🌟 صمام الأمان (Semaphore) لحماية المزود من الضغط (3 طلبات متزامنة كحد أقصى)
PROVIDER_API_SEMAPHORE = asyncio.Semaphore(3)

async def execute_provider_call_with_retry(phone: str, pkg_name: str, user_id: int = 0, max_retries: int = 3, txn_uuid: str = None):
    """الموجه الذكي: يختار المزود، ينفذ الطلب، ويعيد المحاولة إذا لزم الأمر"""

    if SANDBOX_MODE and user_id == 888888:
        add_api_log("sandbox", phone, "success", 100)
        return {"status": "success", "ref_id": f"TEST_{random.randint(10000, 99999)}", "has_loan": False}

    check_circuit_breaker_recovery() # فحص هل حان وقت إيقاظ المزود المنهار
    
    provider_key = get_active_provider()
    if not provider_key:
        return {"status": "failed", "message": "🚨 جميع مزودي الخدمة في حالة طوارئ (متوقفين). يرجى المحاولة لاحقاً."}

    async with PROVIDER_API_SEMAPHORE:
        await asyncio.sleep(0.2) 

        main_response = None  # ✅ تم إزالة المسافة الزائدة هنا
        start_time = time.time()
        
        for attempt in range(max_retries):
            # التوجيه للمزود المناسب
            if provider_key == 'primary':
                main_response = await call_provider_primary(phone, pkg_name, txn_uuid)
            else:
                main_response = await call_provider_backup(phone, pkg_name, txn_uuid)
                
            if main_response['status'] == 'success':
                break
                
            # إذا كان الخطأ "مشغول"، ننتظر ثانيتين ونحاول مجدداً
            if main_response.get('is_busy') or "مشغول" in main_response.get('message', ''):
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
            break # إذا كان خطأ قاطع (مثل رقم مفصول) نخرج من الحلقة فوراً
            
        response_time = int((time.time() - start_time) * 1000)
        add_api_log(TELECOM_PROVIDERS[provider_key]['name'], phone, main_response["status"], response_time)
        
        return main_response

# 👇 الدالة الجديدة لفحص السلفة قبل التسديد (مع مبلغ السلفة) 👇
async def api_check_telecom_loan(request: web.Request):
    """فحص هل الرقم عليه سلفة من شركة الاتصالات وجلب مبلغها"""
    user_id = check_jwt(request) # ✅ السماح للبقالة بالفحص
    data = await request.post()
    phone = data.get('phone', '')

    if len(phone) < 9:
        return fast_json_response({"status": "error", "message": "رقم الهاتف غير صحيح."})

    try:
        # محاكاة الاتصال بسيرفر الشركة للفحص (تأخير ثانية واحدة)
        await asyncio.sleep(1)
        
        # 🧠 محاكاة: أي رقم يبدأ بـ 7772 نعتبره متسلف للتجربة
        has_loan = True if phone.startswith("7772") else False
        
        # 🌟 التعديل الجديد: تحديد مبلغ السلفة (مثلاً 120 ريال شاملة الضريبة)
        loan_amount = 120 if has_loan else 0
        
        return fast_json_response({
            "status": "success", 
            "has_loan": has_loan, 
            "loan_amount": loan_amount, # 👈 إرسال المبلغ للويب
            "phone": phone
        })
    except Exception as e:
        return fast_json_response({"status": "error", "message": "فشل الاتصال بالشركة لفحص السلفة."})
# 👆 نهاية دالة فحص السلفة 👆

# =====================================================================
# تطويرات المرحلة الأولى والرابعة (بوابة التسديدات)
# =====================================================================

# 1. محاكاة الاستعلام عن الباقة الحالية للرقم
async def api_query_phone_package(request: web.Request):
    """استعلام عن حالة الرقم وباقته الحالية من المزود"""
    user_id = check_jwt(request)
    data = await request.post()
    phone = data.get('phone', '')

    if len(phone) < 9:
        return fast_json_response({"status": "error", "message": "رقم الهاتف غير صحيح."})

    try:
        await asyncio.sleep(1.5) # محاكاة الاتصال بالمزود
        
        # محاكاة رد المزود (في الحقيقة سنجلب هذا من API أم دراهم)
        mock_response = {
            "status": "success",
            "phone": phone,
            "current_package": "مزايا 4 جيجا" if random.random() > 0.5 else "لا توجد باقة نشطة",
            "expiry_date": (datetime.now() + timedelta(days=random.randint(1, 20))).strftime('%Y-%m-%d') if random.random() > 0.5 else "غير متوفر",
            "balance": f"{random.randint(10, 500)} ريال"
        }
        return fast_json_response(mock_response)
    except Exception as e:
        return fast_json_response({"status": "error", "message": "فشل الاتصال بالمزود للاستعلام."})

# 2. محاكاة جلب رصيد الوكيل الحقيقي من المزود
async def api_get_provider_balance(request: web.Request):
    """جلب رصيد الوكيل الفعلي المتبقي في سيرفرات المزود"""
    user_id = require_admin(request) 
    
    # 🌟 حماية الخصوصية: منع المدير العام من رؤية رصيد البوابة الخاص بك
    if user_id != ADMIN_ID:
        return fast_json_response({"status": "error", "message": "⛔ هذا القسم خاص بالوكيل فقط!"})
        
    try:
        await asyncio.sleep(1) # محاكاة الاتصال
        
        # 🌟 التعديل: قراءة الرصيد الداخلي من محفظة الوكيل الجديدة
        # (سيتم استبدال هذا السطر بطلب API حقيقي للشركة المزودة لاحقاً)
        async with database.pool.acquire() as conn:
            real_balance = await conn.fetchval("SELECT telecom_balance FROM agent_wallet WHERE id = 1")
            
        return fast_json_response({"status": "success", "real_balance": float(real_balance or 0)})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "تعذر جلب الرصيد من المزود."})

# 3. نظام المطابقة الذكي (Reconciliation)
async def api_telecom_reconciliation(request: web.Request):
    """مطابقة مبيعات النظام مع خصومات المزود الفعلي"""
    user_id = require_admin(request)
    try:
        async with database.pool.acquire() as conn:
            # 🌟 إصلاح المطابقة: حساب التكلفة الحقيقية (بما فيها السلف) لكي تتطابق مع المزود 100%
            system_cost = await conn.fetchval("""
                SELECT COALESCE(SUM(selling_price - profit), 0) 
                FROM telecom_transactions 
                WHERE status = 'success' AND DATE(created_at) = CURRENT_DATE
            """)
            
            # محاكاة: جلب إجمالي ما خصمه المزود منا اليوم (من API المزود)
            provider_deducted = float(system_cost) + random.choice([0, 0, 0, 500, -200]) # محاكاة وجود خطأ أحياناً
            
            difference = float(system_cost) - provider_deducted
            
            status_msg = "✅ الحسابات متطابقة 100%" if difference == 0 else "⚠️ يوجد اختلاف في الحسابات!"
            
            return fast_json_response({
                "status": "success",
                "system_cost": float(system_cost),
                "provider_deducted": provider_deducted,
                "difference": difference,
                "message": status_msg
            })
    except Exception as e:
        return fast_json_response({"status": "error", "message": "فشلت عملية المطابقة."})
        
# =====================================================================
# معالج التسديدات في الخلفية (الاسترداد التلقائي والمطابقة وتسجيل الديون)
# =====================================================================
async def process_telecom_payment_background(trans_id, phone, pkg_name, client_id, final_price, base_cost, is_agent, agent_profit, sale_type_name, customer_sale_type='cash', customer_name='', official_price=Decimal('0.0'), loan_amount=Decimal('0.0'), user_id=0, cashier_name=None, txn_uuid=None):
    try:
        # 1. الاتصال بمزود الخدمة الحقيقي (مع نظام إعادة المحاولة الآلية)
        provider_response = await execute_provider_call_with_retry(phone, pkg_name, user_id, txn_uuid=txn_uuid)
        
        current_status = 'pending' 
        
        async with database.pool.acquire() as conn:
            async with conn.transaction(): 
                
                tx_record = await conn.fetchrow("SELECT status FROM telecom_transactions WHERE id = $1 FOR UPDATE", trans_id)
                if not tx_record:
                    print(f"⚠️ تم تجاهل العملية {trans_id} لأنها محذوفة.")
                    return
                    
                current_status = tx_record['status']
                
                if provider_response['status'] == 'success':
                    ref_id = provider_response.get('ref_id', 'N/A')
                    cashier_text = f" | كاشير: {cashier_name}" if cashier_name else ""
                    
                    if current_status == 'pending':
                        # ✅ نجاح طبيعي
                        await conn.execute("UPDATE telecom_transactions SET status = 'success', provider_reference_id = $1 WHERE id = $2", ref_id, trans_id)
                        
                        # 🌟 تعديلك العبقري: تم إزالة إدخال transactions و telecom_profits لمنع التكرار لأن المحرك المالي سجلها مسبقاً
                        
                        if not is_agent and customer_sale_type == 'credit' and customer_name:
                            await conn.execute("""
                                INSERT INTO client_customers (client_id, customer_name, debt) 
                                VALUES ($1, $2, $3) 
                                ON CONFLICT (client_id, customer_name) 
                                DO UPDATE SET debt = client_customers.debt + $3
                            """, client_id, customer_name, official_price)
                            
                            await conn.execute("INSERT INTO client_customer_ledger (client_id, customer_name, type, amount, details) VALUES ($1, $2, 'دين', $3, $4)", client_id, customer_name, official_price, f"تسديد باقة/رصيد: {pkg_name} للرقم {phone}{cashier_text}")
                                
                    elif current_status in ['failed', 'cancelled']:
                        # 🚨 صائد العمليات المتأخرة (التزامن القاتل)
                        await conn.execute("UPDATE telecom_transactions SET status = 'success', provider_reference_id = $1 WHERE id = $2", ref_id, trans_id)
                        
                        total_agent_deduction = base_cost + loan_amount
                        await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance - $1 WHERE id = 1", total_agent_deduction)
                        
                        if not is_agent:
                            await conn.execute("""
                                UPDATE users 
                                SET telecom_debt = telecom_debt + GREATEST(0, $1 - telecom_balance),
                                    telecom_balance = GREATEST(0, telecom_balance - $1)
                                WHERE user_id = $2
                            """, final_price, client_id)
                            dispute_msg = f"🚨 تسوية متأخرة: نجاح شحن {pkg_name} للرقم {phone} بعد استرداده. تم إعادة الخصم.{cashier_text}"
                            await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES ($1, 'تسوية_تسديد', $2, $3, 'telecom')", client_id, final_price, dispute_msg)
                            
                        # 🌟 سد ثغرة الأرباح: إعادة إضافة الربح لأننا خصمناه عند الاسترداد
                        if agent_profit > 0:
                            await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", agent_profit, f"تسوية ربح متأخر: {pkg_name} للرقم {phone}")
                            
                    else:
                        return 
                        
                else:
                    # ❌ فشل العملية من المزود
                    if current_status == 'pending':
                        # الاسترداد التلقائي (Auto-Refund)
                        await conn.execute("UPDATE telecom_transactions SET status = 'failed' WHERE id = $1", trans_id)                  
                        
                        total_agent_deduction = base_cost + loan_amount
                        await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", total_agent_deduction)

                        if not is_agent:
                            # 🌟 جلب تفاصيل الخصم الدقيقة من قاعدة البيانات
                            tx_details = await conn.fetchrow("SELECT paid_from_balance, added_to_debt FROM telecom_transactions WHERE id = $1", trans_id)
                            paid_bal = tx_details['paid_from_balance'] if tx_details else final_price
                            added_debt = tx_details['added_to_debt'] if tx_details else 0
                            
                            # 🌟 الإصلاح المحاسبي الدقيق للاسترداد التلقائي
                            await conn.execute("""                                                         
                                UPDATE users 
                                SET telecom_balance = telecom_balance + $1,
                                    telecom_debt = GREATEST(0, telecom_debt - $2),
                                    telecom_pos_cash = GREATEST(0, COALESCE(telecom_pos_cash, 0) - $2)
                                WHERE user_id = $3
                            """, paid_bal, added_debt, client_id)

                            cashier_text = f" | كاشير: {cashier_name}" if cashier_name else ""
                            fail_details = f"❌ استرداد تلقائي: فشل تسديد {pkg_name} للرقم {phone}. السبب: {provider_response['message']}{cashier_text}"
                            await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES ($1, 'استرداد_تسديد', $2, $3, 'telecom')", client_id, final_price, fail_details)
                            
                            if agent_profit > 0:
                                await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", -agent_profit, f"إلغاء ربح (فشل تسديد): {pkg_name} للرقم {phone}")
                        else:
                            # 🌟 سد كارثة أرباح الوكيل الوهمية (طياري في الخلفية)
                            fail_details = f"❌ استرداد طياري تلقائي: فشل تسديد {pkg_name} للرقم {phone}. السبب: {provider_response['message']}"
                            await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'قيد_عكسي', $1, $2, 'telecom')", final_price, fail_details)
                            
                            if agent_profit > 0:
                                await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", -agent_profit, f"إلغاء ربح طياري (فشل تسديد): {pkg_name} للرقم {phone}")
                    else:
                        return 
                        
        # 🌟 إرسال الإشعارات
        if provider_response['status'] == 'success':
            if current_status == 'pending':
                await notify_clients(client_id, "new_notification", f"✅ نجاح شحن معلق: تم شحن {pkg_name} للرقم {phone} بنجاح.")
            elif current_status in ['failed', 'cancelled']:
                await notify_clients(client_id, "new_notification", f"🚨 تسوية مالية: عملية الشحن للرقم {phone} التي فشلت سابقاً، تم تأكيد نجاحها من شركة الاتصالات للتو. تم إعادة خصم المبلغ ({final_price} ريال) من حسابك.")
                try: await smart_notify_web(ADMIN_ID, f"👻 **صائد الأشباح (عملية متأخرة):**\nالرقم {phone} نجح عند المزود بعد أن أرجعنا الفلوس للبقالة! تم تدارك الموقف وإعادة خصم المبلغ من البقالة لحماية مالك.")
                except: pass
        else:
            if current_status == 'pending':
                try: await smart_notify_web(ADMIN_ID, f"⚠️ **فشل تسديد واسترداد تلقائي:**\nالرقم: {phone}\nالسبب: {provider_response['message']}\nتم إرجاع المبلغ للعميل.")
                except: pass
                await notify_clients(client_id, "new_notification", f"❌ فشل شحن معلق: تعذر شحن {pkg_name} للرقم {phone}. تم استرداد المبلغ ({final_price} ريال) لحسابك.")
            
        await notify_clients(ADMIN_ID)
                
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"Background Task Error: {error_details}")
        try:
            await smart_notify_web(ADMIN_ID, f"💸 **خطأ حرج في معالجة تسديدة بالخلفية!**\n⚠️ **السبب:** {str(e)}\n🔍 **التفاصيل:**\n`{error_details[-600:]}`")
        except: pass

# =====================================================================
# 🛡️ دالة الإلغاء الآمن للطلبات المعلقة (Safe Cancel)
# =====================================================================
async def api_telecom_safe_cancel(request: web.Request):
    # 🌟 سد ثغرة الكاشير المخرب: منع العامل من إلغاء الطلبات المعلقة
    user_id = await enforce_cashier_perms(request, 'telecom')
    data = await request.post()
    trans_id = int(data.get('trans_id', 0))
    
    try:
        # 🌟 1. فحص مبدئي (بدون قفل) لتسريع الرفض إذا كانت العملية منتهية
        async with database.pool.acquire() as conn:
            tx_initial = await conn.fetchrow("SELECT status, client_id, package_name, phone_number FROM telecom_transactions WHERE id = $1", trans_id)
            if not tx_initial: return fast_json_response({"status": "error", "message": "العملية غير موجودة."})
            if user_id not in [ADMIN_ID, NETWORK_OWNER_ID] and user_id != tx_initial['client_id']:
                return fast_json_response({"status": "error", "message": "غير مصرح لك بإلغاء هذه العملية."})
            if tx_initial['status'] != 'pending':
                return fast_json_response({"status": "error", "message": f"لا يمكن الإلغاء. حالة العملية الحالية: {tx_initial['status']}"})

        # 🌟 2. الاتصال بالمزود (خارج القفل المحاسبي لمنع شلل قاعدة البيانات)
        await asyncio.sleep(1) # محاكاة الاتصال
        import random
        provider_says_success = True if random.random() < 0.05 else False 
        
        # 🌟 3. فتح القفل المحاسبي الصارم للتنفيذ النهائي
        async with database.pool.acquire() as conn:
            async with conn.transaction(): 
                # جلب العملية وقفلها للتأكد أن الروبوت الكاسح لم يغيرها أثناء اتصالنا بالمزود
                tx = await conn.fetchrow("SELECT * FROM telecom_transactions WHERE id = $1 FOR UPDATE", trans_id)
                if tx['status'] != 'pending':
                    return fast_json_response({"status": "error", "message": "تمت معالجة العملية للتو من قبل النظام الآلي."})

                if provider_says_success:
                    await conn.execute("UPDATE telecom_transactions SET status = 'success' WHERE id = $1", trans_id)
                    return fast_json_response({"status": "error", "message": "🚨 توقف! العملية نجحت للتو في سيرفر المزود! لا ترجع الفلوس للزبون."})
                
                # 4. المزود لم ينفذها ➔ نقوم بالإلغاء الآمن والاسترداد
                await conn.execute("UPDATE telecom_transactions SET status = 'cancelled' WHERE id = $1", trans_id)
                
                # 🌟 سد ثغرة تسريب السلفة: إرجاع المبلغ الفعلي المخصوم من الوكيل (سعر البيع - الربح = التكلفة + السلفة)
                actual_deducted = tx['selling_price'] - tx['profit']
                await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", actual_deducted)
                
                # إرجاع رصيد البقالة أو الوكيل
                is_agent = (tx['client_id'] == 0 or tx['client_id'] == ADMIN_ID or tx['client_id'] == NETWORK_OWNER_ID)
                if not is_agent:
                    # 🌟 الإصلاح المحاسبي الدقيق: إرجاع الرصيد والدين بناءً على ما تم خصمه فعلياً بالهللة!
                    await conn.execute("""
                        UPDATE users 
                        SET telecom_balance = telecom_balance + $1,
                            telecom_debt = GREATEST(0, telecom_debt - $2),
                            telecom_pos_cash = GREATEST(0, COALESCE(telecom_pos_cash, 0) - $2)
                        WHERE user_id = $3
                    """, tx.get('paid_from_balance', 0), tx.get('added_to_debt', 0), tx['client_id'])
                    
                    cancel_details = f"✅ إلغاء آمن (بطلب من البقالة): تم استرداد مبلغ {tx['package_name']} للرقم {tx['phone_number']}."
                    # 🌟 سد ثغرة تسريب الإلغاء الآمن لتقرير المدير
                    await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES ($1, 'استرداد_تسديد', $2, $3, 'telecom')", tx['client_id'], tx['selling_price'], cancel_details)
                    
                    # 🌟 سد ثغرة الأرباح الوهمية
                    if tx['profit'] > 0:
                        await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", -tx['profit'], f"إلغاء ربح (إلغاء آمن): للرقم {tx['phone_number']}")
                else:
                    # 🌟 سد كارثة أرباح الوكيل الوهمية (طياري)
                    cancel_details = f"✅ إلغاء آمن (طياري): تم استرداد مبلغ {tx['package_name']} للرقم {tx['phone_number']}."
                    await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'قيد_عكسي', $1, $2, 'telecom')", tx['selling_price'], cancel_details)
                    
                    if tx['profit'] > 0:
                        await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", -tx['profit'], f"إلغاء ربح طياري (إلغاء آمن): للرقم {tx['phone_number']}")
                    
        # إشعار الوكيل
        try: await smart_notify_web(ADMIN_ID, f"🛡️ **إلغاء آمن:**\nتم إلغاء طلب معلق للرقم {tx['phone_number']}. تم استرداد المبلغ بأمان.")
        except: pass
        
        return fast_json_response({"status": "success", "message": "✅ تم الإلغاء بأمان. يمكنك إرجاع الفلوس للزبون الآن."})
    except Exception as e:
        print(f"Safe Cancel Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء الإلغاء."})

# =====================================================================
# دالة استلام طلب التسديد (النسخة المحسنة والمدمجة)
# =====================================================================
async def api_telecom_pay_package(request: web.Request):
    # 🌟 استخراج بيانات التوكن كاملة لمعرفة اسم الكاشير وفحص صلاحياته
    jwt_payload = check_jwt(request, return_full_payload=True)
    if jwt_payload.get('cashier_name') and jwt_payload.get('perms'):
        if not jwt_payload['perms'].get('telecom', False):
            return fast_json_response({"status": "error", "message": "⛔ ليس لديك صلاحية لقسم التسديدات!"})
            
    user_id = jwt_payload['user_id']
    cashier_name = jwt_payload.get('cashier_name')
    
    data = await request.post()
    
    client_id = int(data.get('client_id', 0))
    phone = data.get('phone', '')
    
    # 🚨 حماية الدفع المزدوج (Idempotency) + سد ثغرة المفتاح الفارغ
    txn_uuid = data.get('txn_uuid', '').strip()
    if not txn_uuid:
        import time, random
        # توليد مفتاح إجباري من السيرفر إذا تلاعب الهكر بالمتصفح
        txn_uuid = f"srv_{client_id}_{phone}_{int(time.time())}_{random.randint(1000, 9999)}"
        
    if txn_uuid in active_telecom_requests:
        return fast_json_response({"status": "error", "message": "جاري معالجة هذا الطلب بالفعل، يرجى الانتظار..."})
    active_telecom_requests.add(txn_uuid)
    
    try:
        try:
            pkg_id = int(data.get('pkg_id', 0))
        except ValueError:
            return fast_json_response({"status": "error", "message": "البيانات المدخلة غير صالحة."})
            
        if user_id not in [ADMIN_ID, NETWORK_OWNER_ID] and user_id != client_id:
            return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك!"})
            
        phone = data.get('phone', '')

        # 🌟 إصلاح حساب التجربة (Demo): إرجاع نجاح وهمي فوراً قبل لمس قاعدة البيانات
        if user_id == 999999:
            import random
            await asyncio.sleep(1)
            return fast_json_response({"status": "success", "message": f"✅ تم التسديد بنجاح (وضع التجربة)!\nالرقم: {phone}\nالمرجع: DEMO_{random.randint(10000,99999)}"})

        # 🌟 استقبال بيانات دفتر الديون
        customer_sale_type = data.get('customer_sale_type', 'cash')
        customer_name = data.get('customer_name', '').strip()
        official_price = Decimal(data.get('official_price', 0))
        
        # 🌟 الحماية الفولاذية: حساب السلفة من السيرفر مباشرة
        has_loan = True if phone.startswith("7772") else False 
        loan_amount = Decimal('120.0') if has_loan else Decimal('0.0')
        
        # 🌟 إصلاح حساب التجربة (Demo): إرجاع نجاح وهمي فوراً دون لمس قاعدة البيانات
        if user_id == 999999:
            import random
            await asyncio.sleep(1.5)
            return fast_json_response({"status": "success", "message": f"✅ تم التسديد بنجاح (وضع التجربة)!\nالرقم: {phone}\nالمرجع: DEMO_{random.randint(10000,99999)}"})

        async with database.pool.acquire() as conn:
            # 🌟 إصلاح أمني: جلب حالة الباقة (is_active)
            pkg = await conn.fetchrow("SELECT network, package_name, cost_price, selling_price, credit_price, is_active FROM telecom_packages WHERE id = $1", pkg_id)
            if not pkg: return fast_json_response({"status": "error", "message": "الباقة غير موجودة."})
            
            # 🌟 سد ثغرة الباقات الزومبي: منع شراء الباقات الموقوفة حتى لو تم إرسال الطلب برمجياً
            if not pkg.get('is_active', True):
                return fast_json_response({"status": "error", "message": "🔴 عذراً، هذه الباقة متوقفة حالياً من قبل الإدارة."})

            # 🚨 1. فحص قاطع الدائرة قبل أي شيء
            if not check_circuit_breaker(pkg['network']):
                return fast_json_response({"status": "error", "message": "🔴 عذراً، هذه الشبكة متعطلة حالياً من المصدر. يرجى المحاولة لاحقاً لحماية حسابك."})

            # 🌟 إصلاح حساب المدقق (Auditor): اختبار المزود الحقيقي بدون تسجيل مالي في الدفاتر
            if user_id == 888888:
                provider_response = await execute_provider_call_with_retry(phone, pkg['package_name'], user_id, txn_uuid=txn_uuid)
                is_success = (provider_response['status'] == 'success')
                record_provider_result(pkg['network'], is_success) # تحديث قاطع الدائرة
                
                if is_success:
                    ref_id = provider_response.get('ref_id', 'N/A')
                    return fast_json_response({"status": "success", "message": f"✅ تم التسديد بنجاح (اختبار المدقق)!\nالرقم: {phone}\nالمرجع: {ref_id}"})
                else:
                    return fast_json_response({"status": "error", "message": f"❌ فشل التسديد من المصدر:\n{provider_response.get('message', '')}"})

            # 🌟 التعديل المحاسبي: فصل التكلفة الأساسية عن السلفة
            base_cost = Decimal(pkg['cost_price'] or 0)
            
            is_agent = (client_id == ADMIN_ID or client_id == NETWORK_OWNER_ID)
            agent_sale_type = data.get('agent_sale_type', 'admin') 
            
            # 🌟 حساب الأرباح بناءً على التكلفة الأساسية فقط
            if is_agent and agent_sale_type == 'retail':
                retail_price = Decimal(pkg.get('retail_price') or pkg['selling_price'])
                cash_profit = retail_price - base_cost
                credit_profit = cash_profit
            else:
                cash_profit = Decimal(pkg['selling_price'] or 0) - base_cost
                credit_profit = Decimal(pkg['credit_price'] or 0) - base_cost
                
            # 🚨 حارس الأسعار (منع الخسارة الصامتة) - تم الإبقاء عليه لأهميته
            if cash_profit < 0 or credit_profit < 0:
                asyncio.create_task(smart_notify_web(ADMIN_ID, f"🚨 **إنذار خسارة صامتة!**\nتكلفة الباقة ({pkg['package_name']}) من المزود أصبحت أكبر من سعر البيع للبقالة. تم إيقاف العملية آلياً لحماية رأس مالك."))
                return fast_json_response({"status": "error", "message": "🔴 عذراً، تم إيقاف هذه الباقة مؤقتاً للتحديث من قبل الإدارة."})
            
            pkg_name_with_loan = f"{pkg['package_name']} (شامل السلفة)" if loan_amount > 0 else pkg['package_name']

            # 🌟 تمرير التكلفة الأساسية والسلفة كمتغيرين منفصلين للمحرك المالي
            from core_accounting import FinancialEngine
            engine = FinancialEngine(database.pool)
            try:
                result = await engine.process_telecom(client_id, phone, pkg['network'], pkg_name_with_loan, base_cost, cash_profit, credit_profit, is_agent, loan_amount=loan_amount, key=txn_uuid)
            except Exception as e:
                return fast_json_response({"status": "error", "message": str(e)})

            if result["status"] == "success":
                trans_id = result['trans_id']
                
                # 🚨 2. المهلة الصارمة (25 ثانية كحد أقصى)
                try:
                    provider_response = await asyncio.wait_for(
                        # 🌟 الإصلاح الأمني: تمرير txn_uuid للمزود لمنع الدفع المزدوج
                        execute_provider_call_with_retry(phone, pkg['package_name'], user_id, txn_uuid=txn_uuid), 
                        timeout=25.0
                    )

                    is_success = (provider_response['status'] == 'success')
                    record_provider_result(pkg['network'], is_success)
                    
                    if is_success:
                        # ✅ نجاح فوري
                        ref_id = provider_response.get('ref_id', 'N/A')
                        async with conn.transaction():
                            await conn.execute("UPDATE telecom_transactions SET status = 'success', provider_reference_id = $1 WHERE id = $2", ref_id, trans_id)
                            
                            cashier_text = f" | كاشير: {cashier_name}" if cashier_name else ""
                            details = f"تسديد باقة: {pkg['package_name']} | للرقم: {phone} | مرجع: {ref_id}{cashier_text}"
                            
                            # 🌟 الإصلاح المحاسبي الخطير: إزالة أوامر INSERT INTO transactions و telecom_profits
                            # لأن المحرك المالي (core_accounting) قام بتسجيلها مسبقاً! إبقاؤها هنا يضاعف الأرباح والديون في الدفتر!
                            
                            # فقط نسجل دين الزبون (إذا كان البيع آجلاً) لأن المحرك المالي لا يتدخل في زبائن البقالة
                            if not is_agent and customer_sale_type == 'credit' and customer_name:
                                await conn.execute("INSERT INTO client_customers (client_id, customer_name, debt) VALUES ($1, $2, $3) ON CONFLICT (client_id, customer_name) DO UPDATE SET debt = client_customers.debt + $3", client_id, customer_name, official_price)
                                await conn.execute("INSERT INTO client_customer_ledger (client_id, customer_name, type, amount, details) VALUES ($1, $2, 'دين', $3, $4)", client_id, customer_name, official_price, f"تسديد باقة: {pkg['package_name']} للرقم {phone}{cashier_text}")
                        
                        await notify_clients(client_id)
                        await notify_clients(ADMIN_ID)
                        return fast_json_response({"status": "success", "message": f"✅ تم التسديد بنجاح!\nالرقم: {phone}\nالمرجع: {ref_id}"})
                    
                    else:
                        # 🔴 فشل فوري (استرداد)
                        async with conn.transaction():
                            await conn.execute("UPDATE telecom_transactions SET status = 'failed' WHERE id = $1", trans_id)                  
                            total_agent_deduction = base_cost + loan_amount
                            
                            # 🌟 إرجاع رصيد البوابة للوكيل في المحفظة الجديدة
                            await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", total_agent_deduction)
                            
                            if not is_agent:
                                await conn.execute("""
                                    UPDATE users 
                                    SET telecom_balance = telecom_balance + GREATEST(0, $1 - telecom_debt), 
                                        telecom_debt = GREATEST(0, telecom_debt - $1),
                                        telecom_pos_cash = GREATEST(0, COALESCE(telecom_pos_cash, 0) - $1)
                                    WHERE user_id = $2
                                """, result['final_price'], client_id)
                                
                                # 🌟 إصلاح: تعريف fail_details قبل استخدامه
                                cashier_text = f" | كاشير: {cashier_name}" if cashier_name else ""
                                fail_details = f"❌ استرداد: فشل تسديد {pkg['package_name']} للرقم {phone}{cashier_text}"
                                
                                # 🌟 سد ثغرة تسريب الاسترداد الفوري لتقرير المدير
                                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES ($1, 'استرداد_تسديد', $2, $3, 'telecom')", client_id, result['final_price'], fail_details)
                                
                                # 🌟 سد ثغرة الأرباح الوهمية: خصم الربح الذي سجله المحرك المالي مسبقاً لأن العملية فشلت
                                if result['agent_profit'] > 0:
                                    await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", -result['agent_profit'], f"إلغاء ربح (فشل تسديد): للرقم {phone}")
                        
                        await notify_clients(client_id)
                        await notify_clients(ADMIN_ID)
                        return fast_json_response({"status": "error", "message": f"❌ فشل التسديد من المصدر:\n{provider_response['message']}\n(تم إرجاع المبلغ لحسابك)"})
                        
                except asyncio.TimeoutError:
                    # 🌟 حالة معلق: إطلاق العامل الخلفي (تم دمج الرسالتين لتكون احترافية وتحذيرية في نفس الوقت)
                    asyncio.create_task(
                        process_telecom_payment_background(
                            trans_id, phone, pkg['package_name'], client_id, result['final_price'], 
                            base_cost, is_agent, result['agent_profit'], result['sale_type_name'], 
                            customer_sale_type, customer_name, official_price, loan_amount, user_id, cashier_name,
                            txn_uuid=txn_uuid # 🌟 الإصلاح الأمني: تمرير المفتاح للعامل الخلفي
                        )
                    )
                    return fast_json_response({
                        "status": "pending", 
                        "trans_id": trans_id,
                        "message": "⚠️ الشبكة عليها ضغط والطلب معلق.\nجاري متابعة الطلب في الخلفية، سيصلك إشعار فور نجاحه أو فشله.\n🚨 تنبيه: يرجى عدم إرجاع المبلغ للزبون حتى يصلك الإشعار النهائي."
                    })

            else:
                if result.get("alert_admin"):
                    try: await smart_notify_web(ADMIN_ID, f"🚨 **طوارئ: رصيد البوابة نفد!**\nرصيدك الافتراضي ({result['agent_balance']} ريال) لا يكفي.\nيرجى تغذية الرصيد فوراً.")
                    except: pass
                return fast_json_response(result)

    except Exception as e:
        error_details = traceback.format_exc()
        print(f"Pay Package Error: {error_details}")
        asyncio.create_task(smart_notify_web(ADMIN_ID, f"🔴 **خطأ في تسديد الباقة:**\n`{str(e)}`\n\n`{error_details[-400:]}`"))
        return fast_json_response({"status": "error", "message": "حدث خطأ داخلي أثناء معالجة الطلب."})
    finally:
        if txn_uuid in active_telecom_requests:
            active_telecom_requests.remove(txn_uuid)

# =====================================================================
# دالة تسديد الرصيد المفتوح (مع المهلة الصارمة وقاطع الدائرة)
# =====================================================================
async def api_telecom_pay_open(request: web.Request):
    # 🌟 استخراج بيانات التوكن كاملة لمعرفة اسم الكاشير وفحص صلاحياته
    jwt_payload = check_jwt(request, return_full_payload=True)
    if jwt_payload.get('cashier_name') and jwt_payload.get('perms'):
        if not jwt_payload['perms'].get('telecom', False):
            return fast_json_response({"status": "error", "message": "⛔ ليس لديك صلاحية لقسم التسديدات!"})
            
    user_id = jwt_payload['user_id']
    cashier_name = jwt_payload.get('cashier_name')
    
    data = await request.post()
    
    # استخراج البيانات الأساسية مبكراً لاستخدامها في الحماية
    client_id = int(data.get('client_id', 0))
    phone = data.get('phone', '')
    
    # 🚨 حماية الدفع المزدوج (Idempotency) + سد ثغرة المفتاح الفارغ
    txn_uuid = data.get('txn_uuid', '').strip()
    if not txn_uuid:
        import time, random
        # توليد مفتاح إجباري من السيرفر إذا تلاعب الهكر بالمتصفح
        txn_uuid = f"srv_{client_id}_{phone}_{int(time.time())}_{random.randint(1000, 9999)}"
        
    if txn_uuid in active_telecom_requests:
        return fast_json_response({"status": "error", "message": "جاري معالجة هذا الطلب بالفعل، يرجى الانتظار..."})
    active_telecom_requests.add(txn_uuid)
    
    try:
        try:
            # client_id تم استخراجه بالأعلى
            raw_amount = float(data.get('amount', 0))
            amount = Decimal(str(int(raw_amount)))
        except (ValueError, InvalidOperation):
            return fast_json_response({"status": "error", "message": "البيانات المدخلة غير صالحة."})

        if user_id not in [ADMIN_ID, NETWORK_OWNER_ID] and user_id != client_id:
            return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك!"})
            
        phone = data.get('phone', '')
        network = data.get('network', 'unknown')

        if amount <= 0:
            return fast_json_response({"status": "error", "message": "المبلغ يجب أن يكون أكبر من صفر!"})

        # 🌟 إصلاح حساب التجربة (Demo): إرجاع نجاح وهمي فوراً قبل لمس قاعدة البيانات
        if user_id == 999999:
            import random
            await asyncio.sleep(1)
            return fast_json_response({"status": "success", "message": f"✅ تم التسديد بنجاح (وضع التجربة)!\nالرقم: {phone}\nالمرجع: DEMO_{random.randint(10000,99999)}"})

        # 🌟 استقبال بيانات دفتر الديون
        customer_sale_type = data.get('customer_sale_type', 'cash')
        customer_name = data.get('customer_name', '').strip()
        official_price = Decimal(data.get('official_price', 0))

        # 🌟 الحماية الفولاذية: حساب السلفة من السيرفر مباشرة (لا نثق بالواجهة الأمامية أبداً)
        has_loan = True if phone.startswith("7772") else False # محاكاة فحص السلفة من المزود
        loan_amount = Decimal('120.0') if has_loan else Decimal('0.0')

        # 🌟 الحماية الفولاذية: تحويل الوحدات إلى ريال في السيرفر (لا نثق بالواجهة)
        is_units = data.get('is_units') == 'true'
        if is_units:
            async with database.pool.acquire() as conn:
                unit_price = await conn.fetchval("SELECT value FROM settings WHERE key = 'telecom_unit_price'")
                unit_price = Decimal(unit_price or '12.0')
                amount = amount * unit_price # تحويل الوحدات إلى ريال بقوة السيرفر
                
        # 🌟 إصلاح حساب التجربة (Demo): إرجاع نجاح وهمي فوراً
        if user_id == 999999:
            import random
            await asyncio.sleep(1.5)
            return fast_json_response({"status": "success", "message": f"✅ تم التسديد بنجاح (وضع التجربة)!\nالرقم: {phone}\nالمرجع: DEMO_{random.randint(10000,99999)}"})

        # 🚨 1. فحص قاطع الدائرة
        if not check_circuit_breaker(network):
            return fast_json_response({"status": "error", "message": "🔴 عذراً، هذه الشبكة متعطلة حالياً من المصدر. يرجى المحاولة لاحقاً لحماية حسابك."})

        # 🌟 إصلاح حساب المدقق (Auditor): اختبار المزود الحقيقي بدون تسجيل مالي في الدفاتر
        if user_id == 888888:
            pkg_name = f"رصيد {float(amount)} (اختبار)"
            provider_response = await execute_provider_call_with_retry(phone, pkg_name, user_id, txn_uuid=txn_uuid)
            is_success = (provider_response['status'] == 'success')
            record_provider_result(network, is_success) # تحديث قاطع الدائرة
            
            if is_success:
                ref_id = provider_response.get('ref_id', 'N/A')
                return fast_json_response({"status": "success", "message": f"✅ تم التسديد بنجاح (اختبار المدقق)!\nالرقم: {phone}\nالمرجع: {ref_id}"})
            else:
                return fast_json_response({"status": "error", "message": f"❌ فشل التسديد من المصدر:\n{provider_response.get('message', '')}"})

        async with database.pool.acquire() as conn:
            # 🌟 التعديل المحاسبي: التكلفة الأساسية هي مبلغ الرصيد فقط
            base_cost = amount
            
            # ?? حساب الأرباح من جدول الشرائح (على المبلغ الأساسي فقط بدون السلفة)
            cash_profit = Decimal('0.0')
            credit_profit = Decimal('0.0')

            if base_cost <= 1000:
                tier = await conn.fetchrow("SELECT cash_profit, credit_profit FROM pricing_tiers WHERE $1 >= min_amount AND $1 <= max_amount", base_cost)
                if tier:
                    cash_profit = Decimal(tier['cash_profit'])
                    credit_profit = Decimal(tier['credit_profit'])
                else:
                    # 🌟 إصلاح 1: منع البيع بخسارة إذا نسي الوكيل برمجة شريحة لهذا المبلغ
                    return fast_json_response({"status": "error", "message": f"🔴 عذراً، لا توجد شريحة أرباح مبرمجة للمبلغ ({base_cost}). يرجى مراجعة إعدادات الشرائح."})
            else:
                multiplier = base_cost / Decimal('1000')
                c_prof = await conn.fetchval("SELECT value FROM settings WHERE key = 'over_1000_cash_profit'")
                cr_prof = await conn.fetchval("SELECT value FROM settings WHERE key = 'over_1000_credit_profit'")
                
                # 🌟 اللمسة الاحترافية: تغليف النتيجة بـ Decimal للحفاظ على تطابق أنواع البيانات مع قاعدة البيانات
                cash_profit = Decimal(round(multiplier * Decimal(c_prof if c_prof else '50')))
                credit_profit = Decimal(round(multiplier * Decimal(cr_prof if cr_prof else '70')))

            is_agent = (client_id == ADMIN_ID or client_id == NETWORK_OWNER_ID)
            agent_sale_type = data.get('agent_sale_type', 'admin') 
            
            if is_agent and agent_sale_type == 'retail':
                if base_cost <= 1000:
                    tier = await conn.fetchrow("SELECT retail_profit FROM pricing_tiers WHERE $1 >= min_amount AND $1 <= max_amount", base_cost)
                    if tier: 
                        cash_profit = Decimal(tier['retail_profit'])
                    else:
                        return fast_json_response({"status": "error", "message": f"🔴 عذراً، لا توجد شريحة أرباح طياري للمبلغ ({base_cost})."})
                else:
                    multiplier = base_cost / Decimal('1000')
                    r_prof = await conn.fetchval("SELECT value FROM settings WHERE key = 'over_1000_retail_profit'")
                    # 🌟 اللمسة الاحترافية: تغليف النتيجة بـ Decimal
                    cash_profit = Decimal(round(multiplier * Decimal(r_prof if r_prof else '100')))
                credit_profit = cash_profit

            pkg_name = f"رصيد {float(base_cost)} ريال (شامل السلفة)" if loan_amount > 0 else f"رصيد {float(base_cost)} ريال"

            # ?? تمرير التكلفة الأساسية والسلفة كمتغيرين منفصلين للمحرك المالي
            from core_accounting import FinancialEngine
            engine = FinancialEngine(database.pool)
            try:
                result = await engine.process_telecom(client_id, phone, network, pkg_name, base_cost, cash_profit, credit_profit, is_agent, loan_amount=loan_amount, key=txn_uuid)
            except Exception as e:
                return fast_json_response({"status": "error", "message": str(e)})

            if result["status"] == "success":
                trans_id = result['trans_id']
                
                # 🚨 2. المهلة الصارمة (25 ثانية كحد أقصى)
                try:
                    provider_response = await asyncio.wait_for(
                        # 🌟 الإصلاح الأمني: تمرير txn_uuid للمزود لمنع الدفع المزدوج
                        execute_provider_call_with_retry(phone, pkg_name, user_id, txn_uuid=txn_uuid), 
                        timeout=25.0
                    )

                    is_success = (provider_response['status'] == 'success')
                    record_provider_result(network, is_success)
                    
                    if is_success:
                        ref_id = provider_response.get('ref_id', 'N/A')
                        async with conn.transaction():
                            await conn.execute("UPDATE telecom_transactions SET status = 'success', provider_reference_id = $1 WHERE id = $2", ref_id, trans_id)
                            
                            # 🌟 إضافة اسم الكاشير للتفاصيل إن وجد
                            cashier_text = f" | كاشير: {cashier_name}" if cashier_name else ""
                            details = f"تسديد رصيد: {pkg_name} | للرقم: {phone} | مرجع: {ref_id}{cashier_text}"
                            
                            # 🌟 الإصلاح المحاسبي الخطير: إزالة أوامر INSERT لمنع التكرار (المحرك المالي سجلها مسبقاً)
                            
                            if not is_agent and customer_sale_type == 'credit' and customer_name:
                                await conn.execute("INSERT INTO client_customers (client_id, customer_name, debt) VALUES ($1, $2, $3) ON CONFLICT (client_id, customer_name) DO UPDATE SET debt = client_customers.debt + $3", client_id, customer_name, official_price)
                                await conn.execute("INSERT INTO client_customer_ledger (client_id, customer_name, type, amount, details) VALUES ($1, $2, 'دين', $3, $4)", client_id, customer_name, official_price, f"تسديد رصيد: {pkg_name} للرقم {phone}{cashier_text}")
                        
                        await notify_clients(client_id)
                        await notify_clients(ADMIN_ID)
                        return fast_json_response({"status": "success", "message": f"✅ تم التسديد بنجاح!\nالرقم: {phone}\nالمرجع: {ref_id}"})
                    
                    else:
                        async with conn.transaction():
                            await conn.execute("UPDATE telecom_transactions SET status = 'failed' WHERE id = $1", trans_id)                  
                            total_agent_deduction = base_cost + loan_amount
                            
                            # 🌟 إرجاع رصيد البوابة للوكيل في المحفظة الجديدة
                            await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", total_agent_deduction)

                            if not is_agent:
                                # 🌟 جلب تفاصيل الخصم الدقيقة من قاعدة البيانات للاسترداد
                                tx_details = await conn.fetchrow("SELECT paid_from_balance, added_to_debt FROM telecom_transactions WHERE id = $1", trans_id)
                                paid_bal = tx_details['paid_from_balance'] if tx_details else result['final_price']
                                added_debt = tx_details['added_to_debt'] if tx_details else 0
                                
                                # 🌟 الإصلاح المحاسبي الدقيق للاسترداد
                                await conn.execute("""
                                    UPDATE users 
                                    SET telecom_balance = telecom_balance + $1,
                                        telecom_debt = GREATEST(0, telecom_debt - $2),
                                        telecom_pos_cash = GREATEST(0, COALESCE(telecom_pos_cash, 0) - $2)
                                    WHERE user_id = $3
                                """, paid_bal, added_debt, client_id)
                                
                                cashier_text = f" | كاشير: {cashier_name}" if cashier_name else ""
                                fail_details = f"❌ استرداد: فشل تسديد {pkg_name} للرقم {phone}{cashier_text}"
                                
                                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES ($1, 'استرداد_تسديد', $2, $3, 'telecom')", client_id, result['final_price'], fail_details)
                                
                                if result['agent_profit'] > 0:
                                    await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", -result['agent_profit'], f"إلغاء ربح (فشل تسديد): للرقم {phone}")
                            else:
                                # 🌟 الإصلاح المحاسبي الجذري: تفعيل الزناد المحاسبي لخصم الكاش الفعلي من الدرج
                                await conn.execute("UPDATE transactions SET is_reverted = TRUE WHERE idempotency_key = $1", f"{txn_uuid}_cash")
                                
                                if result['agent_profit'] > 0:
                                    await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", -result['agent_profit'], f"إلغاء ربح طياري (فشل تسديد): للرقم {phone}")
                        
                        await notify_clients(client_id)
                        await notify_clients(ADMIN_ID)
                        return fast_json_response({"status": "error", "message": f"❌ فشل التسديد من المصدر:\n{provider_response['message']}\n(تم إرجاع المبلغ لحسابك)"})

                except asyncio.TimeoutError:
                    # 🌟 حالة معلق: إطلاق العامل الخلفي لمتابعة الطلب واسترداد الأموال إذا فشل
                    asyncio.create_task(
                        process_telecom_payment_background(
                            trans_id, phone, pkg_name, client_id, result['final_price'], 
                            base_cost, is_agent, result['agent_profit'], result['sale_type_name'], 
                            customer_sale_type, customer_name, official_price, loan_amount, user_id, cashier_name,
                            txn_uuid=txn_uuid # 🌟 الإصلاح الأمني: تمرير المفتاح للعامل الخلفي
                        )
                    )
                    return fast_json_response({
                        "status": "pending", 
                        "trans_id": trans_id,
                        "message": "⚠️ الطلب معلق في سيرفرات الشركة!\n\n🚨 يمنع منعاً باتاً إرجاع الفلوس للزبون حتى يأتيك إشعار بالنجاح أو الفشل. إذا أرجعت الفلوس ونجحت العملية لاحقاً، فأنت تتحمل قيمتها."
                    })

            else:
                if result.get("alert_admin"):
                    try: await smart_notify_web(ADMIN_ID, f"🚨 **طوارئ: رصيد البوابة نفد!**\nرصيدك الافتراضي ({result['agent_balance']} ريال) لا يكفي.\nيرجى تغذية الرصيد فوراً.")
                    except: pass
                return fast_json_response(result)

    except Exception as e:
        error_details = traceback.format_exc()
        print(f"Pay Open Error: {error_details}")
        asyncio.create_task(smart_notify_web(ADMIN_ID, f"🔴 **خطأ في تسديد الرصيد المفتوح:**\n`{str(e)}`\n\n`{error_details[-400:]}`"))
        return fast_json_response({"status": "error", "message": "حدث خطأ داخلي أثناء معالجة الطلب."})
    finally:
        if txn_uuid in active_telecom_requests:
            active_telecom_requests.remove(txn_uuid)

async def api_pre_give_cards_check(request: web.Request):
    """استدعاء الحارس الأمني ومستشار الذكاء الاصطناعي من المطبخ المركزي"""
    user_id = require_admin(request)
    client_id = int(request.match_info.get('id', 0))
    
    from core_accounting import core_pre_give_cards_check
    result = await core_pre_give_cards_check(client_id)
    return fast_json_response(convert_decimals_to_float(result))

async def api_bulk_give_cards(request: web.Request):
    """تنفيذ الطلب المجمع (اعتماد نصيحة الذكاء الاصطناعي أو السلة اليدوية)"""
    user_id = require_admin(request)
    data = await request.post()
    client_id = int(data.get('client_id', 0))
    suggested_order_str = data.get('suggested_order', '{}')
    override_secret = data.get('override_secret', '').strip()
    
    try:
        suggested_order = json.loads(suggested_order_str)
        if not suggested_order:
            return fast_json_response({"status": "error", "message": "الطلب فارغ!"})
            
        # دعم التخطي للسقف في الطلب المجمع
        override = (override_secret == '#')
        
        from core_accounting import core_bulk_give_cards
        result = await core_bulk_give_cards(client_id, suggested_order, override_limit=override)
        
        if result["status"] == "success":
            tx_id = result.get("tx_id", result.get("trans_id"))
            total_price = result["total_price"]
            client_name = result["client_name"]
            details_str = result["details_str"]
            
            async def send_bulk_notifications():
                try:
                    from pdf_generator import generate_receipt
                    from aiogram.types import BufferedInputFile
                    from unified_main import safe_send_whatsapp
                    import asyncio
                    
                    pdf_buffer = await asyncio.to_thread(generate_receipt, tx_id, client_name, total_price, "تسليم كروت (مجمع)", details_str)
                    
                    pdf_file_admin = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{tx_id}.pdf")
                    await smart_notify_web(ADMIN_ID, document=pdf_file_admin, caption=f"✅ **تم التسليم بنجاح (من الويب)!**\nتم إضافة الكروت لمخزون العميل، وتسجيل {int(total_price)} ريال كدين على العميل {client_name}.")
                    
                    pdf_buffer.seek(0)
                    wa_text = f"📦 *فاتورة استلام كروت*\nمرحباً {client_name}،\nتم إضافة الكروت التالية لمخزونك:\n{details_str}\nإجمالي المديونية المضافة: *{int(total_price)} ريال*.\nمرفق الفاتورة للتأكيد 🌹"
                    await safe_send_whatsapp(client_id, wa_text, pdf_buffer, f"Receipt_{tx_id}.pdf", bot=bot_instance)
                    
                    from web_api import send_web_push
                    await send_web_push(client_id, "📦 استلام كروت", f"تم إضافة الكروت لمخزونك بنجاح. الدين المضاف: {int(total_price)} ريال.")
                except Exception as e:
                    print(f"Bulk Notification Error: {e}")
                    
            import asyncio
            asyncio.create_task(send_bulk_notifications())
            
            await notify_clients(client_id)
            await notify_clients(ADMIN_ID)
            
        return fast_json_response(convert_decimals_to_float(result))
    except Exception as e:
        print(f"Bulk Give Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تنفيذ الطلب المجمع."})

# =====================================================================
# 3. روابط العمليات (POST Endpoints) مع إدارة الأخطاء (التحسين 4)
# =====================================================================
async def api_sell_cards(request: web.Request):
    user_id = require_admin(request)  # 🚨 حماية الإدارة فقط
    data = await request.post()
    sale_mode = data.get('sale_mode')
    card_type = data.get('card_type')
    override_secret = data.get('override_secret', '').strip()
    
    # 🌟 الإصلاح الأسطوري: استخراج مفتاح منع التكرار من الطلب لتجنب انهيار السيرفر (NameError)
    txn_uuid = data.get('txn_uuid', f"web_sell_{int(time.time())}")

    # 1. حماية تحويل المدخلات (تجنب انهيار السيرفر)
    try:
        quantity = int(data.get('quantity', 0))
    except ValueError:
        return fast_json_response({"status": "error", "message": "الكمية يجب أن تكون رقماً صحيحاً!"})
        
    if quantity <= 0:
        return fast_json_response({"status": "error", "message": "الكمية يجب أن تكون أكبر من صفر!"})
    
    try:
        if sale_mode == 'credit':
            client_id = int(data.get('client_id', 0))
            
            # 🚨 حارس سقف المديونية
            async with database.pool.acquire() as conn:
                user_info = await conn.fetchrow("SELECT debt, credit_limit FROM users WHERE user_id = $1", client_id)
                if user_info:
                    limit = Decimal(user_info['credit_limit']) if user_info['credit_limit'] else Decimal('50000.0')
                    current_debt = Decimal(user_info['debt'])
                    
                    card_price = await conn.fetchval("SELECT price FROM inventory WHERE card_type = $1", card_type)
                    
                    # إصلاح: التأكد من وجود الكرت
                    if card_price is None:
                        return fast_json_response({"status": "error", "message": "نوع الكرت غير موجود في المخزون!"})
                        
                    new_order_value = Decimal(card_price) * quantity
                    
                    if (current_debt + new_order_value) > limit and override_secret != '#':
                        return fast_json_response({
                            "status": "error", 
                            "requires_override": True,
                            "message": f"هذه العملية ستتجاوز سقف المديونية للعميل!\nالدين الحالي: {current_debt} ريال\nقيمة الكروت: {new_order_value} ريال\nالسقف المسموح: {limit} ريال\n\nهل تريد الاستمرار على مسؤوليتك؟"
                        })
            
            from core_accounting import FinancialEngine
            from unified_main import safe_send_whatsapp
            from pdf_generator import generate_receipt
            from aiogram.types import BufferedInputFile
            import asyncio
            
            engine = FinancialEngine(database.pool)
            override = (override_secret == '#')
            
            # 🌟 التعديل: استخدام دالة الكلاس وتمرير الـ key لمنع التكرار
            try:
                result = await engine.give_cards(client_id, card_type, quantity, override=override, key=txn_uuid)
            except Exception as e:
                return fast_json_response({"status": "error", "message": str(e)})
            
            # 🌟 التوحيد: توليد الفاتورة وإرسال الإشعارات إذا نجحت العملية
            if result["status"] == "success":
                trans_id = result.get("trans_id")
                total_price = result["total_price"]
                client_name = result.get("client_name", "عميل")
                
                async def send_sell_notifications():
                    try:
                        # 1. توليد الـ PDF
                        pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, client_name, total_price, "تسليم كروت (جملة)", f"عدد {quantity} كرت من فئة {card_type}")
                        
                        # 2. إرسال الفاتورة للوكيل في التليجرام
                        pdf_file_admin = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
                        await smart_notify_web(ADMIN_ID, document=pdf_file_admin, caption=f"✅ **تم التسليم بنجاح (من الويب)!**\nتم إضافة {quantity} كرت لمخزون العميل، وتسجيل {int(total_price)} ريال كدين على العميل {client_name}.")
                        
                        # 3. إرسال واتساب للعميل
                        pdf_buffer.seek(0)
                        wa_text = f"📦 *فاتورة استلام كروت*\nمرحباً {client_name}،\nتم إضافة {quantity} كرت من فئة {card_type} إلى مخزونك.\nإجمالي المديونية المضافة: *{int(total_price)} ريال*.\nمرفق الفاتورة للتأكيد 🌹\n\n👇 *فضلاً، أجب بـ (نعم) إذا فعلاً استلمت الكروت.*"
                        await safe_send_whatsapp(client_id, wa_text, pdf_buffer, f"Receipt_{trans_id}.pdf", bot=bot_instance)
                        
                        # 4. إشعار الويب (Push)
                        await send_web_push(client_id, "📦 استلام كروت جديدة", f"تم إضافة {quantity} كرت ({card_type}) لمخزونك. الدين المضاف: {int(total_price)} ريال.")
                    except Exception as e:
                        print(f"Notification Error: {e}")
                        
                asyncio.create_task(send_sell_notifications())

                await notify_clients(client_id)
                await notify_clients(ADMIN_ID)
                
            return fast_json_response(convert_decimals_to_float(result))
            
        elif sale_mode in ['retail', 'wholesale']:
            personal_credit_name = data.get('personal_credit_name', '').strip() # 🌟 استقبال اسم الزبون الآجل
            try:
                collected_cash = Decimal(data.get('collected_cash', 0))
            except InvalidOperation:
                return fast_json_response({"status": "error", "message": "المبلغ المستلم غير صالح!"})
                
            if collected_cash < 0:
                return fast_json_response({"status": "error", "message": "المبلغ المستلم لا يمكن أن يكون سالباً!"})
                
            discount = Decimal('0.0')
            
            async with database.pool.acquire() as conn:
                prices = await conn.fetchrow("SELECT price, retail_price FROM inventory WHERE card_type = $1", card_type)
                
                # 2. إصلاح خطأ الانهيار (NoneType)
                if not prices:
                    return fast_json_response({"status": "error", "message": "نوع الكرت غير موجود في المخزون!"})
                    
                if sale_mode == 'retail':
                    official_price = Decimal(prices['retail_price']) * quantity
                else:
                    official_price = Decimal(prices['price']) * quantity
                    
                # 🌟 إذا كان بيع آجل شخصي، الوكيل يتحمل الكاش أمام الإدارة
                if personal_credit_name:
                    collected_cash = official_price

                # 🌟 التعديل الأمني: منع الأخطاء المطبعية التي تدمر ميزانية الوكيل
                if collected_cash > official_price:
                    return fast_json_response({"status": "error", "message": f"المبلغ المستلم ({collected_cash}) أكبر من السعر المطلوب ({official_price})! يرجى التأكد من الرقم."})

                # 3. إصلاح الثغرة المنطقية (التحقق من المبلغ دائماً)
                if collected_cash < official_price:
                    if override_secret == '#':
                        discount = official_price - collected_cash
                    else:
                        price_name = "سعر التجزئة" if sale_mode == 'retail' else "سعر الجملة"
                        return fast_json_response({
                            "status": "error",
                            "requires_override": True,
                            "message": f"المبلغ المستلم ({collected_cash}) أقل من {price_name} الرسمي ({official_price}). هل تريد الاستمرار وتمرير الفارق كخصم؟"
                        })
            
            # 🌟 التعديل: استخدام المحرك المالي للبيع المباشر
            from core_accounting import FinancialEngine
            engine = FinancialEngine(database.pool)
            try:
                result = await engine.direct_sale(card_type, quantity, collected_cash, discount, sale_mode, key=txn_uuid)
            except Exception as e:
                return fast_json_response({"status": "error", "message": str(e)})
                
            if result["status"] == "success":
                # 🌟 تسجيل الدين في دفتر الوكيل الشخصي
                if personal_credit_name:
                    async with database.pool.acquire() as conn:
                        cust_id = await conn.fetchval("""
                            INSERT INTO agent_personal_customers (name, debt) VALUES ($1, $2)
                            ON CONFLICT (name) DO UPDATE SET debt = agent_personal_customers.debt + $2
                            RETURNING id
                        """, personal_credit_name, official_price)
                        if not cust_id:
                            cust_id = await conn.fetchval("SELECT id FROM agent_personal_customers WHERE name = $1", personal_credit_name)
                        await conn.execute("INSERT INTO agent_personal_ledger (customer_id, type, amount, details) VALUES ($1, 'دين', $2, $3)", cust_id, official_price, f"شراء {quantity} كرت {card_type}")
                
                await notify_clients(ADMIN_ID)
                
            return fast_json_response(convert_decimals_to_float(result))
            
        return fast_json_response({"status": "error", "message": "نوع البيع غير مدعوم"})
    except Exception as e:
        print(f"Sell Cards Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء عملية البيع."})

async def api_collect_debt(request: web.Request):
    """دالة تحصيل الديون المدمجة (تدعم التجاوز برمز # وتولد الفواتير)"""
    user_id = require_admin(request) # 🚨 حماية: الإدارة فقط
    
    try:
        # دعم قراءة البيانات سواء جاءت كـ JSON أو FormData
        if request.content_type == 'application/json':
            data = await request.json()
        else:
            data = await request.post()
            
        client_id = int(data.get('client_id', 0))
        amount_str = str(data.get('amount', '')).strip()
        
        # فحص التجاوز سواء جاء كرمز # في المبلغ أو كقيمة منطقية (Boolean)
        bypass = False
        if amount_str.endswith("#"):
            bypass = True
            amount_str = amount_str[:-1]
        elif str(data.get("bypass")).lower() == 'true':
            bypass = True
            
        amount = Decimal(amount_str)
        
    except (ValueError, InvalidOperation):
        return fast_json_response({"status": "error", "message": "البيانات المدخلة غير صالحة، تأكد من إدخال أرقام صحيحة."})
        
    if amount <= 0:
        return fast_json_response({"status": "error", "message": "المبلغ يجب أن يكون أكبر من صفر!"})
        
    try:
        from core_accounting import FinancialEngine
        from unified_main import safe_send_whatsapp
        from pdf_generator import generate_receipt
        from aiogram.types import BufferedInputFile
        import asyncio
        
        engine = FinancialEngine(database.pool)
        
        try:
            # استدعاء المحرك المالي مع تمرير التجاوز
            result = await engine.collect_debt(client_id, amount, bypass_shortage=bypass)
        except Exception as e:
            return fast_json_response({"status": "error", "message": str(e)})
        
        # 🌟 التوحيد: توليد الفاتورة وإرسال الإشعارات إذا نجحت العملية
        if result["status"] == "success":
            trans_id = result.get("trans_id")
            client_name = result.get("client_name", "عميل")
            new_debt = result.get("new_debt", 0)
            
            # دالة خلفية لإرسال الإشعارات بدون تجميد تطبيق الويب
            async def send_notifications():
                try:
                    # 1. توليد الـ PDF
                    pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, client_name, amount, "تسديد دفعة", "دفعة نقدية يداً بيد (من الويب)")
                    
                    # 2. إرسال الفاتورة للوكيل في التليجرام
                    pdf_file_admin = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
                    balance_text = f"الرصيد المتبقي عليه: {int(new_debt)} ريال" if new_debt >= 0 else f"رصيد دائن (لصالح العميل): {abs(int(new_debt))} ريال"
                    
                    bypass_note = "\n*(تم استخدام ميزة التجاوز #)*" if bypass else ""
                    
                    await smart_notify_web(ADMIN_ID, document=pdf_file_admin, caption=f"✅ **تم استلام الدفعة (من الويب)!**\nتم خصم {int(amount)} ريال من حساب {client_name}.\n\n{balance_text}{bypass_note}")
                    
                    # 3. إرسال واتساب للعميل
                    pdf_buffer.seek(0)
                    wa_text = f"🧾 *سند قبض*\nمرحباً {client_name}،\nتم استلام مبلغ *{int(amount)} ريال* بنجاح.\nالرصيد المتبقي: *{int(new_debt)} ريال*.\nمرفق الفاتورة للتأكيد 🌹\n\n👇 *فضلاً، أجب بـ (صحيح) إذا كان المبلغ المسدد والرصيد المتبقي صحيحين.*"
                    await safe_send_whatsapp(client_id, wa_text, pdf_buffer, f"Receipt_{trans_id}.pdf", bot=bot_instance)
                    
                    # 4. إشعار الويب (Push)
                    await send_web_push(client_id, "💰 سند قبض (استلام كاش)", f"تم استلام مبلغ {int(amount)} ريال بنجاح. الرصيد المتبقي: {int(new_debt)} ريال.")
                except Exception as e:
                    print(f"Notification Error: {e}")
                    
            asyncio.create_task(send_notifications())

            await notify_clients(client_id)
            await notify_clients(ADMIN_ID)
            
        return fast_json_response(convert_decimals_to_float(result))
    except Exception as e:
        print(f"Collect Debt Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تحصيل الديون."})

async def api_collect_telecom_debt(request: web.Request):
    """تحصيل ديون الرصيد والباقات (تذهب لجيب الوكيل ولا تظهر للمدير)"""
    user_id = require_agent_only(request)
    data = await request.post()
    
    try:
        client_id = int(data.get('client_id', 0))
        amount = Decimal(data.get('amount', 0))
    except (ValueError, InvalidOperation):
        return fast_json_response({"status": "error", "message": "البيانات المدخلة غير صالحة."})
        
    try:
        from core_accounting import core_collect_telecom_debt
        result = await core_collect_telecom_debt(client_id, amount)
        
        if result["status"] == "success":
            from unified_main import safe_send_whatsapp
            from pdf_generator import generate_receipt
            import asyncio
            
            async def send_telecom_notifications():
                try:
                    details = f"تحصيل دفعة نقدية (شحن فوري) - سداد دين: {result['paid_for_debt']}"
                    if result['added_to_balance'] > 0: details += f" | رصيد مضاف: {result['added_to_balance']}"
                        
                    pdf_buffer = await asyncio.to_thread(generate_receipt, result['trans_id'], result['client_name'], amount, "سند قبض - خدمات إلكترونية", details)
                    wa_text = f"🧾 *سند قبض - خدمات الشحن*\nمرحباً {result['client_name']}،\nتم استلام مبلغ *{int(amount)} ريال* بنجاح.\nمرفق الفاتورة للتأكيد 🌹"
                    await safe_send_whatsapp(client_id, wa_text, pdf_buffer, f"Telecom_Receipt_{result['trans_id']}.pdf", bot=bot_instance)
                    await send_web_push(client_id, "💰 سند قبض (شحن فوري)", f"تم استلام {int(amount)} ريال لحساب الخدمات الإلكترونية.")
                except Exception as e: print(f"Telecom Notification Error: {e}")
                    
            asyncio.create_task(send_telecom_notifications())
            
            await notify_clients(client_id)
            await notify_clients(ADMIN_ID)
            
            return fast_json_response({"status": "success", "message": f"تم تحصيل {amount} ريال بنجاح!"})
        else:
            return fast_json_response(result)
            
    except Exception as e:
        print(f"Collect Telecom Debt Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تحصيل دين الرصيد."})

async def api_finance_action(request: web.Request):
    """معالجة المصروفات وتسديد المدير من الويب"""
    user_id = require_admin(request) # 🚨 حماية: الإدارة فقط
    data = await request.post()
    
    try:
        amount = Decimal(data.get('amount', 0))
    except InvalidOperation:
        return fast_json_response({"status": "error", "message": "المبلغ المدخل غير صالح، تأكد من إدخال أرقام صحيحة."})
        
    action_type = data.get('action_type')
    details = data.get('details', '')
    
    if amount <= 0:
        return fast_json_response({"status": "error", "message": "المبلغ يجب أن يكون أكبر من صفر!"})
    
    try:
        from core_accounting import FinancialEngine, TxType
        engine = FinancialEngine(database.pool)
        
        # تحويل النص إلى Enum
        try: tx_enum = TxType(action_type)
        except ValueError: tx_enum = TxType.EXPENSE # افتراضي
        
        try:
            result = await engine.finance_action(tx_enum, amount, details, source="(من الويب)")
        except Exception as e:
            return fast_json_response({"status": "error", "message": str(e)})
        
        if result["status"] == "success":
            # 🌟 الإصلاح: التوافق مع مفاتيح المحرك الجديد
            trans_id = result.get("tx_id", result.get("trans_id"))
            
            # 🌟 إرسال الفواتير للتليجرام في الخلفية
            async def send_finance_receipt():
                try:
                    from pdf_generator import generate_receipt
                    from aiogram.types import BufferedInputFile
                    import asyncio
                    
                    if action_type == 'expense':
                        pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, "مصروفات", amount, "مصروفات", details)
                        pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
                        await smart_notify_web(ADMIN_ID, document=pdf_file, caption=f"✅ **تم تسجيل المصروف (من الويب)!**\nالمبلغ: {int(amount)} ريال\nالتفاصيل: {details}.")
                        
                    elif action_type == 'pay_manager':
                        pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, "المدير العام", amount, "تسديد للشبكة", "تحويل للمدير العام (من الويب)")
                        pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
                        await smart_notify_web(ADMIN_ID, document=pdf_file, caption=f"✅ **تم تسجيل التحويل (من الويب)!**\nتم تسجيل تحويل مبلغ {int(amount)} ريال للمدير العام.")
                        
                        pdf_buffer.seek(0)
                        pdf_file_mgr = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
                        await smart_notify_web(NETWORK_OWNER_ID, text=f"💸 **إشعار تسديد من الوكيل (عبر الويب):**\nقام الوكيل بتسديد مبلغ **{int(amount)} ريال** لحساب الإدارة العامة.\nمرفق سند القبض.", document=pdf_file_mgr)
                        # 🌟 إضافة إشعار التطبيق للمدير
                        try: await send_web_push(NETWORK_OWNER_ID, "💸 تسديد كاش", f"الوكيل قام بتسديد مبلغ {int(amount)} ريال لحساب الإدارة.")
                        except: pass
                except Exception as e:
                    print(f"Receipt Error: {e}")
                    
            import asyncio
            asyncio.create_task(send_finance_receipt())
            
            await notify_clients(ADMIN_ID)
            if action_type == 'pay_manager':
                await notify_clients(NETWORK_OWNER_ID)
            
        return fast_json_response(convert_decimals_to_float(result))
    except Exception as e:
        print(f"Finance Action Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تنفيذ العملية المالية."})

# =====================================================================
# دوال نظام الاستلام الأعمى (Blind Receive)
# =====================================================================
async def api_manager_send_shipment(request: web.Request):
    """المدير يرسل كروت (سلة كاملة) للوكيل"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    manager_items_str = data.get('manager_items', '{}')
    
    try:
        # 🚨 الحارس الأمني 2: فحص الإرسالية قبل لمس قاعدة البيانات
        manager_items = json.loads(manager_items_str)
        if not manager_items:
            return fast_json_response({"status": "error", "message": "الإرسالية فارغة! لم تقم باختيار أي كروت."})
            
        for ctype, qty in manager_items.items():
            if int(qty) <= 0:
                return fast_json_response({"status": "error", "message": f"الكمية المدخلة للفئة ({ctype}) غير صالحة!"})

        async with database.pool.acquire() as conn:
            await conn.execute("INSERT INTO pending_shipments (manager_items, status) VALUES ($1, 'pending')", manager_items_str)
            
        try: asyncio.create_task(smart_notify_web(ADMIN_ID, "🚚 **إشعار من الإدارة (عبر الويب):**\nتم إرسال دفعة كروت جديدة مع المندوب وهي في الطريق إليك.\n*(الرجاء فرزها وإدخال العدد في تطبيق الويب فور وصولها لمطابقتها).*"))
        except: pass
        # 🌟 إضافة إشعار التطبيق للوكيل
        try: asyncio.create_task(send_web_push(ADMIN_ID, "🚚 إرسالية جديدة", "الإدارة أرسلت كروت جديدة، يرجى فرزها عند وصولها."))
        except: pass
        
        await notify_clients(ADMIN_ID)
        
        return fast_json_response({"status": "success", "message": "تم إرسال الكروت للمندوب بنجاح! بانتظار استلام الوكيل."})

    except Exception as e:
        print(f"Manager Send Shipment Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء إرسال الكروت."})

async def api_check_pending_shipment(request: web.Request):
    """الوكيل يفحص هل توجد إرسالية معلقة في الطريق"""
    requester_id = require_admin(request) # 🚨 حماية الإدارة فقط
    try:
        async with database.pool.acquire() as conn:
            # نبحث عن إرسالية معلقة لم يقم الوكيل بفرزها بعد (agent_items IS NULL)
            row = await conn.fetchrow("SELECT id FROM pending_shipments WHERE status = 'pending' AND agent_items IS NULL ORDER BY created_at ASC LIMIT 1")
            if row:
                return fast_json_response({"status": "success", "shipment_id": row['id']})
            return fast_json_response({"status": "error", "message": "لا يوجد إرساليات"})
    except Exception as e:
        print(f"Check Pending Shipment Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء الفحص."})

async def api_match_shipment(request: web.Request):
    """الوكيل يدخل الجرد الفعلي ويطابقه النظام مع إرسالية المدير"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    shipment_id = int(data.get('shipment_id', 0))
    agent_items_str = data.get('agent_items', '{}')
    agent_items = json.loads(agent_items_str)
    
    try:
        async with database.pool.acquire() as conn:
            # 🌟 التعديل: إزالة القفل المزدوج (Transaction & FOR UPDATE) لمنع الـ Deadlock
            shipment = await conn.fetchrow("SELECT manager_items, status FROM pending_shipments WHERE id = $1", shipment_id)
            if not shipment or shipment['status'] != 'pending':
                return fast_json_response({"status": "error", "message": "الإرسالية غير موجودة أو تمت معالجتها مسبقاً."})
                
            manager_items = json.loads(shipment['manager_items'])
            
            # 🟢 حالة التطابق 100%
            if agent_items == manager_items:
                # 🌟 التوحيد: استدعاء المطبخ المركزي (هو من سيتولى القفل والتحديث)
                from core_accounting import core_receive_shipment
                await core_receive_shipment(shipment_id, agent_items, 'exact')
                
                try: asyncio.create_task(smart_notify_web(NETWORK_OWNER_ID, "✅ **إشعار استلام:**\nالوكيل استلم الإرسالية بنجاح والمطابقة سليمة 100% (من الويب)."))
                except: pass
                # 🌟 إضافة إشعار التطبيق للمدير
                try: asyncio.create_task(send_web_push(NETWORK_OWNER_ID, "✅ مطابقة الإرسالية", "الوكيل استلم الإرسالية والمطابقة سليمة 100%."))
                except: pass
                
                return fast_json_response({"status": "success"})

            # 🔴 حالة الاختلاف
            else:
                await conn.execute("UPDATE pending_shipments SET agent_items = $1 WHERE id = $2", agent_items_str, shipment_id)
                
                try: asyncio.create_task(smart_notify_web(NETWORK_OWNER_ID, "🚨 **تنبيه: اختلاف في الإرسالية!**\nالوكيل قام بفرز الإرسالية ووجد اختلافاً. يرجى فتح تطبيق الويب (لوحة المراقبة) لمعالجة الاختلاف."))
                except: pass
                # 🌟 إضافة إشعار التطبيق للمدير
                try: asyncio.create_task(send_web_push(NETWORK_OWNER_ID, "🚨 اختلاف في الجرد", "الوكيل وجد اختلافاً في الإرسالية، يرجى المراجعة."))
                except: pass
                
                return fast_json_response({"status": "mismatch"})
                
    except Exception as e:
        print(f"Match Shipment Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء المطابقة."})

async def api_get_mismatched_shipments(request: web.Request):
    """المدير يجلب الإرساليات التي اختلف فيها جرد الوكيل"""
    requester_id = require_admin(request) # 🚨 حماية الإدارة فقط
    try:
        async with database.pool.acquire() as conn:
            # نجلب الإرساليات المعلقة التي قام الوكيل بفرزها (agent_items IS NOT NULL)
            rows = await conn.fetch("SELECT id, manager_items, agent_items FROM pending_shipments WHERE status = 'pending' AND agent_items IS NOT NULL ORDER BY created_at ASC")
            shipments = [dict(r) for r in rows]
            return fast_json_response({"status": "success", "shipments": shipments})
    except Exception as e:
        print(f"Get Mismatched Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب البيانات."})

async def api_resolve_shipment(request: web.Request):
    """المدير يقرر: إما اعتماد جرد الوكيل أو إلغاء الإرسالية"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    shipment_id = int(data.get('shipment_id', 0))
    action = data.get('action') # 'accept' or 'cancel'
    
    try:
        async with database.pool.acquire() as conn:
            # 🌟 التعديل: إزالة القفل المزدوج لمنع الـ Deadlock
            shipment = await conn.fetchrow("SELECT agent_items, status FROM pending_shipments WHERE id = $1", shipment_id)
            
            if not shipment or shipment['status'] != 'pending':
                return fast_json_response({"status": "error", "message": "تمت معالجة هذه الإرسالية مسبقاً."})
                
            if action == 'cancel':
                await conn.execute("UPDATE pending_shipments SET status = 'cancelled' WHERE id = $1", shipment_id)
                try: asyncio.create_task(smart_notify_web(ADMIN_ID, "❌ **إشعار:** الإدارة قامت بإلغاء الإرسالية المعلقة بسبب الاختلاف."))
                except: pass
                # 🌟 إضافة إشعار التطبيق للوكيل
                try: asyncio.create_task(send_web_push(ADMIN_ID, "❌ إلغاء الإرسالية", "الإدارة قامت بإلغاء الإرسالية المعلقة بسبب الاختلاف."))
                except: pass
                return fast_json_response({"status": "success", "message": "تم إلغاء الإرسالية بالكامل."})
                
            elif action == 'accept':
                agent_items = json.loads(shipment['agent_items'])
                
                # 🌟 فحص هل هي كروت إلكترونية (تحتوي على أرقام سرية) أم ورقية (أعداد فقط)
                is_ecard = any(isinstance(v, list) for v in agent_items.values())
                
                if is_ecard:
                    # 🌟 معالجة الكروت الإلكترونية: إدخال الأرقام السرية للخزنة
                    total_price = Decimal('0.0')
                    structured_items = [] # 🌟 جديد
                    for ctype, pins in agent_items.items():
                        qty = len(pins)
                        
                        # 🌟 إصلاح ثغرة الكروت الشبحية: التأكد من وجود الفئة في المخزون أولاً، وإلا ننشئها بصفر
                        exists = await conn.fetchval("SELECT 1 FROM inventory WHERE card_type = $1", ctype)
                        if not exists:
                            await conn.execute("INSERT INTO inventory (card_type, quantity, cost_price, price, retail_price) VALUES ($1, 0, 0, 0, 0)", ctype)
                            
                        cost_price = await conn.fetchval("SELECT cost_price FROM inventory WHERE card_type = $1", ctype)
                        total_price += Decimal(str(cost_price or 0)) * qty
                        
                        structured_items.append({"card_type": ctype, "quantity": qty, "cost_price": str(cost_price or 0), "sell_price": "0.0"}) # 🌟 جديد
                        
                        for pin in pins:
                            try: encrypted_pin = encrypt_pin(pin)
                            except NameError: encrypted_pin = pin 
                            await conn.execute("INSERT INTO electronic_cards (card_number, card_type) VALUES ($1, $2)", encrypted_pin, ctype)
                            
                        await conn.execute("UPDATE inventory SET quantity = quantity + $1 WHERE card_type = $2", qty, ctype)
                        
                    # 🌟 الإصلاح المحاسبي: إرسال structured_details
                    await conn.execute("INSERT INTO transactions (user_id, type, amount, details, structured_details) VALUES (0, 'استلام_من_الشبكة', $1, $2, $3)", total_price, "استلام كروت إلكترونية (باعتماد العدد الفعلي للوكيل)", json.dumps(structured_items))
                    await conn.execute("UPDATE pending_shipments SET status = 'resolved' WHERE id = $1", shipment_id)
                else:
                    # 🌟 معالجة الكروت الورقية العادية
                    from core_accounting import core_receive_shipment
                    await core_receive_shipment(shipment_id, agent_items, 'resolved')
                    
                try: asyncio.create_task(smart_notify_web(ADMIN_ID, "✅ **إشعار:** الإدارة اعتمدت العدد الفعلي الذي أدخلته. تم إضافة الكروت لمخزونك بنجاح."))
                except: pass
                # 🌟 إضافة إشعار التطبيق للوكيل
                try: asyncio.create_task(send_web_push(ADMIN_ID, "✅ اعتماد الإرسالية", "الإدارة اعتمدت العدد الفعلي الذي أدخلته وتمت إضافته لمخزونك."))
                except: pass
                return fast_json_response({"status": "success", "message": "تم اعتماد العدد الفعلي للوكيل وتحديث المخزون."})

    except Exception as e:
        print(f"Resolve Shipment Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تنفيذ القرار."})

# =====================================================================
# دالة بيع الكروت للبقالة (مع حماية النقر المزدوج)
# =====================================================================
async def api_client_pos_sell(request: web.Request):
    user_id = await enforce_cashier_perms(request, 'physical')
    data = await request.post()
    
    # 🚨 حماية الدفع المزدوج (Idempotency)
    txn_uuid = data.get('txn_uuid', '')
    if txn_uuid:
        if txn_uuid in active_pos_requests:
            return fast_json_response({"status": "error", "message": "جاري معالجة هذا الطلب بالفعل..."})
        active_pos_requests.add(txn_uuid)
        
    try:
        try:
            client_id = int(data.get('client_id', 0))
            qty = int(data.get('quantity', 0))
        except ValueError:
            return fast_json_response({"status": "error", "message": "البيانات المدخلة غير صالحة."})
        
        if user_id not in [ADMIN_ID, NETWORK_OWNER_ID] and user_id != client_id:
            await send_security_alert(request, user_id, "تزوير الهوية (IDOR)", f"حاول تنفيذ عملية بيع من مخزون بقالة أخرى (ID: {client_id})!")
            return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك بتنفيذ عمليات على هذا الحساب!"})
            
        card_type = data.get('card_type')
        sale_type = data.get('sale_type', 'cash') 
        customer_name = data.get('customer_name', '').strip() 
        
        if qty <= 0:
            return fast_json_response({"status": "error", "message": "الكمية يجب أن تكون أكبر من صفر!"})
            
        if not card_type:
            return fast_json_response({"status": "error", "message": "يجب تحديد نوع الكرت!"})
            
        if sale_type == 'credit' and not customer_name:
            return fast_json_response({"status": "error", "message": "اسم الزبون مطلوب للبيع الآجل!"})
            
        # 🌟 إصلاح حساب التجربة (Demo)
        if user_id == 999999:
            await asyncio.sleep(1)
            return fast_json_response({"status": "success", "message": "تم تسجيل البيع بنجاح (وضع التجربة)!"})

        async with database.pool.acquire() as conn:
            # 🌟 الإصلاح الأمني: التأكد من أن الكرت نشط ولم يتم حذفه
            prices = await conn.fetchrow("SELECT price, retail_price FROM inventory WHERE card_type = $1 AND is_active = TRUE", card_type)
            if not prices:
                return fast_json_response({"status": "error", "message": "نوع الكرت غير موجود أو تم إيقافه من قبل الإدارة."})
                
            wholesale_price = Decimal(prices['price']) if prices['price'] else Decimal('0.0')
            retail_price = Decimal(prices['retail_price']) if prices['retail_price'] else Decimal('0.0')
            
            total_amount = retail_price * qty 
            client_profit = (retail_price - wholesale_price) * qty 
                
            async with conn.transaction():
                current_qty = await conn.fetchval("SELECT quantity FROM client_inventory WHERE user_id = $1 AND card_type = $2 FOR UPDATE", client_id, card_type)
                if not current_qty or current_qty < qty:
                    return fast_json_response({"status": "error", "message": "رصيدك لا يسمح بهذه الكمية!"})
                    
                await conn.execute("UPDATE client_inventory SET quantity = quantity - $1 WHERE user_id = $2 AND card_type = $3", qty, client_id, card_type)
                await conn.execute("INSERT INTO client_sales (client_id, card_type, quantity, total_price, profit) VALUES ($1, $2, $3, $4, $5)", client_id, card_type, qty, total_amount, client_profit)
                
                if sale_type == 'credit':
                    await conn.execute("""
                        INSERT INTO client_customers (client_id, customer_name, debt) 
                        VALUES ($1, $2, $3) 
                        ON CONFLICT (client_id, customer_name) 
                        DO UPDATE SET debt = client_customers.debt + $3
                    """, client_id, customer_name, total_amount)
                        
                    await conn.execute("INSERT INTO client_customer_ledger (client_id, customer_name, type, amount, details) VALUES ($1, $2, 'دين', $3, $4)", client_id, customer_name, total_amount, f"شراء {qty} كرت {card_type}")
                    details = f"بيع آجل: {qty} كرت {card_type} للزبون ({customer_name})"
                else:
                    details = f"بيع كاش: {qty} كرت {card_type}"
                    
                # 🌟 الإصلاح المحاسبي: تسجيلها كـ (مبيعات_بقالة) لحماية كاش الوكيل وتغذية الذكاء الاصطناعي
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type, idempotency_key) VALUES ($1, 'مبيعات_بقالة', $2, $3, 'manager', $4)", client_id, total_amount, details, txn_uuid)
                
                # 🌟 قانون الوكيل الصارم: زيادة الكاش الجاهز للوكيل في كل الحالات (كاش أو آجل) لأن البقالة تتحمل المسؤولية!
                await conn.execute("""
                    UPDATE users 
                    SET pos_cash_collected = GREATEST(0, LEAST(GREATEST(0, debt), COALESCE(pos_cash_collected, 0) + $1))
                    WHERE user_id = $2
                """, wholesale_price * qty, client_id)
                
        # 🌟 الإصلاح الأمني: إخراج الإشعارات خارج بلوك قاعدة البيانات لمنع الـ Deadlock
        try:
            await notify_clients(client_id)
            await notify_clients(ADMIN_ID)
        except Exception as e:
            print(f"Notify Error: {e}")
            
        return fast_json_response({"status": "success", "message": "تم تسجيل البيع بنجاح!"})
        
    except Exception as e:
        print(f"POS Sell Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تسجيل البيع."})
    finally:
        # 🧹 تنظيف الرقم المرجعي
        if txn_uuid in active_pos_requests:
            active_pos_requests.remove(txn_uuid)

async def api_returns(request: web.Request):
    """معالجة جميع أنواع المرتجعات والتوالف من الويب"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    return_type = data.get('return_type')
    
    try:
        qty = int(data.get('quantity', 0))
        amount = Decimal(data.get('amount', 0))
        client_id = int(data.get('client_id', 0))
    except (ValueError, InvalidOperation):
        return fast_json_response({"status": "error", "message": "البيانات المدخلة غير صالحة، يجب أن تكون أرقاماً."})

    card_type = data.get('card_type')
    
    if card_type and ('إلكتروني' in card_type or 'الكتروني' in card_type):
        if return_type in ['to_network', 'direct_sale', 'from_client']:
            return fast_json_response({
                "status": "error", 
                "message": "⛔ عذراً، لا يمكن معالجة الكروت الإلكترونية في قسم المرتجعات العادي لحساسية الأرقام السرية."
            })
            
    try:
        from core_accounting import core_process_return
        from unified_main import safe_send_whatsapp
        
        result = await core_process_return(return_type, qty, amount, client_id, card_type, source="(من الويب)")
        
        if result["status"] == "success":
            trans_id = result.get("trans_id", result.get("tx_id"))
            total_value = result.get("total_value", amount)
            
            # 🌟 إرسال الفواتير للتليجرام والواتساب في الخلفية
            async def send_return_receipt():
                try:
                    from pdf_generator import generate_receipt
                    from aiogram.types import BufferedInputFile
                    import asyncio
                    
                    if return_type == 'damaged':
                        pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, "كروت تالفة", amount, "كروت تالفة", "إرجاع كروت تالفة للإدارة (من الويب)")
                        pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
                        await smart_notify_web(ADMIN_ID, document=pdf_file, caption=f"✅ **تم تسجيل الكروت التالفة (من الويب)!**\nتم تسجيل {int(amount)} ريال كقيمة كروت تالفة.")
                        
                    elif return_type == 'from_client':
                        client_name = result["client_name"]
                        pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, client_name, total_value, "مرتجع من عميل", f"إرجاع {qty} كرت {card_type}")
                        
                        wa_text = f"🧾 *إشعار مرتجع*\nمرحباً {client_name}،\nتم استلام المرتجع: {qty} كرت من فئة {card_type}.\nتم خصم مبلغ *{int(total_value)} ريال* من مديونيتك.\nمرفق إيصال المرتجع للتأكيد 🌹"
                        await safe_send_whatsapp(client_id, wa_text, pdf_buffer, f"Receipt_{trans_id}.pdf", bot=bot_instance)
                        
                        pdf_buffer.seek(0)
                        pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
                        await smart_notify_web(ADMIN_ID, document=pdf_file, caption=f"✅ **تم قبول المرتجع (من الويب)!**\nتم سحب {qty} كرت من العميل، وخصم {int(total_value)} ريال من دينه.")
                        
                    elif return_type == 'to_network':
                        pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, "الإدارة العامة", total_value, "مرتجع للشبكة", f"إرجاع {qty} كرت {card_type} للإدارة (من الويب)")
                        pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
                        await smart_notify_web(ADMIN_ID, document=pdf_file, caption=f"✅ **تم تسجيل المرتجع للإدارة (من الويب)!**\nتم خصم {qty} كرت من مخزونك، وتسجيل {int(total_value)} ريال كمرتجع يخصم من مديونيتك للإدارة.")
                        
                    elif return_type == 'direct_sale':
                        pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, "مبيعات طياري", total_value, "مرتجع بيع مباشر", f"إرجاع {qty} كرت {card_type} طياري (من الويب)")
                        pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
                        await smart_notify_web(ADMIN_ID, document=pdf_file, caption=f"✅ **تم تسجيل مرتجع البيع المباشر (من الويب)!**\nتم إعادة {qty} كرت لمخزونك، وسحب {int(total_value)} ريال من الكاش.")
                        
                except Exception as e:
                    print(f"Return Receipt Error: {e}")
                    
            import asyncio
            asyncio.create_task(send_return_receipt())
            
            await notify_clients(ADMIN_ID)
            if return_type == 'from_client':
                await notify_clients(client_id)
            
        return fast_json_response(convert_decimals_to_float(result))
    except Exception as e:
        print(f"Returns Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ داخلي أثناء معالجة المرتجع."})

async def api_transfer_center(request: web.Request):
    """مركز التحويلات الشامل (مع الحارس الأمني والموجه المحاسبي)"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    sender = data.get('sender')
    receiver = data.get('receiver')
    t_type = data.get('t_type')
    card_type = data.get('card_type')

    try:
        amount = Decimal(data.get('amount', 0))
    except InvalidOperation:
        return fast_json_response({"status": "error", "message": "الكمية أو المبلغ المدخل غير صالح."})

    if amount <= 0:
        return fast_json_response({"status": "error", "message": "الكمية أو المبلغ يجب أن يكون أكبر من صفر!"})

    if sender == receiver:
        return fast_json_response({"status": "error", "message": "لا يمكن التحويل لنفس الحساب!"})

    try:
        from core_accounting import FinancialEngine
        engine = FinancialEngine(database.pool)
        
        # تحويل الأسماء لتناسب الكلاس الجديد
        s_type, s_id = ("agent", 0) if sender == "agent" else ("manager", 0) if sender == "manager" else ("client", int(sender.replace("client_", "")))
        r_type, r_id = ("agent", 0) if receiver == "agent" else ("manager", 0) if receiver == "manager" else ("client", int(receiver.replace("client_", "")))
        
        try:
            result = await engine.transfer_assets(s_type, s_id, r_type, r_id, t_type, amount, card_type)
        except Exception as e:
            return fast_json_response({"status": "error", "message": str(e)})
        
        return fast_json_response(convert_decimals_to_float(result))
    except Exception as e:
        print(f"Transfer Center Error: {e}")
        return fast_json_response({"status": "error", "message": f"حدث خطأ أثناء التحويل: {str(e)}"})

async def api_smart_cart_order(request: web.Request):
    """سلة طلبات البقالة وإشعار الوكيل (مع الجرد ومنع التكرار)"""
    try:
        user_id = check_jwt(request) # ✅ السماح للبقالة بالطلب
        data = await request.post()
        
        # 1. حماية تحويل البيانات
        try:
            client_id = int(data.get('client_id', 0))
            cart_data_str = data.get('cart_data', '[]')
            import json
            cart_items = json.loads(cart_data_str)
            
            # 🌟 التعديل الأمني: منع الهاكرز من إرسال كميات سالبة أو صفرية لاختراق الحسابات
            for item in cart_items:
                if int(item.get('quantity', 0)) <= 0:
                    return fast_json_response({"status": "error", "message": "🚨 محاولة تلاعب مرفوضة: الكمية يجب أن تكون أكبر من صفر!"})
                    
        except (ValueError, json.JSONDecodeError):
            return fast_json_response({"status": "error", "message": "البيانات المدخلة غير صالحة، يرجى تحديث الصفحة والمحاولة مجدداً."})
        
        # 🚨 حماية IDOR للـ POST
        if user_id not in [ADMIN_ID, NETWORK_OWNER_ID] and user_id != client_id:
            return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك!"})
            
        remaining_data = data.get('remaining_data', 'لا يوجد كروت سابقة')
        
        async with database.pool.acquire() as conn:
            # 🚨 حارس منع تكرار الطلبات
            existing_order = await conn.fetchval("SELECT id FROM pending_orders WHERE user_id = $1 AND status = 'pending'", client_id)
            if existing_order:
                return fast_json_response({"status": "error", "message": "لديك طلب سابق قيد المعالجة! يرجى الانتظار حتى يتم تسليمه لك أو تواصل مع الدعم الفني."})
                
            order_text = "\n".join([f"▪️ {item['quantity']} كرت ({item['card_type']})" for item in cart_items])
            
            client_name = "عميل"
            name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
            if name: client_name = name
            
            await conn.execute('''
                INSERT INTO pending_orders (user_id, client_name, remaining_text, new_order_text, status) 
                VALUES ($1, $2, $3, $4, 'pending')
            ''', client_id, client_name, remaining_data, order_text)
            
        # 🌟 التعديل: إرسال إشعار للوكيل في تليجرام (بصوت إجباري وتوجيه للصندوق)
        alert_text = (
            f"🔔 🚨 **طلب كروت جديد (عاجل من الويب)** 🚨 ??\n\n"
            f"👤 **العميل:** {client_name}\n"
            f"📦 **المتبقي في الدرج:**\n{remaining_data}\n\n"
            f"🛒 **الطلب الجديد:**\n{order_text}\n\n"
            f"💡 *الطلب الآن في صندوق الطلبات المعلقة بانتظار مراجعتك.*"
        )
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 فتح صندوق الطلبات المعلقة", callback_data="admin_view_pending_orders")],
            [InlineKeyboardButton(text="✅ تسليم الطلب مباشرة", callback_data=f"gclient_{client_id}")]
        ])
        
        try:
            # disable_notification=False تجبر التليجرام على إصدار صوت التنبيه حتى لو كان التطبيق مغلقاً
            await smart_notify_web(ADMIN_ID, alert_text, reply_markup=admin_kb, disable_notification=False)
        except Exception as tg_err:
            print(f"Telegram notification error: {tg_err}")
        # إرسال إشعار فايربيس للوكيل
        try:
            await send_web_push(ADMIN_ID, "🚨 طلب كروت جديد", f"العميل: {client_name} أرسل طلباً جديداً.")
        except: pass

        return fast_json_response({"status": "success", "message": "تم إرسال طلبك للإدارة بنجاح! سيتم تسليمك الكروت قريباً."})
        
    except Exception as e:
        import traceback
        error_msg = f"Order Error: {str(e)}\n{traceback.format_exc()[:500]}"
        print(error_msg)
        try:
            await smart_notify_web(ADMIN_ID, f"🔴 **كاشف أخطاء الويب (طلب كروت):**\n\n`{str(e)}`")
        except:
            pass
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء إرسال الطلب. يرجى المحاولة مرة أخرى."})

async def api_update_credit_limit(request: web.Request):
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    
    try:
        client_id = int(data.get('client_id', 0))
        new_limit = Decimal(data.get('new_limit', 0))
    except (ValueError, InvalidOperation):
        return fast_json_response({"status": "error", "message": "البيانات المدخلة غير صالحة."})
        
    if new_limit < 0:
        return fast_json_response({"status": "error", "message": "سقف المديونية لا يمكن أن يكون سالباً!"})
        
    # 🌟 التعديل: تحديد من قام بضبط السقف (المدير أم الوكيل)
    set_by = 'manager' if user_id == NETWORK_OWNER_ID else 'agent'
    
    try:
        async with database.pool.acquire() as conn:
            try:
                await conn.execute("UPDATE users SET credit_limit = $1, limit_set_by = $2 WHERE user_id = $3", new_limit, set_by, client_id)
            except:
                # إنشاء العمود تلقائياً إذا لم يكن موجوداً
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS limit_set_by VARCHAR(20) DEFAULT 'agent'")
                await conn.execute("UPDATE users SET credit_limit = $1, limit_set_by = $2 WHERE user_id = $3", new_limit, set_by, client_id)
                
        return fast_json_response({"status": "success", "message": "تم تحديث سقف المديونية بنجاح."})
    except Exception as e:
        print(f"Update Limit Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تحديث السقف."})

async def api_undo_last(request: web.Request):
    """التراجع عن آخر عملية مباشرة من تطبيق الويب"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    
    try:
        async with database.pool.acquire() as conn:
            # 🌟 إصلاح كارثة التراجع الأعمى: يجب أن يتراجع الوكيل عن عملياته الإدارية فقط، ويتجاهل مبيعات البقالات الداخلية
            # 🌟 الإصلاح المنطقي: تجاهل العمليات المتراجعة لكي لا يعلق زر التراجع عليها للأبد
            last_tx = await conn.fetchrow("""
                SELECT id FROM transactions 
                WHERE type != 'قيد_عكسي' 
                AND is_reverted = FALSE 
                AND amount > 0 
                AND details NOT LIKE '%إلغاء عملية%'
                AND type IN ('تسليم_لعميل', 'تسديد_من_عميل', 'مرتجع_من_عميل', 'مصروفات', 'تسديد_للشبكة', 'بيع_مباشر', 'نسبة_الوكيل', 'سحب_أرباح', 'رأس_مال_تسديدات', 'تحويل_رصيد')
                ORDER BY date DESC LIMIT 1
            """)

            if not last_tx:
                return fast_json_response({"status": "error", "message": "لا توجد عمليات حديثة للتراجع عنها."})
                
            tx_id = last_tx['id']
            
        from core_accounting import FinancialEngine
        engine = FinancialEngine(database.pool)
        
        try:
            result = await engine.revert_transaction(tx_id)
        except Exception as e:
            return fast_json_response({"status": "error", "message": str(e)})
        
        if result["status"] == "success":
            # إشعار الوكيل في التليجرام ليكون على علم بما حدث في الويب
            asyncio.create_task(smart_notify_web(ADMIN_ID, f"⚠️ **تراجع من الويب:**\nتم التراجع عن العملية رقم {tx_id} بنجاح عبر تطبيق الويب."))
            return fast_json_response({"status": "success", "message": result["message"]})
        else:
            return fast_json_response({"status": "error", "message": result["message"]})
            
    except Exception as e:
        print(f"Undo Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء التراجع."})
        
async def api_close_account(request: web.Request):
    """تصفية حساب عميل وسحب كروته من تطبيق الويب"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    
    try:
        client_id = int(data.get('client_id', 0))
    except ValueError:
        return fast_json_response({"status": "error", "message": "رقم العميل غير صالح."})
        
    try:
        from core_accounting import FinancialEngine
        engine = FinancialEngine(database.pool)
        
        try:
            result = await engine.close_account(client_id)
        except Exception as e:
            return fast_json_response({"status": "error", "message": str(e)})
        
        if result["status"] == "error":
            return fast_json_response({"status": "error", "message": result["message"]})
            
        client_name = result["client_name"]
        returned_cards_value = result["returned_cards_value"]
        details_str = result["details_str"]
        total_profit_to_reverse = result["total_profit_to_reverse"]
        refund_cash = result["refund_cash"]
        final_debt = result["final_debt"]
        
        msg = f"✅ **تم تصفية حساب العميل ({client_name}) بنجاح (من الويب)!**\n\n"
        if returned_cards_value > 0:
            msg += f"📦 **الكروت المسترجعة للمخزون:** {details_str}\n"
            msg += f"💰 **قيمة الكروت المسترجعة:** {returned_cards_value} ريال\n"
            msg += f"📉 **الربح المخصوم من المحفظة:** {total_profit_to_reverse} ريال\n\n"
        else:
            msg += "📦 **الكروت المسترجعة:** لا يوجد كروت في مخزونه.\n\n"
            
        if refund_cash > 0:
            msg += f"💵 **يجب إرجاع كاش للعميل من الصندوق بقيمة:** **{refund_cash} ريال**\n*(تم تسجيلها آلياً كـ تسوية نقدية لضبط كاش الصندوق)*"
        else:
            msg += f"💵 **الصافي المطلوب تسديده كاش لإغلاق الحساب:** **{final_debt} ريال**"
            
        # إشعار الوكيل في التليجرام بالتقرير المفصل
        asyncio.create_task(smart_notify_web(ADMIN_ID, msg))
        
        return fast_json_response({"status": "success", "message": f"تم تصفية حساب {client_name} بنجاح! راجع التليجرام للتفاصيل."})
        
    except Exception as e:
        print(f"Close Account Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تصفية الحساب."})
        
async def api_delete_offline_client(request: web.Request):
    """حذف عميل أوفلاين (وهمي) نهائياً من قاعدة البيانات"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    
    try:
        client_id = int(data.get('client_id', 0))
    except ValueError:
        return fast_json_response({"status": "error", "message": "رقم العميل غير صالح."})
        
    try:
        async with database.pool.acquire() as conn:
            # 1. التأكد أن العميل موجود وأنه فعلاً أوفلاين (الآيدي يبدأ بـ 999)
            if client_id < 9990000:
                return fast_json_response({"status": "error", "message": "لا يمكن حذف العملاء الحقيقيين من هنا!"})
                
            # 🌟 إصلاح ثغرة الحذف غير النظيف: يجب فحص ديون وأرصدة التسديدات أيضاً قبل الحذف
            user = await conn.fetchrow("SELECT debt, pending_profit, telecom_debt, telecom_balance FROM users WHERE user_id = $1", client_id)
            if not user:
                return fast_json_response({"status": "error", "message": "العميل غير موجود."})
                
            # 2. الحارس الأمني: التأكد أن الحساب مصفر تماماً (كروت + تسديدات)
            if Decimal(user['debt']) != 0 or Decimal(user['pending_profit']) != 0 or Decimal(user.get('telecom_debt', 0)) != 0 or Decimal(user.get('telecom_balance', 0)) != 0:
                return fast_json_response({"status": "error", "message": "⛔ لا يمكن حذف العميل لأن حسابه المالي (كروت أو تسديدات) غير مصفر!"})
                
            inv_count = await conn.fetchval("SELECT COALESCE(SUM(quantity), 0) FROM client_inventory WHERE user_id = $1", client_id)
            if inv_count > 0:
                return fast_json_response({"status": "error", "message": "⛔ لا يمكن حذف العميل لأن لديه كروت في المخزون!"})
                
            tx_count = await conn.fetchval("SELECT COUNT(*) FROM transactions WHERE user_id = $1", client_id)
            if tx_count > 0:
                return fast_json_response({"status": "error", "message": "⛔ لا يمكن حذف العميل لوجود عمليات مالية مسجلة باسمه. استخدم (تصفية حساب) بدلاً من ذلك."})

            # 3. الحذف النهائي الآمن
            await conn.execute("DELETE FROM users WHERE user_id = $1", client_id)
            
        return fast_json_response({"status": "success", "message": "✅ تم حذف العميل الوهمي نهائياً وبنجاح!"})
    except Exception as e:
        print(f"Delete Offline Client Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء الحذف."})
        
async def api_ai_daily_entry_image(request: web.Request):
    """استقبال صورة الدفتر من الويب وإرسالها للبوت للتحليل والتأكيد"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    client_id = int(data.get('client_id', 0))
    image = data.get('image')
    
    if image:
        try:
            # 🌟 التعديل الأمني: قراءة الصورة في مسار خلفي لمنع تجميد السيرفر
            import asyncio
            image_bytes = await asyncio.to_thread(image.file.read)
            from aiogram.types import BufferedInputFile
            photo = BufferedInputFile(image_bytes, filename="ledger.jpg")
            
            client_name = "المدير العام (الشبكة)" if client_id == 0 else "عميل"
            if client_id != 0:
                async with database.pool.acquire() as conn:
                    name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
                    if name: client_name = name
            
            # إرسال الصورة للبوت في تليجرام لكي يقوم الوكيل بتحليلها وتأكيدها هناك
            caption = f"📸 **صورة دفتر يومي (مرفوعة من الويب)**\n👤 الحساب: {client_name}\n\n💡 *قم بتحميل الصورة ثم استخدم زر (إدخال بالصورة يومي) هنا في البوت لتحليلها.*"
            asyncio.create_task(bot_instance.send_photo(ADMIN_ID, photo=photo, caption=caption))
            
            return fast_json_response({"status": "success", "message": "تم إرسال الصورة بنجاح."})
        except Exception as e:
            print(f"AI Image Upload Error: {e}")
            return fast_json_response({"status": "error", "message": "حدث خطأ أثناء رفع الصورة."})
            
    return fast_json_response({"status": "error", "message": "لم يتم استلام الصورة."})
    
# =====================================================================
# 4. دوال دفتر ديون الزبائن (ميزة البقالة)
# =====================================================================

async def api_client_customers(request: web.Request):
    requester_id = check_jwt(request) 
    client_id = int(request.match_info.get('id', 0))
    
    # 🚨 حماية IDOR: منع التجسس على زبائن البقالات الأخرى
    if requester_id not in [ADMIN_ID, NETWORK_OWNER_ID] and requester_id != client_id:
        return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك بالوصول لبيانات هذا الحساب!"})

    try:
        async with database.pool.acquire() as conn:
            customers = await conn.fetch("SELECT customer_name, debt FROM client_customers WHERE client_id = $1 AND debt > 0", client_id)
            data = {"status": "success", "customers": [dict(c) for c in customers]}
        return fast_json_response(convert_decimals_to_float(data))
    except Exception as e:
        print(f"Client Customers Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب بيانات الزبائن."})

import os
# تأكد من وجود هذا المتغير في أعلى الملف مع بقية الإعدادات
BRIDGE_SECRET_KEY = os.getenv("BRIDGE_SECRET_KEY", "").strip()

async def api_add_manual_debt(request: web.Request):
    data = await request.post()
    
    # 1. التحقق مما إذا كان الطلب قادماً من الجسر السري (تطبيق دُكّاني)
    bridge_key = data.get('bridge_key', '').strip()
    is_from_bridge = (bridge_key != "" and bridge_key == BRIDGE_SECRET_KEY)
    
    # 2. إذا لم يكن من الجسر، نطلب توكن تسجيل الدخول العادي
    user_id = None
    if not is_from_bridge:
        user_id = check_jwt(request)

    try:
        client_id = int(data.get('client_id', 0))
        amount = Decimal(data.get('amount', 0))
    except (ValueError, InvalidOperation):
        return fast_json_response({"status": "error", "message": "البيانات المدخلة غير صالحة، تأكد من إدخال أرقام صحيحة."})
    
    # 🚨 حماية IDOR (نتجاوزها فقط إذا كان الطلب موثوقاً من الجسر السري)
    if not is_from_bridge:
        if user_id not in [ADMIN_ID, NETWORK_OWNER_ID] and user_id != client_id:
            return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك!"})
            
    customer_name = data.get('customer_name', '')
    
    if amount <= 0:
        return fast_json_response({"status": "error", "message": "المبلغ يجب أن يكون أكبر من صفر!"})
    
    try:
        async with database.pool.acquire() as conn:
            async with conn.transaction(): # 🚨 قفل المعاملة المحاسبية
                await conn.execute("""
                    INSERT INTO client_customers (client_id, customer_name, debt) 
                    VALUES ($1, $2, $3) 
                    ON CONFLICT (client_id, customer_name) 
                    DO UPDATE SET debt = client_customers.debt + $3
                """, client_id, customer_name, amount)
                
                # تسجيل الحركة في كشف الحساب التفصيلي
                # 🌟 لمسة احترافية: توضيح مصدر الدين في كشف الحساب
                details_text = 'طلب آجل من تطبيق دُكّاني' if is_from_bridge else 'تسجيل دين يدوي'
                await conn.execute("INSERT INTO client_customer_ledger (client_id, customer_name, type, amount, details) VALUES ($1, $2, 'دين', $3, $4)", client_id, customer_name, amount, details_text)
                
        # 👇 التحديث اللحظي 👇
        await notify_clients(client_id)
        await notify_clients(ADMIN_ID)
        
        return fast_json_response({"status": "success", "message": f"تم تسجيل {amount} ريال على {customer_name}."})
    except Exception as e:
        print(f"Add Manual Debt Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تسجيل الدين."})

async def api_client_customer_pay(request: web.Request):
    user_id = check_jwt(request) # ✅ السماح للبقالة بتسديد ديون زبائنها
    data = await request.post()
    
    try:
        client_id = int(data.get('client_id', 0))
        amount = Decimal(data.get('amount', 0))
    except (ValueError, InvalidOperation):
        return fast_json_response({"status": "error", "message": "البيانات المدخلة غير صالحة، تأكد من إدخال أرقام صحيحة."})
    
    if user_id not in [ADMIN_ID, NETWORK_OWNER_ID] and user_id != client_id:
        return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك!"})
        
    customer_name = data.get('customer_name', '')
    
    if amount <= 0:
        return fast_json_response({"status": "error", "message": "المبلغ يجب أن يكون أكبر من صفر!"})
    
    try:
        async with database.pool.acquire() as conn:
            async with conn.transaction(): 
                # 🌟 التعديل الأمني: التأكد من وجود الزبون وقفل حسابه لمنع التلاعب
                customer = await conn.fetchrow("SELECT debt FROM client_customers WHERE client_id = $1 AND customer_name = $2 FOR UPDATE", client_id, customer_name)
                if not customer:
                    return fast_json_response({"status": "error", "message": "الزبون غير موجود في دفتر الديون!"})
                    
                if amount > Decimal(customer['debt']):
                    return fast_json_response({"status": "error", "message": f"المبلغ المدخل أكبر من دين الزبون ({customer['debt']} ريال)!"})
                    
                await conn.execute("UPDATE client_customers SET debt = debt - $1 WHERE client_id = $2 AND customer_name = $3", amount, client_id, customer_name)
                await conn.execute("INSERT INTO client_customer_ledger (client_id, customer_name, type, amount, details) VALUES ($1, $2, 'تسديد', $3, 'تسديد دفعة نقدية')", client_id, customer_name, amount)
                
                # 🌟 الشلال المالي العكسي: توزيع كاش الزبون على درج الكروت ثم درج التسديدات
                user_cash = await conn.fetchrow("SELECT debt, pending_profit, pos_cash_collected, telecom_debt, telecom_pos_cash FROM users WHERE user_id = $1 FOR UPDATE", client_id)
                
                # 🌟 الإصلاح: السقف هو الدين الإجمالي (debt) ليتطابق مع باقي النظام
                max_cards_cash = max(Decimal('0.0'), Decimal(user_cash['debt'] or 0))
                current_cards_cash = Decimal(user_cash['pos_cash_collected'] or 0)
                
                max_telecom_cash = max(Decimal('0.0'), Decimal(user_cash['telecom_debt'] or 0))
                current_telecom_cash = Decimal(user_cash['telecom_pos_cash'] or 0)
                
                amount_to_distribute = amount
                
                # 1. تعبئة درج الكروت أولاً (بحد أقصى سقف الدين)
                cards_space = max_cards_cash - current_cards_cash
                if cards_space > 0:
                    fill_cards = min(amount_to_distribute, cards_space)
                    current_cards_cash += fill_cards
                    amount_to_distribute -= fill_cards
                    
                # 2. تعبئة درج التسديدات بما فاض من المبلغ
                if amount_to_distribute > 0:
                    telecom_space = max_telecom_cash - current_telecom_cash
                    if telecom_space > 0:
                        fill_telecom = min(amount_to_distribute, telecom_space)
                        current_telecom_cash += fill_telecom
                        
                await conn.execute("UPDATE users SET pos_cash_collected = $1, telecom_pos_cash = $2 WHERE user_id = $3", current_cards_cash, current_telecom_cash, client_id)

        # 👇 التحديث اللحظي 👇
        await notify_clients(client_id)
        await notify_clients(ADMIN_ID)
        
        return fast_json_response({"status": "success", "message": f"تم تسديد {amount} ريال من حساب {customer_name}."})
    except Exception as e:
        print(f"Customer Pay Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تسديد الدين."})

# =====================================================================
# 5. دوال المحادثة والدعم الفني (Chat)
# =====================================================================

async def api_chat_history(request: web.Request):
    requester_id = check_jwt(request) 
    client_id = int(request.match_info.get('id', 0))
    
    # 🚨 حماية IDOR: منع قراءة محادثات البقالات الأخرى مع الإدارة
    if requester_id not in [ADMIN_ID, NETWORK_OWNER_ID] and requester_id != client_id:
        return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك بالوصول لبيانات هذا الحساب!"})

    try:
        async with database.pool.acquire() as conn:
            msgs = await conn.fetch("SELECT sender_type, message_text, created_at FROM chat_messages WHERE client_id = $1 ORDER BY created_at ASC", client_id)
            formatted_msgs = []
            for m in msgs:
                formatted_msgs.append({
                    "sender": "client" if m['sender_type'] == 'client' else "admin",
                    "text": m['message_text'],
                    "time": m['created_at'].strftime('%H:%M')
                })
        return fast_json_response({"status": "success", "messages": formatted_msgs})
    except Exception as e:
        print(f"Chat History Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب المحادثات."})

async def api_chat_send(request: web.Request):
    """استقبال رسالة العميل، الرد بالذكاء الاصطناعي، وإشعار الوكيل"""
    user_id = check_jwt(request) # ✅ السماح للبقالة بإرسال رسائل
    data = await request.post()
    client_id = int(data.get('client_id', 0))
    
    # 🚨 حماية IDOR
    if user_id not in [ADMIN_ID, NETWORK_OWNER_ID] and user_id != client_id:
        return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك!"})
        
    message = data.get('message', '')
    msg_type = data.get('msg_type', 'support')
    
    try:
        client_name = "عميل"
        async with database.pool.acquire() as conn:
            name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
            if name: client_name = name
            # 1. حفظ رسالة العميل
            await conn.execute("INSERT INTO chat_messages (client_id, sender_type, message_type, message_text) VALUES ($1, 'client', $2, $3)", client_id, msg_type, message)
            
        # 🌟 التعديل الأمني: إرسال إشعار للوكيل فوراً قبل انتظار الذكاء الاصطناعي لضمان عدم ضياع الرسالة
        admin_msg = (
            f"💬 **رسالة جديدة من عميل (ويب):**\n"
            f"👤 **العميل:** {client_name}\n"
            f"🗣️ **يقول:** {message}"
        )
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 الرد يدوياً على العميل", callback_data=f"msg_client_{client_id}")]
        ])
        asyncio.create_task(smart_notify_web(ADMIN_ID, admin_msg, reply_markup=admin_kb))
        # إرسال إشعار فايربيس للوكيل
        try:
            await send_web_push(ADMIN_ID, f"💬 رسالة من {client_name}", message)
        except: pass

        # 2. توليد رد الذكاء الاصطناعي (مع حماية من الانهيار)
        try:
            from ai_chat_panel import get_system_prompt, generate_groq_response
            system_prompt, _ = await get_system_prompt(client_id)
            ai_reply = await generate_groq_response(system_prompt, client_id, message)
            clean_reply = re.sub(r'\[.*?\]', '', ai_reply).strip()
            if not clean_reply: clean_reply = "تم استلام رسالتك وسيتم مراجعتها."
        except Exception as ai_err:
            print(f"AI Reply Error: {ai_err}")
            clean_reply = "عذراً، المساعد الذكي مشغول حالياً. تم تحويل رسالتك للإدارة وسيتم الرد عليك قريباً."

        # 3. حفظ رد الذكاء الاصطناعي (أو الرد البديل) ليظهر للعميل في الويب
        async with database.pool.acquire() as conn:
            await conn.execute("INSERT INTO chat_messages (client_id, sender_type, message_type, message_text) VALUES ($1, 'agent', $2, $3)", client_id, msg_type, clean_reply)

        # إشعار الوكيل برد الذكاء الاصطناعي
        if "المساعد الذكي مشغول" not in clean_reply:
            asyncio.create_task(smart_notify_web(ADMIN_ID, f"🤖 **رد الذكاء الاصطناعي على {client_name}:**\n{clean_reply}"))

        return fast_json_response({"status": "success"})
    except Exception as e:
        print(f"Chat Send Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء إرسال الرسالة."})

# =====================================================================
# 6. دوال عامة (إشعارات، أخطاء، تقارير)
# =====================================================================
async def api_log_error(request: web.Request):
    data = await request.post()
    error_msg = data.get('error_msg', 'خطأ غير معروف')
    client_id = data.get('client_id', 'غير مسجل')
    
    try:
        telegram_msg = (
            f"📱 **خطأ في هاتف عميل (تطبيق الويب)** 📱\n\n"
            f"👤 **رقم الحساب:** `{client_id}`\n"
            f"⚠️ **التفاصيل:**\n`{error_msg[:1000]}`"
        )
        asyncio.create_task(smart_notify_web(ADMIN_ID, telegram_msg))
    except Exception as e:
        print(f"Failed to send frontend error to Telegram: {e}")
        
    return fast_json_response({"status": "success"})
    
# =====================================================================
# دالة مساعدة لتوليد ملف إكسل لديون السوق (تعمل في مسار منفصل لمنع التجميد)
# =====================================================================
def generate_market_debt_excel(clients):
    import openpyxl
    from io import BytesIO
    from openpyxl.styles import Font, PatternFill, Alignment
    from decimal import Decimal
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ديون السوق"
    ws.sheet_view.rightToLeft = True
    
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    ws.append(["رقم العميل", "اسم البقالة", "الدين الحالي (ريال)", "سقف المديونية", "تاريخ الانضمام"])
    
    for col in range(1, 6):
        ws.cell(row=1, column=col).font = header_font
        ws.cell(row=1, column=col).fill = header_fill
        ws.cell(row=1, column=col).alignment = Alignment(horizontal="center")

    total_debt = Decimal('0.0')
    for c in clients:
        limit = c['credit_limit'] if c['credit_limit'] else 50000.0
        ws.append([str(c['user_id']), c['name'], abs(Decimal(c['debt'])), limit, str(c['joined_at'])[:10]])
        total_debt += Decimal(c['debt'])
    ws.append(["", "الإجمالي الكلي:", abs(total_debt), "", ""])
        
    stream = BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream

async def api_client_request_statement(request: web.Request):
    """العميل يطلب كشف حسابه (كروت أو تسديدات) ويصله عبر الواتساب وإشعار الويب"""
    user_id = check_jwt(request) # السماح للعميل بالمرور
    data = await request.post()
    report_type = data.get('report_type') # 'cards' or 'telecom'
    
    # 🌟 الجدار الناري: منع المدير العام من طلب كشف حساب التسديدات لأي عميل!
    if report_type == 'telecom' and user_id == NETWORK_OWNER_ID:
        return fast_json_response({"status": "error", "message": "⛔ هذا القسم خاص بالوكيل فقط ولا يحق للإدارة الاطلاع عليه."})
    
    # التأكد من أن العميل يطلب كشف حسابه هو فقط (أو الوكيل/المدير يطلب نيابة عنه)
    client_id = user_id
    if user_id in [ADMIN_ID, NETWORK_OWNER_ID]:
        client_id = int(data.get('client_id', 0))
        
    async def generate_and_send_wa():
        try:
            from datetime import timedelta
            from pdf_generator import generate_detailed_statement
            from unified_main import safe_send_whatsapp
            import asyncio
            from decimal import Decimal
            
            end_date_obj = datetime.now().date()
            start_date_obj = end_date_obj - timedelta(days=30) # كشف لآخر 30 يوم
            
            async with database.pool.acquire() as conn:
                # 🌟 الإصلاح المحاسبي: جلب الأرباح المعلقة لحساب الدين الصافي
                user = await conn.fetchrow("SELECT name, debt, telecom_debt, pending_profit FROM users WHERE user_id = $1", client_id)
                if not user: return
                
                # تنظيف اسم العميل من المسافات لتجنب مشاكل أسماء الملفات في الواتساب
                safe_name = user['name'].replace(" ", "_").replace("/", "_")
                
                if report_type == 'telecom':
                    # جلب حركات التسديدات فقط (عبر الجدار الناري)
                    txs = await conn.fetch('''
                        SELECT date, type, amount, details 
                        FROM transactions 
                        WHERE user_id = $1 AND date >= $2::date AND date <= $3::date + interval '1 day'
                        AND wallet_type = 'telecom'
                        ORDER BY date DESC
                    ''', client_id, start_date_obj, end_date_obj)
                    
                    pdf_buffer = await asyncio.to_thread(generate_detailed_statement, user['name'] + " (قسم التسديدات)", Decimal(user['telecom_debt'] or 0), txs, str(start_date_obj), str(end_date_obj))
                    wa_text = f"📑 *كشف حساب التسديدات*\nمرحباً {user['name']}،\nمرفق كشف حسابك التفصيلي لقسم الخدمات الإلكترونية لآخر 30 يوم 🌹"
                    file_name = f"Telecom_Statement_{safe_name}.pdf"
                    
                else: 
                    # جلب حركات الكروت فقط (عبر الجدار الناري)
                    txs = await conn.fetch('''
                        SELECT date, type, amount, details 
                        FROM transactions 
                        WHERE user_id = $1 AND date >= $2::date AND date <= $3::date + interval '1 day'
                        AND wallet_type = 'manager'
                        ORDER BY date DESC
                    ''', client_id, start_date_obj, end_date_obj)
                    
                    # 🌟 الإصلاح المحاسبي: إرسال الدين الصافي للعميل لكي لا يغضب من الفاتورة
                    net_debt = Decimal(user['debt'] or 0) - Decimal(user.get('pending_profit') or 0)
                    
                    pdf_buffer = await asyncio.to_thread(generate_detailed_statement, user['name'], net_debt, txs, str(start_date_obj), str(end_date_obj))
                    wa_text = f"📑 *كشف حساب الكروت*\nمرحباً {user['name']}،\nمرفق كشف حسابك التفصيلي لقسم الكروت لآخر 30 يوم 🌹"
                    file_name = f"Cards_Statement_{safe_name}.pdf"
                    
                # 1. إرسال الـ PDF للواتساب
                await safe_send_whatsapp(client_id, wa_text, pdf_buffer, file_name, bot=bot_instance)
                
                # 2. إرسال إشعار لجرس الويب (Web Push)
                await send_web_push(client_id, "📑 كشف الحساب جاهز", "تم إرسال كشف الحساب التفصيلي (PDF) إلى رقمك في الواتساب بنجاح.")
                
        except Exception as e:
            print(f"Statement WA Error: {e}")

    # تشغيل التوليد والإرسال في الخلفية لكي لا يتجمد تطبيق الويب
    import asyncio
    asyncio.create_task(generate_and_send_wa())
    
    return fast_json_response({"status": "success", "message": "تم استلام الطلب. سيصلك كشف الحساب (PDF) عبر الواتساب خلال لحظات."})

# =====================================================================
# دالة إرسال التقارير (المطورة)
# =====================================================================
async def api_send_report(request: web.Request):
    """استقبال طلب التقرير من الويب، توليده، وإرساله للتليجرام"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط (هذا هو الرقم الآمن الموثوق)
    data = await request.post()
    report_type = data.get('report_type')
    # 🌟 الإصلاح الأمني: تم حذف السطر الذي كان يمسح هوية المدير ويستبدلها بـ 0
    
    # دالة داخلية لإنشاء وإرسال التقرير في الخلفية (لكي لا يتجمد تطبيق الويب)
    async def generate_and_send():
        try:
            from aiogram.types import BufferedInputFile
            import asyncio
            
            if report_type == 'market_debt':
                # 1. توليد إكسل ديون السوق
                async with database.pool.acquire() as conn:
                    clients = await conn.fetch("SELECT user_id, name, debt, credit_limit, joined_at FROM users WHERE role = 'client' ORDER BY debt DESC")
                    
                # 🌟 التعديل: تشغيل الإكسل في مسار منفصل لكي لا يتجمد السيرفر
                stream = await asyncio.to_thread(generate_market_debt_excel, clients)
                
                file_name = f"Market_Debt_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
                document = BufferedInputFile(stream.read(), filename=file_name)
                await smart_notify_web(user_id, text="📊 **ملف ديون السوق (مطلوب من الويب)**", document=document)

            elif report_type == 'monthly':
                # 2. توليد التقرير الشهري (إكسل)
                import calendar
                now = datetime.now()
                start_date = now.replace(day=1).strftime('%Y-%m-%d')
                last_day = calendar.monthrange(now.year, now.month)[1]
                end_date = now.replace(day=last_day).strftime('%Y-%m-%d')
                
                from core_accounting import generate_detailed_excel_report
                excel_stream = await generate_detailed_excel_report(start_date, end_date)
                
                file_name = f"Monthly_Report_{now.strftime('%Y-%m')}.xlsx"
                document = BufferedInputFile(excel_stream.read(), filename=file_name)
                await smart_notify_web(user_id, text="📊 **التقرير الشهري (مطلوب من الويب)**", document=document)

            elif report_type == 'client_statement':
                # 3. توليد كشف حساب تفصيلي لبقالة (PDF)
                client_id = int(data.get('client_id', 0))
                from datetime import timedelta
                end_date_obj = datetime.now().date()
                start_date_obj = end_date_obj - timedelta(days=30) # افتراضياً آخر 30 يوم
                
                async with database.pool.acquire() as conn:
                    # 🌟 الإصلاح المحاسبي: جلب الأرباح المعلقة لحساب الدين الصافي
                    user = await conn.fetchrow("SELECT name, debt, pending_profit FROM users WHERE user_id = $1", client_id)
                    if user:
                        transactions = await conn.fetch('''
                            SELECT date, type, amount, details 
                            FROM transactions 
                            WHERE user_id = $1 AND date >= $2::date AND date <= $3::date + interval '1 day'
                            ORDER BY date DESC
                        ''', client_id, start_date_obj, end_date_obj)
                        
                        # 🌟 الإصلاح المحاسبي: إرسال الدين الصافي
                        net_debt = Decimal(user['debt'] or 0) - Decimal(user.get('pending_profit') or 0)
                        from pdf_generator import generate_detailed_statement
                        pdf_buffer = await asyncio.to_thread(generate_detailed_statement, user['name'], net_debt, transactions, str(start_date_obj), str(end_date_obj))
                        
                        pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Ledger_{user['name']}.pdf")
                        await smart_notify_web(user_id, text=f"📑 **كشف حساب تفصيلي (مطلوب من الويب)**\nالعميل: {user['name']}", document=pdf_file)

        except Exception as e:
            print(f"Background Report Error: {e}")
            await smart_notify_web(user_id, f"❌ حدث خطأ أثناء تجهيز التقرير: {e}")

    # تشغيل الدالة في الخلفية لكي لا يتجمد تطبيق الويب
    import asyncio
    asyncio.create_task(generate_and_send())
    
    return fast_json_response({"status": "success", "message": "تم استلام طلب التقرير، جاري تجهيزه وإرساله إلى محادثتك في تيليجرام..."})

async def api_manager_broadcast(request: web.Request):
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    # 🌟 حماية الصلاحيات: المدير فقط يرسل تعميمات الإدارة
    if user_id != NETWORK_OWNER_ID:
        return fast_json_response({"status": "error", "message": "⛔ هذا القسم خاص بالمدير العام فقط!"})
    data = await request.post()
    message = data.get('message', '')
    # 👈 التعديل: إرسال التعميم الفعلي للوكيل
    asyncio.create_task(smart_notify_web(ADMIN_ID, f"📢 **تعميم عاجل من الإدارة العامة (من الويب):**\n\n{message}\n\n*(يمكنك نسخ هذه الرسالة وتعميمها على عملائك إذا لزم الأمر)*"))
    # 🌟 إضافة إشعار التطبيق للوكيل
    try: asyncio.create_task(send_web_push(ADMIN_ID, "📢 تعميم إداري", "الإدارة أرسلت تعميماً جديداً، يرجى الاطلاع عليه."))
    except: pass
    return fast_json_response({"status": "success", "message": "تم إرسال التعميم للوكيل بنجاح."})

async def api_manager_request_cash(request: web.Request):
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    # 🌟 حماية الصلاحيات: المدير فقط يطلب الكاش
    if user_id != NETWORK_OWNER_ID:
        return fast_json_response({"status": "error", "message": "⛔ هذا القسم خاص بالمدير العام فقط!"})
    # 👈 التعديل: إرسال الطلب الفعلي للوكيل
    asyncio.create_task(smart_notify_web(ADMIN_ID, f"🚨 **طلب تحويل عاجل (من الويب):**\nالمدير العام (رهيب) يطلب تحويل السيولة النقدية المتوفرة لديك حالياً."))
    # 🌟 إضافة إشعار التطبيق للوكيل
    try: asyncio.create_task(send_web_push(ADMIN_ID, "🚨 طلب كاش عاجل", "المدير العام يطلب تحويل السيولة النقدية المتوفرة."))
    except: pass
    return fast_json_response({"status": "success", "message": "تم إرسال طلب الكاش للوكيل."})

async def api_upload_card_image(request: web.Request):
    """رفع صورة الكرت مع استخراج الرقم بذكاء (مطابق للجافاسكريبت)"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    card_type = data.get('card_type', '')
    image = data.get('image')
    
    if image:
        try:
            import os
            import re
            import asyncio
            if not os.path.exists('static'):
                os.makedirs('static')
                
            # 🌟 الإصلاح الأمني: استخدام اسم الكرت كاملاً لمنع تداخل الصور للشركات المختلفة
            # نستبدل المسافات والرموز بشرطة سفلية ليكون اسماً صالحاً للملفات
            img_name = card_type.replace(" ", "_").replace("/", "_").replace("\\", "_")
            file_path = f"static/{img_name}.png"
            
            # 🌟 التعديل الأمني: قراءة وحفظ الصورة على دفعات (Chunks) لحماية الذاكرة (RAM)
            def save_image_in_chunks():
                with open(file_path, "wb") as f:
                    while True:
                        chunk = image.file.read(8192) # قراءة 8 كيلوبايت في كل دفعة
                        if not chunk:
                            break
                        f.write(chunk)
            await asyncio.to_thread(save_image_in_chunks)
            
            return fast_json_response({"status": "success", "message": "تم تحديث صورة الكرت بنجاح."})
        except Exception as e:
            print(f"Upload Image Error: {e}")
            return fast_json_response({"status": "error", "message": "حدث خطأ أثناء حفظ الصورة."})
            
    return fast_json_response({"status": "error", "message": "لم يتم استلام أي صورة."})
        
async def api_notifications(request: web.Request):
    """جلب الإشعارات الحقيقية من قاعدة البيانات"""
    requester_id = check_jwt(request) 
    user_id = int(request.match_info.get('id', 0))
    
    # 🚨 حماية IDOR: منع قراءة إشعارات الآخرين
    if requester_id not in [ADMIN_ID, NETWORK_OWNER_ID] and requester_id != user_id:
        return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك بالوصول لبيانات هذا الحساب!"})

    try:
        async with database.pool.acquire() as conn:
            notifs = await conn.fetch("SELECT title, message, created_at FROM web_notifications WHERE user_id = $1 ORDER BY created_at DESC LIMIT 10", user_id)
            formatted_notifs = [{"title": n['title'], "message": n['message'], "created_at": n['created_at'].strftime('%Y-%m-%d %H:%M')} for n in notifs]
        return fast_json_response({"status": "success", "notifications": formatted_notifs})
    except Exception as e:
        print(f"Notifications Error: {e}")
        return fast_json_response({"status": "error", "message": "خطأ في جلب الإشعارات"})

async def api_mark_notifications_read(request: web.Request):
    """حذف الإشعارات بعد أن يقرأها المستخدم في الويب"""
    requester_id = check_jwt(request) 
    data = await request.post()
    target_user_id = int(data.get('user_id', 0))
    
    # 🚨 حماية IDOR
    if requester_id not in [ADMIN_ID, NETWORK_OWNER_ID] and requester_id != target_user_id:
        return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك!"})
        
    try:
        async with database.pool.acquire() as conn:
            await conn.execute("DELETE FROM web_notifications WHERE user_id = $1", target_user_id)
        return fast_json_response({"status": "success"})
    except Exception as e:
        return fast_json_response({"status": "error"})

async def api_chat_voice(request: web.Request):
    """استقبال البصمة الصوتية من العميل وإرسالها للوكيل في تليجرام"""
    user_id = check_jwt(request) # ✅ السماح للبقالة بإرسال بصمة صوتية
    data = await request.post()
    client_id = int(data.get('client_id', 0))
    
    # 🚨 حماية IDOR
    if user_id not in [ADMIN_ID, NETWORK_OWNER_ID] and user_id != client_id:
        return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك!"})
    voice = data.get('voice')
    
    if voice:
        try:
            import asyncio
            # 🌟 الإصلاح الأمني: قراءة الملف الصوتي في مسار خلفي لمنع تجميد السيرفر
            if hasattr(voice, 'file'):
                voice_bytes = await asyncio.to_thread(voice.file.read)
            else:
                return fast_json_response({"status": "error", "message": "الملف الصوتي غير صالح."})
            from aiogram.types import BufferedInputFile
            voice_file = BufferedInputFile(voice_bytes, filename="voice_message.ogg")
            
            client_name = "عميل"
            async with database.pool.acquire() as conn:
                name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
                if name: client_name = name
                # تسجيل أن العميل أرسل بصمة صوتية في سجل المحادثة
                await conn.execute("INSERT INTO chat_messages (client_id, sender_type, message_type, message_text) VALUES ($1, 'client', 'support', '🎤 [رسالة صوتية]')", client_id)
            
            # إرسال البصمة الصوتية للوكيل في تليجرام لكي يسمعها
            caption = f"🎤 **رسالة صوتية من العميل (عبر الويب):**\n👤 {client_name}"
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            admin_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 الرد يدوياً على العميل", callback_data=f"msg_client_{client_id}")]
            ])
            asyncio.create_task(bot_instance.send_voice(ADMIN_ID, voice=voice_file, caption=caption, reply_markup=admin_kb))
                        # إرسال إشعار فايربيس للوكيل
            try:
                await send_web_push(ADMIN_ID, f"🎤 رسالة صوتية من {client_name}", "العميل أرسل بصمة صوتية.")
            except: pass

            return fast_json_response({"status": "success", "message": "تم إرسال رسالتك الصوتية للإدارة بنجاح."})
        except Exception as e:
            print(f"Voice Chat Error: {e}")
            return fast_json_response({"status": "error", "message": "حدث خطأ أثناء معالجة الصوت."})
            
    return fast_json_response({"status": "error", "message": "لم يتم استلام الصوت."})

from aiohttp import web

# 🌟 دالة ذكية لجلب رقم الإصدار من قاعدة البيانات مباشرة
async def get_current_app_version():
    version = 31.4
    if database.pool:
        try:
            async with database.pool.acquire() as conn:
                db_version = await conn.fetchval("SELECT value FROM settings WHERE key = 'app_version'")
                if db_version: version = float(db_version)
        except: pass
    return version

async def api_app_version(request: web.Request):
    """إخبار الهواتف برقم الإصدار الأخير"""
    current_version = await get_current_app_version()
    
    # 🌟 جلب رقم الـ APK المطلوب من قاعدة البيانات
    required_apk_version = 0
    if database.pool:
        try:
            async with database.pool.acquire() as conn:
                db_apk_ver = await conn.fetchval("SELECT value FROM settings WHERE key = 'required_apk_version'")
                if db_apk_ver: required_apk_version = int(db_apk_ver)
        except: pass

    return fast_json_response({
        "status": "success", 
        "latest_version": current_version,
        "required_apk_version": required_apk_version # 🌟 إرسال الرقم للتطبيق
    })

async def api_auto_bump_version(request: web.Request):
    """رابط سري يستقبله السيرفر من GitHub لرفع رقم الإصدار تلقائياً"""
    import hmac
    token = request.query.get('token', '')
    run_number = request.query.get('run_number', '0') # 🌟 استقبال رقم البناء من جيت هاب
    
    if not hmac.compare_digest(str(token).strip(), str(API_SECRET_KEY).strip()):
        return web.Response(text="Unauthorized", status=401)
        
    try:
        new_version = "31.5"
        if database.pool:
            async with database.pool.acquire() as conn:
                current_version = await get_current_app_version()
                new_version = str(round(float(current_version) + 0.1, 1))
                
                # تحديث إصدار الويب
                await conn.execute("""
                    INSERT INTO settings (key, value) VALUES ('app_version', $1)
                    ON CONFLICT (key) DO UPDATE SET value = $1
                """, new_version)
                
                # 🌟 حفظ رقم إصدار الـ APK التلقائي
                if run_number != '0':
                    await conn.execute("""
                        INSERT INTO settings (key, value) VALUES ('required_apk_version', $1)
                        ON CONFLICT (key) DO UPDATE SET value = $1
                    """, str(run_number))
                
        await notify_clients(action="update_needed")
        import asyncio
        asyncio.create_task(smart_notify_web(ADMIN_ID, f"🚀 **تحديث آلي للنظام:**\nتم بناء APK جديد في GitHub (رقم البناء: {run_number})، وقام السيرفر برفع رقم الإصدار تلقائياً.\nجميع العملاء الذين يمتلكون النسخة القديمة سيطلب منهم التحديث الآن!"))
        
        return web.Response(text=f"Version bumped to {new_version}, APK Run: {run_number}", status=200)
    except Exception as e:
        return web.Response(text=f"Error: {e}", status=500)

async def serve_index_html(request: web.Request):
    try:
        with open('index.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
            
        html_content = html_content.replace("MANAGER_ID_PLACEHOLDER", str(NETWORK_OWNER_ID))
        html_content = html_content.replace("ADMIN_ID_PLACEHOLDER", str(ADMIN_ID))
        
        # جلب الإصدار الحالي من قاعدة البيانات وحقنه في ملف HTML
        current_version = await get_current_app_version()
        html_content = html_content.replace("APP_VERSION_PLACEHOLDER", str(current_version))

        # 🌟 السر هنا: إجبار المتصفح على جلب النسخة الجديدة دائماً مع السماح للـ SW بتخزينها للأوفلاين
        headers = {
            'Cache-Control': 'no-cache, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0'
        }

        return web.Response(text=html_content, content_type='text/html', headers=headers)
    except FileNotFoundError:
        # تم إصلاح القطع في النص هنا
        return web.Response(text="❌ ملف index.html غير موجود في المجلد الرئيسي!", status=404)

async def serve_assetlinks(request: web.Request):
    """دالة لتقديم ملف التحقق الخاص بتطبيق الأندرويد (Deep Linking)"""
    assetlinks_data = [{
      "relation": ["delegate_permission/common.handle_all_urls"],
      "target": {
        "namespace": "android_app",
        "package_name": "com.alshehab.pro",  # 🌟 تم توحيد اسم الحزمة ليتطابق مع تطبيق الـ APK
        "sha256_cert_fingerprints": ["E7:7A:00:74:B4:AC:3B:EB:29:D6:74:F3:F7:4D:43:2F:19:2E:E3:A0:33:44:12:45:C2:15:28:3D:66:F3:CF:23"]
      }
    }]
    return fast_json_response(assetlinks_data)

async def serve_sw(request: web.Request):
    """دالة لتقديم ملف Service Worker مع حقن رقم الإصدار لتحديث الكاش"""
    try:
        # نقرأ الملف المدمج الجديد من المجلد الرئيسي
        with open('firebase-messaging-sw.js', 'r', encoding='utf-8') as f:
            sw_content = f.read()

        # 🌟 التعديل هنا: جلب الإصدار من قاعدة البيانات بدلاً من المتغير الثابت
        current_version = await get_current_app_version()
        sw_content = sw_content.replace("CACHE_VERSION_PLACEHOLDER", str(current_version))
        
        # 🌟 اللمسة الاحترافية: منع المتصفح من تخزين ملف الـ SW نفسه لكي يكتشف التحديثات فوراً
        headers = {
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
            'Service-Worker-Allowed': '/' # تأكيد صلاحية الملف للتحكم بكامل الموقع
        }
        
        return web.Response(text=sw_content, content_type='application/javascript', headers=headers)
    except FileNotFoundError:
        return web.Response(text="Service Worker not found", status=404)

async def api_agent_wallet_info(request: web.Request):
    """جلب بيانات محفظة الوكيل بالكامل (الرصيد والسجل)"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط

    # 🌟 حماية الخصوصية: منع المدير العام من التجسس على أرباح ورصيد الوكيل
    if user_id != ADMIN_ID:
        return fast_json_response({"status": "error", "message": "⛔ هذا القسم خاص بالوكيل فقط!"})

    try:
        async with database.pool.acquire() as conn:
            # 🌟 تحسين الأداء: تمرير الاتصال (conn) للدالة لتوفير استهلاك قاعدة البيانات
            total_profit = await database.get_agent_total_profit(conn)
            
            history = await conn.fetch("SELECT amount, details, date FROM agent_profits ORDER BY date DESC LIMIT 10")
            
            # 👇 جلب رصيد البوابة للوكيل من المحفظة الجديدة 👇
            agent_telecom_balance = await conn.fetchval("SELECT telecom_balance FROM agent_wallet WHERE id = 1")
            agent_telecom_balance = float(agent_telecom_balance) if agent_telecom_balance else 0.0

        history_list = [{"amount": float(h['amount']), "details": h['details'], "date": str(h['date'])[:16]} for h in history]
        return fast_json_response({
            "status": "success", 
            "total_profit": float(total_profit), 
            "history": history_list,
            "telecom_balance": agent_telecom_balance  # 👈 إرسال الرصيد للويب
        })
    except Exception as e:
        return fast_json_response({"status": "error", "message": str(e)})

async def api_withdraw_profit(request: web.Request):
    """سحب أرباح الوكيل من الويب"""
    user_id = require_admin(request) # 🚨 حماية: الإدارة فقط
    # 🌟 حماية الصلاحيات: هذا الزر للوكيل فقط
    if user_id != ADMIN_ID:
        return fast_json_response({"status": "error", "message": "⛔ هذا القسم خاص بالوكيل فقط!"})
    data = await request.post()
    
    try:
        amount = Decimal(data.get('amount', 0))
    except InvalidOperation:
        return fast_json_response({"status": "error", "message": "المبلغ المدخل غير صالح."})
    
    if amount <= 0:
        return fast_json_response({"status": "error", "message": "المبلغ يجب أن يكون أكبر من صفر!"})
    
    try:
        # 🌟 التعديل: استخدام المحرك المالي لسحب الأرباح
        from core_accounting import FinancialEngine, TxType
        engine = FinancialEngine(database.pool)
        try:
            result = await engine.finance_action(TxType.WITHDRAW_PROFIT, amount, "سحب أرباح شخصية للوكيل", source="(من الويب)")
        except Exception as e:
            return fast_json_response({"status": "error", "message": str(e)})
        
        if result["status"] == "success":
            # 🌟 الإصلاح: التوافق مع مفاتيح المحرك الجديد
            trans_id = result.get("tx_id", result.get("trans_id"))
            
            # 🌟 إرسال الفاتورة للتليجرام في الخلفية
            async def send_withdraw_receipt():
                try:
                    from pdf_generator import generate_receipt
                    from aiogram.types import BufferedInputFile
                    import asyncio
                    
                    pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, "الوكيل (د. وليد)", amount, "سحب أرباح", "سحب أرباح شخصية للوكيل (من الويب)")
                    pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
                    await smart_notify_web(ADMIN_ID, document=pdf_file, caption=f"✅ **تم سحب الأرباح (من الويب)!**\nأخذت {int(amount)} ريال من الصندوق حلالاً زلالاً.")
                except Exception as e:
                    print(f"Receipt Error: {e}")
                    
            import asyncio
            asyncio.create_task(send_withdraw_receipt())
            
            await notify_clients(ADMIN_ID)
            
        return fast_json_response(convert_decimals_to_float(result))
    except Exception as e:
        print(f"Withdraw Profit Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء السحب."})

async def api_client_customer_ledger(request: web.Request):
    """جلب كشف الحساب التفصيلي لزبون البقالة"""
    user_id = check_jwt(request) # ✅ السماح للبقالة برؤية كشف زبائنها
    data = await request.post()
    client_id = int(data.get('client_id', 0))
    
    # 🚨 حماية IDOR
    if user_id not in [ADMIN_ID, NETWORK_OWNER_ID] and user_id != client_id:
        return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك!"})
    customer_name = data.get('customer_name', '')
    
    try:
        async with database.pool.acquire() as conn:
            ledger = await conn.fetch("""
                SELECT type, amount, details, date 
                FROM client_customer_ledger 
                WHERE client_id = $1 AND customer_name = $2 
                ORDER BY date DESC LIMIT 50
            """, client_id, customer_name)
            
            data_resp = {"status": "success", "ledger": [dict(l) for l in ledger]}
        return fast_json_response(convert_decimals_to_float(data_resp))
    except Exception as e:
        print(f"Customer Ledger Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب كشف الحساب."})
        
# =====================================================================
# مستقبل رسائل الواتساب (النسخة النهائية الشاملة 🚀)
# =====================================================================
async def process_whatsapp_background(data):
    """معالجة رسائل الواتساب في الخلفية لمنع الـ Timeout"""
    import asyncio
    import re
    import traceback
    try:
        message_text = ""
        sender_phone = ""

        # =================================================================
        # 🌟 الاستخراج المرن (يدعم كل إصدارات الواتساب و Evolution API)
        # =================================================================
        msg_obj = {}
        if "data" in data:
            if "message" in data["data"] and "key" in data["data"]["message"]:
                msg_obj = data["data"]["message"]
            elif "messages" in data["data"] and isinstance(data["data"]["messages"], list):
                msg_obj = data["data"]["messages"][0]
            else:
                msg_obj = data["data"]
        else:
            msg_obj = data

        # تجاهل رسائل البوت لنفسه لمنع التكرار
        if msg_obj.get("key", {}).get("fromMe") == True:
            return

        # استخراج الرقم
        key_obj = msg_obj.get("key", {})
        remote_jid = key_obj.get("remoteJid", "")
        remote_jid_alt = key_obj.get("remoteJidAlt", "")
        
        if "@lid" in remote_jid and remote_jid_alt:
            sender_phone = remote_jid_alt.split("@")[0]
        elif remote_jid:
            sender_phone = remote_jid.split("@")[0]

        # استخراج النص
        msg_content = msg_obj.get("message", {})
        if "conversation" in msg_content:
            message_text = msg_content["conversation"]
        elif "extendedTextMessage" in msg_content and "text" in msg_content["extendedTextMessage"]:
            message_text = msg_content["extendedTextMessage"]["text"]
        elif "imageMessage" in msg_content and "caption" in msg_content["imageMessage"]:
            message_text = msg_content["imageMessage"]["caption"]
        elif "conversation" in msg_obj:
            message_text = msg_obj["conversation"]

        if not message_text or not sender_phone:
            return

        # 🛠️ إرسال إشعار للتليجرام (الرادار) لكي تعرف من يراسل النظام
        try:
            asyncio.create_task(smart_notify_web(ADMIN_ID, f"✅ **تم استلام رسالة واتساب:**\nالرقم: `{sender_phone}`\nالنص: {message_text}"))
        except: pass

        from unified_main import send_whatsapp_message

        # =================================================================
        # 1. سيناريو التفعيل لأول مرة + استعادة كلمة المرور (مدمج وذكي)
        # =================================================================
        if ("تفعيل" in message_text or "كلمة المرور" in message_text) and "رقم" in message_text:
            match = re.search(r"رقم.*?(\d+)", message_text)
            if match:
                user_id = int(match.group(1))
                if database.pool:
                    async with database.pool.acquire() as conn:
                        user = await conn.fetchrow("SELECT name, phone FROM users WHERE user_id = $1 AND role = 'client'", user_id)
                        if user:
                            import random
                            new_pass = str(random.randint(100000, 999999))
                            
                            # 🟢 الحالة الأولى: عميل جديد
                            if not user['phone']:
                                await conn.execute("UPDATE users SET phone = $1, wa_status = 'on', web_password = $2, device_id = NULL WHERE user_id = $3", sender_phone, new_pass, user_id)
                                reply_msg = f"✅ أهلاً بك يا ({user['name']}) 🌹\nتم تفعيل حسابك وربط رقمك بنجاح!\n\n🔐 رمز الدخول الخاص بك هو: *{new_pass}*\n\n_(ملاحظة: هذا الرمز يستخدم لمرة واحدة فقط، وسيتم ربط حسابك بهاتفك الحالي لحمايته)_"
                                await send_whatsapp_message(sender_phone, reply_msg, bot=bot_instance)
                            
                            # 🟡 الحالة الثانية: عميل قديم يطلب استعادة
                            else:
                                if user['phone'] == sender_phone:
                                    await conn.execute("UPDATE users SET web_password = $1, device_id = NULL WHERE user_id = $2", new_pass, user_id)
                                    reply_msg = f"🔐 *رمز دخول جديد (OTP)*\nمرحباً {user['name']}،\nرمز الدخول الجديد لتطبيق الويب هو: *{new_pass}*\n\n_(ملاحظة: هذا الرمز يستخدم لمرة واحدة فقط، وسيتم ربط حسابك بهاتفك الحالي لحمايته)_"
                                    await send_whatsapp_message(sender_phone, reply_msg, bot=bot_instance)
                                else:
                                    await send_whatsapp_message(sender_phone, "⛔ عذراً، هذا الحساب مربوط برقم واتساب آخر. لحماية الحساب تم رفض الطلب. إذا كنت صاحب الحساب وتغير رقمك، تواصل مع الوكيل.", bot=bot_instance)
                        else:
                            await send_whatsapp_message(sender_phone, "❌ عذراً، رقم الحساب غير مسجل لدينا.", bot=bot_instance)

        # =================================================================
        # 2. سيناريو طلب تفعيل خدمة (كروت إلكترونية / تسديدات)
        # =================================================================
        elif "تفعيل خدمة" in message_text and "رقم:" in message_text:
            alert_msg = f"🔔 **طلب تفعيل خدمة من عميل (عبر الواتساب):**\n\n{message_text}\n\n*(يرجى الدخول لتطبيق الويب وتفعيل الخدمة له من ملف العميل)*"
            asyncio.create_task(smart_notify_web(ADMIN_ID, alert_msg))
            await send_whatsapp_message(sender_phone, "✅ تم استلام طلبك وإرساله للإدارة. سيتم تفعيل الخدمة لك في أقرب وقت ممكن.", bot=bot_instance)

        # =================================================================
        # 3. سيناريو إيقاف الإشعارات
        # =================================================================
        elif message_text.strip() == "إيقاف":
            if database.pool:
                async with database.pool.acquire() as conn:
                    user = await conn.fetchrow("SELECT user_id FROM users WHERE phone = $1", sender_phone)
                    if user:
                        await conn.execute("UPDATE users SET wa_status = 'off' WHERE user_id = $1", user["user_id"])
                        await send_whatsapp_message(sender_phone, "🔕 تم إيقاف إرسال الفواتير والإشعارات لهذا الرقم.\nلإعادة التفعيل، قم بطلب ذلك من التطبيق.", bot=bot_instance)

    except Exception as e:
        error_details = traceback.format_exc()
        print(f"Webhook Background Error: {error_details}")
        try:
            await smart_notify_web(ADMIN_ID, f"💬 **خطأ حرج في معالجة الواتساب!**\n⚠️ **السبب:** {str(e)}\n🔍 **التفاصيل:**\n`{error_details[-600:]}`")
        except: pass

async def whatsapp_webhook(request: web.Request):
    """بوابة الاستقبال السريعة (ترد بـ 200 OK فوراً لمنع التكرار)"""
    import asyncio
    import json
    import hmac # 🌟 استدعاء مكتبة التشفير
    
    # 1. التحقق من التوكن السري (بحماية من هجوم التوقيت)
    token = request.query.get('token', '')
    if not hmac.compare_digest(str(token).strip(), str(API_SECRET_KEY).strip()):
        return web.Response(text="Unauthorized", status=401)

    try:
        raw_data = await request.text()
        if not raw_data or raw_data.strip() == "":
            return web.Response(text="OK", status=200)
            
        data = json.loads(raw_data)
        
        # 🌟 التعديل الأسطوري: إطلاق المعالجة في الخلفية فوراً
        asyncio.create_task(process_whatsapp_background(data))
        
        # ?? الرد الفوري على سيرفر الواتساب في نفس الجزء من الثانية
        return web.Response(text="OK", status=200)
        
    except Exception as e:
        print(f"Webhook Parse Error: {e}")
        return web.Response(text="Error", status=500)

# =====================================================================
# 8. دوال الكروت الإلكترونية (الخزنة الذكية) - النسخة المحسنة أمنياً
# =====================================================================
async def api_parse_ecards(request: web.Request):
    """الفرّازة الآلية: تستقبل الفئة من الوكيل، وتستخرج الأرقام من النص"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    raw_text = data.get('raw_text', '')
    card_type = data.get('card_type', '') 
    
    try:
        import re
        arabic_to_english = str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789')
        raw_text = raw_text.translate(arabic_to_english)

        # البحث عن أي رقم يتكون من 9 خانات إلى 16 خانة (لدعم جميع الشركات)
        matches = re.findall(r'\b\d{9,16}\b', raw_text)
        
        if not matches:
            return fast_json_response({"status": "error", "message": "لم يتم العثور على أي أرقام صالحة في النص."})

        unique_pins = list(set(matches))
        duplicates_in_text = len(matches) - len(unique_pins)

        async with database.pool.acquire() as conn:
            # 🌟 الإصلاح الأمني للأداء: جلب كروت هذه الفئة فقط وليس كل قاعدة البيانات
            existing = await conn.fetch("SELECT card_number FROM electronic_cards WHERE card_type = $1", card_type)
            
        existing_plain_pins = set()
        for r in existing:
            try:
                existing_plain_pins.add(decrypt_pin(r['card_number']))
            except:
                existing_plain_pins.add(r['card_number']) 
                
        final_valid_pins = [p for p in unique_pins if p not in existing_plain_pins]
        duplicates_in_db = len(unique_pins) - len(final_valid_pins)

        parsed_cards = {card_type: final_valid_pins} if final_valid_pins else {}

        return fast_json_response({
            "status": "success",
            "parsed": parsed_cards,
            "invalid_count": 0,
            "duplicates_count": duplicates_in_text + duplicates_in_db
        })
    except Exception as e:
        print(f"Parse E-Cards Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ داخلي أثناء فرز الكروت."})

async def api_save_ecards(request: web.Request):
    """حفظ الكروت في الخزنة بعد تأكيد الوكيل ومطابقتها مع الإدارة"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    shipment_id = int(data.get('shipment_id', 0))
    parsed_data_str = data.get('parsed_data', '{}')
    parsed_data = json.loads(parsed_data_str)
    
    try:
        async with database.pool.acquire() as conn:
            # 🌟 التعديل الأمني: فتح المعاملة من البداية وقفل الإرسالية لمنع الحفظ المزدوج
            async with conn.transaction():
                shipment = await conn.fetchrow("SELECT manager_items FROM pending_shipments WHERE id = $1 AND status = 'pending' FOR UPDATE", shipment_id)
                if not shipment:
                    return fast_json_response({"status": "error", "message": "الإرسالية غير موجودة أو تمت معالجتها مسبقاً."})
                    
                manager_items = json.loads(shipment['manager_items'])
                
                agent_items = {}
                total_price = Decimal('0.0')
                cards_to_insert = []
                
                for real_ctype, pins in parsed_data.items():
                    cost_price = await conn.fetchval("SELECT cost_price FROM inventory WHERE card_type = $1", real_ctype)
                    if cost_price is None:
                        return fast_json_response({"status": "error", "message": f"لم يتم العثور على فئة ({real_ctype}) في المخزون."})
                        
                    qty = len(pins)
                    agent_items[real_ctype] = qty
                    total_price += Decimal(str(cost_price)) * qty
                    
                    for pin in pins:
                        cards_to_insert.append((pin, real_ctype))
                        
                # الجدار الأمني: المطابقة مع إرسالية المدير
                if agent_items != manager_items:
                    escalate = data.get('escalate', 'false')
                    if escalate != 'true':
                        # 🌟 إرجاع تحذير للوكيل أولاً ليعيد اللصق دون إزعاج المدير
                        return fast_json_response({
                            "status": "agent_warning", 
                            "manager_items": manager_items, 
                            "agent_items": agent_items
                        })
                    else:
                        # 🌟 الوكيل أصر على الرفع للمدير
                        # نحفظ الأرقام السرية (parsed_data) في agent_items لكي لا تضيع إذا وافق المدير
                        await conn.execute("UPDATE pending_shipments SET agent_items = $1 WHERE id = $2", json.dumps(parsed_data), shipment_id)
                        
                        try:
                            mismatch_details = "?? **تنبيه: اختلاف في إرسالية كروت إلكترونية!**\nالوكيل قام بلصق الكروت ووجد اختلافاً، ويقول أن الخطأ من طرفك:\n\n"
                            mismatch_details += "📦 **ما أرسلته أنت:**\n"
                            for k, v in manager_items.items(): mismatch_details += f"▪️ {k}: {v} كرت\n"
                            mismatch_details += "\n👀 **ما لصقه الوكيل:**\n"
                            for k, v in agent_items.items(): mismatch_details += f"▪️ {k}: {v} كرت\n"
                            mismatch_details += "\nيرجى اتخاذ القرار:"
                            
                            import asyncio
                            from unified_main import bot_instance
                            from config import NETWORK_OWNER_ID
                            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                            
                            mgr_kb = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="✅ اعتماد العدد الفعلي للوكيل", callback_data=f"resolve_shipment_{shipment_id}_accept")],
                                [InlineKeyboardButton(text="❌ إلغاء الإرسالية بالكامل", callback_data=f"resolve_shipment_{shipment_id}_cancel")]
                            ])
                            asyncio.create_task(smart_notify_web(NETWORK_OWNER_ID, mismatch_details, reply_markup=mgr_kb))
                        except Exception as e:
                            pass
                            
                        return fast_json_response({"status": "success", "message": "تم رفع البلاغ للمدير بنجاح. بانتظار قراره."})
                    
                # الحفظ النهائي (في حالة التطابق 100%)
                records_to_insert = []
                for pin, ctype in cards_to_insert:
                    try: encrypted_pin = encrypt_pin(pin)
                    except NameError: encrypted_pin = pin 
                    records_to_insert.append((encrypted_pin, ctype))
                    
                # 🌟 الإصلاح الأمني: إدخال جماعي (Bulk Insert) صاروخي بدلاً من الإدخال الفردي لمنع تجميد السيرفر
                if records_to_insert:
                    await conn.executemany("INSERT INTO electronic_cards (card_number, card_type) VALUES ($1, $2)", records_to_insert)
                        
                structured_items = []
                for ctype, qty in agent_items.items():
                    await conn.execute("UPDATE inventory SET quantity = quantity + $1 WHERE card_type = $2", qty, ctype)
                    # 🌟 تجهيز البيانات المهيكلة لكي تظهر في تقرير الإكسل الخاص بالمدير
                    cost_p = await conn.fetchval("SELECT cost_price FROM inventory WHERE card_type = $1", ctype)
                    structured_items.append({"card_type": ctype, "quantity": qty, "cost_price": str(cost_p or 0), "sell_price": "0.0"})
                        
                # 🌟 الإصلاح المحاسبي: إرسال structured_details لكي لا تختفي الكروت من التقرير الشهري
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, structured_details) VALUES (0, 'استلام_من_الشبكة', $1, $2, $3)", total_price, "استلام كروت إلكترونية (مطابقة 100%)", json.dumps(structured_items))
                await conn.execute("UPDATE pending_shipments SET status = 'matched', agent_items = $1 WHERE id = $2", json.dumps(agent_items), shipment_id)
                
        return fast_json_response({"status": "success", "message": "تم حفظ الكروت الإلكترونية في الخزنة ورفع المخزون بنجاح!"})
    except Exception as e:
        print(f"Save ECards Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء الحفظ."})

async def api_my_ecards(request: web.Request):
    """جلب آخر الكروت الإلكترونية التي سحبها العميل"""
    requester_id = check_jwt(request) # ✅ السماح للبقالة برؤية كروتها
    client_id = int(request.match_info.get('id', 0))
    
    # 🚨 حماية IDOR: منع سرقة أو رؤية كروت البقالات الأخرى
    if requester_id not in [ADMIN_ID, NETWORK_OWNER_ID] and requester_id != client_id:
        return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك بالوصول لبيانات هذا الحساب!"})
        
    try:
        async with database.pool.acquire() as conn:
            cards = await conn.fetch("""
                SELECT card_type, card_number, sold_at 
                FROM electronic_cards 
                WHERE sold_to_client = $1 
                ORDER BY sold_at DESC LIMIT 10
            """, client_id)
            
            # 🌟 التعديل: إضافة 3 ساعات ليتطابق مع توقيت اليمن/السعودية بدلاً من توقيت سيرفر Render
            from datetime import timedelta
            cards_list = [{
                "type": c['card_type'], 
                "pin": decrypt_pin(c['card_number']), # فك التشفير
                "date": (c['sold_at'] + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M') if c['sold_at'] else "غير محدد"
            } for c in cards]
            
        return fast_json_response({"status": "success", "cards": cards_list})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "خطأ في جلب الكروت"})

async def api_agent_sell_ecards(request: web.Request):
    """الوكيل يبيع كروت إلكترونية مباشرة (كاش) للطياري"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    
    try:
        quantity = int(data.get('quantity', 0))
        collected_cash = Decimal(data.get('collected_cash', 0))
    except (ValueError, InvalidOperation):
        return fast_json_response({"status": "error", "message": "البيانات المدخلة غير صالحة، تأكد من إدخال أرقام صحيحة."})
        
    card_type = data.get('card_type')
    sale_mode = data.get('sale_mode')
    override_secret = data.get('override_secret', '').strip()
    personal_credit_name = data.get('personal_credit_name', '').strip() # 🌟 استقبال اسم الزبون الآجل

    if quantity <= 0:
        return fast_json_response({"status": "error", "message": "الكمية يجب أن تكون أكبر من صفر!"})

    try:
        async with database.pool.acquire() as conn:
            # 1. فحص توفر الكروت في الخزنة
            available_count = await conn.fetchval("SELECT COUNT(*) FROM electronic_cards WHERE card_type = $1 AND status = 'available'", card_type)
            if available_count < quantity:
                return fast_json_response({"status": "error", "message": f"عذراً، الخزنة لا تحتوي سوى على ({available_count}) كرت من هذه الفئة!"})

                       # الكود الجديد (حماية ضد القيم الفارغة None)
            # 2. جلب الأسعار
            prices = await conn.fetchrow("SELECT cost_price, price, retail_price FROM inventory WHERE card_type = $1", card_type)
            if not prices:
                return fast_json_response({"status": "error", "message": "الفئة غير موجودة في المخزون."})

            cost_price = to_decimal(prices['cost_price'])
            wholesale_price = to_decimal(prices['price'])
            retail_price = to_decimal(prices['retail_price'])

            if sale_mode == "retail":
                total_price = retail_price * quantity
                agent_profit = (retail_price - cost_price) * quantity
            else:
                total_price = wholesale_price * quantity
                agent_profit = (wholesale_price - cost_price) * quantity

            # 🌟 إذا كان بيع آجل شخصي، الوكيل يتحمل الكاش أمام الإدارة
            if personal_credit_name:
                collected_cash = total_price

            # 3. فحص المبلغ المستلم
            discount = Decimal('0.0')
            
            # 🌟 التعديل الأمني: منع الأخطاء المطبعية التي تدمر ميزانية الوكيل
            if collected_cash > total_price:
                return fast_json_response({"status": "error", "message": f"المبلغ المستلم ({collected_cash}) أكبر من السعر المطلوب ({total_price})! يرجى التأكد من الرقم."})
                
            if collected_cash < total_price:
                if override_secret == '#':
                    discount = total_price - collected_cash
                else:
                    price_name = "السعر الرسمي" if sale_mode == "retail" else "سعر الجملة"
                    return fast_json_response({
                        "status": "error",
                        "requires_override": True,
                        "message": f"المبلغ المستلم ({collected_cash}) أقل من {price_name} ({total_price})."
                    })

            agent_profit = agent_profit - discount
            network_amount = collected_cash - agent_profit
            details = f"بيع إلكتروني مباشر: {quantity} كرت {card_type} كاش" if sale_mode == "retail" else f"بيع إلكتروني جملة كاش: {quantity} كرت {card_type}"
            if discount > 0: details += f" (بخصم {discount} ريال)"

            # 4. السحب والتحديث (Transaction)
            extracted_pins = []
            async with conn.transaction():
                # سحب الكروت
                cards = await conn.fetch("""
                    SELECT id, card_number FROM electronic_cards
                    WHERE card_type = $1 AND status = 'available'
                    FOR UPDATE SKIP LOCKED
                    LIMIT $2
                """, card_type, quantity)

                if len(cards) < quantity:
                    return fast_json_response({"status": "error", "message": "حدث تعارض، الكروت المتاحة أقل من المطلوب. حاول مجدداً."})

                card_ids = [c['id'] for c in cards]
                for c in cards:
                    extracted_pins.append(decrypt_pin(c['card_number'])) # فك التشفير

                # تحديث حالة الكروت
                await conn.execute("""
                    UPDATE electronic_cards
                    SET status = 'sold', sold_to_client = 0, sold_at = CURRENT_TIMESTAMP
                    WHERE id = ANY($1)
                """, card_ids)

                # تحديث المخزون العام
                await conn.execute('UPDATE inventory SET quantity = quantity - $1 WHERE card_type = $2', quantity, card_type)

                # تسجيل العملية
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details) VALUES (0, 'بيع_مباشر', $1, $2)", collected_cash, details)

                if agent_profit != 0:
                    profit_details = f"ربح بيع إلكتروني مباشر: {quantity} كرت {card_type}" if agent_profit > 0 else f"خصم بيع إلكتروني: {quantity} كرت {card_type}"
                    await conn.execute("INSERT INTO agent_profits (amount, details) VALUES ($1, $2)", agent_profit, profit_details)

                # 🌟 تسجيل الدين في دفتر الوكيل الشخصي
                if personal_credit_name:
                    cust_id = await conn.fetchval("""
                        INSERT INTO agent_personal_customers (name, debt) VALUES ($1, $2)
                        ON CONFLICT (name) DO UPDATE SET debt = agent_personal_customers.debt + $2
                        RETURNING id
                    """, personal_credit_name, total_price)
                    if not cust_id:
                        cust_id = await conn.fetchval("SELECT id FROM agent_personal_customers WHERE name = $1", personal_credit_name)
                    await conn.execute("INSERT INTO agent_personal_ledger (customer_id, type, amount, details) VALUES ($1, 'دين', $2, $3)", cust_id, total_price, f"شراء {quantity} كرت إلكتروني {card_type}")

        return fast_json_response({
            "status": "success",
            "pins": extracted_pins,
            "network_amount": float(network_amount),
            "agent_profit": float(agent_profit),
            "message": "تم البيع واستخراج الكروت بنجاح!"
        })

    except Exception as e:
        print(f"Agent Sell ECards Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء استخراج الكروت."})

async def api_extract_ecard(request: web.Request):
    requester_id = await enforce_cashier_perms(request, 'ecard')
    data = await request.post()
    
    # 🌟 الإصلاح الأمني: حماية النقر المزدوج (Idempotency) لمنع سحب كروت متعددة بالخطأ
    txn_uuid = data.get('txn_uuid', '')
    if txn_uuid:
        if txn_uuid in active_pos_requests:
            return fast_json_response({"status": "error", "message": "جاري سحب الكرت بالفعل، يرجى الانتظار..."})
        active_pos_requests.add(txn_uuid)
        
    try:
        client_id = int(data.get('client_id', 0))
        card_type = data.get('card_type', '') 
        sale_type = data.get('sale_type', 'cash') 
        customer_name = data.get('customer_name', '').strip()

        if requester_id not in [ADMIN_ID, NETWORK_OWNER_ID] and requester_id != client_id:
            await send_security_alert(request, requester_id, "سرقة كروت إلكترونية (IDOR)", f"حاول سحب كرت إلكتروني وتسجيل الدين على بقالة أخرى (ID: {client_id})!")
            return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك بالسحب من هذا الحساب!"})

        if sale_type == 'credit' and not customer_name:
            return fast_json_response({"status": "error", "message": "اسم الزبون مطلوب للبيع الآجل!"})

        # 🌟 إصلاح حساب التجربة (Demo)
        if requester_id == 999999:
            import random
            import asyncio
            await asyncio.sleep(1)
            return fast_json_response({"status": "success", "pin": str(random.randint(100000000, 999999999)), "message": "تم سحب الكرت بنجاح (وضع التجربة)!"})

        from core_accounting import core_extract_ecard
        result = await core_extract_ecard(client_id, card_type, sale_type, customer_name)
        
        if result["status"] == "success":
            try: decrypted_pin = decrypt_pin(result['card_number'])
            except Exception: decrypted_pin = result.get('card_number', 'غير معروف')
            
            import asyncio
            
            client_name = result.get('client_name_db', result.get('client_name', 'عميل'))
            price = result.get('wholesale_price', result.get('price', 0))
            new_debt = result.get('new_total_debt', result.get('debt', 0))
            
                        # 🌟 تطبيق القاعدة الذهبية: إرسال إشعار للتطبيق بدلاً من الواتساب النصي
            try:
                from web_api import send_web_push
                await send_web_push(client_id, "🧾 سحب كرت إلكتروني", f"تم سحب كرت {card_type} بقيمة {price} ريال. إجمالي مديونيتك الآن: {new_debt} ريال.")
            except Exception as e: logging.error(f"Error: {e}")

            alert_msg = f"📱 **سحب إلكتروني جديد:**\n🏪 البقالة: {client_name}\n💳 الفئة: {card_type}\n💰 القيمة: {price} ريال\n🔴 إجمالي دينه الآن: **{new_debt} ريال**"
            try: asyncio.create_task(smart_notify_web(ADMIN_ID, alert_msg))
            except: pass

            await notify_clients(client_id)
            await notify_clients(ADMIN_ID)

            return fast_json_response({"status": "success", "pin": decrypted_pin, "message": "تم سحب الكرت وتسجيل الأرباح بنجاح!"})

        else:
            return fast_json_response(result)
            
    except Exception as e:
        print(f"Extract E-Card Error: {e}")
        return fast_json_response({"status": "error", "message": f"خطأ داخلي: {str(e)}"})
    finally:
        # 🧹 تنظيف الرقم المرجعي لفك القفل بعد انتهاء العملية
        if txn_uuid in active_pos_requests:
            active_pos_requests.remove(txn_uuid)

async def api_toggle_client_ecard(request: web.Request):
    """تشغيل أو إيقاف الكروت الإلكترونية لبقالة معينة من الويب"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    client_id = int(data.get('client_id', 0))
    
    try:
        async with database.pool.acquire() as conn:
            current_status = await conn.fetchval("SELECT ecard_status FROM users WHERE user_id = $1", client_id)
            new_status = 'off' if current_status == 'on' else 'on'
            await conn.execute("UPDATE users SET ecard_status = $1 WHERE user_id = $2", new_status, client_id)
            
        await notify_clients(client_id, "update_needed", "") # 🌟 السطر السحري لتحديث شاشة العميل فوراً
        msg = "✅ تم تفعيل الكروت الإلكترونية للعميل." if new_status == 'on' else "⛔ تم إيقاف الكروت الإلكترونية عن العميل."
        return fast_json_response({"status": "success", "message": msg, "new_status": new_status})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء التعديل."})
        
# 👇 الدالة الجديدة لتفعيل/إيقاف خدمة التسديدات 👇
async def api_toggle_telecom_service(request: web.Request):
    """تشغيل أو إيقاف خدمة التسديدات لبقالة معينة من الويب"""
    user_id = require_agent_only(request)
    data = await request.post()
    client_id = int(data.get('client_id', 0))
    
    try:
        async with database.pool.acquire() as conn:
            current_status = await conn.fetchval("SELECT telecom_status FROM users WHERE user_id = $1", client_id)
            new_status = 'on' if current_status == 'off' else 'off'
            await conn.execute("UPDATE users SET telecom_status = $1 WHERE user_id = $2", new_status, client_id)
            
        await notify_clients(client_id, "update_needed", "") # 🌟 السطر السحري لتحديث شاشة العميل فوراً
        msg = "✅ تم تفعيل خدمة التسديدات للعميل." if new_status == 'on' else "⛔ تم إيقاف خدمة التسديدات عن العميل."
        return fast_json_response({"status": "success", "message": msg, "new_status": new_status})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء التعديل."})
# 👆 نهاية الدالة الجديدة 👆

async def api_toggle_physical_service(request: web.Request):
    """تشغيل أو إيقاف الكروت الورقية لبقالة معينة من الويب"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    client_id = int(data.get('client_id', 0))
    
    try:
        async with database.pool.acquire() as conn:
            current_status = await conn.fetchval("SELECT physical_status FROM users WHERE user_id = $1", client_id)
            new_status = 'off' if current_status == 'on' else 'on'
            await conn.execute("UPDATE users SET physical_status = $1 WHERE user_id = $2", new_status, client_id)
            
        await notify_clients(client_id, "update_needed", "") # 🌟 السطر السحري لتحديث شاشة العميل فوراً
        msg = "✅ تم تفعيل الكروت الورقية للعميل." if new_status == 'on' else "⛔ تم إيقاف الكروت الورقية عن العميل."
        return fast_json_response({"status": "success", "message": msg, "new_status": new_status})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء التعديل."})

async def api_ecards_stats(request: web.Request):
    """جلب إحصائيات سحب الكروت الإلكترونية للبقالات (اليوم / الشهر)"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    try:
        async with database.pool.acquire() as conn:
            query = """
                SELECT 
                    u.name,
                    COUNT(CASE WHEN DATE(e.sold_at) = CURRENT_DATE THEN 1 END) as today_count,
                    COUNT(CASE WHEN DATE_TRUNC('month', e.sold_at) = DATE_TRUNC('month', CURRENT_DATE) THEN 1 END) as month_count
                FROM electronic_cards e
                JOIN users u ON e.sold_to_client = u.user_id
                WHERE e.status = 'sold'
                GROUP BY u.name
                ORDER BY month_count DESC
            """
            stats = await conn.fetch(query)
            stats_list = [{"name": r['name'], "today": r['today_count'], "month": r['month_count']} for r in stats]
            
        return fast_json_response({"status": "success", "stats": stats_list})
    except Exception as e:
        print(f"E-Cards Stats Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب الإحصائيات."})
        
        # ================= دوال الطلبات والتذاكر لتطبيق الويب =================

async def api_get_pending_orders(request: web.Request):
    """جلب الطلبات المعلقة لعرضها في تطبيق الويب للوكيل"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    try:
        async with database.pool.acquire() as conn:
            orders = await conn.fetch("SELECT id, user_id, client_name, new_order_text, created_at FROM pending_orders WHERE status = 'pending' ORDER BY created_at ASC")
            orders_list = [{"id": o['id'], "client_id": o['user_id'], "client_name": o['client_name'], "details": o['new_order_text'], "date": str(o['created_at'])[:16]} for o in orders]
        return fast_json_response({"status": "success", "orders": orders_list})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب الطلبات."})

async def api_reject_order(request: web.Request):
    """رفض طلب معلق من تطبيق الويب"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    data = await request.post()
    order_id = int(data.get('order_id', 0))
    try:
        async with database.pool.acquire() as conn:
            order = await conn.fetchrow("SELECT user_id FROM pending_orders WHERE id = $1 AND status = 'pending'", order_id)
            if order:
                await conn.execute("UPDATE pending_orders SET status = 'rejected' WHERE id = $1", order_id)
                try: await smart_notify_web(order['user_id'], "عذراً، تم رفض طلبك الأخير من قبل الإدارة. يرجى التواصل مع الوكيل للتفاصيل.")
                except: pass
                return fast_json_response({"status": "success"})
        return fast_json_response({"status": "error", "message": "الطلب غير موجود أو تمت معالجته."})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء الرفض."})

async def api_get_tickets(request: web.Request):
    """جلب تذاكر الكروت التالفة لعرضها في تطبيق الويب"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    try:
        async with database.pool.acquire() as conn:
            tickets = await conn.fetch("SELECT c.id, u.name, c.message_text, c.created_at FROM chat_messages c JOIN users u ON c.client_id = u.user_id WHERE c.message_text LIKE '%بلاغ كرت تالف%' ORDER BY c.created_at DESC LIMIT 20")
            t_list = [{"id": t['id'], "client_name": t['name'], "details": t['message_text'], "date": str(t['created_at'])[:16]} for t in tickets]
        return fast_json_response({"status": "success", "tickets": t_list})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب التذاكر."})

# 🌟 استبدل دالة api_report_damaged_image القديمة بهذه 🌟
async def api_report_damaged_image(request: web.Request):
    """استقبال بلاغ كرت تالف وتوجيهه للمدير والوكيل معاً"""
    try:
        user_id = check_jwt(request) # ✅ السماح للبقالة بالإبلاغ
        data = await request.post()
        client_id = int(data.get('client_id', 0))
        
        # 🚨 حماية IDOR
        if user_id not in [ADMIN_ID, NETWORK_OWNER_ID] and user_id != client_id:
            return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك!"})
            
        details = data.get('details', '')
        image = data.get('image') 
        
        if image:
            import asyncio
            import os
            
            # 🌟 الإصلاح الأمني: قراءة الصورة على دفعات (Chunks) لحماية الذاكرة (RAM)
            file_path = f"static/damaged_{client_id}_{int(time.time())}.jpg"
            
            def save_image_in_chunks():
                if hasattr(image, 'file'):
                    with open(file_path, "wb") as f:
                        while True:
                            chunk = image.file.read(8192)
                            if not chunk: break
                            f.write(chunk)
                else:
                    with open(file_path, "wb") as f:
                        f.write(image if isinstance(image, bytes) else image.encode('utf-8'))
                        
            await asyncio.to_thread(save_image_in_chunks)
                
            client_name = "عميل"
            async with database.pool.acquire() as conn:
                name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
                if name: client_name = name
                
                # 🌟 التعديل الذكي: حفظ البلاغ مع حماية من نقص الأعمدة في قاعدة البيانات
                ticket_text = f"🚨 بلاغ كرت تالف بانتظار قرار الإدارة:\n{details}"
                try:
                    ticket_id = await conn.fetchval("INSERT INTO chat_messages (client_id, sender_type, message_type, message_text) VALUES ($1, 'client', 'complaint', $2) RETURNING id", client_id, ticket_text)
                except Exception as db_err:
                    if 'message_type' in str(db_err):
                        await conn.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS message_type VARCHAR(50) DEFAULT 'support'")
                        ticket_id = await conn.fetchval("INSERT INTO chat_messages (client_id, sender_type, message_type, message_text) VALUES ($1, 'client', 'complaint', $2) RETURNING id", client_id, ticket_text)
                    else:
                        await conn.execute("INSERT INTO chat_messages (client_id, sender_type, message_text) VALUES ($1, 'client', $2)", client_id, ticket_text)
                        ticket_id = random.randint(1000, 9999)
            
            try:
                from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
                
                # 🌟 الإصلاح الجذري: قراءة الصورة من الملف الذي تم حفظه للتو
                with open(file_path, "rb") as f:
                    image_bytes = f.read()
                
                # 1. إرسال الصورة للوكيل (للعلم فقط)
                photo_agent = BufferedInputFile(image_bytes, filename="damaged_card.jpg")
                agent_caption = f"⚠️💳 **بلاغ كرت تالف من الويب!**\n👤 البقالة: {client_name}\n💬 التفاصيل: {details}\n\n*(البلاغ الآن عند المدير العام لاتخاذ القرار)*"
                await smart_notify_web(ADMIN_ID, text=agent_caption, photo=photo_agent)

                # 2. إرسال الصورة للمدير (مع أزرار القرار)
                photo_mgr = BufferedInputFile(image_bytes, filename="damaged_card2.jpg")
                mgr_caption = f"⚠️💳 **بلاغ كرت تالف من الويب!**\n👤 البقالة: {client_name}\n💬 التفاصيل: {details}\n\nيرجى فحص الكرت واتخاذ القرار:"
                
                mgr_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ تعويض مباشر (إرسال رقم كرت)", callback_data=f"mgr_dmg_direct_{client_id}_{ticket_id}")],
                    [InlineKeyboardButton(text="👨‍💻 تحويل للوكيل ليعوضه", callback_data=f"mgr_dmg_agent_{client_id}_{ticket_id}")],
                    [InlineKeyboardButton(text="❌ رفض التعويض (مع ذكر السبب)", callback_data=f"mgr_dmg_reject_{client_id}_{ticket_id}")]
                ])
                await smart_notify_web(NETWORK_OWNER_ID, text=mgr_caption, photo=photo_mgr, reply_markup=mgr_kb)

                try: await send_web_push(NETWORK_OWNER_ID, "⚠️ بلاغ كرت تالف", f"بقالة {client_name} أبلغت عن كرت تالف، يرجى الدخول لاتخاذ قرار.")
                except: pass
                
            except Exception as bot_err:
                print(f"Bot send photo error: {bot_err}")
                asyncio.create_task(smart_notify_web(ADMIN_ID, f"🔴 خطأ في إرسال صورة الكرت التالف للتليجرام:\n`{bot_err}`"))
            
            return fast_json_response({"status": "success", "message": "تم إرسال البلاغ للإدارة بنجاح. سيتم فحص الكرت وتعويضك قريباً."})
            
        return fast_json_response({"status": "error", "message": "لم يتم استلام الصورة."})
    except Exception as e:
        error_details = traceback.format_exc()
        print(f"Damaged Card Error: {error_details}")
        asyncio.create_task(smart_notify_web(ADMIN_ID, f"🔴 **خطأ في معالجة الكرت التالف:**\n`{str(e)}`\n\n`{error_details[-400:]}`"))
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء معالجة الصورة في السيرفر."})

async def api_report_damaged_ecard(request: web.Request):
    """استقبال بلاغ كرت إلكتروني تالف من الويب (النسخة المدرعة ضد انهيار الذاكرة)"""
    try:
        user_id = check_jwt(request) 
        data = await request.post()
        client_id = int(data.get('client_id', 0))
        
        if user_id not in [ADMIN_ID, NETWORK_OWNER_ID] and user_id != client_id:
            return fast_json_response({"status": "error", "message": "⛔ غير مصرح لك!"})
            
        raw_pin = data.get('pin', '').strip()
        raw_details = data.get('details', 'بلاغ كرت إلكتروني تالف')
        
        safe_pin = raw_pin.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')
        safe_details = raw_details.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')
        
        if not safe_pin:
            return fast_json_response({"status": "error", "message": "رقم الكرت مطلوب."})
            
        client_name = "عميل"
        found_card = None
        actual_buyer_name = "غير معروف"
        
        async with database.pool.acquire() as conn:
            name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
            if name: 
                client_name = name.replace('*', '').replace('_', '').replace('`', '')
            
            recent_cards = await conn.fetch("""
                SELECT * FROM electronic_cards 
                WHERE status = 'available' 
                OR (status = 'sold' AND sold_at >= CURRENT_DATE - INTERVAL '60 days')
            """)
            
            for card in recent_cards:
                try:
                    plain_pin = decrypt_pin(card['card_number'])
                    if plain_pin == safe_pin:                      
                        found_card = card
                        if card['sold_to_client']:
                            buyer = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", card['sold_to_client'])
                            if buyer: actual_buyer_name = buyer.replace('*', '').replace('_', '')
                        break
                except: pass
                
            ticket_text = f"بلاغ كرت إلكتروني تالف:\nالرقم: {safe_pin}\nالتفاصيل: {safe_details}"
            try:
                ticket_id = await conn.fetchval("INSERT INTO chat_messages (client_id, sender_type, message_type, message_text) VALUES ($1, 'client', 'complaint', $2) RETURNING id", client_id, ticket_text)
            except:
                await conn.execute("INSERT INTO chat_messages (client_id, sender_type, message_text) VALUES ($1, 'client', $2)", client_id, ticket_text)
                import random
                ticket_id = random.randint(1000, 9999)

        if not found_card:
            mufattish_report = "❌ تحذير: هذا الكرت غير مسجل في نظامنا (أو تم بيعه منذ أكثر من شهرين)!"
        else:
            is_owner = (found_card['sold_to_client'] == client_id)
            match_icon = "✅" if is_owner else "❌"
            buyer_text = "نعم" if is_owner else f"لا (سحبه: {actual_buyer_name})"
            date_text = str(found_card['sold_at'])[:16] if found_card['sold_at'] else 'غير محدد'
            
            mufattish_report = (
                f"🤖 تقرير المفتش الآلي:\n"
                f"📦 الفئة: {found_card['card_type']}\n"
                f"{match_icon} هل سحبه هذا العميل؟ {buyer_text}\n"
                f"📅 تاريخ السحب: {date_text}"
            )

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        import asyncio
        
        msg_text = f"⚠️📱 بلاغ كرت إلكتروني تالف!\n👤 البقالة: {client_name}\n💳 الرقم: {safe_pin}\n💬 التفاصيل: {safe_details}\n\n{mufattish_report}"
        
        mgr_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ تعويض مباشر (إرسال رقم كرت)", callback_data=f"mgr_dmg_direct_{client_id}_{ticket_id}")],
            [InlineKeyboardButton(text="👨‍💻 تحويل للوكيل ليعوضه", callback_data=f"mgr_dmg_agent_{client_id}_{ticket_id}")],
            [InlineKeyboardButton(text="❌ رفض التعويض", callback_data=f"mgr_dmg_reject_{client_id}_{ticket_id}")]
        ])
        
        async def send_telegram_alerts():
            try:
                # 🌟 الإصلاح: استخدام bot_instance مباشرة بدون استيراد دائري
                await smart_notify_web(ADMIN_ID, msg_text + "\n\n(البلاغ الآن عند المدير العام لاتخاذ القرار)")
                await smart_notify_web(NETWORK_OWNER_ID, msg_text + "\n\nيرجى اتخاذ القرار:", reply_markup=mgr_kb)
                try: await send_web_push(NETWORK_OWNER_ID, "⚠️ بلاغ كرت إلكتروني تالف", f"بقالة {client_name} أبلغت عن كرت تالف، يرجى الدخول لاتخاذ قرار.")
                except: pass
            except Exception as tg_err:
                print(f"Telegram Send Error: {tg_err}")
                
        asyncio.create_task(send_telegram_alerts())
        
        return fast_json_response({"status": "success", "message": "تم إرسال البلاغ للإدارة بنجاح. سيتم فحص الكرت وتعويضك قريباً."})
        
    except Exception as e:
        print(f"E-Card Report Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء إرسال البلاغ."})

# ================= دالة إرسال التعميم في الخلفية (إشعارات التطبيق فقط) =================
async def send_broadcast_background(message, clients):
    import asyncio

    for client in clients:
        # 1. إرسال للتليجرام (سريع ولا مشكلة فيه)
        try:
            await smart_notify_web(client["user_id"], f"📢 **تعميم من وكيل فرع بني علي:**\n\n{message}")
        except:
            pass
            
        # 2. إرسال إشعار للتطبيق (Firebase)
        try:
            await send_web_push(client["user_id"], "📢 تعميم إداري هام", message)
        except Exception as e:
            print(f"Push Error: {e}")
            
        await asyncio.sleep(0.1) # تأخير بسيط جداً لمنع الضغط على السيرفر

# ================= دالة استقبال التعميم من الويب =================
async def api_agent_broadcast(request: web.Request):
    """الوكيل يرسل تعميماً من تطبيق الويب لجميع العملاء"""
    user_id = require_admin(request) # 🚨 حماية الإدارة فقط
    # 🌟 حماية الصلاحيات: الوكيل فقط يرسل تعميمات للعملاء
    if user_id != ADMIN_ID:
        return fast_json_response({"status": "error", "message": "⛔ هذا القسم خاص بالوكيل فقط!"})
    data = await request.post()
    message = data.get('message', '')
    
    try:
        async with database.pool.acquire() as conn:
            # جلب جميع العملاء
            clients = await conn.fetch("SELECT user_id, phone, wa_status FROM users WHERE role = 'client'")
            
        # 🚀 تشغيل الإرسال في الخلفية لكي لا تتجمد شاشة الوكيل
        import asyncio
        asyncio.create_task(send_broadcast_background(message, clients))
        
        # تحديث شاشات جميع المستخدمين المتصلين (WebSockets)
        await notify_clients()
                
        return fast_json_response({"status": "success", "message": "✅ تم استلام التعميم! جاري إرساله للعملاء في الخلفية بأمان (لتجنب حظر الواتساب)."})
    except Exception as e:
        print(f"Agent Broadcast Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء إرسال التعميم."})
        
        # =====================================================================
# دالة جلب باقات الاتصالات للتطبيق
# =====================================================================
async def api_get_telecom_packages(request: web.Request):
    """جلب الباقات المتاحة لشبكة معينة لعرضها في التطبيق"""
    requester_id = check_jwt(request) 
    network = request.match_info.get('network', '')
    
    if not database.pool:
        return fast_json_response({"status": "error", "message": "قاعدة البيانات غير متصلة"})
        
    try:
        async with database.pool.acquire() as conn:
            packages = await conn.fetch('''
                SELECT id, package_name, cost_price, selling_price, credit_price, retail_price 
                FROM telecom_packages 
                WHERE network = $1 AND is_active = TRUE
                ORDER BY selling_price ASC
            ''', network)
            
            if not packages:
                return fast_json_response({"status": "error", "message": "لا توجد باقات متاحة لهذه الشبكة حالياً."})
                
            pkg_list = []
            for p in packages:
                pkg_list.append({
                    "id": p['id'],
                    "name": p['package_name'],
                    "cost_price": float(p['cost_price']),
                    "price": float(p['selling_price']),
                    "credit_price": float(p['credit_price']),
                    "retail_price": float(p['retail_price'] if p['retail_price'] else p['selling_price']) # 🌟 سعر الطياري
                })
                
            return fast_json_response({"status": "success", "packages": pkg_list})
            
    except Exception as e:
        print(f"Get Telecom Packages Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء جلب الباقات."})

# =====================================================================
# الروبوتات الخلفية المحصنة (Background Tasks)
# =====================================================================
async def automated_daily_telecom_report():
    """يعمل في الخلفية، ويرسل تقريراً الساعة 11:55 مساءً إذا كان هناك عمل"""
    while True:
        try:
            now = datetime.now()
            if now.hour == 23 and now.minute == 55:
                if database.pool:
                    async with database.pool.acquire() as conn:
                        # 🌟 إصلاح جديد: تجاهل التراجعات في المبيعات
                        query = """
                            SELECT u.name, SUM(t.amount) as total_amount
                            FROM transactions t
                            JOIN users u ON t.user_id = u.user_id
                            WHERE t.wallet_type = 'telecom' AND t.type IN ('تسديد_باقة', 'تسديد_باقة_وكيل') AND t.is_reverted = FALSE AND DATE(t.date) = CURRENT_DATE
                            GROUP BY u.name
                        """
                        records = await conn.fetch(query)
                        
                        if records:
                            total_sales = sum(Decimal(r['total_amount']) for r in records)
                            # 🌟 إصلاح جديد: حساب (الصافي) بجمع الموجب والسالب معاً لمعرفة الربح الحقيقي
                            profit = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM telecom_profits WHERE DATE(date) = CURRENT_DATE")
                            profit = Decimal(profit) if profit else Decimal('0.0')
                            
                            msg = "📊 *التقرير اليومي التلقائي (بوابة التسديدات)* 📱\n\n"
                            msg += "🏪 *البقالات التي سددت اليوم:*\n"
                            for r in records:
                                msg += f"▪️ {r['name']}: {float(r['total_amount'])} ريال\n"
                                
                            msg += f"\n💰 *إجمالي مسحوبات اليوم:* {float(total_sales)} ريال"
                            msg += f"\n💎 *صافي أرباحك اليوم:* {float(profit)} ريال\n"
                            msg += "\n*(تم توليد هذا التقرير آلياً من النظام)*"
                            
                            await smart_notify_web(ADMIN_ID, msg, parse_mode="Markdown")
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            break # السماح للسيرفر بالانطفاء بأمان
        except Exception as e:
            print(f"Automated Report Error: {e}")
            await asyncio.sleep(60) # 🌟 التعديل الأمني: منع الانهيار الصامت

# =====================================================================
# 🌟 الروبوت الكاسح (Sweeper): حل معضلة الجنرالين والعمليات المعلقة للأبد
# =====================================================================
async def sweep_stuck_pending_transactions():
    """يبحث عن العمليات التي علقت في حالة Pending بسبب انطفاء السيرفر ويعالجها"""
    while True:
        try:
            await asyncio.sleep(300) # يعمل كل 5 دقائق بصمت
            if not database.pool: continue
                
            async with database.pool.acquire() as conn:
                # جلب العمليات المعلقة التي مر عليها أكثر من 10 دقائق
                stuck_txs = await conn.fetch("""
                    SELECT id, phone_number, package_name, client_id, selling_price, profit, paid_from_balance, added_to_debt, idempotency_key 
                    FROM telecom_transactions 
                    WHERE status = 'pending' AND created_at < NOW() - INTERVAL '10 minutes'
                """)
                
                for tx in stuck_txs:
                    trans_id = tx['id']
                    phone = tx['phone_number']
                    client_id = tx['client_id']
                    final_price = tx['selling_price']
                    
                    # 1. نسأل المزود الحقيقي: "هل هذه العملية تنفذت عندكم أم لا؟"
                    # 🌟 الإصلاح الأمني: استخدام الدالة الحقيقية المعتمدة في النظام مع تمرير مفتاح منع التكرار
                    provider_check = await execute_provider_call_with_retry(phone, tx['package_name'], txn_uuid=tx['idempotency_key'])

                    async with conn.transaction():
                        # التأكد أنها لا تزال معلقة
                        check_status = await conn.fetchval("SELECT status FROM telecom_transactions WHERE id = $1 FOR UPDATE", trans_id)
                        if check_status != 'pending': continue
                        
                        if provider_check['status'] == 'success':
                            # ✅ المزود نفذها: نؤكد النجاح فقط
                            ref_id = provider_check.get('ref_id', 'N/A')
                            await conn.execute("UPDATE telecom_transactions SET status = 'success', provider_reference_id = $1 WHERE id = $2", ref_id, trans_id)
                            try: await smart_notify_web(ADMIN_ID, f"🧹 **الروبوت الكاسح:**\nتم تأكيد نجاح عملية معلقة للرقم {phone} بعد انقطاع الاتصال.")
                            except: pass
                            
                        else:
                            # ❌ المزود لم ينفذها: نقوم بالاسترداد المالي (Refund)
                            await conn.execute("UPDATE telecom_transactions SET status = 'failed' WHERE id = $1", trans_id)
                            
                            # إرجاع رصيد البوابة للوكيل
                            base_cost = tx['selling_price'] - tx['profit']
                            await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", base_cost)
                            
                            is_agent = (client_id == 0 or client_id == ADMIN_ID or client_id == NETWORK_OWNER_ID)
                            if not is_agent:
                                await conn.execute("""
                                    UPDATE users 
                                    SET telecom_balance = telecom_balance + $1,
                                        telecom_debt = GREATEST(0, telecom_debt - $2),
                                        telecom_pos_cash = GREATEST(0, COALESCE(telecom_pos_cash, 0) - $2)
                                    WHERE user_id = $3
                                """, tx['paid_from_balance'], tx['added_to_debt'], client_id)
                                
                                fail_details = f"❌ استرداد آلي (Sweeper): فشل تسديد {tx['package_name']} للرقم {phone}"
                                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES ($1, 'استرداد_تسديد', $2, $3, 'telecom')", client_id, final_price, fail_details)
                                
                                if tx['profit'] > 0:
                                    await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", -tx['profit'], f"إلغاء ربح (Sweeper): للرقم {phone}")
                            else:
                                fail_details = f"❌ استرداد طياري آلي (Sweeper): فشل تسديد {tx['package_name']} للرقم {phone}"
                                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'قيد_عكسي', $1, $2, 'telecom')", final_price, fail_details)
                                if tx['profit'] > 0:
                                    await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", -tx['profit'], f"إلغاء ربح طياري (Sweeper): للرقم {phone}")
                                    
                            try: await smart_notify_web(ADMIN_ID, f"🧹 **الروبوت الكاسح:**\nتم استرداد مبلغ {final_price} ريال للرقم {phone} لأن العملية كانت معلقة والمزود لم ينفذها.")
                            except: pass
                            
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Sweeper Error: {e}")
            await asyncio.sleep(60)

async def monitor_provider_balance_background():
    """روبوت يعمل في الخلفية يفحص رصيدك في المزود كل 30 دقيقة وينبهك إذا انخفض"""
    while True:
        try:
            # 1. الاتصال بـ API المزود لمعرفة الرصيد (محاكاة)
            current_real_balance = 8500 # لنفترض أن الرصيد الحقيقي الآن 8500 ريال
            alert_threshold = 5000 
            
            if current_real_balance < alert_threshold:
                msg = (
                    f"🚨 **تنبيه عاجل: انخفاض رصيد المزود!** 🚨\n\n"
                    f"رصيدك الفعلي لدى مزود الخدمة (أم دراهم) انخفض إلى:\n"
                    f"💰 **{current_real_balance} ريال**\n\n"
                    f"⚠️ يرجى تغذية حسابك لدى المزود لتجنب توقف خدمة الشحن عن البقالات."
                )
                await smart_notify_web(ADMIN_ID, msg)
                await asyncio.sleep(7200) # ننتظر ساعتين
            else:
                await asyncio.sleep(1800) # نفحص بعد 30 دقيقة
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Monitor Balance Error: {e}")
            await asyncio.sleep(1800) # 🌟 التعديل الأمني: منع الانهيار الصامت

# 3. مراقب صحة النظام (System Health Monitor)
async def system_health_monitor():
    """طبيب السيرفر: يراقب الضغط، يفرغ الذاكرة، ويفعل التمدد والانكماش الذاتي"""
    import gc
    
    # 🌟 استدعاء أولي لتصفير العداد (لكي يبدأ الحساب من الآن)
    import psutil
    psutil.cpu_percent(interval=None)
    
    while True:
        try:
            # 🌟 الإصلاح 1: استخدام interval=None لمنع تجميد السيرفر (سيحسب المتوسط لآخر 30 ثانية)
            cpu_usage = psutil.cpu_percent(interval=None)
            ram_usage = psutil.virtual_memory().percent
            
            # 🌟 1. التمدد والانكماش الذاتي (Global Auto-Lite Mode) 🌟
            if database.pool:
                async with database.pool.acquire() as conn:
                    global_lite = await conn.fetchval("SELECT value FROM settings WHERE key = 'global_lite_mode'")
                    
                    # 🌟 الإصلاح 2: رفع حد الخطر إلى 95% للمعالج و 90% للذاكرة لتجاهل القفزات الوهمية
                    if cpu_usage > 95 or ram_usage > 90:
                        # أ. الصيانة الذاتية: تفريغ الذاكرة العشوائية فوراً
                        try:
                            ip_request_tracker.clear()
                        except: pass
                        gc.collect()
                        
                        # ب. الانكماش: إجبار جميع الهواتف على وضع التيربو
                        if global_lite != 'on':
                            await conn.execute("INSERT INTO settings (key, value) VALUES ('global_lite_mode', 'on') ON CONFLICT (key) DO UPDATE SET value = 'on'")
                            try: await notify_clients(action="force_lite_mode_on")
                            except: pass
                            try: await smart_notify_web(ADMIN_ID, f"🚨 **طبيب السيرفر (طوارئ):**\nالضغط مرتفع جداً (CPU: {cpu_usage}%, RAM: {ram_usage}%).\nتم تفريغ الذاكرة وإجبار جميع العملاء على (وضع التيربو) لحماية السيرفر من السقوط.")
                            except: pass
                            
                    # إذا انخفض الضغط وعاد للاستقرار (أقل من 75%)
                    elif cpu_usage < 75 and ram_usage < 75:
                        # التمدد: إعادة التطبيق لشكله الفخم
                        if global_lite == 'on':
                            await conn.execute("UPDATE settings SET value = 'off' WHERE key = 'global_lite_mode'")
                            try: await notify_clients(action="force_lite_mode_off")
                            except: pass
                            try: await smart_notify_web(ADMIN_ID, f"✅ **طبيب السيرفر:**\nانخفض الضغط واستقر النظام (CPU: {cpu_usage}%, RAM: {ram_usage}%).\nتم إيقاف وضع التيربو الإجباري وعاد التطبيق لشكله الفخم.")
                            except: pass

            # 🌟 2. التقرير الصباحي المعتاد 🌟
            now = datetime.now()
            if now.hour == 8 and now.minute == 0:
                db_status = "🟢 متصلة ومستقرة"
                active_users = 0
                if database.pool:
                    async with database.pool.acquire() as conn:
                        active_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role = 'client'")
                        await conn.execute("DELETE FROM idempotency_keys WHERE created_at < NOW() - INTERVAL '24 hours'")
                else:
                    db_status = "🔴 غير متصلة!"

                report = (
                    f"📊 **تقرير صحة النظام الصباحي** 📊\n\n"
                    f"🖥️ **المعالج (CPU):** `{cpu_usage}%`\n"
                    f"🧠 **الذاكرة (RAM):** `{ram_usage}%`\n"
                    f"🗄️ **قاعدة البيانات:** {db_status}\n"
                    f"👥 **إجمالي البقالات:** `{active_users}`\n\n"
                    f"✅ النظام محمي ويعمل بكفاءة يا وكيلنا."
                )
                try: await smart_notify_web(ADMIN_ID, report)
                except: pass
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"System Health Error: {e}")
            await asyncio.sleep(60)

# =====================================================================
# جدار الحماية ومراقبة النظام (Security & Monitoring)
# =====================================================================
# 1. صائد أخطاء السيرفر (Backend Error Catcher)
@web.middleware
async def error_catcher_middleware(request, handler):
    try:
        return await handler(request)
    except web.HTTPException as ex:
        # 🌟 التعديل: تجاهل الأخطاء الأمنية الطبيعية مثل (401 غير مصرح) لكي لا يزعجك في تليجرام
        raise ex
    except (ConnectionResetError, asyncio.CancelledError):
        # 🌟 التعديل الجديد: تجاهل أخطاء انقطاع الإنترنت من طرف العميل بصمت تام لكي لا يزعجك في التليجرام
        return fast_json_response({"status": "error", "message": "انقطع الاتصال من طرف العميل."})
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"🔴 Server Error: {error_details}")
        try:
            error_msg = (
                f"🚨 **انهيار في السيرفر (Backend Error 500)** 🚨\n\n"
                f"🔗 **الرابط:** `{request.path}`\n"
                f"👤 **الآيبي:** `{request.remote}`\n"
                f"⚠️ **نوع الخطأ:** `{type(e).__name__}`\n"
                f"📝 **التفاصيل:** `{str(e)}`\n\n"
                f"🔍 **الكود المسبب:**\n`{error_details[-700:]}`"
            )
            asyncio.create_task(smart_notify_web(ADMIN_ID, error_msg))
        except:
            pass
        return fast_json_response({"status": "error", "message": "عذراً، حدث خطأ في النظام. تم إبلاغ الوكيل وجاري الإصلاح."})

# =====================================================================
# 🛡️ الجدار الناري والإغلاق الشامل (Security & Lockdown Middleware)
# =====================================================================
banned_ips_cache = set()
lockdown_cache = {"status": "off", "last_check": 0}

# 🌟 دالة ذكية لاستخراج الآيبي الحقيقي للعميل متجاوزة حماية Render
def get_real_ip(request):
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote

@web.middleware
async def security_middleware(request, handler):
    # 🌟 الحل السحري: السماح لملفات الصور والتصميم بالمرور فوراً لمنع استنزاف قاعدة البيانات
    if request.path.startswith('/static/'):
        return await handler(request)
        
    # 1. فحص الآيبي المحظور (طرد فوري بدون استهلاك موارد)
    client_ip = get_real_ip(request)
    if client_ip in banned_ips_cache:
        return web.Response(status=403, text="⛔ Access Denied: Your IP is permanently banned due to security violations.")
    
    # 2. فحص وضع الإغلاق الشامل (تحديث الكاش كل 30 ثانية لتخفيف الضغط على الداتا بيز)
    current_time = time.time()
    if current_time - lockdown_cache["last_check"] > 30:
        if database.pool:
            async with database.pool.acquire() as conn:
                status = await conn.fetchval("SELECT value FROM settings WHERE key = 'lockdown_mode'")
                lockdown_cache["status"] = status or "off"
                
                ips = await conn.fetch("SELECT ip FROM banned_ips")
                banned_ips_cache.clear()
                banned_ips_cache.update([r['ip'] for r in ips])
                
        lockdown_cache["last_check"] = current_time

    # 3. تفعيل الإغلاق الشامل
    if lockdown_cache["status"] == "on":
        if not request.path.startswith('/webhook/'):
            if request.path.startswith('/api/'):
                return fast_json_response({"status": "error", "message": "🚨 النظام في وضع الإغلاق الأمني الشامل (Lockdown). يرجى الانتظار حتى تنهي الإدارة الفحص الأمني."})
            else:
                return web.Response(status=503, text="🚨 النظام مغلق حالياً لدواعي أمنية. يرجى المحاولة لاحقاً.", content_type="text/plain; charset=utf-8")

    # 🌟 إضافة ذاكرة مؤقتة خارج الدالة (ضع هذا السطر قبل الدالة مباشرة إذا أردت، أو اتركه هنا)
    if not hasattr(security_middleware, "user_status_cache"):
        security_middleware.user_status_cache = {}

    # 4. 🌟 الحارس اللحظي (طرد الكاشير المطرود أو البقالة الموقوفة فوراً) 🌟
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            user_id = payload.get('user_id')
            cashier_name = payload.get('cashier_name')

            # فحص البقالات والعمال فقط (تجاوز الإدارة والزوار)
            if user_id and user_id not in [0, ADMIN_ID, NETWORK_OWNER_ID, 888888, 999999]:
                current_time = time.time()
                # 🌟 جلب البيانات من الكاش إذا مر عليها أقل من 60 ثانية (لتخفيف الضغط عن قاعدة البيانات)
                if user_id in security_middleware.user_status_cache and current_time - security_middleware.user_status_cache[user_id]['time'] < 60:
                    user = security_middleware.user_status_cache[user_id]['data']
                else:
                    if database.pool:
                        async with database.pool.acquire() as conn:
                            user_row = await conn.fetchrow("SELECT account_status, cashiers FROM users WHERE user_id = $1", user_id)
                            if user_row:
                                user = dict(user_row)
                                security_middleware.user_status_cache[user_id] = {'data': user, 'time': current_time}
                            else:
                                user = None
                        
                        # أ. إذا تم حذف الحساب نهائياً
                        if not user or user.get('account_status') == 'deleted':
                            raise web.HTTPUnauthorized(reason="⛔ حسابك محذوف! يرجى مراجعة الإدارة.")
                            
                        # ب. الجدار الأمني للتجميد: يمنع السحب والشحن، ويسمح بالتصفح والمحادثة
                        if user.get('account_status') == 'suspended':
                            allowed_paths = ['/api/logout', '/api/chat/send', '/api/chat/voice', '/api/client_request_statement', '/api/change_my_password']
                            if request.method == 'POST' and request.path not in allowed_paths:
                                return web.json_response({"status": "error", "message": f"❄️ حسابك مجمد مؤقتاً من قبل الإدارة.\nالسبب: {user.get('suspend_reason', 'مراجعة إدارية')}\nلا يمكنك تنفيذ عمليات مالية."})
                        
                        # ب. إذا كان الداخل عامل (كاشير)، نتأكد أن صاحب البقالة لم يقم بطرده
                        if cashier_name:
                            cashiers_json = user.get('cashiers')
                            if not cashiers_json:
                                raise web.HTTPUnauthorized(reason="⛔ تم حذف حسابك كعامل!")
                            
                            import json
                            cashiers = json.loads(cashiers_json) if isinstance(cashiers_json, str) else cashiers_json
                            if not any(c.get('name') == cashier_name for c in cashiers):
                                raise web.HTTPUnauthorized(reason="⛔ تم طردك أو حذف حسابك كعامل!")
        except jwt.ExpiredSignatureError:
            pass # سيتم التعامل معها في check_jwt
        except jwt.InvalidTokenError:
            pass # سيتم التعامل معها في check_jwt
        except web.HTTPUnauthorized as ex:
            raise ex # 👈 تنفيذ الطرد الفوري وإجبار التطبيق على تسجيل الخروج
        except Exception as e:
            pass 

    return await handler(request)
    
# 2. جدار الحماية من الهجمات (Rate Limiter)
ip_request_tracker = {}

@web.middleware
async def rate_limit_middleware(request, handler):
    if request.path.startswith('/static/') or request.path == '/' or request.path.startswith('/webhook/'):
        return await handler(request)

    client_ip = get_real_ip(request) # 🌟 استخدام الآيبي الحقيقي
    current_time = time.time()
    
    # 🌟 الإصلاح الأمني (DDoS Memory Leak): حماية الذاكرة من الامتلاء بذكاء
    # إذا تجاوز عدد الآيبيهات 5000، نحذف أقدم 1000 آيبي خامل ونبقي على المهاجمين النشطين لحظْرهم
    if len(ip_request_tracker) > 5000:
        # ترتيب الآيبيهات حسب أقدمية آخر طلب (LRU Cache)
        sorted_ips = sorted(ip_request_tracker.items(), key=lambda x: x[1][-1] if x[1] else 0)
        # حذف أقدم 1000 آيبي
        for ip, _ in sorted_ips[:1000]:
            del ip_request_tracker[ip]
    
    if random.random() < 0.05: 
        keys_to_delete = [ip for ip, times in ip_request_tracker.items() if not times or current_time - times[-1] > 10]
        for ip in keys_to_delete:
            del ip_request_tracker[ip]

    if client_ip not in ip_request_tracker:
        ip_request_tracker[client_ip] = []
        
    ip_request_tracker[client_ip] = [t for t in ip_request_tracker[client_ip] if current_time - t < 10]
    
    if len(ip_request_tracker[client_ip]) > 100:
        if len(ip_request_tracker[client_ip]) == 101: 
            # 🌟 التعديل الأمني: حظر الآيبي نهائياً في قاعدة البيانات
            async def ban_ip_db(ip):
                if database.pool:
                    async with database.pool.acquire() as conn:
                        await conn.execute("INSERT INTO banned_ips (ip) VALUES ($1) ON CONFLICT DO NOTHING", ip)
            asyncio.create_task(ban_ip_db(client_ip))
            
            alert_msg = f"🛑 **إنذار أمني (جدار الحماية)** 🛑\n\nتم حظر الآيبي `{client_ip}` نهائياً بسبب إرسال طلبات مكثفة جداً (DDoS Attempt)."
            asyncio.create_task(smart_notify_web(ADMIN_ID, alert_msg))
            
        return web.Response(status=429, text="Too Many Requests. تم حظرك نهائياً بسبب الطلبات المكثفة.")
        
    ip_request_tracker[client_ip].append(current_time)
    return await handler(request)

# =====================================================================
# 9. دوال إدارة الباقات والرصيد (لوحة تحكم الوكيل)
# =====================================================================
async def api_get_telecom_admin_data(request: web.Request):
    user_id = require_agent_only(request) # 🚨 حماية الإدارة فقط
    try:
        async with database.pool.acquire() as conn:
            packages = await conn.fetch("SELECT * FROM telecom_packages ORDER BY is_active DESC, network, selling_price ASC")
            
            # جلب نسب الأرباح من جدول الإعدادات (الافتراضي 2% كاش، 4% دين)
            networks = ['yemen_mobile', 'you', 'sabafon', 'adsl']
            percentages = {}
            for net in networks:
                cash_pct = await conn.fetchval("SELECT value FROM settings WHERE key = $1", f"{net}_cash_pct")
                credit_pct = await conn.fetchval("SELECT value FROM settings WHERE key = $1", f"{net}_credit_pct")
                percentages[net] = {
                    "cash": float(cash_pct) if cash_pct else 2.0,
                    "credit": float(credit_pct) if credit_pct else 4.0
                }
                
        return fast_json_response({
            "status": "success", 
            "packages": [dict(p) for p in packages],
            "percentages": percentages
        })
    except Exception as e:
        return fast_json_response({"status": "error", "message": "خطأ في جلب بيانات لوحة التحكم."})

async def api_save_telecom_package(request: web.Request):
       # الكود الجديد (حماية ضد إدخال نصوص فارغة من الويب)
    user_id = require_agent_only(request)
    data = await request.post()
    pkg_id = int(data.get('id', 0))
    network = data.get('network')
    name = data.get('name')
    cost = to_decimal(data.get('cost', 0))
    cash_price = to_decimal(data.get('cash_price', 0))
    credit_price = to_decimal(data.get('credit_price', 0))
    retail_price = to_decimal(data.get('retail_price', 0)) # 🌟 استقبال سعر الطياري
    is_active = str(data.get('is_active', 'true')).lower() == 'true' # 🌟 استقبال حالة الباقة من الويب

    try:
        async with database.pool.acquire() as conn:
            if pkg_id == 0: 
                await conn.execute("""
                    INSERT INTO telecom_packages (network, package_name, cost_price, selling_price, credit_price, retail_price, is_active)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                """, network, name, cost, cash_price, credit_price, retail_price, is_active)
            else: 
                await conn.execute("""
                    UPDATE telecom_packages 
                    SET network=$1, package_name=$2, cost_price=$3, selling_price=$4, credit_price=$5, retail_price=$6, is_active=$7
                    WHERE id=$8
                """, network, name, cost, cash_price, credit_price, retail_price, is_active, pkg_id)
        return fast_json_response({"status": "success", "message": "تم حفظ الباقة بنجاح!"})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "خطأ في حفظ الباقة."})

# =====================================================================
# دالة تغذية رصيد البوابة (مصححة لخصم الكاش من الدرج)
# =====================================================================
async def api_recharge_telecom_balance(request: web.Request):
    """الوكيل يقوم بتغذية رصيد بوابة التسديدات من كاش الصندوق"""
    user_id = require_agent_only(request) # 🚨 حماية الإدارة فقط

    # 🌟 حماية الصلاحيات: هذا الزر للوكيل فقط
    if user_id != ADMIN_ID:
        return fast_json_response({"status": "error", "message": "⛔ هذا القسم خاص بالوكيل فقط!"})
        
    data = await request.post()
    
    try:
        amount = Decimal(data.get('amount', 0))
    except InvalidOperation:
        return fast_json_response({"status": "error", "message": "المبلغ المدخل غير صالح."})
    
    if amount <= 0:
        return fast_json_response({"status": "error", "message": "المبلغ يجب أن يكون أكبر من صفر!"})
        
    try:
        async with database.pool.acquire() as conn:
            async with conn.transaction(): # 🌟 اللمسة الاحترافية: قفل المعاملة لضمان عدم ضياع السجل
                # 🌟 العزل التام: إضافة المبلغ لمحفظة الوكيل الجديدة (الرصيد الرقمي)
                await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", amount)
                
                # 🌟 الإصلاح المحاسبي العبقري: تسجيلها كـ (تغذية_رصيد_بوابة) لكي يقوم النظام بخصم الكاش الورقي من الدرج!
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'تغذية_رصيد_بوابة', $1, 'تغذية رصيد المزود من كاش الصندوق', 'telecom')", amount)
                
        return fast_json_response({"status": "success", "message": f"تم تغذية رصيد البوابة بمبلغ {amount} ريال بنجاح!\n(وتم خصم المبلغ من كاش التسديدات في درجك)"})
    except Exception as e:
        print(f"Recharge Telecom Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تغذية الرصيد."})

async def api_manage_client_telecom(request: web.Request):
    """الوكيل يضيف رصيداً مقدماً للبقالة أو يحدد سقف المديونية أو يدير الـ VIP والخصومات"""
    user_id = require_agent_only(request) # 🚨 حماية الوكيل فقط
    if user_id not in [ADMIN_ID, NETWORK_OWNER_ID]:
        return fast_json_response({"status": "error", "message": "غير مصرح لك."})
        
    data = await request.post()
    action = data.get('action') # 'limit', 'balance', 'vip', or 'discount'
    
    try:
        client_id = int(data.get('client_id', 0))
        amount = Decimal(data.get('amount', 0))
    except (ValueError, InvalidOperation):
        return fast_json_response({"status": "error", "message": "البيانات المدخلة غير صالحة."})
    
    if amount < 0:
        return fast_json_response({"status": "error", "message": "المبلغ لا يمكن أن يكون سالباً."})
        
    try:
        async with database.pool.acquire() as conn:
            if action == 'limit':
                await conn.execute("UPDATE users SET telecom_credit_limit = $1 WHERE user_id = $2", amount, client_id)
                return fast_json_response({"status": "success", "message": f"تم تحديث سقف مديونية الرصيد إلى {amount} ريال بنجاح."})
            
            elif action == 'balance':
                if amount <= 0:
                    return fast_json_response({"status": "error", "message": "يجب إدخال مبلغ أكبر من الصفر للشحن."})
                
                async with conn.transaction():
                    # 🌟 التعديل المحاسبي الذكي: تسديد الدين أولاً، وما فاض يذهب للرصيد
                    user = await conn.fetchrow("SELECT telecom_debt FROM users WHERE user_id = $1 FOR UPDATE", client_id)
                    if not user: return fast_json_response({"status": "error", "message": "العميل غير موجود."})
                    
                    current_debt = Decimal(user['telecom_debt'] or 0)
                    
                    if current_debt > 0:
                        if amount >= current_debt:
                            # المبلغ يغطي الدين ويفيض
                            remaining_balance = amount - current_debt
                            await conn.execute("""
                                UPDATE users 
                                SET telecom_debt = 0, 
                                    telecom_balance = telecom_balance + $1,
                                    telecom_pos_cash = 0
                                WHERE user_id = $2
                            """, remaining_balance, client_id)
                            details = f"استلام كاش ({amount} ريال): تسديد دين ({current_debt}) وإضافة رصيد ({remaining_balance})"
                        else:
                            # المبلغ يغطي جزء من الدين فقط
                            await conn.execute("""
                                UPDATE users 
                                SET telecom_debt = telecom_debt - $1,
                                    telecom_pos_cash = GREATEST(0, COALESCE(telecom_pos_cash, 0) - $1)
                                WHERE user_id = $2
                            """, amount, client_id)
                            details = f"استلام كاش ({amount} ريال): تسديد جزء من دين الرصيد"
                    else:
                        # لا يوجد دين، المبلغ كله يذهب للرصيد
                        await conn.execute("UPDATE users SET telecom_balance = telecom_balance + $1 WHERE user_id = $2", amount, client_id)
                        details = f"استلام كاش ({amount} ريال): إضافة رصيد مقدم"
                            
                    # 🌟 الإصلاح المحاسبي: استخدام 'تحصيل_رصيد' لكي يلتقطه الـ Trigger ويزيد الكاش الفعلي في درجك
                    await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES ($1, 'تحصيل_رصيد', $2, $3, 'telecom')", client_id, amount, details)
                
                return fast_json_response({"status": "success", "message": f"تم استلام {amount} ريال بنجاح وتحديث حساب البقالة."})
            
            elif action == 'vip':
                is_vip = data.get('is_vip') == 'true'
                await conn.execute("UPDATE users SET is_vip = $1 WHERE user_id = $2", is_vip, client_id)
                msg = "✅ تم تفعيل معاملة VIP للعميل (سيحسب له سعر الكاش دائماً)." if is_vip else "⛔ تم إلغاء معاملة VIP."
                return fast_json_response({"status": "success", "message": msg})
            
            elif action == 'discount':
                await conn.execute("UPDATE users SET special_discount = $1 WHERE user_id = $2", amount, client_id)
                return fast_json_response({"status": "success", "message": f"تم تحديد خصم خاص بقيمة {amount} ريال للعميل."})
                
    except Exception as e:
        print(f"Manage Telecom Client Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء التحديث."})

async def api_save_telecom_percentages(request: web.Request):
    user_id = require_agent_only(request) # 🚨 حماية الوكيل فقط
    data = await request.post()
    try:
        async with database.pool.acquire() as conn:
            for key, value in data.items():
                if key.endswith('_pct'):
                    await conn.execute("""
                        INSERT INTO settings (key, value) VALUES ($1, $2)
                        ON CONFLICT (key) DO UPDATE SET value = $2
                    """, key, str(value))
        return fast_json_response({"status": "success", "message": "تم حفظ نسب الأرباح بنجاح!"})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "خطأ في حفظ النسب."})
        
async def api_delete_telecom_package(request: web.Request):
    user_id = require_agent_only(request) # 🚨 حماية الوكيل فقط
    data = await request.post()
    pkg_id = int(data.get('id', 0))
    try:
        async with database.pool.acquire() as conn:
            await conn.execute("DELETE FROM telecom_packages WHERE id = $1", pkg_id)
        return fast_json_response({"status": "success", "message": "تم حذف الباقة بنجاح!"})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "خطأ في الحذف."})

async def api_sync_telecom_packages_mock(request: web.Request):
    """محاكاة المزامنة الذكية: جلب باقات من المزود وتطبيق نسب الأرباح آلياً"""
    user_id = require_agent_only(request) # 🚨 حماية الوكيل فقط
    if user_id not in [ADMIN_ID, NETWORK_OWNER_ID]:
        return fast_json_response({"status": "error", "message": "غير مصرح لك."})
        
    try:
        # 1. هذه البيانات تحاكي ما سيأتينا من API المزود الحقيقي لاحقاً
        mock_api_data = [
            {"network": "yemen_mobile", "name": "باقة مزايا 2 جيجا", "cost": 2300},
            {"network": "yemen_mobile", "name": "باقة مزايا 4 جيجا", "cost": 4600},
            {"network": "you", "name": "باقة سمارت 4 جيجا", "cost": 2800},
            {"network": "you", "name": "باقة ماكس 10 جيجا", "cost": 6500},
            {"network": "sabafon", "name": "باقة سوبر 3 جيجا", "cost": 2000},
            {"network": "adsl", "name": "يمن نت فئة 1500", "cost": 1650},
            {"network": "adsl", "name": "يمن نت فئة 3000", "cost": 3300}
        ]
        
        async with database.pool.acquire() as conn:
            # 2. جلب نسب الأرباح التي حددها الوكيل من الإعدادات
            networks = ['yemen_mobile', 'you', 'sabafon', 'adsl']
            margins = {}
            for net in networks:
                cash_pct = await conn.fetchval("SELECT value FROM settings WHERE key = $1", f"{net}_cash_pct")
                credit_pct = await conn.fetchval("SELECT value FROM settings WHERE key = $1", f"{net}_credit_pct")
                margins[net] = {
                    "cash": Decimal(cash_pct) if cash_pct else Decimal('2.0'),
                    "credit": Decimal(credit_pct) if credit_pct else Decimal('4.0')
                }
            
            # 3. تطبيق الحسبة الذكية وحفظها في قاعدة البيانات
            async with conn.transaction():
                for pkg in mock_api_data:
                    net = pkg['network']
                    cost = Decimal(pkg['cost'])
                    
                    from decimal import ROUND_HALF_UP
                    # حساب الأسعار: التكلفة + (التكلفة * النسبة / 100)
                    cash_price = cost + (cost * margins[net]['cash'] / Decimal('100'))
                    credit_price = cost + (cost * margins[net]['credit'] / Decimal('100'))
                    
                    # 🌟 التقريب المحاسبي الصارم (إزالة الفواصل بدقة بدون خسارة هللات)
                    cash_price = cash_price.quantize(Decimal('1.'), rounding=ROUND_HALF_UP)
                    credit_price = credit_price.quantize(Decimal('1.'), rounding=ROUND_HALF_UP)
                    
                    # إدخال الباقة فقط إذا لم تكن موجودة مسبقاً
                    await conn.execute("""
                        INSERT INTO telecom_packages (network, package_name, cost_price, selling_price, credit_price)
                        SELECT $1, $2, $3, $4, $5
                        WHERE NOT EXISTS (
                            SELECT 1 FROM telecom_packages WHERE network = $1 AND package_name = $2
                        )
                    """, net, pkg['name'], cost, cash_price, credit_price)
                    
        return fast_json_response({"status": "success", "message": "تمت المزامنة الذكية! تم جلب الباقات وتطبيق نسب أرباحك عليها آلياً."})
    except Exception as e:
        print(f"Sync Packages Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء المزامنة."})
                
async def api_telecom_wallet_info(request: web.Request):
    """جلب بيانات محفظة أرباح التسديدات ومبيعات اليوم والشهر"""
    user_id = require_agent_only(request)
    try:
        total_profit = await database.get_telecom_total_profit()
        async with database.pool.acquire() as conn:
            history = await conn.fetch("SELECT amount, details, date FROM telecom_profits ORDER BY date DESC LIMIT 10")
            
            # 🌟 حساب مبيعات اليوم بدون أرباح (للمطابقة اليومية السريعة)
            today_cost = await conn.fetchval("""
                SELECT COALESCE(SUM(selling_price - profit), 0) 
                FROM telecom_transactions 
                WHERE status = 'success' AND DATE(created_at) = CURRENT_DATE
            """)

            # 🌟 حساب مبيعات الشهر بدون أرباح (لمعرفة حجم السحب الكلي)
            month_cost = await conn.fetchval("""
                SELECT COALESCE(SUM(selling_price - profit), 0) 
                FROM telecom_transactions 
                WHERE status = 'success' AND created_at >= date_trunc('month', CURRENT_DATE)
            """)
            
            # 🌟 حساب أرباح اليوم
            today_profit = await conn.fetchval("""
                SELECT COALESCE(SUM(amount), 0) 
                FROM telecom_profits 
                WHERE DATE(date) = CURRENT_DATE AND amount > 0
            """)
            
        history_list = [{"amount": float(h['amount']), "details": h['details'], "date": str(h['date'])[:16]} for h in history]
        return fast_json_response({
            "status": "success", 
            "total_profit": float(total_profit), 
            "today_cost": float(today_cost or 0),
            "month_cost": float(month_cost or 0),
            "today_profit": float(today_profit or 0),
            "history": history_list
        })
    except Exception as e:
        return fast_json_response({"status": "error", "message": str(e)})

async def api_withdraw_telecom_profit(request: web.Request):
    """سحب أرباح التسديدات"""
    user_id = require_agent_only(request)
    # 🌟 حماية الصلاحيات: هذا الزر للوكيل فقط، يمنع المدير من سحب أرباح التسديدات
    if user_id != ADMIN_ID:
        return fast_json_response({"status": "error", "message": "⛔ هذا القسم خاص بالوكيل فقط!"})
    data = await request.post()
    
    try:
        amount = Decimal(data.get('amount', 0))
    except InvalidOperation:
        return fast_json_response({"status": "error", "message": "المبلغ المدخل غير صالح."})
    
    if amount <= 0:
        return fast_json_response({"status": "error", "message": "المبلغ يجب أن يكون أكبر من صفر!"})
    
    try:
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                # 🌟 التعديل الأمني: قفل الصندوق لمنع السحب المزدوج
                await conn.execute("SELECT 1 FROM users WHERE user_id = 0 FOR UPDATE")
                
                total_profit = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM telecom_profits")
                if amount > (total_profit or Decimal('0.0')):
                    return fast_json_response({"status": "error", "message": f"رصيد الأرباح لا يكفي! أقصى مبلغ هو {int(total_profit or 0)} ريال."})
                    
                                # الجزء الجديد
                from core_accounting import FinancialEngine
                # 🌟 الإصلاح الأمني: تمرير الاتصال (conn) لمنع استنزاف الاتصالات وتجميد السيرفر (Deadlock)
                engine = FinancialEngine(database.pool)
                fin_stats = await engine.get_summary(conn)
                
                # 🌟 العزل التام: سحب أرباح التسديدات يتم من (كاش التسديدات) فقط!
                telecom_cash = fin_stats.get("telecom", Decimal('0.0'))
                
                if amount > telecom_cash:
                    return fast_json_response({"status": "error", "message": f"كاش التسديدات المتوفر لا يكفي للسحب!\n(يمنع النظام سحب أرباح التسديدات من كاش الكروت).\nالمتوفر في صندوق التسديدات: {int(telecom_cash)} ريال."})
                    
                await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", -amount, "سحب أرباح تسديدات (من الويب)")
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'سحب_أرباح_تسديدات', $1, $2, 'telecom')", amount, "سحب أرباح تسديدات (من الويب)")
                
        return fast_json_response({"status": "success", "message": f"تم سحب {int(amount)} ريال من أرباح التسديدات بنجاح!"})
    except Exception as e:
        print(f"Withdraw Telecom Profit Error: {e}")
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء السحب."})
        
# =====================================================================
# دوال إدارة شرائح الأرباح (نظام الرصيد المفتوح وسعر الوحدة)
# =====================================================================
async def api_get_pricing_tiers(request: web.Request):
    """جلب جميع الشرائح وإعدادات المبالغ الكبيرة وسعر الوحدة"""
    user_id = require_agent_only(request)
    try:
        async with database.pool.acquire() as conn:
            tiers = await conn.fetch("SELECT * FROM pricing_tiers ORDER BY min_amount ASC")
            
            over_1000_cash = await conn.fetchval("SELECT value FROM settings WHERE key = 'over_1000_cash_profit'")
            over_1000_credit = await conn.fetchval("SELECT value FROM settings WHERE key = 'over_1000_credit_profit'")
            over_1000_retail = await conn.fetchval("SELECT value FROM settings WHERE key = 'over_1000_retail_profit'")
            
            # 🌟 التعديل: جلب سعر الوحدة
            unit_price = await conn.fetchval("SELECT value FROM settings WHERE key = 'telecom_unit_price'")
            
            settings_over_1000 = {
                "cash": float(over_1000_cash or 50),
                "credit": float(over_1000_credit or 70),
                "retail": float(over_1000_retail or 100),
                "unit_price": float(unit_price or 12.0) # السعر الافتراضي 12 ريال
            }
            
        return fast_json_response({
            "status": "success", 
            "tiers": [dict(t) for t in tiers], 
            "over_1000": settings_over_1000
        })
    except Exception as e:
        return fast_json_response({"status": "error", "message": "خطأ في جلب الشرائح."})

async def api_save_pricing_tier(request: web.Request):
    """إضافة/تعديل شريحة أو حفظ إعدادات المبالغ الكبيرة وسعر الوحدة"""
    user_id = require_agent_only(request)
    data = await request.post()
    
    if data.get('is_over_1000') == 'true':
        try:
            async with database.pool.acquire() as conn:
                await conn.execute("UPDATE settings SET value = $1 WHERE key = 'over_1000_cash_profit'", str(data.get('cash_profit', 50)))
                await conn.execute("UPDATE settings SET value = $1 WHERE key = 'over_1000_credit_profit'", str(data.get('credit_profit', 70)))
                await conn.execute("UPDATE settings SET value = $1 WHERE key = 'over_1000_retail_profit'", str(data.get('retail_profit', 100)))
                
                # 🌟 التعديل: حفظ سعر الوحدة
                unit_price = str(data.get('unit_price', 12.0))
                await conn.execute("""
                    INSERT INTO settings (key, value) VALUES ('telecom_unit_price', $1)
                    ON CONFLICT (key) DO UPDATE SET value = $1
                """, unit_price)
                
            return fast_json_response({"status": "success", "message": "تم حفظ الإعدادات بنجاح!"})
        except Exception as e:
            return fast_json_response({"status": "error", "message": "خطأ في الحفظ."})

        # الكود الجديد (حماية ضد إدخال نصوص فارغة من الويب)
    tier_id = int(data.get('id', 0))
    try:
        min_amt = to_decimal(data.get('min_amount', 0))
        max_amt = to_decimal(data.get('max_amount', 0))
        cash_p = to_decimal(data.get('cash_profit', 0))
        credit_p = to_decimal(data.get('credit_profit', 0))
        retail_p = to_decimal(data.get('retail_profit', 0))
        
        async with database.pool.acquire() as conn:
            if tier_id == 0:
                await conn.execute("""
                    INSERT INTO pricing_tiers (min_amount, max_amount, cash_profit, credit_profit, retail_profit)
                    VALUES ($1, $2, $3, $4, $5)
                """, min_amt, max_amt, cash_p, credit_p, retail_p)
            else:
                await conn.execute("""
                    UPDATE pricing_tiers 
                    SET min_amount=$1, max_amount=$2, cash_profit=$3, credit_profit=$4, retail_profit=$5
                    WHERE id=$6
                """, min_amt, max_amt, cash_p, credit_p, retail_p, tier_id)
        return fast_json_response({"status": "success", "message": "تم حفظ الشريحة بنجاح!"})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "خطأ في حفظ الشريحة."})

async def api_delete_pricing_tier(request: web.Request):
    """حذف شريحة"""
    user_id = require_agent_only(request)
    data = await request.post()
    tier_id = int(data.get('id', 0))
    try:
        async with database.pool.acquire() as conn:
            await conn.execute("DELETE FROM pricing_tiers WHERE id = $1", tier_id)
        return fast_json_response({"status": "success", "message": "تم حذف الشريحة بنجاح!"})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "خطأ في الحذف."})
        
# ================= دالة إرسال الإشعارات والمزامنة الشبحية (Web Push & FCM) =================
async def send_web_push(user_id: int, title: str, message: str, is_silent: bool = False):
    import database
    try:
        if not is_silent:
            if database.pool:
                async with database.pool.acquire() as conn:
                    await conn.execute("INSERT INTO web_notifications (user_id, title, message) VALUES ($1, $2, $3)", user_id, title, message)
            await notify_clients(user_id, "new_notification", f"{title}\n{message}")

        if database.pool:
            async with database.pool.acquire() as conn:
                sub_record = await conn.fetchval("SELECT subscription_json FROM push_subscriptions WHERE user_id = $1", user_id)
                
                if sub_record:
                    import json
                    import asyncio
                    
                    # 🌟 إذا كان التوكن يبدأ بقوس { فهو توكن متصفح (Web Push)
                    if sub_record.startswith('{'):
                        from pywebpush import webpush
                        from config import VAPID_PRIVATE_KEY, VAPID_ADMIN_EMAIL
                        sub_info = json.loads(sub_record)
                        payload = json.dumps({"title": title, "body": message, "icon": "/static/logo.png", "is_silent": is_silent})
                        try:
                            await asyncio.to_thread(
                                webpush, subscription_info=sub_info, data=payload,
                                vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={"sub": VAPID_ADMIN_EMAIL},
                                headers={"Urgency": "high", "TTL": "60"}
                            )
                        except Exception as ex:
                            if "404" in str(ex) or "410" in str(ex):
                                await conn.execute("DELETE FROM push_subscriptions WHERE user_id = $1", user_id)
                    
                    # 🌟 أما إذا كان نصاً عادياً فهو توكن تطبيق الأندرويد (FCM)
                    else:
                        import firebase_admin
                        from firebase_admin import messaging as fcm_messaging
                        from firebase_admin import credentials
                        
                        if not firebase_admin._apps:
                            cred = credentials.Certificate("firebase-adminsdk.json")
                            firebase_admin.initialize_app(cred)
                            
                        fcm_msg = fcm_messaging.Message(
                            notification=fcm_messaging.Notification(title=title, body=message),
                            android=fcm_messaging.AndroidConfig(
                                priority='high',
                                notification=fcm_messaging.AndroidNotification(
                                    channel_id='alshehab_alerts_v2', # 🌟 القناة الجديدة
                                    sound='bell' # 🌟 الصوت المخصص الذي رفعناه
                                )
                            ),
                            token=sub_record
                        )
                        try:
                            await asyncio.to_thread(fcm_messaging.send, fcm_msg)
                        except Exception as ex:
                            if "not-found" in str(ex) or "UNREGISTERED" in str(ex):
                                await conn.execute("DELETE FROM push_subscriptions WHERE user_id = $1", user_id)
    except Exception as e:
        print(f"Push Error: {e}")

# =====================================================================
# 🌐 مسار جلب مفتاح VAPID للواجهة الأمامية
# =====================================================================
async def api_get_vapid_key(request: web.Request):
    from config import VAPID_PUBLIC_KEY
    return fast_json_response({"public_key": VAPID_PUBLIC_KEY})

# 👇 الدالة الجديدة لاستقبال اشتراك الهاتف (Firebase) 👇
async def api_save_subscription(request: web.Request):
    """حفظ توكن فايربيس الخاص بالهاتف لكي نتمكن من إيقاظه لاحقاً"""
    user_id = check_jwt(request)
    
    # دعم قراءة البيانات كـ JSON (لأن الواجهة الجديدة ترسلها هكذا)
    try:
        if request.content_type == 'application/json':
            data = await request.json()
        else:
            data = await request.post()
    except:
        data = await request.post()
        
    sub_token = data.get('subscription')
    
    if sub_token:
        try:
            async with database.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO push_subscriptions (user_id, subscription_json) 
                    VALUES ($1, $2) 
                    ON CONFLICT (user_id) DO UPDATE SET subscription_json = $2
                """, user_id, sub_token)
            return fast_json_response({"status": "success"})
        except Exception as e:
            print(f"Save Sub Error: {e}")
            return fast_json_response({"status": "error"})
    return fast_json_response({"status": "error"})

# =====================================================================
# التحديث التلقائي لقاعدة البيانات (للمبرمجين من الهاتف)
# =====================================================================
async def auto_update_db():
    """تحديث قاعدة البيانات تلقائياً عند تشغيل السيرفر"""
    await asyncio.sleep(5) 
    if database.pool:
        try:
            async with database.pool.acquire() as conn:
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS pos_cash_collected NUMERIC DEFAULT 0.0;")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS telecom_balance NUMERIC DEFAULT 0.0;")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS telecom_debt NUMERIC DEFAULT 0.0;")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS telecom_credit_limit NUMERIC DEFAULT 20000.0;")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS telecom_status VARCHAR(10) DEFAULT 'off';")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS physical_status VARCHAR(10) DEFAULT 'on';") 
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS web_password VARCHAR(50) DEFAULT '1234';")                             

                # 🌟 التعديل الأمني الجديد: إضافة عمود بصمة الهاتف (OTP) 🌟
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS device_id VARCHAR(255);")
                
                # 🌟 إضافة عمود العمال (الكاشير) للبقالات 🌟
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS cashiers JSONB DEFAULT '[]'::jsonb;")
                
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_vip BOOLEAN DEFAULT FALSE;")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS special_discount NUMERIC DEFAULT 0.0;")
                
                # 🌟 إنشاء جدول القائمة السوداء للآيبيهات المحظورة (حماية السيرفر) 🌟
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS banned_ips (
                        ip VARCHAR(50) PRIMARY KEY,
                        banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                
                try: await conn.execute("ALTER TABLE telecom_transactions ADD COLUMN IF NOT EXISTS cost_price NUMERIC DEFAULT 0.0;")
                except: pass
                try: await conn.execute("ALTER TABLE telecom_transactions ADD COLUMN IF NOT EXISTS profit NUMERIC DEFAULT 0.0;")
                except: pass
                try: await conn.execute("ALTER TABLE telecom_packages ADD COLUMN IF NOT EXISTS retail_price NUMERIC DEFAULT 0.0;")
                except: pass
                
                await smart_notify_web(ADMIN_ID, "✅ **تم تحديث قاعدة البيانات تلقائياً بنجاح!**\nتمت إضافة (بصمة الهاتف) وعمود (العمال) وجدول (القائمة السوداء).")
        except Exception as e:
            await smart_notify_web(ADMIN_ID, f"❌ **فشل تحديث قاعدة البيانات:**\n`{str(e)}`")

async def websocket_handler(request: web.Request):
    """بوابة الاتصال اللحظي (WebSocket) لتطبيق الويب"""
    token = request.query.get('token')
    if not token:
        return web.HTTPUnauthorized(reason="Missing Token")
    
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        user_id = payload['user_id']
    except Exception:
        return web.HTTPUnauthorized(reason="Invalid Token")

    # فتح قناة الاتصال مع نبضات (Heartbeat) كل 30 ثانية لمنع انقطاع الاتصال
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    
    # تسجيل دخول الجهاز
    connected_ws_clients[user_id].add(ws)
    print(f"🟢 WebSocket Connected: User {user_id}")
    
    try:
        # إبقاء القناة مفتوحة للاستماع (نحن نستخدمها للإرسال من السيرفر فقط حالياً)
        async for msg in ws:
            pass 
    finally:
        # عند إغلاق المتصفح أو انقطاع النت، نحذف الجهاز من القائمة
        connected_ws_clients[user_id].discard(ws)
        if not connected_ws_clients[user_id]:
            del connected_ws_clients[user_id]
        print(f"🔴 WebSocket Disconnected: User {user_id}")
        
    return ws
    
    # =====================================================================
# دفتر ديون الوكيل الشخصي (معزول تماماً عن حسابات الإدارة)
# =====================================================================
async def api_get_agent_personal_customers(request: web.Request):
    user_id = require_admin(request)
    try:
        async with database.pool.acquire() as conn:
            customers = await conn.fetch("SELECT id, name, debt FROM agent_personal_customers WHERE debt > 0 ORDER BY name")
            return fast_json_response({"status": "success", "customers": [dict(c) for c in customers]})
    except Exception as e:
        return fast_json_response({"status": "error", "message": str(e)})

async def api_add_agent_personal_debt(request: web.Request):
    user_id = require_admin(request)
    data = await request.post()
    name = data.get('name', '').strip()
    try: amount = Decimal(data.get('amount', 0))
    except: amount = Decimal('0.0')
    details = data.get('details', 'دين شخصي')
    
    if not name or amount <= 0: return fast_json_response({"status": "error", "message": "بيانات غير صالحة"})
    try:
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                # 🌟 استكمال دالة إضافة الدين الشخصي
                cust_id = await conn.fetchval("""
                    INSERT INTO agent_personal_customers (name, debt) VALUES ($1, $2)
                    ON CONFLICT (name) DO UPDATE SET debt = agent_personal_customers.debt + $2
                    RETURNING id
                """, name, amount)
                
                if not cust_id:
                    cust_id = await conn.fetchval("SELECT id FROM agent_personal_customers WHERE name = $1", name)
                    
                await conn.execute("INSERT INTO agent_personal_ledger (customer_id, type, amount, details) VALUES ($1, 'دين', $2, $3)", cust_id, amount, details)
                
        return fast_json_response({"status": "success", "message": f"تم تسجيل {amount} ريال كدين شخصي على {name}."})
    except Exception as e:
        return fast_json_response({"status": "error", "message": str(e)})

async def api_pay_agent_personal_debt(request: web.Request):
    user_id = require_admin(request)
    data = await request.post()
    cust_id = int(data.get('customer_id', 0))
    try: amount = Decimal(data.get('amount', 0))
    except: amount = Decimal('0.0')
    
    if cust_id == 0 or amount <= 0: return fast_json_response({"status": "error", "message": "بيانات غير صالحة"})
    try:
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE agent_personal_customers SET debt = GREATEST(0, debt - $1) WHERE id = $2", amount, cust_id)
                await conn.execute("INSERT INTO agent_personal_ledger (customer_id, type, amount, details) VALUES ($1, 'تسديد', $2, 'تسديد دفعة نقدية')", cust_id, amount)
        return fast_json_response({"status": "success", "message": "تم تسديد الدفعة وخصمها من دفترك الشخصي بنجاح."})
    except Exception as e:
        return fast_json_response({"status": "error", "message": str(e)})

async def api_get_agent_personal_ledger(request: web.Request):
    user_id = require_admin(request)
    cust_id = int(request.match_info.get('id', 0))
    try:
        async with database.pool.acquire() as conn:
            ledger = await conn.fetch("SELECT type, amount, details, date FROM agent_personal_ledger WHERE customer_id = $1 ORDER BY date DESC LIMIT 50", cust_id)
            return fast_json_response({"status": "success", "ledger": [dict(l) for l in ledger]})
    except Exception as e:
        return fast_json_response({"status": "error", "message": str(e)})
        
async def api_toggle_sandbox(request: web.Request):
    user_id = check_jwt(request)
    if user_id != 888888: return fast_json_response({"status": "error"})
    global SANDBOX_MODE
    SANDBOX_MODE = not SANDBOX_MODE
    return fast_json_response({"status": "success", "sandbox_mode": SANDBOX_MODE})
        
async def api_debt_health(request: web.Request):
    user_id = require_admin(request)
    
    # 🌟 حماية السيرفر من الانهيار إذا أرسل المتصفح بيانات خاطئة
    try:
        client_id = int(request.match_info.get('id', 0))
    except ValueError:
        return fast_json_response({"status": "error", "message": "عذراً، رقم العميل غير صالح. يرجى تحديث الصفحة."})

    try:
        async with database.pool.acquire() as conn:
            # 🌟 التعديل 1: جلب حالة الحساب (account_status)
            user = await conn.fetchrow("SELECT name, debt, pending_profit, account_status FROM users WHERE user_id = $1", client_id)
            if not user:
                return fast_json_response({"status": "error", "message": "العميل غير موجود"})
            
            net_debt = Decimal(user['debt'] or 0) - Decimal(user.get('pending_profit') or 0)
            
            # مسحوبات آخر 30 يوم (كروت فقط - الجدار الناري مفعل)
            taken_30d = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND type IN ('تسليم_لعميل', 'مبيعات_آجلة') AND is_reverted = FALSE AND wallet_type = 'manager' AND date >= CURRENT_DATE - INTERVAL '30 days'", client_id)
            
            # تسديدات آخر 30 يوم
            paid_30d = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND type IN ('تسديد_من_عميل', 'مرتجع_من_عميل') AND is_reverted = FALSE AND wallet_type = 'manager' AND date >= CURRENT_DATE - INTERVAL '30 days'", client_id)
            
            # آخر دفعة
            last_payment = await conn.fetchval("SELECT MAX(date) FROM transactions WHERE user_id = $1 AND type = 'تسديد_من_عميل' AND is_reverted = FALSE", client_id)
            
            taken_30d = Decimal(taken_30d or 0)
            paid_30d = Decimal(paid_30d or 0)
            
            # المعادلة المحاسبية (FIFO)
            new_debt = min(net_debt, taken_30d)
            old_debt = max(Decimal('0.0'), net_debt - new_debt)
            
            days_since_payment = -1
            if last_payment:
                days_since_payment = (datetime.now() - last_payment).days
            
            # 🌟 التعديل 2: مطابقة أسماء المتغيرات والألوان مع الجافاسكريبت
            if net_debt <= 0:
                health_status = "ممتاز 🟢"
                health_message = "لا يوجد ديون مستحقة على هذا العميل."
                health_color = "#38ef7d"
            elif days_since_payment > 30 or (days_since_payment == -1 and net_debt > 0):
                health_status = "خطر 🔴 (دين ميت)"
                health_message = "لم يسدد أي دفعة منذ أكثر من شهر! يجب التحصيل فوراً."
                health_color = "#ff4757"
            elif old_debt > new_debt:
                health_status = "تحذير 🟡 (تراكم ديون)"
                health_message = "الدين القديم المتراكم أكبر من مسحوباته الجديدة."
                health_color = "#f5af19"
            else:
                health_status = "جيد 🟢 (دين نشط)"
                health_message = "العميل يسحب بضاعة ويسدد بانتظام."
                health_color = "#38ef7d"
                
            data = {
                "status": "success",
                "current_debt": float(net_debt),
                "taken_30d": float(taken_30d),
                "paid_30d": float(paid_30d),
                "new_debt": float(new_debt),
                "old_debt": float(old_debt),
                "days_since_payment": days_since_payment,
                "health_status": health_status,
                "health_message": health_message,
                "health_color": health_color,
                "account_status": user.get('account_status', 'active') # 🌟 التعديل 3: إرسال حالة الحساب لزر التجميد
            }
            return fast_json_response(data)
    except Exception as e:
        print(f"Debt Health Error: {e}")
        return fast_json_response({"status": "error", "message": "خطأ في تحليل الدين"})

async def api_update_debt_schedule(request: web.Request):
    """تفعيل أو إلغاء جدولة الديون للعميل (تقليص السقف آلياً)"""
    user_id = require_admin(request)
    data = await request.post()
    client_id = int(data.get('client_id', 0))
    pct = Decimal(data.get('pct', 0))
    
    if pct < 0 or pct > 100:
        return fast_json_response({"status": "error", "message": "النسبة يجب أن تكون بين 0 و 100."})
        
    try:
        async with database.pool.acquire() as conn:
            await conn.execute("UPDATE users SET debt_schedule_pct = $1 WHERE user_id = $2", pct, client_id)
            
        msg = f"تم تفعيل الجدولة بنسبة {pct}% بنجاح." if pct > 0 else "تم إيقاف الجدولة الذكية للعميل."
        return fast_json_response({"status": "success", "message": msg})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء التحديث."})

async def api_toggle_suspend_client(request: web.Request):
    """تجميد أو فك تجميد حساب بقالة (للمدير العام)"""
    user_id = require_admin(request)
    
    if user_id != NETWORK_OWNER_ID:
        return fast_json_response({"status": "error", "message": "⛔ هذا الإجراء من صلاحيات المدير العام فقط!"})
        
    data = await request.post()
    client_id = int(data.get('client_id', 0))
    action = data.get('action') 
    reason = data.get('reason', '').strip()
    
    try:
        async with database.pool.acquire() as conn:
            client_name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
            
            if action == 'suspend':
                if not reason:
                    return fast_json_response({"status": "error", "message": "يجب كتابة سبب التجميد!"})
                await conn.execute("UPDATE users SET account_status = 'suspended', suspend_reason = $1 WHERE user_id = $2", reason, client_id)
                msg = f"تم تجميد حساب ({client_name}) بنجاح."
                
                # 🌟 الإصلاح: استخدام bot_instance مباشرة بدون استيراد
                import asyncio
                asyncio.create_task(smart_notify_web(ADMIN_ID, f"❄️ **قرار إداري:**\nقام المدير العام بتجميد حساب البقالة ({client_name}).\nالسبب: {reason}"))
                try: asyncio.create_task(send_web_push(ADMIN_ID, "❄️ قرار إداري", f"المدير العام قام بتجميد حساب البقالة ({client_name})."))
                except: pass
                
            else:
                await conn.execute("UPDATE users SET account_status = 'active', suspend_reason = NULL WHERE user_id = $1", client_id)
                msg = f"تم فك التجميد عن حساب ({client_name}) بنجاح."
                
        return fast_json_response({"status": "success", "message": msg})
    except Exception as e:
        return fast_json_response({"status": "error", "message": "حدث خطأ أثناء تنفيذ الإجراء."})

# =====================================================================
# 🚀 نظام الاسترداد اللحظي (Telecom Webhook) - Zero-Hanging Money
# =====================================================================
async def telecom_webhook_handler(request: web.Request):
    """
    يستقبل هذا المسار النتيجة النهائية من مزود الخدمة (أم دراهم) فور حدوثها.
    الرابط سيكون: https://your-domain.onrender.com/webhook/telecom?token=YOUR_SECRET
    """
    # 1. حماية الـ Webhook (التحقق من أن الطلب قادم فعلاً من المزود وليس هكر )
    token = request.query.get('token', '')
    import hmac
    if not hmac.compare_digest(str(token).strip(), str(API_SECRET_KEY).strip()):
        return web.Response(text="Unauthorized", status=401)

    try:
        data = await request.json()
        # المزود سيرسل لنا: رقم العملية عندنا (txn_uuid) أو (ref_id)، والحالة (success/failed)
        txn_uuid = data.get('txn_uuid')
        status = data.get('status')
        provider_msg = data.get('message', 'مرفوضة من المصدر')
        
        if not txn_uuid or not status:
            return web.json_response({"status": "error", "message": "بيانات ناقصة"}, status=400)

        async with database.pool.acquire() as conn:
            async with conn.transaction():
                # البحث عن العملية المعلقة
                tx = await conn.fetchrow("SELECT * FROM telecom_transactions WHERE idempotency_key = $1 FOR UPDATE", txn_uuid)
                
                if not tx:
                    return web.json_response({"status": "ignored", "message": "العملية غير موجودة"})
                    
                if tx['status'] != 'pending':
                    return web.json_response({"status": "ignored", "message": "تمت معالجة العملية مسبقاً"})

                trans_id = tx['id']
                client_id = tx['client_id']
                phone = tx['phone_number']
                pkg_name = tx['package_name']
                final_price = tx['selling_price']
                profit = tx['profit']
                is_agent = (client_id == 0 or client_id == ADMIN_ID or client_id == NETWORK_OWNER_ID)

                if status == 'success':
                    # ✅ نجاح لحظي
                    ref_id = data.get('ref_id', 'N/A')
                    await conn.execute("UPDATE telecom_transactions SET status = 'success', provider_reference_id = $1 WHERE id = $2", ref_id, trans_id)
                    
                    # إشعار العميل فوراً
                    await notify_clients(client_id, "new_notification", f"✅ نجاح شحن معلق: تم شحن {pkg_name} للرقم {phone} بنجاح.")
                    
                elif status == 'failed':
                    # ❌ فشل واسترداد لحظي (Refund)
                    await conn.execute("UPDATE telecom_transactions SET status = 'failed' WHERE id = $1", trans_id)
                    
                    # إرجاع رصيد البوابة للوكيل
                    base_cost = tx['selling_price'] - tx['profit']
                    await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", base_cost)
                    
                    if not is_agent:
                        await conn.execute("""
                            UPDATE users 
                            SET telecom_balance = telecom_balance + $1,
                                telecom_debt = GREATEST(0, telecom_debt - $2),
                                telecom_pos_cash = GREATEST(0, COALESCE(telecom_pos_cash, 0) - $2)
                            WHERE user_id = $3
                        """, tx['paid_from_balance'], tx['added_to_debt'], client_id)
                        
                        fail_details = f"❌ استرداد لحظي (Webhook): فشل تسديد {pkg_name} للرقم {phone}. السبب: {provider_msg}"
                        await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES ($1, 'استرداد_تسديد', $2, $3, 'telecom')", client_id, final_price, fail_details)
                        
                        if profit > 0:
                            await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", -profit, f"إلغاء ربح (Webhook): للرقم {phone}")
                    else:
                        fail_details = f"❌ استرداد طياري لحظي (Webhook): فشل تسديد {pkg_name} للرقم {phone}. السبب: {provider_msg}"
                        await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'قيد_عكسي', $1, $2, 'telecom')", final_price, fail_details)
                        if profit > 0:
                            await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", -profit, f"إلغاء ربح طياري (Webhook): للرقم {phone}")

                    # إشعار العميل فوراً بالاسترداد
                    await notify_clients(client_id, "new_notification", f"❌ فشل شحن معلق: تعذر شحن {pkg_name} للرقم {phone}. تم استرداد المبلغ ({final_price} ريال) لحسابك.")
                    
                    # إرسال إشعار ويب (Push) ليرن هاتف العميل
                    await send_web_push(client_id, "❌ استرداد مبلغ", f"فشل شحن {phone}. تم إرجاع {final_price} ريال لحسابك.")

        # تحديث شاشات الإدارة
        await notify_clients(ADMIN_ID)
        return web.json_response({"status": "success"})

    except Exception as e:
        print(f"Webhook Error: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

# =====================================================================
# إعداد التطبيق وتجميع الروابط (Setup)
# =====================================================================
def setup_web_api(bot=None): 
    global bot_instance
    bot_instance = bot 

    # 👇 التعديل الجذري: رفع سقف حجم الصور المسموح بها إلى 50 ميجابايت لمنع انهيار السيرفر
    app = web.Application(
        middlewares=[error_catcher_middleware, security_middleware, rate_limit_middleware], # 👈 أضفنا security_middleware هنا
        client_max_size=1024 * 1024 * 50  
    )

    # 👇 تشغيل المهام الخلفية بحماية (safe_task) لمنع الموت الصامت
    asyncio.create_task(safe_task(auto_update_db(), "تحديث قاعدة البيانات"))
    asyncio.create_task(safe_task(automated_daily_telecom_report(), "التقرير اليومي الآلي"))
    asyncio.create_task(safe_task(system_health_monitor(), "مراقب صحة النظام"))
    asyncio.create_task(safe_task(sweep_stuck_pending_transactions(), "الروبوت الكاسح للعمليات المعلقة"))
    
    # إضافة مسار الصفحة الرئيسية ومجلد الصور (static)
    app.router.add_get('/', serve_index_html)
    
    # 🌟 التعديل السحري هنا: تغيير الرابط ليتطابق مع ملف فايربيس الجديد 🌟
    app.router.add_get('/firebase-messaging-sw.js', serve_sw)
        
    # إضافة مسار تطبيق الأندرويد (TWA)
    app.router.add_get('/.well-known/assetlinks.json', serve_assetlinks)
    
    import os
    if not os.path.exists('static'):
        os.makedirs('static')
    app.router.add_static('/static/', path='static', name='static')

    # روابط GET
    app.router.add_get('/api/app_version', api_app_version) 
    app.router.add_get('/api/user_data/{id}', api_get_user_data)
    app.router.add_get('/api/network_data', api_network_data)
    app.router.add_get('/api/manager_data', api_manager_data)
    app.router.add_get('/api/ghost_mode/{id}', api_ghost_mode)
    app.router.add_get('/api/client_customers/{id}', api_client_customers)
    app.router.add_get('/api/client_transactions/{id}', api_client_transactions)
    app.router.add_get('/api/agent_daily_profit', api_agent_daily_profit)
    app.router.add_get('/api/notifications/{id}', api_notifications)
    app.router.add_get('/api/chat/history/{id}', api_chat_history)
    app.router.add_get('/api/agent_dashboard_stats', api_agent_dashboard_stats)
    app.router.add_get('/api/live_activity', api_live_activity)          
    app.router.add_get('/api/agent_wallet_info', api_agent_wallet_info)
    
    # روابط GET الجديدة لنظام الاستلام الأعمى
    app.router.add_get('/api/check_pending_shipment', api_check_pending_shipment)
    app.router.add_get('/api/get_mismatched_shipments', api_get_mismatched_shipments)
    app.router.add_get('/api/ecards_stats', api_ecards_stats)
    app.router.add_get('/api/get_pending_orders', api_get_pending_orders)
    app.router.add_get('/api/get_tickets', api_get_tickets)
    app.router.add_get('/api/telecom_packages/{network}', api_get_telecom_packages)
    app.router.add_get('/api/telecom_admin_data', api_get_telecom_admin_data)
    app.router.add_get('/api/my_ecards/{id}', api_my_ecards)
        
    # روابط POST
    app.router.add_post('/api/sell', api_sell_cards)
    app.router.add_post('/api/collect', api_collect_debt)
    app.router.add_post('/api/finance_action', api_finance_action)
    app.router.add_post('/api/client_pos_sell', api_client_pos_sell)
    app.router.add_post('/api/returns', api_returns)
    app.router.add_post('/api/transfer_center', api_transfer_center)
    app.router.add_post('/api/smart_cart_order', api_smart_cart_order)
    app.router.add_post('/api/update_credit_limit', api_update_credit_limit)
    app.router.add_post('/api/undo_last', api_undo_last)
    app.router.add_post('/api/add_manual_debt', api_add_manual_debt)
    app.router.add_post('/api/client_customer_pay', api_client_customer_pay)
    app.router.add_post('/api/chat/send', api_chat_send)
    app.router.add_post('/api/chat/voice', api_chat_voice)
    app.router.add_post('/api/log_error', api_log_error)
    app.router.add_post('/api/send_report', api_send_report)
    app.router.add_post('/api/manager_broadcast', api_manager_broadcast)
    app.router.add_post('/api/manager_request_cash', api_manager_request_cash)
    app.router.add_post('/api/report_damaged_image', api_report_damaged_image)
    app.router.add_post('/api/upload_card_image', api_upload_card_image)
    app.router.add_post('/api/mark_notifications_read', api_mark_notifications_read)
    app.router.add_post('/api/withdraw_profit', api_withdraw_profit)
    
    # روابط POST الجديدة
    app.router.add_post('/api/logout', api_logout)
    app.router.add_post('/api/manager_send_shipment', api_manager_send_shipment)
    app.router.add_post('/api/match_shipment', api_match_shipment)
    app.router.add_post('/api/resolve_shipment', api_resolve_shipment) 
    app.router.add_post('/api/ai_daily_entry_image', api_ai_daily_entry_image)
    app.router.add_post('/api/client_customer_ledger', api_client_customer_ledger)
    app.router.add_post('/api/login', api_login)
    app.router.add_post('/api/parse_ecards', api_parse_ecards)
    app.router.add_post('/api/save_ecards', api_save_ecards)
    app.router.add_post('/api/extract_ecard', api_extract_ecard)
    app.router.add_post('/api/agent_sell_ecards', api_agent_sell_ecards)
    app.router.add_post('/api/toggle_client_ecard', api_toggle_client_ecard)
    app.router.add_post('/api/reject_order', api_reject_order)
    app.router.add_post('/api/agent_broadcast', api_agent_broadcast)
    app.router.add_post('/api/collect_telecom', api_collect_telecom_debt)
    app.router.add_post('/api/telecom_pay', api_telecom_pay_package)
    app.router.add_post('/api/telecom_pay_open', api_telecom_pay_open)
    app.router.add_post('/api/toggle_telecom', api_toggle_telecom_service)           
    app.router.add_post('/api/check_loan', api_check_telecom_loan)
    app.router.add_post('/api/save_telecom_package', api_save_telecom_package)
    app.router.add_post('/api/save_telecom_percentages', api_save_telecom_percentages)
    app.router.add_post('/api/delete_telecom_package', api_delete_telecom_package)
    app.router.add_post('/api/recharge_telecom', api_recharge_telecom_balance)
    app.router.add_post('/api/manage_client_telecom', api_manage_client_telecom)
    app.router.add_post('/api/sync_telecom_packages', api_sync_telecom_packages_mock)
    app.router.add_post('/api/withdraw_telecom_profit', api_withdraw_telecom_profit)
    app.router.add_post('/api/save_pricing_tier', api_save_pricing_tier)
    app.router.add_post('/api/delete_pricing_tier', api_delete_pricing_tier)
    app.router.add_post('/api/close_account', api_close_account)
    app.router.add_get('/api/client_telecom_history/{id}', api_client_telecom_history)
    
    # تشغيل روبوت مراقبة الرصيد في الخلفية
    asyncio.create_task(monitor_provider_balance_background())
    
    # إضافة الروابط الجديدة (POST و GET)
    app.router.add_post('/api/query_phone', api_query_phone_package)
    app.router.add_get('/api/provider_balance', api_get_provider_balance)
    app.router.add_get('/api/telecom_reconciliation', api_telecom_reconciliation)
    app.router.add_post('/api/toggle_physical', api_toggle_physical_service)
    app.router.add_post('/api/report_damaged_ecard', api_report_damaged_ecard)
    app.router.add_post('/api/change_my_password', api_change_my_password)
    app.router.add_post('/api/set_client_password', api_set_client_password)
    app.router.add_get('/api/pre_give_cards_check/{id}', api_pre_give_cards_check)
    app.router.add_post('/api/bulk_give_cards', api_bulk_give_cards)
    app.router.add_post('/api/delete_offline_client', api_delete_offline_client)
    app.router.add_post('/api/save_subscription', api_save_subscription)

    # روابط دفتر الوكيل الشخصي
    app.router.add_get('/api/agent_personal_customers', api_get_agent_personal_customers)
    app.router.add_post('/api/add_agent_personal_debt', api_add_agent_personal_debt)
    app.router.add_post('/api/pay_agent_personal_debt', api_pay_agent_personal_debt)
    app.router.add_get('/api/agent_personal_ledger/{id}', api_get_agent_personal_ledger)
    app.router.add_post('/api/telecom_safe_cancel', api_telecom_safe_cancel)
    app.router.add_get('/api/manage_cashiers', api_manage_cashiers)
    app.router.add_post('/api/manage_cashiers', api_manage_cashiers) 
    app.router.add_post('/api/client_request_statement', api_client_request_statement)
    app.router.add_get('/api/debt_health/{id}', api_debt_health)
    app.router.add_post('/api/update_debt_schedule', api_update_debt_schedule)
    app.router.add_post('/api/toggle_suspend_client', api_toggle_suspend_client)
    app.router.add_get('/api/vapid_public_key', api_get_vapid_key)
    # رابط الـ Webhook الخاص بمزود الاتصالات (الاسترداد اللحظي)
    app.router.add_post('/webhook/telecom', telecom_webhook_handler)
    app.router.add_post('/api/auto_bump_version', api_auto_bump_version)

    # رابط وضع الاختبار (Sandbox)
    app.router.add_post('/api/toggle_sandbox', api_toggle_sandbox)
    
    # رابط الاتصال اللحظي (WebSockets)
    app.router.add_get('/ws/live_updates', websocket_handler)
    
    # رابط الـ Webhook الخاص بالواتساب (يدعم GET و POST لسهولة الفحص)
    app.router.add_get('/webhook/whatsapp', whatsapp_webhook)
    app.router.add_post('/webhook/whatsapp', whatsapp_webhook)
 
    # ================= إعدادات CORS للسماح لتطبيق الـ APK بالاتصال =================
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=False, # 🌟 الحل السحري: إيقافها لكي يقبل الأندرويد الاتصال
            expose_headers="*",
            allow_headers="*",
            max_age=86400,
          )
    })

    # تطبيق CORS على جميع المسارات
    for route in list(app.router.routes()):
        cors.add(route)
    # =====================================================================

    return app
