import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from io import BytesIO
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler # 🌟 السطر المنقذ يجب أن يكون هنا في القمة!
from aiogram import Router, F, types, Bot
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import CommandObject

# 🌟 التعديل الأمني: جلبنا API_SECRET_KEY من ملف config
from config import ADMIN_ID, NETWORK_OWNER_ID, API_SECRET_KEY, SYSTEM_VAULT_PIN

import database
from datetime import datetime
from pdf_generator import generate_receipt, generate_custom_report_pdf
import random
import re
import os
import time
os.environ['TZ'] = 'Asia/Aden' # ضبط توقيت السيرفر على توقيت اليمن/السعودية
time.tzset()

from openpyxl.drawing.image import Image
from decimal import Decimal
import math
import aiohttp
import base64
import json
from aiohttp import web # 👈 تمت إضافة مكتبة الويب هنا
from core_accounting import get_financial_summary, get_dashboard_stats, check_and_alert_low_stock, generate_detailed_review_ledger, generate_full_backup_excel, generate_detailed_excel_report, core_give_cards, core_collect_debt, core_direct_sale, core_finance_action, core_process_return, core_bulk_give_cards, core_transfer_center

router = Router(  )

# ================= إعدادات ودالة الواتساب =================
# 🌟 التعديل: جلب الإعدادات من ملف config.py لتوحيد النظام
from config import WA_API_URL, WA_INSTANCE, WA_API_KEY, GM_WA_NUMBER

import time
import logging
import os
import firebase_admin
from firebase_admin import credentials

# إعداد سجل الأخطاء (Log File ) لحفظ أخطاء الواتساب والنظام
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

# ================= تهيئة Firebase للإشعارات =================
try:
    if not firebase_admin._apps:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        cert_path = os.path.join(base_dir, "firebase-adminsdk.json")
        cred = credentials.Certificate(cert_path)
        firebase_admin.initialize_app(cred)
        logging.info("✅ تم تهيئة Firebase بنجاح في التطبيق الأساسي")
except Exception as e:
    logging.error(f"❌ خطأ في تهيئة Firebase: {e}")

WA_ALERT_CACHE = {"last_sent": 0}

async def notify_admin_wa_error(bot: Bot):
    """دالة مساعدة لإرسال تنبيه للوكيل بدون إزعاج (مرة كل 30 دقيقة)"""
    if bot is None: return
    current_time = time.time()
    if current_time - WA_ALERT_CACHE["last_sent"] > 1800: # 1800 ثانية = 30 دقيقة
        try:
            await smart_notify(bot, ADMIN_ID, "🚨 **تنبيه نظام عاجل:**\nسيرفر الواتساب لا يستجيب أو يرفض إرسال الرسائل!\nيرجى التحقق من السيرفر.\n*(تم كتم التنبيهات لمدة 30 دقيقة لمنع الإزعاج)*")
            WA_ALERT_CACHE["last_sent"] = current_time
        except:
            pass

async def send_whatsapp_message(phone_number: str, message_text: str, pdf_buffer: BytesIO = None, filename: str = None, bot: Bot = None):
    """دالة ذكية ترسل نص فقط، أو نص + PDF إذا تم توفيره"""
    if not phone_number or phone_number.lower() == "none":
        return False

    clean_phone = phone_number.replace("+", "").strip()
    headers = {"apikey": WA_API_KEY, "Content-Type": "application/json"}
    
        # 🌟 التعديل الأمني: إضافة Timeout و Connector لحماية منافذ السيرفر من الاختناق
    timeout = aiohttp.ClientTimeout(total=15 )
    connector = aiohttp.TCPConnector(limit=10, force_close=True ) # تحديد الاتصالات المتزامنة
    
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector ) as session:

            if pdf_buffer and filename:
                pdf_buffer.seek(0)
                base64_pdf = base64.b64encode(pdf_buffer.read()).decode('utf-8')
                url = f"{WA_API_URL}/message/sendMedia/{WA_INSTANCE}"
                payload = {
                    "number": clean_phone,
                    "mediatype": "document",
                    "mimetype": "application/pdf",
                    "fileName": filename,
                    "caption": message_text,
                    "media": base64_pdf
                }
            else:
                url = f"{WA_API_URL}/message/sendText/{WA_INSTANCE}"
                payload = {
                    "number": clean_phone,
                    "text": message_text
                }
            
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status in [200, 201]: 
                    logging.info(f"✅ تم إرسال الواتساب بنجاح إلى: {clean_phone}")
                    return True
                else:
                    error_text = await response.text()
                    logging.error(f"❌ فشل إرسال الواتساب للرقم {clean_phone}. كود الخطأ: {response.status}. السبب: {error_text}")
                    await notify_admin_wa_error(bot)
    except asyncio.TimeoutError:
        logging.error(f"⏱️ انتهى وقت الاتصال (Timeout) بسيرفر الواتساب للرقم {clean_phone}.")
        await notify_admin_wa_error(bot)
    except Exception as e:
        logging.error(f"⚠️ خطأ غير متوقع في API الواتساب للرقم {clean_phone}: {e}")
        await notify_admin_wa_error(bot)
    return False

# ================= دالة الإرسال الآمن للواتساب (الدرع الواقي) =================
# 🌟 التعديل البرمجي: إضافة bot: Bot = None لكي لا ينهار الكود عند تمرير البوت
async def safe_send_whatsapp(client_id: int, message_text: str, pdf_buffer: BytesIO = None, filename: str = None, bot: Bot = None):
    """هذه الدالة تفحص حالة الواتساب للعميل وتضيف سطر الإيقاف لحماية الرقم من الحظر"""
    if not database.pool: return False
    
    async with database.pool.acquire() as conn:
        user = await conn.fetchrow("SELECT phone, wa_status FROM users WHERE user_id = $1", client_id)
        
        if not user or not user['phone'] or user['wa_status'] != 'on':
            return False
            
        safety_footer = "\n\n〰️〰️〰️〰️\n💡 *(لإيقاف استلام الفواتير على الواتساب، أرسل كلمة \"إيقاف\")*"
        final_message = message_text + safety_footer
        
        return await send_whatsapp_message(user['phone'], final_message, pdf_buffer, filename, bot)

# ================= دالة الإشعارات الذكية (تليجرام + تطبيق) =================
async def smart_notify(bot: Bot, user_id: int, text: str, document=None, photo=None, reply_markup=None):
    """
    دالة ذكية ترسل الإشعار للتليجرام والتطبيق معاً.
    إذا كان هناك ملف، ترسل تنبيهاً للتطبيق وتترك الملف للتليجرام.
    """
    # 1. الإرسال إلى التليجرام
    try:
        if document:
            await bot.send_document(user_id, document=document, caption=text, reply_markup=reply_markup)
        elif photo:
            await bot.send_photo(user_id, photo=photo, caption=text, reply_markup=reply_markup)
        else:
            await bot.send_message(user_id, text, reply_markup=reply_markup)
    except Exception as e:
        logging.error(f"Telegram send error: {e}")

    # 2. الإرسال إلى تطبيق الويب (Web Push)
    try:
        from web_api import send_web_push
        # تنظيف النص من علامات التليجرام (النجمتين وغيرها)
        clean_text = text.replace('**', '').replace('`', '').replace('_', '')
        
        if document or photo:
            # إذا كان هناك ملف أو صورة، نرسل تنبيهاً فقط للتطبيق
            file_type = "ملف (PDF/Excel)" if document else "صورة"
            push_title = f"📎 إشعار بـ {file_type} جديد"
            push_body = f"تم إرسال {file_type} في التليجرام:\n{clean_text[:100]}..."
            await send_web_push(user_id, push_title, push_body)
        else:
            # إذا كانت رسالة نصية، نرسلها للتطبيق
            lines = clean_text.split('\n')
            title = lines[0][:50] if lines else "🔔 إشعار جديد"
            body = '\n'.join(lines[1:])[:200] if len(lines) > 1 else clean_text[:200]
            await send_web_push(user_id, title, body)
    except Exception as e:
        logging.error(f"Web Push error: {e}")

# ================= أوامر تفعيل وإيقاف الواتساب للعملاء =================
@router.message(Command("wa_on"))
async def enable_wa_status(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ **الاستخدام:**\n`/wa_on [رقم_العميل]`")
    
    client_id = int(command.args.replace('[', '').replace(']', '').strip())
    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute("UPDATE users SET wa_status = 'on' WHERE user_id = $1", client_id)
            user = await conn.fetchrow("SELECT name, phone FROM users WHERE user_id = $1", client_id)
            
    try:
        from web_api import notify_clients
        await notify_clients(client_id)
    except Exception as e: logging.error(f"Error: {e}")
    await message.answer(f"✅ **تم تفعيل إشعارات الواتساب بنجاح!**\n👤 العميل: {user['name']}\n📱 الرقم: {user['phone']}\n*(الآن ستصله الفواتير تلقائياً)*")

@router.message(Command("wa_off"))
async def disable_wa_status(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ **الاستخدام:**\n`/wa_off [رقم_العميل]`")
    
    client_id = int(command.args.replace('[', '').replace(']', '').strip())
    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute("UPDATE users SET wa_status = 'off' WHERE user_id = $1", client_id)
            user = await conn.fetchrow("SELECT name FROM users WHERE user_id = $1", client_id)
            
    try:
        from web_api import notify_clients
        await notify_clients(client_id)
    except Exception as e: logging.error(f"Error: {e}")
    await message.answer(f"?? **تم إيقاف إشعارات الواتساب!**\n👤 العميل: {user['name']}\n*(لن يرسل له النظام أي رسالة واتساب بعد الآن)*")

# ================= أوامر إيقاف وتشغيل الكروت الإلكترونية للبقالات =================
@router.message(Command("ecard_off"))
async def disable_ecards(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ **الاستخدام:**\n`/ecard_off [رقم_العميل]`")
    
    client_id = int(command.args.replace('[', '').replace(']', '').strip())
    if database.pool:
        async with database.pool.acquire() as conn:
            # إضافة العمود إذا لم يكن موجوداً (حماية إضافية)
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ecard_status VARCHAR(10) DEFAULT 'on'")
            await conn.execute("UPDATE users SET ecard_status = 'off' WHERE user_id = $1", client_id)
            user = await conn.fetchrow("SELECT name FROM users WHERE user_id = $1", client_id)
            
    try:
        from web_api import notify_clients
        await notify_clients(client_id)
    except Exception as e: logging.error(f"Error: {e}")
    await message.answer(f"⛔ **تم إيقاف الكروت الإلكترونية!**\n👤 البقالة: {user['name']}\n*(لن يستطيع سحب أي كرت إلكتروني من التطبيق حتى تقوم بتفعيلها له)*")

@router.message(Command("ecard_on"))
async def enable_ecards(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ **الاستخدام:**\n`/ecard_on [رقم_العميل]`")
    
    client_id = int(command.args.replace('[', '').replace(']', '').strip())
    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ecard_status VARCHAR(10) DEFAULT 'on'")
            await conn.execute("UPDATE users SET ecard_status = 'on' WHERE user_id = $1", client_id)
            user = await conn.fetchrow("SELECT name FROM users WHERE user_id = $1", client_id)
            
    try:
        from web_api import notify_clients
        await notify_clients(client_id)
    except Exception as e: logging.error(f"Error: {e}")
    await message.answer(f"✅ **تم تفعيل الكروت الإلكترونية!**\n👤 البقالة: {user['name']}\n*(يمكنه الآن سحب الكروت من التطبيق بشكل طبيعي)*")

# ================= الحارس الأمني (Middleware) =================
from aiogram import BaseMiddleware
import logging

class SecurityGuardMiddleware(BaseMiddleware):
    def __init__(self):
        super().__init__()
        # 🌟 التعديل الاحترافي: استخدام Set (مجموعة) بدلاً من Tuple لتسريع البحث (O(1) بدلاً من O(N))
        # ووضعها في دالة __init__ لكي لا يتم إعادة إنشائها مع كل رسالة يستلمها البوت
        self.allowed_ids = {ADMIN_ID, int(NETWORK_OWNER_ID)}
        
        self.protected_prefixes = {
            "admin_", "owner_", "toggle_", "set_comp_", 
            "delcard_", "editprice_", "add_card_type",
            "delete_card_type_menu", "edit_card_price_menu",
            "rename_card_type_menu", "rencard_", # 👈 الأزرار الجديدة لتعديل الاسم
            "visit_", "gclient_", "ccollect_", "retclient_", "retnet_", "retdir_",
            "limit_page_", "setlimit_", "auto_give_",
            "review_order_", "reject_order_", "msg_client_",
            "ai_daily_entry", "daily_client_", "confirm_daily_entry",
            "ai_monthly_audit", "audit_next_client", "audit_auto_fix",
            # 🌟 [جديد] سد الثغرات وإضافة الأزرار المنسية للجدار الناري
            "revert_tx_", "pull_archive_", "mgr_dmg_", "export_", 
            "promo_action_", "approve_final_draft", "qa_cmd_", "qa_cat_", "admin_audit_logs",
            "ticket_", "net_", "acomp_", "upgrade_client_" # 👈 تمت إضافة زر الترقية وأزرار التذاكر هنا
        }
        
        # 🌟 سد الثغرة: إضافة جميع الأوامر الإدارية التي كانت مفقودة في القائمة القديمة
        self.protected_commands = {                  
            "/factory_reset_100", "/set_debt", "/test_system", 
            "/set_inv", "/link_account", "/set_gm_debt", 
            "/discount_gm", "/undo", "/fix_sys", "/set_group", 
            "/close_account", "/demote", "/maintenance",
            "/edit_date", "/move_today", "/setup_db", "/restore_backup",
            "/delete_report", "/clean_numbers", "/update_db", "/rename",
            "/set_phone", "/set_region", "/wa_list", "/test_wa", "/set_wa_webhook",
            "/fix_timezone", "/clear_alerts", "/align_dish", "/add_t_cash",
            "/lockdown", "/unlock_system", "/ban_ip", "/unban_all", "/rehab", "/fix_ready_cash",
            "/set_promo", "/test_backup", "/blackbox", "/fingerprint", "/mega_stress_test", "/emergency_clean_test"
        }

    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        bot = data.get('bot')

        # فحص أزرار الإنلاين (Callback Queries)
        if isinstance(event, types.CallbackQuery) and event.data:
            if any(event.data.startswith(prefix) for prefix in self.protected_prefixes):
                if user_id not in self.allowed_ids:
                    await self.send_alert(event, bot)
                    await event.answer("⛔ وصول مرفوض! هذه الأزرار مخصصة للإدارة فقط.", show_alert=True)
                    return 

        # فحص الرسائل النصية (Commands)
        elif isinstance(event, types.Message) and event.text and event.text.startswith('/'):
            # استخراج الأمر بأمان تام وتجاهل المسافات أو اسم البوت وتحويله لحروف صغيرة
            command = event.text.split()[0].split('@')[0].lower()
            
            if command in self.protected_commands:
                if user_id not in self.allowed_ids:
                    await self.send_alert(event, bot)
                    await event.answer("⛔ ليس لديك صلاحية لاستخدام هذا الأمر السري.")
                    return 

        return await handler(event, data)

    async def send_alert(self, event, bot):
        user_id = event.from_user.id
        username = event.from_user.username or event.from_user.first_name or "مجهول"
        
        # 🌟 التعديل الاحترافي: تحديد نوع المحاولة وما هو الزر/الأمر الذي تم الضغط عليه
        attempt_type = "زر إنلاين" if isinstance(event, types.CallbackQuery) else "أمر نصي"
        attempt_data = event.data if isinstance(event, types.CallbackQuery) else event.text

        alert_msg = (
            f"🚨 **إنذار أمني (IDS) - محاولة اختراق!** 🚨\n\n"
            f"👤 **المستخدم:** @{username}\n"
            f"🆔 **الآيدي:** `{user_id}`\n"
            f"⚠️ **الحدث:** حاول الوصول إلى ({attempt_type})\n"
            f"📝 **التفاصيل:** `{attempt_data}`"
        )
        try:
            if bot:
                await smart_notify(bot, NETWORK_OWNER_ID, alert_msg)
                await smart_notify(bot, ADMIN_ID, alert_msg)
        except Exception as e:
            # 🌟 منع الأخطاء الصامتة
            logging.error(f"فشل إرسال الإنذار الأمني: {e}")

router.callback_query.middleware(SecurityGuardMiddleware())
router.message.middleware(SecurityGuardMiddleware())

# ================= الحماية من السبام (Rate Limiting) =================
import time
from cachetools import TTLCache

USER_LAST_ACTION = TTLCache(maxsize=10000, ttl=1.0)

class AntiSpamMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        
        if user_id in USER_LAST_ACTION:
            if isinstance(event, types.CallbackQuery):
                await event.answer("⚠️ مهلاً! الرجاء عدم الضغط بسرعة.", show_alert=False)
            return 
        
        USER_LAST_ACTION[user_id] = time.time()
        return await handler(event, data)

router.callback_query.middleware(AntiSpamMiddleware())
router.message.middleware(AntiSpamMiddleware())

# ================= الحالات (FSM) =================
class ManageCardsFlow(StatesGroup):
    waiting_for_new_name = State()
    waiting_for_cost_price = State()
    waiting_for_new_price = State()
    waiting_for_retail_price = State()

class EditCardFlow(StatesGroup):
    waiting_for_cost = State()
    waiting_for_wholesale = State()
    waiting_for_retail = State()

class ReceiveCardsFlow(StatesGroup):
    waiting_for_type = State()
    waiting_for_quantity = State()

class GiveCardsFlow(StatesGroup):
    waiting_for_client = State()
    waiting_for_type = State()
    waiting_for_quantity = State()

class DirectSaleStrictFlow(StatesGroup):
    waiting_for_type = State()
    waiting_for_quantity = State()
    waiting_for_cash = State()
    waiting_for_secret_override = State()

class CollectMoneyFlow(StatesGroup):
    waiting_for_client = State()
    waiting_for_amount = State()
    waiting_for_confirmation = State()

class PayNetworkFlow(StatesGroup):
    waiting_for_amount = State()

class AddExpenseFlow(StatesGroup):
    waiting_for_amount = State()
    waiting_for_details = State()

class OfflineClientFlow(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()

class DamagedCardsFlow(StatesGroup):
    waiting_for_amount = State()

class AgentCommissionFlow(StatesGroup):
    waiting_for_amount = State()

class AgentBroadcastFlow(StatesGroup):
    waiting_for_message = State()

class OwnerBroadcastFlow(StatesGroup):
    waiting_for_message = State()

class PromoFlow(StatesGroup): # 👈 حالة لوحة العروض
    waiting_for_promo = State()
    waiting_for_action = State() # 👈 الحالة الجديدة لانتظار قرار الوكيل (إضافة أم استبدال)

class ReturnClientFlow(StatesGroup):
    waiting_for_client = State()
    waiting_for_type = State()
    waiting_for_quantity = State()

class ReturnNetworkFlow(StatesGroup):
    waiting_for_type = State()
    waiting_for_quantity = State()

class ReturnDirectFlow(StatesGroup):
    waiting_for_type = State()
    waiting_for_quantity = State()

class FieldVisitFlow(StatesGroup):
    waiting_for_card_count = State()
    waiting_for_inventory = State()
    waiting_for_cash = State()
    waiting_for_secret_override = State()

class AgentCompSettingsFlow(StatesGroup):
    waiting_for_value = State()

class CustomReportFlow(StatesGroup):
    waiting_for_start_date = State()
    waiting_for_end_date = State()
    
class DetailedLedgerFlow(StatesGroup):
    waiting_for_client = State()
    waiting_for_dates = State()

class CardDistributionFlow(StatesGroup):
    waiting_for_dates = State()
    
class PreAuditLedgerFlow(StatesGroup):
    waiting_for_dates = State()
    
class DishAlignmentFlow(StatesGroup):
    waiting_for_tx_location = State()
    waiting_for_rx_location = State()
    
class CreditLimitFlow(StatesGroup):
    waiting_for_limit = State()
    
class AdminReplyFlow(StatesGroup):
    waiting_for_reply_msg = State()
    
class LedgerReconciliationFlow(StatesGroup):
    waiting_for_client = State()
    waiting_for_dates = State()
    waiting_for_image = State()
    
class AIDailyEntryFlow(StatesGroup):
    waiting_for_client = State()
    waiting_for_image = State()
    waiting_for_confirm = State()

class AIMonthlyAuditFlow(StatesGroup):
    waiting_for_dates = State()
    reviewing_client = State()
     
class ManagerSendCardsFlow(StatesGroup):
    waiting_for_type = State()
    waiting_for_quantity = State()

class AgentReceiveBlindFlow(StatesGroup):
    waiting_for_type = State()
    waiting_for_quantity = State()
    
class ComprehensiveTransferFlow(StatesGroup):
    waiting_for_sender = State()
    waiting_for_receiver = State()
    waiting_for_type = State()
    waiting_for_card_type = State()
    waiting_for_amount_or_qty = State()
    
class RestoreBackupFlow(StatesGroup):
    waiting_for_document = State()
    
class AgentWithdrawProfitFlow(StatesGroup):
    waiting_for_amount = State()
    
class RenameCardFlow(StatesGroup):
    waiting_for_new_name = State()

class SecurityVaultFlow(StatesGroup):
    waiting_for_format_pin = State()
    waiting_for_restore_pin = State()
    waiting_for_backup_pin = State()

class AgentQuickActionsFlow(StatesGroup):
    waiting_for_rename_data = State()
    waiting_for_phone_data = State()
    waiting_for_region_data = State()
    waiting_for_close_account_id = State()
    waiting_for_demote_id = State()
    waiting_for_link_account_data = State()
    waiting_for_ecard_on_id = State()
    waiting_for_ecard_off_id = State()
    waiting_for_wa_on_id = State()
    waiting_for_wa_off_id = State()
    waiting_for_debt_data = State()
    waiting_for_inv_data = State()
    waiting_for_gm_debt_data = State()
    waiting_for_discount_gm_data = State()
    waiting_for_add_t_cash_data = State()
    waiting_for_edit_date_data = State()
    waiting_for_move_today_data = State()
    waiting_for_test_wa_phone = State()
    waiting_for_delete_report_month = State()
    waiting_for_rehab_id = State()
    waiting_for_ban_ip = State()
    waiting_for_check_cash_id = State() # 👈 الحالة الجديدة
    

# كلاس وهمي لتمرير النصوص للدوال القديمة
class FakeCommand:
    def __init__(self, args):
        self.args = args
     
# ================= دوال مساعدة =================
async def get_cards_keyboard(prefix, category="all"):
    kb = []
    if database.pool:
        async with database.pool.acquire() as conn:
            # 🌟 الإصلاح الأمني: جلب الكروت الفعالة فقط (تجاهل الكروت المحذوفة وهمياً)
            cards = await conn.fetch("SELECT card_type FROM inventory WHERE is_active = TRUE")
            row = []
            for c in cards:
                ctype = c["card_type"]
                is_ecard = 'إلكتروني' in ctype or 'الكتروني' in ctype
                
                # الفلترة الذكية
                if category == "physical" and is_ecard: continue
                if category == "electronic" and not is_ecard: continue
                
                row.append(InlineKeyboardButton(text=ctype, callback_data=f"{prefix}_{ctype}"))
                if len(row) == 2: # ترتيب الأزرار (زرين في كل سطر)
                    kb.append(row)
                    row = []
            if row: kb.append(row)
    kb.append([InlineKeyboardButton(text="🔙 العودة للرئيسية", callback_data="cancel_action")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_pagination_buttons(page: int, total_pages: int, callback_prefix: str):
    """دالة ذكية لإنشاء أزرار التنقل بين الصفحات لتقليل التكرار"""
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ السابق", callback_data=f"{callback_prefix}_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="التالي ➡️", callback_data=f"{callback_prefix}_{page+1}"))
    return nav_buttons
    
def get_pagination_math(total_items: int, page: int, items_per_page: int) -> tuple[int, int]:
    """دالة ذكية لحساب عدد الصفحات ونقطة التخطي (Offset) لمنع تكرار الكود"""
    if total_items == 0:
        return 1, 0
    total_pages = (total_items + items_per_page - 1) // items_per_page
    offset = page * items_per_page
    return total_pages, offset
    
async def get_manage_cards_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ إضافة فئة جديدة", callback_data="add_card_type")],
        [InlineKeyboardButton(text="✏️ تعديل سعر فئة", callback_data="edit_card_price_menu")],
        [InlineKeyboardButton(text="📝 تعديل اسم فئة", callback_data="rename_card_type_menu")],
        [InlineKeyboardButton(text="🗑️ حذف فئة", callback_data="delete_card_type_menu")],
        [InlineKeyboardButton(text="🔙 العودة لإدارة الكروت", callback_data="admin_settings_menu")]
    ])

# ================= لوحات المفاتيح (الاحترافية) =================
async def get_main_admin_keyboard():
    # جلب بيانات حية للأزرار
    pending_orders = 0
    total_debt = 0
    if database.pool:
        async with database.pool.acquire() as conn:
            debt_val = await conn.fetchval("SELECT COALESCE(SUM(debt), 0) FROM users WHERE role = 'client'")
            total_debt = int(debt_val) if debt_val else 0
            
            pending_val = await conn.fetchval("SELECT COUNT(*) FROM pending_orders WHERE status = 'pending'")
            pending_orders = int(pending_val) if pending_val else 0

    btn_ops_text = f"📦 العمليات والطلبات ({pending_orders} معلق)" if pending_orders > 0 else "📦 العمليات والطلبات"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_ops_text, callback_data="admin_operations_menu")],
        [InlineKeyboardButton(text=f"💰 المالية (ديون: {total_debt})", callback_data="admin_finance_menu"), 
         InlineKeyboardButton(text="📊 التقارير والجرد", callback_data="admin_reports_menu")],
        [InlineKeyboardButton(text="⚙️ الإعدادات", callback_data="admin_settings_menu"),
         InlineKeyboardButton(text="🔒 الخصوصية", callback_data="admin_privacy_settings")]
    ])
    
async def get_quick_actions_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 إدارة العملاء", callback_data="qa_cat_clients"),
         InlineKeyboardButton(text="💰 المالية والمخزون", callback_data="qa_cat_finance")],
        [InlineKeyboardButton(text="💬 إدارة الواتساب", callback_data="qa_cat_whatsapp"),
         InlineKeyboardButton(text="⚙️ أوامر النظام", callback_data="qa_cat_system")],
        [InlineKeyboardButton(text="🔙 رجوع للإعدادات", callback_data="admin_settings_menu")]
    ])

async def get_qa_clients_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ تعديل اسم", callback_data="qa_cmd_rename"),
         InlineKeyboardButton(text="📱 ربط واتساب", callback_data="qa_cmd_phone")],
        [InlineKeyboardButton(text="📍 تحديد منطقة", callback_data="qa_cmd_region"),
         InlineKeyboardButton(text="🔗 دمج حساب أوفلاين", callback_data="qa_cmd_link_account")],
        [InlineKeyboardButton(text="?? تصفية حساب", callback_data="qa_cmd_close_account"),
         InlineKeyboardButton(text="⬇️ تحويل لزائر", callback_data="qa_cmd_demote")],
        [InlineKeyboardButton(text="🟢 تفعيل كروت إلكترونية", callback_data="qa_cmd_ecard_on"),
         InlineKeyboardButton(text="🔴 إيقاف كروت إلكترونية", callback_data="qa_cmd_ecard_off")],
        [InlineKeyboardButton(text="🟢 تفعيل إشعارات الواتساب", callback_data="qa_cmd_wa_on"),
         InlineKeyboardButton(text="🔴 إيقاف إشعارات الواتساب", callback_data="qa_cmd_wa_off")],
        [InlineKeyboardButton(text="🛡️ إعادة تأهيل بقالة (طرد هكر)", callback_data="qa_cmd_rehab")], # 👈 الزر الجديد
        [InlineKeyboardButton(text="🔙 رجوع للأوامر السريعة", callback_data="agent_quick_actions")]
    ])

async def get_qa_finance_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 دين سابق (عميل)", callback_data="qa_cmd_set_debt"),
         InlineKeyboardButton(text="📦 مخزون سابق (عميل)", callback_data="qa_cmd_set_inv")],
        [InlineKeyboardButton(text="👑 دين سابق (للمدير)", callback_data="qa_cmd_set_gm_debt"),
         InlineKeyboardButton(text="🎁 خصم من المدير", callback_data="qa_cmd_discount_gm")],
        [InlineKeyboardButton(text="💸 ضخ رأس مال تسديدات", callback_data="qa_cmd_add_t_cash"),
         InlineKeyboardButton(text="⏪ تراجع (قيد عكسي)", callback_data="qa_cmd_undo")],
        [InlineKeyboardButton(text="📅 تعديل تاريخ عملية", callback_data="qa_cmd_edit_date"),
         InlineKeyboardButton(text="🔄 نقل عمليات اليوم", callback_data="qa_cmd_move_today")],
        [InlineKeyboardButton(text="🔙 رجوع للأوامر السريعة", callback_data="agent_quick_actions")]
    ])

async def get_qa_whatsapp_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 عرض عملاء الواتساب", callback_data="qa_cmd_wa_list")],
        [InlineKeyboardButton(text="🧪 فحص رقم (Test)", callback_data="qa_cmd_test_wa")],
        [InlineKeyboardButton(text="🔗 ربط السيرفر (Webhook)", callback_data="qa_cmd_set_wa_webhook")],
        [InlineKeyboardButton(text="📡 اختبار السيرفر", callback_data="qa_cmd_test_my_webhook")],
        [InlineKeyboardButton(text="🔙 رجوع للأوامر السريعة", callback_data="agent_quick_actions")]
    ])

async def get_qa_system_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚨 إغلاق طارئ (Lockdown)", callback_data="qa_cmd_lockdown"),
         InlineKeyboardButton(text="🟢 فك الإغلاق الشامل", callback_data="qa_cmd_unlock")],
        [InlineKeyboardButton(text="🚫 حظر آيبي (IP Ban)", callback_data="qa_cmd_ban_ip"),
         InlineKeyboardButton(text="✅ فك حظر الجميع", callback_data="qa_cmd_unban_all")],
        [InlineKeyboardButton(text="🚨 فورمات شامل", callback_data="qa_cmd_factory_reset"),
         InlineKeyboardButton(text="💾 استعادة نسخة", callback_data="qa_cmd_restore_backup")],
        [InlineKeyboardButton(text="🛠️ تحديث الجداول", callback_data="qa_cmd_setup_db"),
         InlineKeyboardButton(text="🚀 تحديث الفهارس", callback_data="qa_cmd_update_db")],
        [InlineKeyboardButton(text="🩺 فحص النظام", callback_data="qa_cmd_test_system"),
         InlineKeyboardButton(text="🔧 إصلاح حساب النظام", callback_data="qa_cmd_fix_sys")],
        
        # 👇 الأزرار الجديدة الخارقة للكاش 👇
        [InlineKeyboardButton(text="🔍 فحص الكاش الشامل", callback_data="qa_cmd_audit_all_cash"),
         InlineKeyboardButton(text="🧹 تنظيف الكاش الوهمي", callback_data="qa_cmd_clean_all_cash")],
        [InlineKeyboardButton(text="🔎 فحص كاش بقالة محددة", callback_data="qa_cmd_check_cash")],
         
        [InlineKeyboardButton(text="🧬 بصمة النظام (للمطابقة)", callback_data="qa_cmd_fingerprint"),
         InlineKeyboardButton(text="🗑️ حذف تقرير", callback_data="qa_cmd_delete_report")],
        [InlineKeyboardButton(text="⏱️ ضبط التوقيت", callback_data="qa_cmd_fix_timezone"),
         InlineKeyboardButton(text="🧹 تنظيف التنبيهات", callback_data="qa_cmd_clear_alerts")],
        [InlineKeyboardButton(text="🔄 مزامنة تواريخ العملاء", callback_data="qa_cmd_fix_all_joins")],
        [InlineKeyboardButton(text="📢 إعداد لوحة العروض", callback_data="qa_cmd_set_promo"),
         InlineKeyboardButton(text="🕵️‍♂️ فحص رسائل المدير", callback_data="qa_cmd_audit_manager")],
        
        # 👇 الزر الجديد للتحكم بقسم التسديدات 👇
        [InlineKeyboardButton(text="🔌 تشغيل/إيقاف قسم التسديدات", callback_data="qa_cmd_toggle_telecom_master")],
        
        [InlineKeyboardButton(text="🛑 وضع الصيانة", callback_data="toggle_maintenance_btn")],
        [InlineKeyboardButton(text="🔙 رجوع للأوامر السريعة", callback_data="agent_quick_actions")]
    ])

async def get_operations_submenu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 صندوق الطلبات المعلقة", callback_data="admin_view_pending_orders")], # 👈 الزر الجديد
        [InlineKeyboardButton(text="🔄 مركز التحويلات الشامل", callback_data="admin_transfer_center")],
        [InlineKeyboardButton(text="📥 استلام كروت من الإدارة", callback_data="admin_receive"),
         InlineKeyboardButton(text="📤 تسليم كروت لعميل", callback_data="admin_give")],
        [InlineKeyboardButton(text="🛒 مبيعات الكاش المباشرة", callback_data="admin_direct_sales_menu")],
        [InlineKeyboardButton(text="📸 إدخال بالصورة (يومي)", callback_data="ai_daily_entry"),
         InlineKeyboardButton(text="🔄 قسم المرتجعات والتوالف", callback_data="admin_returns_menu")],
        [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu"),
         InlineKeyboardButton(text="❌ إغلاق", callback_data="cancel_action")]
    ])

async def get_reports_submenu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 مركز التصدير والنسخ الاحتياطي", callback_data="export_and_backup_menu")],
        [InlineKeyboardButton(text="🕵️‍♂️ ملف العميل الشامل", callback_data="client_profile_menu"),
         InlineKeyboardButton(text="📦 جرد المخزون", callback_data="admin_inventory")],
        [InlineKeyboardButton(text="📊 كشف ديون السوق", callback_data="admin_market_debt_inventory_report"),
         InlineKeyboardButton(text="📜 كشف حساب تفصيلي", callback_data="admin_detailed_ledger")],
        [InlineKeyboardButton(text="🕵️‍♂️ معالج المطابقة الشهرية", callback_data="ai_monthly_audit"),
         InlineKeyboardButton(text="📈 تقرير حركة الكروت", callback_data="admin_card_distribution")],
        [InlineKeyboardButton(text="📊 التقرير الشهري", callback_data="admin_report"),
         InlineKeyboardButton(text="📅 تقرير مخصص (PDF)", callback_data="custom_pdf_report")],
        [InlineKeyboardButton(text="📑 دفتر المراجعة التفصيلي", callback_data="admin_pre_audit_ledger"),
         InlineKeyboardButton(text="📂 أرشيف التقارير", callback_data="admin_reports_archive")],
        [InlineKeyboardButton(text="🛡️ سجل المراقبة والتدقيق", callback_data="admin_audit_logs")], # 👈 الزر الجديد
        [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu"),
         InlineKeyboardButton(text="❌ إغلاق", callback_data="cancel_action")]
    ])

async def get_finance_submenu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 تحصيل ديون البقالات", callback_data="admin_collect")],
        [InlineKeyboardButton(text="💵 تفاصيل الكاش المتوفر", callback_data="admin_view_cash")],
        [InlineKeyboardButton(text="💸 تسديد للمدير العام", callback_data="admin_pay"),
         InlineKeyboardButton(text="📉 مصروفات وحوالات", callback_data="admin_expenses")],
        [InlineKeyboardButton(text="💎 محفظة أرباح الوكيل", callback_data="admin_agent_wallet"),
         InlineKeyboardButton(text="💸 سحب أرباحي", callback_data="admin_withdraw_profit")],
        [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu"),
         InlineKeyboardButton(text="❌ إغلاق", callback_data="cancel_action")]
    ])

async def get_settings_submenu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛠️ الأوامر السريعة (بديل الكتابة)", callback_data="agent_quick_actions")],
        [InlineKeyboardButton(text="⚙️ إدارة فئات الكروت", callback_data="admin_manage_cards")],
        [InlineKeyboardButton(text="📥 إدخال أرصدة افتتاحية", callback_data="admin_initial_data"),
         InlineKeyboardButton(text="🤝 مستحقات الوكيل", callback_data="admin_comp_settings")],
        [InlineKeyboardButton(text="👴 عملاء أوفلاين", callback_data="view_offline_clients"),
         InlineKeyboardButton(text="📢 إرسال تعميم", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📡 أداة ضبط صحون الشبكة", callback_data="tool_align_dish"),
         InlineKeyboardButton(text="🛑 سقف المديونية", callback_data="admin_credit_limits")],
        [InlineKeyboardButton(text="🛑 تشغيل/إيقاف وضع الصيانة", callback_data="toggle_maintenance_btn")],
        [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu"),
         InlineKeyboardButton(text="❌ إغلاق", callback_data="cancel_action")]
    ])
    
async def get_privacy_settings_keyboard():
    s_inv = await database.get_setting("share_inventory")
    s_agent = await database.get_setting("share_agent_inventory")
    s_debt = await database.get_setting("share_market_debt")
    s_cash = await database.get_setting("share_available_cash")
    s_report = await database.get_setting("share_monthly_report")
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"مخزوني: {'🟢 مفعل' if s_agent=='on' else '🔴 معطل'}", callback_data="toggle_share_agent_inventory"),
         InlineKeyboardButton(text=f"مخزون العملاء: {'🟢 مفعل' if s_inv=='on' else '🔴 معطل'}", callback_data="toggle_share_inventory")],
        [InlineKeyboardButton(text=f"ديون السوق: {'🟢 مفعل' if s_debt=='on' else '🔴 معطل'}", callback_data="toggle_share_market_debt"),
         InlineKeyboardButton(text=f"الكاش: {'🟢 مفعل' if s_cash=='on' else '🔴 معطل'}", callback_data="toggle_share_available_cash")],
        [InlineKeyboardButton(text=f"التقرير للمدير: {'🟢 مفعل' if s_report=='on' else '🔴 معطل'}", callback_data="toggle_share_monthly_report")],
        [InlineKeyboardButton(text="🔙 رجوع للإعدادات", callback_data="admin_settings_menu"),
         InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")]
    ])

async def get_owner_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 المراقبة المالية والمخزون", callback_data="owner_monitoring_menu")],
        [InlineKeyboardButton(text="📑 التقارير والشكاوي", callback_data="owner_reports_menu"),
         InlineKeyboardButton(text="⚡ الأوامر العاجلة", callback_data="owner_urgent_commands_menu")]
    ])

async def get_owner_monitoring_submenu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧠 المستشار المالي الذكي (AI)", callback_data="owner_ai_advisor")],
        [InlineKeyboardButton(text="📦 مخزون الوكيل", callback_data="owner_view_agent_inventory"),
         InlineKeyboardButton(text="👀 مخزون البقالات", callback_data="owner_view_inventory")],
        [InlineKeyboardButton(text="👥 ديون السوق", callback_data="owner_view_market_debt"),
         InlineKeyboardButton(text="💵 الكاش المتوفر", callback_data="owner_view_cash")],
        [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="owner_main_menu"),
         InlineKeyboardButton(text="❌ إغلاق", callback_data="cancel_action")]
    ])

async def get_owner_reports_submenu_keyboard():
    report_approved = await database.get_setting("share_monthly_report") == "on"
    report_button_text = "📊 سحب التقرير الشهري" if report_approved else "📊 التقرير الشهري (غير معتمد)"
    report_callback_data = "owner_pull_report" if report_approved else "owner_report_not_approved"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=report_button_text, callback_data=report_callback_data)],
        [InlineKeyboardButton(text="📂 أرشيف التقارير", callback_data="owner_reports_archive"),
         InlineKeyboardButton(text="❓ الشكاوي والاقتراحات", callback_data="owner_complaints_suggestions")],
        [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="owner_main_menu"),
         InlineKeyboardButton(text="❌ إغلاق", callback_data="cancel_action")]
    ])

async def get_owner_urgent_commands_submenu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 إرسال كروت للوكيل", callback_data="owner_send_cards")],
        [InlineKeyboardButton(text="💸 طلب تحويل كاش", callback_data="owner_request_cash"),
         InlineKeyboardButton(text="📢 إرسال تعميم عاجل", callback_data="owner_broadcast")],
        [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="owner_main_menu"),
         InlineKeyboardButton(text="❌ إغلاق", callback_data="cancel_action")]
    ])
    
# ================= الأوامر السرية (لك كوكيل) =================
@router.message(Command("cancel"))
async def cancel_any_state(message: types.Message, state: FSMContext):
    """أمر لإلغاء أي عملية معلقة وإرجاع البوت للوضع الطبيعي"""
    await state.clear()
    user_id = message.from_user.id
    
    if user_id == int(NETWORK_OWNER_ID):
        kb = await get_owner_main_keyboard()
        await message.answer("🚫 **تم الإلغاء.**\n👑 **لوحة تحكم الإدارة العامة**", reply_markup=kb)
    elif user_id == ADMIN_ID:
        kb = await get_main_admin_keyboard()
        await message.answer("🚫 **تم الإلغاء.**\n👨‍💻 **لوحة تحكم الوكيل**", reply_markup=kb)
    else:
        # توجيه العميل العادي أو الطياري لقائمته الصحيحة
        await message.answer("🚫 **تم الإلغاء.**\nلإظهار القائمة الرئيسية الخاصة بك، اضغط هنا 👉 /start")

# ================= أمر اختبار النسخ الاحتياطي السحابي =================
from aiogram.filters import Command
@router.message(Command("test_backup"))
@router.message(F.text.in_(["/test_backup", "test_backup", "test backup"]))
async def test_cloud_backup_command(message: types.Message):
    if message.from_user.id != ADMIN_ID: return

    wait_msg = await message.answer("⏳ جاري ضغط وتشفير قاعدة البيانات وإرسالها للإيميل...")
    
    try:
        from cloud_backup import execute_scheduled_cloud_backup
        await execute_scheduled_cloud_backup(message.bot)
        await wait_msg.edit_text("✅ **تم الإرسال بنجاح!** 🚀\nافتح إيميلك الآن (وتفقد مجلد Spam إذا لم تجدها في الوارد).")
    except Exception as e:
        await wait_msg.edit_text(f"❌ **فشل الإرسال!**\nالسبب: `{str(e)}`")

@router.message(Command("factory_reset_100"))
async def request_hard_reset(message: types.Message, state: FSMContext):
    """الخطوة 1: طلب الفورمات وإظهار التحذير"""
    if message.from_user.id != ADMIN_ID: return
    
    await message.answer(
        "🚨 **تحذير نووي خطير جداً!** 🚨\n\n"
        "أنت على وشك مسح **كل شيء** في النظام (العملاء، الديون، المخزون، العمليات، الأرباح).\n"
        "هذه العملية لا يمكن التراجع عنها أبداً!\n\n"
        "🔐 **لتأكيد العملية، الرجاء إدخال الرمز السري (PIN) المكون من 6 أرقام:**\n"
        "*(أو أرسل /cancel للإلغاء فوراً)*"
    )
    await state.set_state(SecurityVaultFlow.waiting_for_format_pin)

@router.message(SecurityVaultFlow.waiting_for_format_pin)
async def execute_hard_reset(message: types.Message, state: FSMContext):
    """الخطوة 2: التحقق من الرمز والتنفيذ"""
    if message.text.strip() != SYSTEM_VAULT_PIN:
        await state.clear()
        return await message.answer("❌ **رمز خاطئ!** تم إلغاء عملية الفورمات لحماية النظام.")
        
    status_msg = await message.answer("✅ تم قبول الرمز السري. ⏳ جاري عمل فورمات شامل للنظام...")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            try:
                async with conn.transaction():
                    # 1. مسح جميع البيانات وتصفير العدادات (IDs)
                    await conn.execute("""
                        TRUNCATE TABLE 
                            transactions, inventory, client_inventory, users, 
                            learned_facts, agent_profits, reports_archive,
                            pending_orders, pending_shipments, client_customers,
                            client_sales, client_customer_ledger, web_notifications, 
                            chat_messages, push_subscriptions, electronic_cards,
                            telecom_packages, pricing_tiers, telecom_profits, telecom_transactions,
                            agent_personal_customers, agent_personal_ledger, agent_wallet, banned_ips,
                            audit_logs, idempotency_keys, system_blackbox
                        RESTART IDENTITY CASCADE;
                    """)
                    
                    # 🌟 الحماية الفولاذية: تصفير عداد عملاء الأوفلاين ليعود للرقم الافتراضي
                    try:
                        await conn.execute("ALTER SEQUENCE offline_users_seq RESTART WITH 9990000;")
                    except Exception:
                        pass
                    
                    # 2. إعادة تهيئة محفظة الوكيل بصفر (تم دمج الإدخال والتحديث في خطوة واحدة نظيفة)
                    await conn.execute("""
                        INSERT INTO agent_wallet (id, telecom_balance, manager_cash, telecom_cash, realized_profit) 
                        VALUES (1, 0.0, 0.0, 0.0, 0.0) 
                        ON CONFLICT (id) DO NOTHING;
                    """)
                    
                    # 3. إعادة ضبط الإعدادات للافتراضي
                    await conn.execute("UPDATE settings SET value = 'off' WHERE key IN ('share_inventory', 'share_available_cash', 'share_monthly_report', 'maintenance_mode', 'archive_channel_id')")
                    await conn.execute("UPDATE settings SET value = 'on' WHERE key IN ('share_agent_inventory', 'share_market_debt')")
                    await conn.execute("UPDATE settings SET value = 'none' WHERE key = 'agent_comp_type'")
                    await conn.execute("UPDATE settings SET value = '0' WHERE key = 'agent_comp_value'")
                    
                    # 4. إعادة إنشاء حساب النظام الأساسي
                    await conn.execute("""
                        INSERT INTO users (user_id, name, role, debt) 
                        VALUES (0, 'النظام / مبيعات مباشرة', 'system', 0.0) 
                        ON CONFLICT (user_id) DO NOTHING;
                    """)

                await status_msg.edit_text("✅ **تم تصفير النظام بالكامل بنجاح!** 🧹\nالنظام الآن جديد كلياً وجاهز للعمل من الصفر. 🚀")
            except Exception as e:
                await status_msg.edit_text(f"❌ حدث خطأ أثناء الفورمات:\n`{e}`")
    
    await state.clear()

@router.message(Command("restore_backup"))
async def request_restore_backup(message: types.Message, state: FSMContext):
    """الخطوة 1: طلب الاستعادة وإظهار التحذير"""
    if message.from_user.id != ADMIN_ID: return
    
    await message.answer(
        "⚠️ **تحذير خطير:**\n"
        "استعادة النسخة الاحتياطية ستقوم **بمسح جميع البيانات الحالية في النظام** واستبدالها بالبيانات الموجودة في الملف.\n\n"
        "🔐 **لتأكيد أنك بكامل وعيك، الرجاء إدخال الرمز السري (PIN):**\n"
        "*(أو أرسل /cancel للإلغاء)*"
    )
    await state.set_state(SecurityVaultFlow.waiting_for_restore_pin)

@router.message(SecurityVaultFlow.waiting_for_restore_pin)
async def verify_restore_pin(message: types.Message, state: FSMContext):
    """الخطوة 2: التحقق من الرمز ثم طلب الملف"""
    if message.text.strip() != SYSTEM_VAULT_PIN:
        await state.clear()
        return await message.answer("❌ **رمز خاطئ!** تم إلغاء عملية الاستعادة.")
        
    await message.answer("✅ **تم فتح الخزنة.**\nالرجاء إرسال ملف الإكسل (النسخة الاحتياطية الشاملة) الآن:")
    # نقله للحالة القديمة التي تستقبل الملف
    await state.set_state(RestoreBackupFlow.waiting_for_document)

@router.message(RestoreBackupFlow.waiting_for_document, F.document)
async def process_restore_backup(message: types.Message, state: FSMContext, bot: Bot):
    if not message.document.file_name.endswith('.xlsx'):
        return await message.answer("❌ الرجاء إرسال ملف بصيغة Excel (.xlsx) فقط.")
    if message.document.file_size > 10 * 1024 * 1024:
        return await message.answer("❌ رفض أمني: حجم الملف كبير جداً! (الحد الأقصى 10 ميجابايت)")
        
    wait_msg = await message.answer("⏳ جاري تحليل الملف واستعادة النظام الشامل بالكامل... الرجاء عدم إرسال أي شيء حتى أنتهي.")
    
    # دالة مساعدة لتحويل النصوص إلى تواريخ بأمان
    def parse_date(d_str):
        try: return datetime.strptime(str(d_str)[:16], '%Y-%m-%d %H:%M')
        except: return datetime.now()

    # دالة مساعدة للقراءة الآمنة للأعمدة
    def safe_get(row, index, default=None):
        return row[index] if row and index < len(row) and row[index] is not None else default

    # 🌟 دالة التنظيف الفولاذية: تحول الفراغات إلى NULL حقيقي لمنع انهيار قاعدة البيانات
    def clean_val(val):
        if val is None or str(val).strip() == '' or str(val).strip().lower() == 'none':
            return None
        return str(val).strip()

    try:
           
        # 🌟 1. قراءة الملف وتحليله (خارج القفل المحاسبي لمنع شلل قاعدة البيانات)
        file = await bot.get_file(message.document.file_id)
        file_bytes = await bot.download_file(file.file_path)
        
        wb = await asyncio.to_thread(openpyxl.load_workbook, file_bytes)
        
        required_sheets = ["العملاء والديون", "المخزون العام", "سجل العمليات"]
        missing_sheets = [sheet for sheet in required_sheets if sheet not in wb.sheetnames]
        
        if missing_sheets:
            await wait_msg.edit_text(f"❌ **عذراً، هذا الملف ليس نسخة احتياطية صحيحة!**\nالملف يفتقد للصفحات الأساسية: {', '.join(missing_sheets)}\n\nتم إلغاء العملية لحماية بياناتك.")
            return await state.clear()

        # 🌟 2. فتح القفل المحاسبي الصارم للتنفيذ النهائي
        if database.pool:
            async with database.pool.acquire() as conn:
                async with conn.transaction():

                    # 1. تصفير جميع الجداول بالكامل
                    await conn.execute("""
                        TRUNCATE TABLE 
                            transactions, inventory, client_inventory, users, 
                            learned_facts, agent_profits, reports_archive,
                            pending_orders, pending_shipments, client_customers,
                            client_sales, client_customer_ledger, web_notifications, 
                            chat_messages, push_subscriptions, electronic_cards,
                            telecom_packages, pricing_tiers, telecom_profits, telecom_transactions,
                            agent_personal_customers, agent_personal_ledger, agent_wallet, banned_ips,
                            audit_logs, idempotency_keys, system_blackbox
                        RESTART IDENTITY CASCADE;
                    """)

                    # إعادة إنشاء حساب النظام الأساسي
                    await conn.execute("INSERT INTO users (user_id, name, role, debt) VALUES (0, 'النظام / مبيعات مباشرة', 'system', 0.0) ON CONFLICT DO NOTHING;")
                    
                    # 2. استعادة العملاء والديون (محدثة لتتطابق مع النسخة الاحتياطية الجديدة 100%)
                    if "العملاء والديون" in wb.sheetnames:
                        for row in wb["العملاء والديون"].iter_rows(min_row=2, values_only=True):
                            if row and len(row) > 0 and row[0] is not None and str(row[0]).isdigit():
                                uid = int(row[0])
                                if uid == 0: continue 
                                
                                name = str(safe_get(row, 1, ''))
                                role = str(safe_get(row, 2, 'client'))
                                debt = Decimal(str(safe_get(row, 3, 0) or 0))
                                old_debt = Decimal(str(safe_get(row, 4, 0) or 0))
                                pending_profit = Decimal(str(safe_get(row, 5, 0) or 0))
                                credit_limit = Decimal(str(safe_get(row, 6, 50000) or 50000))
                                region = str(safe_get(row, 7, ''))
                                phone = str(safe_get(row, 8, ''))
                                wa_status = str(safe_get(row, 9, 'off'))
                                promised_payment = Decimal(str(safe_get(row, 10, 0) or 0))
                                joined_at = parse_date(safe_get(row, 11, ''))
                                telecom_balance = Decimal(str(safe_get(row, 12, 0) or 0))
                                telecom_debt = Decimal(str(safe_get(row, 13, 0) or 0))
                                telecom_status = str(safe_get(row, 14, 'off'))
                                ecard_status = str(safe_get(row, 15, 'off'))
                                physical_status = str(safe_get(row, 16, 'on'))
                                telecom_credit_limit = Decimal(str(safe_get(row, 17, 20000) or 20000))
                                pos_cash_collected = Decimal(str(safe_get(row, 18, 0) or 0))
                                telecom_pos_cash = Decimal(str(safe_get(row, 19, 0) or 0)) # 🌟 العمود الجديد
                                is_vip = str(safe_get(row, 20, 'False')).lower() == 'true'
                                special_discount = Decimal(str(safe_get(row, 21, 0) or 0))
                                account_status = str(safe_get(row, 22, 'active'))
                                
                                limit_set_by = str(safe_get(row, 23, 'agent'))
                                suspend_reason = clean_val(safe_get(row, 24, None))
                                debt_schedule_pct = Decimal(str(safe_get(row, 25, 0) or 0))
                                last_sales_log = parse_date(safe_get(row, 26, '')) if safe_get(row, 26, '') else None
                                device_id = clean_val(safe_get(row, 27, None))
                                cashiers_raw = clean_val(safe_get(row, 28, '[]'))
                                cashiers = cashiers_raw if cashiers_raw else '[]'
                                web_password = str(safe_get(row, 29, '1234'))
                                
                                await conn.execute('''
                                    INSERT INTO users (
                                        user_id, name, role, debt, old_debt, pending_profit, credit_limit, 
                                        region, phone, wa_status, promised_payment, joined_at, telecom_balance, 
                                        telecom_debt, telecom_status, ecard_status, physical_status, 
                                        telecom_credit_limit, pos_cash_collected, telecom_pos_cash, is_vip, special_discount, 
                                        account_status, limit_set_by, suspend_reason, debt_schedule_pct, 
                                        last_sales_log, device_id, cashiers, web_password
                                    ) 
                                    VALUES (
                                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, 
                                        $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29::jsonb, $30
                                    )
                                ''', uid, name, role, debt, old_debt, pending_profit, credit_limit, 
                                     region, phone, wa_status, promised_payment, joined_at, telecom_balance, 
                                     telecom_debt, telecom_status, ecard_status, physical_status, 
                                     telecom_credit_limit, pos_cash_collected, telecom_pos_cash, is_vip, special_discount, 
                                     account_status, limit_set_by, suspend_reason, debt_schedule_pct, 
                                     last_sales_log, device_id, cashiers, web_password)

                    # 3. استعادة المخزون العام
                    if "المخزون العام" in wb.sheetnames:
                        for row in wb["المخزون العام"].iter_rows(min_row=2, values_only=True):
                            if row and len(row) > 0 and row[0] is not None:
                                if str(row[0]) == "نوع الكرت": continue
                                
                                card_type = str(row[0])
                                qty = int(safe_get(row, 1, 0) or 0)
                                cost_price = Decimal(str(safe_get(row, 2, 0) or 0))
                                price = Decimal(str(safe_get(row, 3, 0) or 0))
                                retail_price = Decimal(str(safe_get(row, 4, 0) or 0))
                                is_active = str(safe_get(row, 5, 'True')).lower() == 'true'
                                
                                await conn.execute('''
                                    INSERT INTO inventory (card_type, quantity, cost_price, price, retail_price, is_active) 
                                    VALUES ($1, $2, $3, $4, $5, $6)
                                ''', card_type, qty, cost_price, price, retail_price, is_active)
                                            # 4. استعادة مخزون البقالات
                    if "مخزون البقالات" in wb.sheetnames:
                        for row in wb["مخزون البقالات"].iter_rows(min_row=2, values_only=True):
                            if row and len(row) > 0 and row[0] is not None and str(row[0]).isdigit():
                                uid = int(row[0])
                                card_type = str(safe_get(row, 1, ''))
                                qty = int(safe_get(row, 2, 0) or 0)
                                await conn.execute('INSERT INTO client_inventory (user_id, card_type, quantity) VALUES ($1, $2, $3)', uid, card_type, qty)

                    # 5. استعادة سجل العمليات (محدثة بالكامل ومحمية - محسنة لتكون سريعة جداً)
                    if "سجل العمليات" in wb.sheetnames:
                        transactions_data = []
                        max_tx_id = 0 # 🌟 لحفظ أعلى رقم فاتورة لتحديث العداد
                        for row in wb["سجل العمليات"].iter_rows(min_row=2, values_only=True):
                            if row and len(row) > 1 and row[0] is not None and str(row[0]).isdigit():
                                tx_id = int(row[0]) # 🌟 استعادة رقم الفاتورة الأصلي
                                if tx_id > max_tx_id: max_tx_id = tx_id
                                
                                uid = int(safe_get(row, 1, 0) or 0)
                                type_ = str(safe_get(row, 2, ''))
                                amount = Decimal(str(safe_get(row, 3, 0) or 0))
                                details = str(safe_get(row, 4, ''))
                                date_val = parse_date(safe_get(row, 5, ''))
                                
                                # 🌟 الحماية الفولاذية للـ JSON: التأكد من صحة النص قبل إدخاله لقاعدة البيانات
                                structured_details_raw = clean_val(safe_get(row, 6, None))
                                structured_details = None
                                if structured_details_raw:
                                    try:
                                        import json
                                        json.loads(structured_details_raw) # اختبار صحة الـ JSON
                                        structured_details = structured_details_raw
                                    except:
                                        structured_details = None # تجاهل النص إذا كان تالفاً لمنع انهيار الاستعادة
                                        
                                idempotency_key = clean_val(safe_get(row, 7, None))
                                
                                is_reverted = str(safe_get(row, 8, 'False')).lower() == 'true'
                                wallet_type = str(safe_get(row, 9, 'manager'))
                                
                                transactions_data.append((tx_id, uid, type_, amount, details, date_val, structured_details, idempotency_key, is_reverted, wallet_type))
                        
                        # إدخال جميع العمليات دفعة واحدة مع أرقامها الأصلية
                        if transactions_data:
                            await conn.executemany('''
                                INSERT INTO transactions (id, user_id, type, amount, details, date, structured_details, idempotency_key, is_reverted, wallet_type) 
                                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10)
                            ''', transactions_data)
                            
                            # 🌟 تحديث العداد التلقائي لكي لا يحدث تعارض عند إضافة فاتورة جديدة بعد الاستعادة
                            if max_tx_id > 0:
                                await conn.execute(f"SELECT setval('transactions_id_seq', {max_tx_id})")

                    # 6. استعادة أرباح الوكيل
                    if "أرباح الوكيل" in wb.sheetnames:
                        for row in wb["أرباح الوكيل"].iter_rows(min_row=2, values_only=True):
                            if row and len(row) > 1 and row[0] is not None and str(row[0]).isdigit():
                                p_id = int(row[0]) # 🌟 استعادة الـ ID
                                amount = Decimal(str(safe_get(row, 1, 0) or 0))
                                details = str(safe_get(row, 2, ''))
                                date_val = parse_date(safe_get(row, 3, ''))
                                await conn.execute('INSERT INTO agent_profits (id, amount, details, date) VALUES ($1, $2, $3, $4)', p_id, amount, details, date_val)

                    # 7. استعادة ديون ومبيعات الزبائن (شاملة كشف الحساب)
                    if "ديون ومبيعات الزبائن" in wb.sheetnames:
                        mode = "debt"
                        for row in wb["ديون ومبيعات الزبائن"].iter_rows(min_row=1, values_only=True):
                            if not row or len(row) == 0 or row[0] is None: continue
                            if "مبيعات البقالات" in str(row[0]): mode = "sales"; continue
                            if "سجل حركات الزبائن" in str(row[0]): mode = "ledger"; continue
                            if "ديون الزبائن" in str(row[0]) or "رقم البقالة" in str(row[0]) or "---" in str(row[0]): continue
                            
                            if mode == "debt" and str(row[0]).isdigit():
                                uid = int(row[0])
                                c_name = str(safe_get(row, 1, ''))
                                debt = Decimal(str(safe_get(row, 2, 0) or 0))
                                await conn.execute('INSERT INTO client_customers (client_id, customer_name, debt) VALUES ($1, $2, $3)', uid, c_name, debt)
                            elif mode == "sales" and str(row[0]).isdigit():
                                uid = int(row[0])
                                c_type = str(safe_get(row, 1, ''))
                                qty = int(safe_get(row, 2, 0) or 0)
                                total_price = Decimal(str(safe_get(row, 3, 0) or 0))
                                profit = Decimal(str(safe_get(row, 4, 0) or 0))
                                sale_date = parse_date(safe_get(row, 5, ''))
                                await conn.execute('INSERT INTO client_sales (client_id, card_type, quantity, total_price, profit, sale_date) VALUES ($1, $2, $3, $4, $5, $6)', uid, c_type, qty, total_price, profit, sale_date)
                            elif mode == "ledger" and str(row[0]).isdigit():
                                uid = int(row[0])
                                c_name = str(safe_get(row, 1, ''))
                                l_type = str(safe_get(row, 2, ''))
                                amount = Decimal(str(safe_get(row, 3, 0) or 0))
                                details = str(safe_get(row, 4, ''))
                                l_date = parse_date(safe_get(row, 5, ''))
                                await conn.execute('INSERT INTO client_customer_ledger (client_id, customer_name, type, amount, details, date) VALUES ($1, $2, $3, $4, $5, $6)', uid, c_name, l_type, amount, details, l_date)

                    # 8. استعادة الطلبات والإرساليات
                    if "الطلبات والإرساليات" in wb.sheetnames:
                        for row in wb["الطلبات والإرساليات"].iter_rows(min_row=2, values_only=True):
                            if not row or len(row) == 0 or row[0] is None or "النوع" in str(row[0]): continue
                            
                            req_id = int(safe_get(row, 1, 0) or 0) # 🌟 استعادة الـ ID الأصلي
                            
                            if row[0] == "إرسالية إدارة":
                                status = str(safe_get(row, 2, ''))
                                m_items = str(safe_get(row, 3, '{}'))
                                c_date = parse_date(safe_get(row, 4, ''))
                                a_items = str(safe_get(row, 5, ''))
                                a_items = a_items if a_items else None
                                # 🌟 الحماية الفولاذية: إجبار قاعدة البيانات على قبول النص كـ JSONB
                                await conn.execute('INSERT INTO pending_shipments (id, status, manager_items, created_at, agent_items) VALUES ($1, $2, $3::jsonb, $4, $5::jsonb)', req_id, status, m_items, c_date, a_items)
                            elif row[0] == "طلب بقالة":
                                status = str(safe_get(row, 2, ''))
                                details = str(safe_get(row, 3, ''))
                                c_name = details.split(" - ")[0] if " - " in details else "عميل"
                                c_date = parse_date(safe_get(row, 4, ''))
                                order_items = str(safe_get(row, 5, '{}'))
                                total_price = Decimal(str(safe_get(row, 6, 0) or 0))
                                rem_text = str(safe_get(row, 7, ''))
                                user_id = int(safe_get(row, 8, 0) or 0)
                                await conn.execute('INSERT INTO pending_orders (id, user_id, client_name, new_order_text, status, created_at, order_items, total_price, remaining_text) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)', req_id, user_id, c_name, details, status, c_date, order_items, total_price, rem_text)

                    # 9. استعادة المحادثات والإشعارات
                    if "المحادثات والإشعارات" in wb.sheetnames:
                        mode = "chat"
                        for row in wb["المحادثات والإشعارات"].iter_rows(min_row=1, values_only=True):
                            if not row or len(row) == 0 or row[0] is None: continue
                            if "الإشعارات" in str(row[0]): mode = "notif"; continue
                            if "رقم البقالة" in str(row[0]) or "---" in str(row[0]) or "المعرف" in str(row[0]): continue
                            
                            if mode == "chat" and str(row[0]).isdigit():
                                msg_id = int(row[0]) # 🌟 استعادة الـ ID
                                uid = int(safe_get(row, 1, 0) or 0)
                                s_type = str(safe_get(row, 2, ''))
                                m_type = str(safe_get(row, 3, ''))
                                m_text = str(safe_get(row, 4, ''))
                                c_date = parse_date(safe_get(row, 5, ''))
                                is_read = str(safe_get(row, 6, 'False')).lower() == 'true'
                                await conn.execute('INSERT INTO chat_messages (id, client_id, sender_type, message_type, message_text, created_at, is_read) VALUES ($1, $2, $3, $4, $5, $6, $7)', msg_id, uid, s_type, m_type, m_text, c_date, is_read)
                            elif mode == "notif" and str(row[0]).isdigit():
                                notif_id = int(row[0]) # 🌟 استعادة الـ ID
                                uid = int(safe_get(row, 1, 0) or 0)
                                title = str(safe_get(row, 3, ''))
                                msg = str(safe_get(row, 4, ''))
                                c_date = parse_date(safe_get(row, 5, ''))
                                is_read = str(safe_get(row, 6, 'False')).lower() == 'true'
                                img_file = clean_val(safe_get(row, 7, None))
                                await conn.execute('INSERT INTO web_notifications (id, user_id, title, message, created_at, is_read, image_file_id) VALUES ($1, $2, $3, $4, $5, $6, $7)', notif_id, uid, title, msg, c_date, is_read, img_file)

                    # 10. استعادة الإعدادات والذاكرة
                    if "الإعدادات والذاكرة" in wb.sheetnames:
                        mode = "settings"
                        for row in wb["الإعدادات والذاكرة"].iter_rows(min_row=1, values_only=True):
                            if not row or len(row) == 0 or row[0] is None: continue
                            if "أرشيف التقارير" in str(row[0]): mode = "archive"; continue
                            if "الذاكرة الدائمة" in str(row[0]): mode = "facts"; continue
                            if "اشتراكات الإشعارات" in str(row[0]): mode = "push"; continue
                            if "المفتاح" in str(row[0]) or "شهر التقرير" in str(row[0]) or "---" in str(row[0]): continue
                            
                            if mode == "settings":
                                val = str(safe_get(row, 1, ''))
                                await conn.execute('INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2', str(row[0]), val)
                            elif mode == "archive":
                                c_date = parse_date(safe_get(row, 1, ''))
                                file_id = str(safe_get(row, 2, ''))
                                await conn.execute('INSERT INTO reports_archive (month_year, created_at, file_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING', str(row[0]), c_date, file_id)
                            elif mode == "facts" and str(row[0]).isdigit():
                                uid = int(row[0])
                                f_text = str(safe_get(row, 1, ''))
                                c_date = parse_date(safe_get(row, 2, ''))
                                await conn.execute('INSERT INTO learned_facts (added_by, fact_text, date_added) VALUES ($1, $2, $3)', uid, f_text, c_date)
                            elif mode == "push" and str(row[0]).isdigit():
                                uid = int(row[0])
                                sub_json = str(safe_get(row, 1, ''))
                                await conn.execute('INSERT INTO push_subscriptions (user_id, subscription_json) VALUES ($1, $2) ON CONFLICT DO NOTHING', uid, sub_json)
                            # 11. استعادة الكروت الإلكترونية (الخزنة الذكية)
                    if "الكروت الإلكترونية" in wb.sheetnames:
                        for row in wb["الكروت الإلكترونية"].iter_rows(min_row=2, values_only=True):
                            if row and len(row) > 0 and row[0] is not None and str(row[0]).isdigit():
                                e_id = int(row[0]) # 🌟 استعادة الـ ID
                                pin = str(safe_get(row, 1, ''))
                                ctype = str(safe_get(row, 2, ''))
                                status = str(safe_get(row, 3, 'available'))
                                sold_to = safe_get(row, 4, None)
                                sold_to = int(sold_to) if sold_to and str(sold_to).isdigit() else None
                                sold_at = parse_date(safe_get(row, 5, '')) if safe_get(row, 5, '') else None
                                added_at = parse_date(safe_get(row, 6, ''))
                                
                                await conn.execute('''
                                    INSERT INTO electronic_cards (id, card_number, card_type, status, sold_to_client, sold_at, added_at) 
                                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                                ''', e_id, pin, ctype, status, sold_to, sold_at, added_at)

                    # 12. استعادة بوابة التسديدات (محدثة بالكامل)
                    if "بوابة التسديدات" in wb.sheetnames:
                        mode = "packages"
                        for row in wb["بوابة التسديدات"].iter_rows(min_row=1, values_only=True):
                            if not row or len(row) == 0 or row[0] is None: continue
                            row_str = str(row[0])
                            
                            if "شرائح الأرباح" in row_str: mode = "tiers"; continue
                            if "محفظة أرباح التسديدات" in row_str: mode = "profits"; continue
                            if "سجل عمليات التسديد" in row_str: mode = "transactions"; continue
                            if "---" in row_str or "المعرف" in row_str: continue
                            
                            if mode == "packages" and row_str.isdigit():
                                p_id = int(row[0]) # 🌟 استعادة الـ ID
                                network = str(safe_get(row, 1, ''))
                                pkg_name = str(safe_get(row, 2, ''))
                                cost = Decimal(str(safe_get(row, 3, 0) or 0))
                                sell = Decimal(str(safe_get(row, 4, 0) or 0))
                                credit = Decimal(str(safe_get(row, 5, 0) or 0))
                                retail = Decimal(str(safe_get(row, 6, 0) or 0))
                                is_active = str(safe_get(row, 7, 'True')).lower() == 'true'
                                
                                await conn.execute('''
                                    INSERT INTO telecom_packages (id, network, package_name, cost_price, selling_price, credit_price, retail_price, is_active) 
                                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                                ''', p_id, network, pkg_name, cost, sell, credit, retail, is_active)
                                
                            elif mode == "tiers" and row_str.isdigit():
                                t_id = int(row[0]) # 🌟 استعادة الـ ID
                                min_amt = Decimal(str(safe_get(row, 1, 0) or 0))
                                max_amt = Decimal(str(safe_get(row, 2, 0) or 0))
                                cash_p = Decimal(str(safe_get(row, 3, 0) or 0))
                                credit_p = Decimal(str(safe_get(row, 4, 0) or 0))
                                retail_p = Decimal(str(safe_get(row, 5, 0) or 0))
                                
                                await conn.execute('''
                                    INSERT INTO pricing_tiers (id, min_amount, max_amount, cash_profit, credit_profit, retail_profit) 
                                    VALUES ($1, $2, $3, $4, $5, $6)
                                ''', t_id, min_amt, max_amt, cash_p, credit_p, retail_p)
                                
                            elif mode == "profits" and row_str.isdigit():
                                pr_id = int(row[0]) # 🌟 استعادة الـ ID
                                amount = Decimal(str(safe_get(row, 1, 0) or 0))
                                details = str(safe_get(row, 2, ''))
                                date_val = parse_date(safe_get(row, 3, ''))
                                await conn.execute('INSERT INTO telecom_profits (id, amount, details, date) VALUES ($1, $2, $3, $4)', pr_id, amount, details, date_val)
                                
                            elif mode == "transactions" and row_str.isdigit():
                                tx_id = int(row[0]) # 🌟 استعادة رقم الفاتورة الأصلي
                                client_id = int(safe_get(row, 1, 0) or 0)
                                network = str(safe_get(row, 2, ''))
                                phone_number = str(safe_get(row, 3, ''))
                                package_name = str(safe_get(row, 4, ''))
                                cost_price = Decimal(str(safe_get(row, 5, 0) or 0))
                                selling_price = Decimal(str(safe_get(row, 6, 0) or 0))
                                profit = Decimal(str(safe_get(row, 7, 0) or 0))
                                status = str(safe_get(row, 8, 'success'))
                                created_at = parse_date(safe_get(row, 9, ''))
                                
                                amount = Decimal(str(safe_get(row, 10, 0) or 0))
                                ref_id = clean_val(safe_get(row, 11, None))
                                idem_key = clean_val(safe_get(row, 12, None))
                                paid_bal = Decimal(str(safe_get(row, 13, 0) or 0))
                                added_debt = Decimal(str(safe_get(row, 14, 0) or 0))
                                
                                await conn.execute('''
                                    INSERT INTO telecom_transactions (id, client_id, network, phone_number, package_name, cost_price, selling_price, profit, status, created_at, amount, provider_reference_id, idempotency_key, paid_from_balance, added_to_debt) 
                                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                                ''', tx_id, client_id, network, phone_number, package_name, cost_price, selling_price, profit, status, created_at, amount, ref_id, idem_key, paid_bal, added_debt)

                    # 13. استعادة دفتر الوكيل الشخصي
                    if "دفتر الوكيل الشخصي" in wb.sheetnames:
                        mode = "customers"
                        for row in wb["دفتر الوكيل الشخصي"].iter_rows(min_row=1, values_only=True):
                            if not row or len(row) == 0 or row[0] is None: continue
                            row_str = str(row[0])
                            if "سجل الحركات الشخصية" in row_str: mode = "ledger"; continue
                            if "---" in row_str or "المعرف" in row_str: continue
                            
                            if mode == "customers" and row_str.isdigit():
                                name = str(safe_get(row, 1, ''))
                                debt = Decimal(str(safe_get(row, 2, 0) or 0))
                                await conn.execute('INSERT INTO agent_personal_customers (id, name, debt) VALUES ($1, $2, $3)', int(row[0]), name, debt)
                            elif mode == "ledger" and row_str.isdigit():
                                cust_id = int(safe_get(row, 1, 0) or 0)
                                l_type = str(safe_get(row, 2, ''))
                                amount = Decimal(str(safe_get(row, 3, 0) or 0))
                                details = str(safe_get(row, 4, ''))
                                l_date = parse_date(safe_get(row, 5, ''))
                                await conn.execute('INSERT INTO agent_personal_ledger (id, customer_id, type, amount, details, date) VALUES ($1, $2, $3, $4, $5, $6)', int(row[0]), cust_id, l_type, amount, details, l_date)

                    # 14. استعادة بيانات النظام الحساسة
                    if "بيانات النظام الحساسة" in wb.sheetnames:
                        mode = "wallet"
                        for row in wb["بيانات النظام الحساسة"].iter_rows(min_row=1, values_only=True):
                            if not row or len(row) == 0 or row[0] is None: continue
                            row_str = str(row[0])
                            if "القائمة السوداء" in row_str: mode = "banned"; continue
                            if "---" in row_str or "المعرف" in row_str or "الآيبي" in row_str: continue
                            
                            if mode == "wallet" and row_str.isdigit():
                                t_balance = Decimal(str(safe_get(row, 1, 0) or 0))
                                # 🌟 الإصلاح المحاسبي: استعادة جميع الأرصدة (الكاش والأرباح) لحماية أموال الوكيل
                                m_cash = Decimal(str(safe_get(row, 2, 0) or 0))
                                t_cash = Decimal(str(safe_get(row, 3, 0) or 0))
                                r_profit = Decimal(str(safe_get(row, 4, 0) or 0))
                                
                                await conn.execute('''
                                    INSERT INTO agent_wallet (id, telecom_balance, manager_cash, telecom_cash, realized_profit) 
                                    VALUES (1, $1, $2, $3, $4) 
                                    ON CONFLICT (id) DO UPDATE 
                                    SET telecom_balance = $1, manager_cash = $2, telecom_cash = $3, realized_profit = $4
                                ''', t_balance, m_cash, t_cash, r_profit)
                            elif mode == "banned":
                                ip = str(row[0])
                                b_date = parse_date(safe_get(row, 1, ''))
                                await conn.execute('INSERT INTO banned_ips (ip, banned_at) VALUES ($1, $2) ON CONFLICT DO NOTHING', ip, b_date)
                                
                    # 15. استعادة سجل التدقيق (الجديد)
                    if "سجل التدقيق" in wb.sheetnames:
                        for row in wb["سجل التدقيق"].iter_rows(min_row=2, values_only=True):
                            if row and len(row) > 0 and row[0] is not None and str(row[0]).isdigit():
                                t_name = str(safe_get(row, 1, ''))
                                r_id = str(safe_get(row, 2, ''))
                                action = str(safe_get(row, 3, ''))
                                details = str(safe_get(row, 4, ''))
                                c_date = parse_date(safe_get(row, 5, ''))
                                await conn.execute('''
                                    INSERT INTO audit_logs (table_name, record_id, action, details, changed_at) 
                                    VALUES ($1, $2, $3, $4, $5)
                                ''', t_name, r_id, action, details, c_date)

                    # ==========================================
                    # 🚀 16. الصيانة الذاتية: تحديث جميع العدادات (Sequences)
                    # لمنع انهيار النظام (Duplicate Key) عند أول عملية بعد الاستعادة
                    # ==========================================
                    sequences_to_fix = [
                        ('transactions', 'id', 'transactions_id_seq'),
                        ('agent_profits', 'id', 'agent_profits_id_seq'),
                        ('reports_archive', 'id', 'reports_archive_id_seq'),
                        ('pending_orders', 'id', 'pending_orders_id_seq'),
                        ('pending_shipments', 'id', 'pending_shipments_id_seq'),
                        ('client_customers', 'id', 'client_customers_id_seq'),
                        ('client_sales', 'id', 'client_sales_id_seq'),
                        ('client_customer_ledger', 'id', 'client_customer_ledger_id_seq'),
                        ('web_notifications', 'id', 'web_notifications_id_seq'),
                        ('chat_messages', 'id', 'chat_messages_id_seq'),
                        ('electronic_cards', 'id', 'electronic_cards_id_seq'),
                        ('telecom_packages', 'id', 'telecom_packages_id_seq'),
                        ('pricing_tiers', 'id', 'pricing_tiers_id_seq'),
                        ('telecom_profits', 'id', 'telecom_profits_id_seq'),
                        ('telecom_transactions', 'id', 'telecom_transactions_id_seq'),
                        ('agent_personal_customers', 'id', 'agent_personal_customers_id_seq'),
                        ('agent_personal_ledger', 'id', 'agent_personal_ledger_id_seq'),
                        ('audit_logs', 'id', 'audit_logs_id_seq')
                    ]
                    
                    for table, column, seq_name in sequences_to_fix:
                        try:
                            # جلب أعلى رقم في الجدول، وتحديث العداد بناءً عليه
                            await conn.execute(f"""
                                SELECT setval('{seq_name}', COALESCE((SELECT MAX({column}) + 1 FROM {table}), 1), false);
                            """)
                        except Exception as seq_err:
                            pass # نتجاهل الخطأ إذا كان الجدول فارغاً
                            
                    # 🌟 تحديث عداد عملاء الأوفلاين (لمنع تعارض الآيديهات عند إضافة عميل جديد بعد الاستعادة)
                    try:
                        await conn.execute("""
                            SELECT setval('offline_users_seq', COALESCE((SELECT MAX(user_id) + 1 FROM users WHERE user_id >= 9990000), 9990000), false);
                        """)
                    except Exception:
                        pass

        await wait_msg.edit_text("✅ **تم استعادة النسخة الاحتياطية الشاملة بنجاح!** 🚀\nجميع البيانات والعدادات الآن مطابقة للملف المرفق وجاهزة للعمل.")
        await state.clear()
    except Exception as e:
        await wait_msg.edit_text(f"❌ حدث خطأ أثناء الاستعادة:\n`{e}`\n\nتأكد من أن الملف هو النسخة الاحتياطية الصحيحة ولم يتم التلاعب به.")
        await state.clear()

@router.message(Command("set_debt"))
async def set_previous_debt(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: 
        return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/set_debt [رقم_العميل] [إجمالي_الدين] [ربحك_من_هذا_الدين]`\n\nمثال: `/set_debt 12345 10000 2000`\n*(إذا كان الدين بدون أرباح، اكتب 0 في الأخير)*")
    
    args = command.args.replace('[', '').replace(']', '').split()
    if len(args) not in [2, 3]: 
        return await message.answer("⚠️ يرجى إدخال رقم العميل، إجمالي الدين، والربح (اختياري).")
    
    try:
        client_id = int(args[0])
        amount = Decimal(args[1])
        profit = Decimal(args[2]) if len(args) == 3 else Decimal('0.0')
    except ValueError: 
        return await message.answer("⚠️ يرجى إدخال أرقام صحيحة.")
        
    from core_accounting import core_set_debt
    result = await core_set_debt(client_id, amount, profit)
    
    if result["status"] == "error": 
        return await message.answer(f"❌ {result['message']}")
        
    profit_msg = f"\n💎 *(تم حفظ {profit} ريال كأرباح معلقة لك، ستنزل لمحفظتك عند السداد)*" if profit > 0 else ""
    await message.answer(f"✅ تم تسجيل دين افتتاحي بقيمة **{amount} ريال** على العميل (**{result['client_name']}**) بنجاح.{profit_msg}")

@router.message(Command("check_cash"))
async def check_cash_source(message: types.Message, command: CommandObject):
    """أمر سري لفحص الكاش الجاهز لبقالة محددة (فحص مجهري للدرجين)"""
    if message.from_user.id != ADMIN_ID: return
    if not command.args: 
        return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/check_cash [رقم_العميل]`\nمثال: `/check_cash 12345`")
    
    try:
        client_id = int(command.args.replace('[', '').replace(']', '').strip())
    except ValueError: 
        return await message.answer("⚠️ يرجى إدخال رقم العميل بشكل صحيح.")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            user = await conn.fetchrow("SELECT name, debt, pending_profit, pos_cash_collected, telecom_debt, telecom_pos_cash FROM users WHERE user_id = $1", client_id)
            if not user:
                return await message.answer("❌ هذا العميل غير موجود.")
                
            # 1. فحص كاش الكروت (السقف هو الدين الإجمالي لحماية الأرباح)
            gross_debt = max(Decimal('0.0'), Decimal(user['debt'] or 0))
            current_cards_cash = Decimal(user['pos_cash_collected'] or 0)
            fake_cards_cash = max(Decimal('0.0'), current_cards_cash - gross_debt)
            
            # 2. فحص كاش التسديدات
            net_telecom_debt = max(Decimal('0.0'), Decimal(user['telecom_debt'] or 0))
            current_telecom_cash = Decimal(user['telecom_pos_cash'] or 0)
            fake_telecom_cash = max(Decimal('0.0'), current_telecom_cash - net_telecom_debt)
            
            msg = f"🔍 **تقرير فحص الكاش للعميل ({user['name']}):**\n\n"
            
            msg += f"📦 **درج الكروت:**\n"
            msg += f"▪️ الكاش المسجل: {current_cards_cash} ريال\n"
            msg += f"▪️ الحد الأقصى المسموح (الدين الإجمالي): {gross_debt} ريال\n"
            if fake_cards_cash > 0:
                msg += f"🚨 **يوجد كاش كروت وهمي:** {fake_cards_cash} ريال!\n\n"
            else:
                msg += "🟢 الكاش سليم 100%.\n\n"
                
            msg += f"⚡ **درج التسديدات:**\n"
            msg += f"▪️ الكاش المسجل: {current_telecom_cash} ريال\n"
            msg += f"▪️ الحد الأقصى المسموح (الدين): {net_telecom_debt} ريال\n"
            if fake_telecom_cash > 0:
                msg += f"🚨 **يوجد كاش تسديدات وهمي:** {fake_telecom_cash} ريال!\n"
            else:
                msg += "🟢 الكاش سليم 100%.\n"
                
            if fake_cards_cash > 0 or fake_telecom_cash > 0:
                msg += f"\n💡 **الحل:** استخدم أمر `/clean_all_cash` لتصحيح النظام آلياً."
                
            await message.answer(msg)

@router.message(Command("audit_all_cash"))
async def audit_all_system_cash(message: types.Message):
    """أمر سري لعمل مسح شامل لكل النظام وكشف الكاش الوهمي (مسح راداري)"""
    if message.from_user.id != ADMIN_ID: return
    wait_msg = await message.answer("🔍 جاري الفحص الشامل لجميع بقالات النظام...")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            clients = await conn.fetch("SELECT name, debt, pending_profit, pos_cash_collected, telecom_debt, telecom_pos_cash FROM users WHERE role = 'client' AND (pos_cash_collected > 0 OR telecom_pos_cash > 0)")
            
            if not clients:
                return await wait_msg.edit_text("✅ **النظام نظيف 100%!**\nلا يوجد أي كاش جاهز مسجل في أي بقالة حالياً.")
            
            report = "🔍 **تقرير الفحص الشامل للكاش الجاهز:**\n\n"
            total_fake_cards = Decimal('0.0')
            total_fake_telecom = Decimal('0.0')
            
            for c in clients:
                # فحص الكروت (السقف هو الدين الإجمالي)
                gross_debt = max(Decimal('0.0'), Decimal(c['debt'] or 0))
                current_cards_cash = Decimal(c['pos_cash_collected'] or 0)
                fake_cards = max(Decimal('0.0'), current_cards_cash - gross_debt)
                
                # فحص التسديدات
                net_telecom_debt = max(Decimal('0.0'), Decimal(c['telecom_debt'] or 0))
                current_telecom_cash = Decimal(c['telecom_pos_cash'] or 0)
                fake_telecom = max(Decimal('0.0'), current_telecom_cash - net_telecom_debt)
                
                if fake_cards > 0 or fake_telecom > 0:
                    report += f"🔴 **{c['name']}:**\n"
                    if fake_cards > 0: 
                        report += f"   - كاش كروت وهمي: {fake_cards} ريال\n"
                        total_fake_cards += fake_cards
                    if fake_telecom > 0: 
                        report += f"   - كاش تسديدات وهمي: {fake_telecom} ريال\n"
                        total_fake_telecom += fake_telecom
            
            if total_fake_cards == 0 and total_fake_telecom == 0:
                return await wait_msg.edit_text("✅ **النظام نظيف 100%!**\nجميع الأرصدة النقدية في البقالات حقيقية ومطابقة للديون.")
                
            report += "\n━━━━━━━━━━━━━\n"
            report += f"🚨 **إجمالي كاش الكروت الوهمي:** {total_fake_cards} ريال\n"
            report += f"🚨 **إجمالي كاش التسديدات الوهمي:** {total_fake_telecom} ريال\n\n"
            report += "💡 **لإصلاح النظام وتصحيح كل الكاش الوهمي بضغطة زر، أرسل:**\n`/clean_all_cash`"
                
            await wait_msg.edit_text(report)

@router.message(Command("clean_all_cash"))
async def clean_all_system_cash(message: types.Message):
    """أمر سري لتنظيف وتصحيح كل الكاش الوهمي في النظام دفعة واحدة"""
    if message.from_user.id != ADMIN_ID: return
    wait_msg = await message.answer("🧹 جاري تنظيف وتصحيح الكاش لجميع البقالات...")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # 1. تنظيف كاش الكروت (السقف هو الدين الإجمالي لحماية الأرباح)
            await conn.execute("""
                UPDATE users 
                SET pos_cash_collected = GREATEST(0, debt)
                WHERE pos_cash_collected > debt AND role = 'client'
            """)
            
            # 2. تنظيف كاش التسديدات
            await conn.execute("""
                UPDATE users 
                SET telecom_pos_cash = GREATEST(0, telecom_debt)
                WHERE telecom_pos_cash > telecom_debt AND role = 'client'
            """)
            
    await wait_msg.edit_text("✅ **تم تنظيف النظام بالكامل!** 🧹\nتم مسح كل الكاش الوهمي وضبط العدادات على الكاش الحقيقي فقط بدقة متناهية.")

@router.message(Command("fix_balance"))
async def fix_accounting_balance(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    if database.pool:
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                # 1. مسح كل الأرصدة الافتتاحية والقيود العكسية القديمة للمدير لتنظيف السجل
                await conn.execute("DELETE FROM transactions WHERE user_id = 0 AND type IN ('رصيد_افتتاحي', 'رصيد_افتتاحي_كاش', 'قيد_عكسي')")
                
                # 2. إدخال رأس مال المدير (قيمة البضاعة والديون)
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'رصيد_افتتاحي', 569469, 'رصيد افتتاحي (ديون وبضاعة)', 'manager')")
                
                # 3. إدخال الكاش الفعلي الموجود في الصندوق
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'رصيد_افتتاحي_كاش', 25631, 'رصيد افتتاحي (كاش متوفر بالصندوق)', 'manager')")
                
                # 4. ضبط محفظة البوابة لضمان تطابق الكاش
                await conn.execute("UPDATE agent_wallet SET manager_cash = 25631 WHERE id = 1")
                
    await message.answer("✅ **تمت العملية الجراحية بنجاح!**\nتم تنظيف السجل وضبط رأس مال المدير إلى **595,100 ريال** بالهللة، واختفى إنذار العجز المحاسبي.")

@router.message(F.text.startswith("تسديد"))
async def handle_quick_cash_collection(message: types.Message, bot: Bot):
    """معالجة أمر تسديد الكاش السريع من البقالات مع دعم التجاوز #"""
    # حماية: الأمر مخصص للوكيل فقط
    if message.from_user.id != ADMIN_ID: return 
    
    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("⚠️ **الصيغة الصحيحة:**\n`تسديد [رقم_العميل] [المبلغ]`\nمثال: `تسديد 12345 50000`")
        return

    try:
        client_id = int(parts[1])
        amount_str = parts[2]
        
        # فحص وجود رمز التجاوز #
        bypass = False
        if amount_str.endswith("#"):
            bypass = True
            amount_str = amount_str[:-1] # إزالة الرمز بعد تفعيله
            
        amount = Decimal(amount_str)
        
        # استدعاء المحرك المالي المركزي مع تمرير التجاوز
        from core_accounting import core_collect_debt
        res = await core_collect_debt(client_id, amount, bypass)
        
        if res["status"] == "success":
            trans_id = res["trans_id"]
            client_name = res["client_name"]
            new_debt = res["new_debt"]
            
            # 🌟 توليد الفاتورة (PDF) وإرسالها
            from pdf_generator import generate_receipt
            from aiogram.types import BufferedInputFile
            import asyncio
            
            pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, client_name, amount, "تسديد دفعة", "دفعة نقدية يداً بيد (أمر سريع)")
            pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
            
            balance_text = f"الرصيد المتبقي عليه: {int(new_debt)} ريال" if new_debt >= 0 else f"رصيد دائن (لصالح العميل): {abs(int(new_debt))} ريال"
            bypass_note = "\n*(تم استخدام صلاحية التجاوز الإدارية)*" if bypass else ""
            
            await message.answer_document(document=pdf_file, caption=f"✅ **تم استلام الدفعة بنجاح!**\nتم خصم {int(amount)} ريال من حساب {client_name}.\n\n{balance_text}{bypass_note}")

            # 🌟 إرسال الفاتورة للعميل عبر الواتساب
            wa_text = f"🧾 *سند قبض*\nمرحباً {client_name}،\nتم استلام مبلغ *{int(amount)} ريال* بنجاح.\nالرصيد المتبقي: *{int(new_debt)} ريال*.\nمرفق الفاتورة للتأكيد 🌹"
            asyncio.create_task(safe_send_whatsapp(client_id, wa_text, pdf_buffer, f"Receipt_{trans_id}.pdf", bot=bot))
            
            # 🌟 إشعار الويب (Push)
            try:
                from web_api import send_web_push
                await send_web_push(client_id, "💰 سند قبض (استلام كاش)", f"تم استلام مبلغ {int(amount)} ريال بنجاح. الرصيد المتبقي: {int(new_debt)} ريال.")
            except Exception as e: logging.error(f"Error: {e}")

        else:
            await message.reply(f"❌ خطأ: {res.get('message', 'غير معروف')}")
            
    except ValueError:
        await message.reply("⚠️ تأكد من إدخال الأرقام بشكل صحيح.")
    except Exception as e:
        await message.reply(f"❌ حدث خطأ أثناء التنفيذ: {e}")
        
@router.message(Command("test_system"))
async def test_system_health(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    status_msg = await message.answer("⏳ جاري فحص جميع أنظمة البوت...")
    report = "🩺 **تقرير الفحص الشامل للنظام:**\n\n"
    try:
        if database.pool:
            async with database.pool.acquire() as conn: await conn.execute("SELECT 1")
            report += "✅ **قاعدة البيانات:** متصلة وتعمل بكفاءة.\n"
        else: report += "❌ **قاعدة البيانات:** غير متصلة!\n"
    except Exception as e: report += f"❌ **قاعدة البيانات:** خطأ ({e})\n"
    report += "\n🚀 **النتيجة النهائية:** النظام جاهز للعمل الفعلي 100%!"
    await status_msg.edit_text(report)

from aiogram.filters import Command

@router.message(Command("setup_db"))
async def setup_database_tables(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    wait_msg = await message.answer("⏳ جاري تحديث قاعدة البيانات وإنشاء الخزنة الرقمية...")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            try:
                # 1. إنشاء جدول الإرساليات (موجود مسبقاً لكن للتأكيد)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS pending_shipments (
                        id SERIAL PRIMARY KEY,
                        status VARCHAR(20) DEFAULT 'pending',
                        manager_items JSONB,
                        agent_items JSONB,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                
                # 2. ?? إنشاء جدول الكروت الإلكترونية (الخزنة الذكية) 🌟
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS electronic_cards (
                        id SERIAL PRIMARY KEY,
                        card_number VARCHAR(50) UNIQUE NOT NULL,
                        card_type VARCHAR(50) NOT NULL,
                        status VARCHAR(20) DEFAULT 'available',
                        sold_to_client INT DEFAULT NULL,
                        sold_at TIMESTAMP DEFAULT NULL,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                
                # 3. إضافة فهارس لتسريع البحث والسحب الآلي
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_ecards_type_status ON electronic_cards(card_type, status);")
                
                await wait_msg.edit_text("✅ **تم إنشاء الخزنة الرقمية (electronic_cards) بنجاح!**\nقاعدة البيانات الآن جاهزة لاستقبال الكروت الإلكترونية. 🚀")
            except Exception as e:
                await wait_msg.edit_text(f"❌ حدث خطأ: {e}")

@router.message(Command("set_inv"))
async def set_client_inventory(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/set_inv [رقم_العميل] [فئة_الكرت] [الكمية]`")
    
    args = command.args.replace('[', '').replace(']', '').split()
    if len(args) != 3: return await message.answer("⚠️ يرجى إدخال رقم العميل، فئة الكرت، والكمية.")
    
    try:
        client_id, card_type, quantity = int(args[0]), args[1].replace("_", " "), int(args[2])
    except ValueError: return await message.answer("⚠️ يرجى إدخال أرقام صحيحة.")
        
    from core_accounting import core_set_inv
    result = await core_set_inv(client_id, card_type, quantity)
    if result["status"] == "error": return await message.answer(f"❌ {result['message']}")
    await message.answer(f"✅ تم ضبط مخزون العميل (**{result['client_name']}**) لكرت ({card_type}) إلى **{quantity}** كرت بنجاح.\n(تم إضافة أرباحها المعلقة لمحفظتك).")

@router.message(Command("link_account"))
async def link_offline_to_real(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/link_account [رقم_الأوفلاين] [رقم_التليجرام_الجديد]`")
    
    args = command.args.replace('[', '').replace(']', '').split()
    if len(args) != 2: return await message.answer("⚠️ يرجى إدخال رقم حساب الأوفلاين ورقم حساب التليجرام الجديد.")
    
    try: offline_id, real_id = int(args[0]), int(args[1])
    except ValueError: return await message.answer("⚠️ يرجى إدخال أرقام صحيحة.")
        
    from core_accounting import core_link_account
    result = await core_link_account(offline_id, real_id)
    if result["status"] == "error": return await message.answer(f"❌ {result['message']}")
    await message.answer(f"✅ **تم دمج الحسابات بنجاح! (بدون حذف أي بيانات)**\nتم نقل جميع ديون وعمليات ومخزون ({result['offline_name']}) إلى الحساب الحقيقي ({result['real_name']})، وتمت أرشفة الحساب القديم.")
    
@router.message(Command("set_gm_debt"))
async def set_gm_previous_debt(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/set_gm_debt [المبلغ]`")
    
    clean_args = command.args.replace('[', '').replace(']', '')
    try: amount = Decimal(clean_args)
    except ValueError: return await message.answer("⚠️ يرجى إدخال رقم صحيح للمبلغ.")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                # 🌟 المكنسة السحرية: مسح أي إدخالات سابقة خاطئة لنفس الغرض لكي لا تتراكم الأرقام
                await conn.execute("DELETE FROM transactions WHERE user_id = 0 AND type = 'رصيد_افتتاحي' AND details LIKE '%دين سابق للمدير العام%'")
                
                # 🌟 إدخال الرصيد الجديد النظيف
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details) VALUES (0, 'رصيد_افتتاحي', $1, 'رصيد افتتاحي (دين سابق للمدير العام)')", amount)            
    await message.answer(f"✅ تم تصحيح وتسجيل دين افتتاحي لصالح المدير العام بقيمة **{amount} ريال** بنجاح.\n*(تم مسح الإدخالات السابقة الخاطئة تلقائياً لضبط الميزان)*.")

@router.message(Command("set_opening_cash"))
async def set_opening_cash(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ الاستخدام: `/set_opening_cash [المبلغ]`")
    
    try: cash_amount = Decimal(command.args.strip())
    except: return await message.answer("رقم غير صالح.")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                # 1. خصم المبلغ من الرصيد الافتتاحي العام لكي لا يتضاعف رأس مال المدير
                await conn.execute("UPDATE transactions SET amount = amount - $1 WHERE user_id = 0 AND type = 'رصيد_افتتاحي' AND details LIKE '%دين سابق للمدير العام%'", cash_amount)
                
                # 2. تسجيل المبلغ كـ "كاش افتتاحي" ليدخل في الصندوق
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'رصيد_افتتاحي_كاش', $1, 'رصيد افتتاحي (كاش متوفر بالصندوق)', 'manager')", cash_amount)
                
    await message.answer(f"✅ **تم ضبط الميزان المحاسبي بنجاح!**\nتم تسجيل **{cash_amount} ريال** كسيولة نقدية (كاش) في الصندوق.")

@router.message(Command("discount_gm"))
async def discount_gm_debt(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/discount_gm [المبلغ]`")
    
    try: amount = Decimal(command.args.replace('[', '').replace(']', ''))
    except ValueError: return await message.answer("⚠️ يرجى إدخال رقم صحيح للمبلغ.")
    
    from core_accounting import core_discount_gm
    await core_discount_gm(amount)
    await message.answer(f"✅ تم تسجيل خصم/مسامحة من المدير العام بقيمة **{amount} ريال** بنجاح.\n(تم إنقاص مديونيتك للمدير، وإضافة المبلغ لمحفظة أرباحك، دون التأثير على كاش الصندوق).")

@router.message(Command("add_t_cash"))
async def add_telecom_capital(message: types.Message, command: CommandObject):
    """أمر سري لضخ رأس مال من جيب الوكيل إلى صندوق التسديدات"""
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/add_t_cash [المبلغ]`\nمثال: `/add_t_cash 50000`")
    
    try: amount = Decimal(command.args.replace('[', '').replace(']', '').strip())
    except ValueError: return await message.answer("⚠️ يرجى إدخال رقم صحيح للمبلغ.")
    
    if amount <= 0: return await message.answer("⚠️ المبلغ يجب أن يكون أكبر من صفر.")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            async with conn.transaction(): 
                # 🌟 الإصلاح المحاسبي: تحديث الرصيد الرقمي فقط، أما الكاش الفعلي (telecom_cash) سيتكفل به الـ Trigger تلقائياً
                await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", amount)
                
                # تسجيل العملية في الدفتر (الـ Trigger سيقرأها ويزيد الكاش)
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'رأس_مال_تسديدات', $1, 'ضخ رأس مال من الوكيل لصندوق التسديدات', 'telecom')", amount)
            
    await message.answer(f"✅ **تم ضخ رأس المال بنجاح!**\nتم إضافة **{int(amount)} ريال** إلى (كاش التسديدات) ورصيد البوابة الفعلي.\nيمكنك الآن تسديد الباقات للعملاء مباشرة دون الحاجة للذهاب للويب.")

@router.message(Command("fix_cash"))
async def fix_cash_bug(message: types.Message):
    """تم تعطيل هذا الأمر لأن نظام التراجع الجديد (core_revert_transaction) يعالج الكاش تلقائياً وبدقة."""
    if message.from_user.id != ADMIN_ID: return
    await message.answer("⚠️ **هذا الأمر معطل!**\nنظام التراجع الجديد يعالج الكاش تلقائياً وبدقة. لا حاجة لاستخدام هذا الأمر بعد الآن.")

@router.message(Command("clean_cash"))
async def clean_cash_duplicates(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    if database.pool:
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                # مسح كل التكرارات واللخبطة السابقة
                await conn.execute("DELETE FROM transactions WHERE user_id = 0 AND type IN ('رصيد_افتتاحي', 'رصيد_افتتاحي_كاش', 'قيد_عكسي')")
                
                # إدخال نسخة واحدة فقط صحيحة
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'رصيد_افتتاحي', 139759, 'رصيد افتتاحي (دين سابق للمدير العام)', 'manager')")
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'رصيد_افتتاحي_كاش', 25631, 'رصيد افتتاحي (كاش متوفر بالصندوق)', 'manager')")
                
    await message.answer("✅ تم مسح التكرار بنجاح!\nعاد الكاش المطلوب توريده إلى 25,631 ريال، ورأس مال المدير مضبوط.")

# ================= نظام التراجع الشامل (القيد العكسي الذكي) =================
@router.message(Command("undo"))
async def show_undo_menu(message: types.Message):
    """أمر لإظهار قائمة بآخر العمليات للتراجع عن إحداها"""
    if message.from_user.id != ADMIN_ID: return
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # جلب آخر 10 عمليات مع اسم العميل (باستخدام LEFT JOIN)
            # 🌟 إصلاح: استخدام is_reverted = FALSE لجلب العمليات القابلة للتراجع فقط (أسرع وأنظف)
            recent_txs = await conn.fetch("""
                SELECT t.id, t.type, t.amount, t.details, t.date, u.name 
                FROM transactions t
                LEFT JOIN users u ON t.user_id = u.user_id
                WHERE t.type != 'قيد_عكسي' AND t.amount > 0 AND t.is_reverted = FALSE
                ORDER BY t.date DESC LIMIT 10
            """)
            
            if not recent_txs:
                return await message.answer("❌ لا توجد عمليات حديثة للتراجع عنها.")
                
            kb = []
            for tx in recent_txs:
                status = "✅"
                callback = f"revert_tx_{tx['id']}"
                    
                # تحديد اسم الجهة (إذا لم يكن عميلاً، نكتب الإدارة أو طياري)
                client_name = tx['name'] if tx['name'] else "الإدارة/طياري"
                tx_type = tx['type'].replace('_', ' ')
                
                # تنسيق اسم الزر ليظهر فيه اسم العميل بوضوح
                btn_text = f"{status} {client_name} | {tx_type} | {tx['amount']} ريال"
                kb.append([InlineKeyboardButton(text=btn_text, callback_data=callback)])
                
            kb.append([InlineKeyboardButton(text="❌ إغلاق", callback_data="cancel_action")])
            
            await message.answer(
                "⏪ **نظام التراجع المحاسبي (القيود العكسية):**\n\n"
                "اختر العملية التي اكتشفت فيها خطأ وتريد إبطالها:\n"
                "*(سيقوم النظام بإرجاع الأرصدة والكروت تلقائياً وتسجيل قيد عكسي)*",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
            )

@router.callback_query(F.data.startswith("revert_tx_"))
async def process_revert_transaction(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_ID: return
    
    tx_id = int(callback.data.split("_")[2])
    await callback.answer("⏳ جاري فحص الأرصدة والتراجع...")
    
    # 🌟 جلب بيانات العملية قبل التراجع لمعرفة العميل الذي يجب إشعاره
    client_id = 0
    tx_type = ""
    tx_amount = 0
    if database.pool:
        async with database.pool.acquire() as conn:
            tx = await conn.fetchrow("SELECT user_id, type, amount FROM transactions WHERE id = $1", tx_id)
            if tx:
                client_id = tx['user_id']
                tx_type = tx['type'].replace('_', ' ')
                tx_amount = int(tx['amount'])

    # 🌟 التوحيد: استدعاء المطبخ المركزي
    from core_accounting import core_revert_transaction
    result = await core_revert_transaction(tx_id)
    
    if result["status"] == "error":
        await callback.message.edit_text(f"❌ **رفض أمني:** {result['message']}")
    else:
        await callback.message.edit_text(f"✅ **تم التراجع بنجاح!**\n{result['message']}")
        
        # 🌟 الإصلاح الأمني (حماية الثقة): إشعار العميل بالتراجع فوراً عبر التطبيق
        if client_id != 0:
            try:
                from web_api import send_web_push
                await send_web_push(client_id, "⚠️ إشعار تسوية عكسية", f"تم التراجع عن عملية ({tx_type}) بقيمة {tx_amount} ريال. يرجى مراجعة كشف حسابك.")
            except: pass

@router.message(Command("undo_fix"))
async def undo_accounting_balance(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    if database.pool:
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                # 1. مسح المليون الوهمي الذي تسببنا به 😂
                await conn.execute("DELETE FROM transactions WHERE user_id = 0 AND type IN ('رصيد_افتتاحي', 'رصيد_افتتاحي_كاش')")
                
                # 2. إرجاع أرقامك الذهبية الصحيحة التي كانت موجودة
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'رصيد_افتتاحي', 139759, 'رصيد افتتاحي (دين سابق للمدير العام)', 'manager')")
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'رصيد_افتتاحي_كاش', 25631, 'رصيد افتتاحي (كاش متوفر بالصندوق)', 'manager')")
                
    await message.answer("✅ **تم سحب المليون بنجاح!** 😅\nعادت حساباتك الأصلية الدقيقة، ورأس مال المدير الآن 595,100 ريال.")

@router.callback_query(F.data == "ignore_action")
async def ignore_action(callback: types.CallbackQuery):
    await callback.answer("هذه العملية تم التراجع عنها مسبقاً!", show_alert=True)

@router.message(Command("delete_report"))
async def delete_archived_report(message: types.Message, command: CommandObject):
    """أمر سري لحذف تقرير من الأرشيف (مفيد بعد التقارير التجريبية)"""
    if message.from_user.id != ADMIN_ID: return
    
    if not command.args:
        return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/delete_report [الشهر]`\nمثال: `/delete_report 2026-05`")
        
    month_str = command.args.strip()
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # التحقق من وجود التقرير
            exists = await conn.fetchval("SELECT id FROM reports_archive WHERE month_year = $1", month_str)
            if not exists:
                return await message.answer(f"❌ لم يتم العثور على تقرير لشهر ({month_str}) في الأرشيف.")
                
            # حذف التقرير
            await conn.execute("DELETE FROM reports_archive WHERE month_year = $1", month_str)
            
            # إذا أصبح الأرشيف فارغاً، نوقف ميزة مشاركة التقرير للمدير
            count = await conn.fetchval("SELECT COUNT(*) FROM reports_archive")
            if count == 0:
                await conn.execute("UPDATE settings SET value = 'off' WHERE key = 'share_monthly_report'")
                
    await message.answer(f"✅ **تم حذف تقرير شهر ({month_str}) من الأرشيف بنجاح!** 🗑️\nالآن سيعتبر النظام أن هذا التقرير لم يُعتمد أبداً.")

@router.message(Command("fix_sys"))
async def fix_system_user(message: types.Message):
    """أمر سريع لإنشاء حساب النظام الوهمي"""
    if message.from_user.id != ADMIN_ID: return
    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, name, role, debt) 
                VALUES (0, 'النظام / مبيعات مباشرة', 'system', 0.0) 
                ON CONFLICT (user_id) DO NOTHING;
            """)
            await message.answer("✅ **تم إصلاح حساب النظام (0) بنجاح!**\nيمكنك الآن استلام الكروت أو البيع المباشر بدون أي أخطاء. 🚀")

@router.message(Command("clean_numbers"))
async def clean_database_numbers(message: types.Message):
    """تم تعطيل هذا الأمر لحماية الميزان المحاسبي."""
    if message.from_user.id != ADMIN_ID: return
    await message.answer("⚠️ **هذا الأمر معطل!**\nالنظام الآن يتعامل مع الكسور العشرية بدقة متناهية (Decimal) لحماية الميزان المحاسبي. يتم إخفاء الكسور فقط عند العرض للمستخدم.")

@router.message(Command("update_db"))
async def update_database_schema(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    wait_msg = await message.answer("⏳ جاري تحديث قاعدة البيانات وإضافة الفهارس لتسريع النظام...")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            try:
                # 1. التحديثات السابقة (سقف المديونية، الهاتف، سعر التكلفة)
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS credit_limit DECIMAL DEFAULT 50000.0")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(20)")
                await conn.execute("ALTER TABLE inventory ADD COLUMN IF NOT EXISTS cost_price DECIMAL DEFAULT 0.0")
                await conn.execute("UPDATE inventory SET cost_price = price WHERE cost_price = 0.0")
                
                # 2. 🌟 التحديثات الجديدة (المرحلة الأولى من النظام المحاسبي) 🌟
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS old_debt DECIMAL DEFAULT 0.0")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_profit DECIMAL DEFAULT 0.0")
                
                # 3. ترحيل الديون الحالية إلى "ديون قديمة" (لكي نبدأ صفحة جديدة نظيفة)
                # ملاحظة: هذا السطر سيعمل مرة واحدة فقط ولن يؤثر على الديون المستقبلية
                await conn.execute("UPDATE users SET old_debt = debt WHERE old_debt = 0.0 AND debt > 0")
                
                # 4. 🚀 إضافة الفهارس (Indexes) لتسريع النظام 100 ضعف 🚀
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id);")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_type_date ON transactions(type, date);")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_role_debt ON users(role, debt);")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_client_inventory_user ON client_inventory(user_id, card_type);")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_profits_date ON agent_profits(date);")
                
                await wait_msg.edit_text("✅ **تم تحديث قاعدة البيانات بنجاح!** 🚀\nتمت إضافة نظام (الديون القديمة) و (الأرباح المعلقة)، وتم إنشاء الفهارس (Indexes) لتسريع استعلامات النظام بشكل كبير.")
            except Exception as e:
                await wait_msg.edit_text(f"❌ حدث خطأ أثناء تحديث قاعدة البيانات: {e}")

@router.message(Command("set_group"))
async def set_archive_group(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    if message.chat.type in ["group", "supergroup"]:
        group_id = str(message.chat.id)
        await database.set_setting("archive_channel_id", group_id)
        await message.answer(f"✅ **تم ربط المجموعة بنجاح!**\nرقم المجموعة: `{group_id}`\nسيتم إرسال النسخ الاحتياطية والتقارير اليومية إلى هنا تلقائياً.")
    else:
        await message.answer("⚠️ يرجى إرسال هذا الأمر داخل المجموعة التي أنشأتها، وليس هنا في الخاص.")

@router.message(Command("close_account"))
async def close_client_account(message: types.Message, command: CommandObject):
    """أمر سري لتصفية حساب عميل وسحب كروته"""
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/close_account [رقم_العميل]`")
    
    clean_args = command.args.replace('[', '').replace(']', '')
    try: client_id = int(clean_args.strip())
    except ValueError: return await message.answer("⚠️ يرجى إدخال رقم العميل بشكل صحيح.")
    
    # 🌟 التوحيد: استدعاء المطبخ المركزي
    from core_accounting import core_close_client_account
    result = await core_close_client_account(client_id)
    
    if result["status"] == "error":
        return await message.answer(f"❌ {result['message']}")
        
    client_name = result["client_name"]
    returned_cards_value = result["returned_cards_value"]
    details_str = result["details_str"]
    total_profit_to_reverse = result["total_profit_to_reverse"]
    refund_cash = result["refund_cash"]
    final_debt = result["final_debt"]
    
    msg = f"✅ **تم تصفية حساب العميل ({client_name}) بنجاح!**\n\n"
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
        
    await message.answer(msg)

@router.message(Command("demote"))
async def demote_client(message: types.Message, command: CommandObject):
    """أمر سري لسحب صلاحيات البقالة من شخص وإعادته لزائر"""
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/demote [رقم_العميل]`")
    
    clean_args = command.args.replace('[', '').replace(']', '')
    try: client_id = int(clean_args.strip())
    except ValueError: return await message.answer("⚠️ يرجى إدخال رقم العميل بشكل صحيح.")
    
    if database.pool:
        async with database.pool.acquire() as conn:
                        # 🌟 سد ثغرة تهريب ديون التسديدات: جلب أرصدة التسديدات أيضاً
            user = await conn.fetchrow("SELECT name, role, debt, telecom_debt, telecom_balance FROM users WHERE user_id = $1", client_id)
            if not user: return await message.answer("❌ هذا الحساب غير مسجل في النظام.")
            if user['role'] == 'visitor': return await message.answer("⚠️ هذا الشخص هو زائر عادي بالفعل.")
            
            inv_count = await conn.fetchval("SELECT COALESCE(SUM(quantity), 0) FROM client_inventory WHERE user_id = $1", client_id)
            
            # 🌟 الحماية الشاملة: منع سحب الصلاحيات إذا كان عليه دين كروت أو تسديدات أو لديه رصيد
            if Decimal(user['debt']) != Decimal('0.0') or inv_count > 0 or Decimal(user['telecom_debt'] or 0) != Decimal('0.0') or Decimal(user['telecom_balance'] or 0) != Decimal('0.0'):
                return await message.answer(f"🚨 **رفض أمني:** لا يمكن سحب صلاحيات البقالة ({user['name']})!\nعليه ديون أو أرصدة معلقة (كروت أو تسديدات) أو مخزون.\nيجب تصفية حسابه وتصفير رصيده تماماً باستخدام `/close_account` أولاً.")

            async with conn.transaction():
                await conn.execute("UPDATE users SET role = 'visitor' WHERE user_id = $1", client_id)
                # 🌟 التعديل الأمني: إلغاء أي طلبات معلقة لهذا العميل آلياً لمنع تسليم كروت لزائر
                await conn.execute("UPDATE pending_orders SET status = 'rejected' WHERE user_id = $1 AND status = 'pending'", client_id)
            
    try:
        from web_api import notify_clients
        await notify_clients(client_id)
    except Exception as e: logging.error(f"Error: {e}")
    await message.answer(f"✅ تم سحب الصلاحيات بنجاح!\nعاد الحساب ({user['name']}) إلى رتبة (زائر عادي)، وتم إلغاء أي طلبات معلقة له آلياً.")

@router.message(Command("rename"))
async def rename_client(message: types.Message, command: CommandObject):
    """أمر سري لتعديل اسم العميل في النظام"""
    if message.from_user.id != ADMIN_ID: return
    if not command.args: 
        return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/rename [رقم_العميل] [الاسم_الجديد]`\n\nمثال:\n`/rename 123456789 ماجد علي سعيد`")
    
    # فصل الآيدي عن الاسم الجديد
    args = command.args.split(maxsplit=1)
    
    if len(args) < 2: 
        return await message.answer("⚠️ يرجى إدخال رقم العميل والاسم الجديد.")
    
    try:
        client_id = int(args[0].replace('[', '').replace(']', '').strip())
        new_name = args[1].replace('[', '').replace(']', '').strip()
    except ValueError:
        return await message.answer("⚠️ يرجى إدخال رقم العميل بشكل صحيح.")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # التحقق من وجود العميل
            user = await conn.fetchrow("SELECT name FROM users WHERE user_id = $1", client_id)
            if not user: 
                return await message.answer("❌ هذا العميل غير موجود في النظام.")
            
            old_name = user['name']
            # تحديث الاسم
            await conn.execute("UPDATE users SET name = $1 WHERE user_id = $2", new_name, client_id)
            
    try:
        from web_api import notify_clients
        await notify_clients(client_id)
        await notify_clients(ADMIN_ID)
    except Exception as e: logging.error(f"Error: {e}")
    await message.answer(f"✅ **تم تعديل الاسم بنجاح!**\n\nالاسم القديم: {old_name}\nالاسم الجديد: **{new_name}**")

@router.message(Command("final_fix"))
async def final_fix_manager_capital(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    if database.pool:
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                # تنظيف أي أرصدة افتتاحية سابقة للمدير لمنع التكرار
                await conn.execute("DELETE FROM transactions WHERE user_id = 0 AND type IN ('رصيد_افتتاحي', 'رصيد_افتتاحي_كاش', 'قيد_عكسي')")
                
                # إرجاع الرقم المفقود (139,759) والكاش (25,631)
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'رصيد_افتتاحي', 139759, 'رصيد افتتاحي (دين سابق للمدير العام)', 'manager')")
                await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'رصيد_افتتاحي_كاش', 25631, 'رصيد افتتاحي (كاش متوفر بالصندوق)', 'manager')")
                
    await message.answer("✅ تم استعادة الرقم المفقود (139,759) بنجاح!\nرأس مال المدير الآن عاد إلى 595,100 ريال بالهللة.")

@router.message(Command("sync_cash"))
async def sync_cash_balance(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # إجبار صندوق الكاش على التطابق مع رأس مال المدير (مسح الزيادة الوهمية)
            await conn.execute("UPDATE agent_wallet SET manager_cash = 25631 WHERE id = 1")
            
    await message.answer("✅ تم مسح الزيادة الوهمية ومطابقة الميزان المحاسبي بنجاح!\nرأس المال الآن 595,100 والكاش المطلوب 25,631.")

@router.message(Command("set_phone"))
async def set_client_phone(message: types.Message, command: CommandObject):
    """أمر سري لإضافة أو تعديل رقم الواتساب لعميل موجود"""
    if message.from_user.id != ADMIN_ID: return
    
    if not command.args: 
        return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/set_phone [رقم_العميل] [رقم_الواتساب]`\n\nمثال:\n`/set_phone 123456789 967777123456`")
    
    args = command.args.split()
    if len(args) != 2: 
        return await message.answer("⚠️ يرجى إدخال رقم العميل ورقم الواتساب فقط.")
    
    try:
        client_id = int(args[0].replace('[', '').replace(']', '').strip())
        phone = args[1].replace('[', '').replace(']', '').replace('+', '').strip()
    except ValueError:
        return await message.answer("⚠️ يرجى إدخال أرقام صحيحة.")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # التحقق من وجود العميل
            user = await conn.fetchrow("SELECT name FROM users WHERE user_id = $1", client_id)
            if not user: 
                return await message.answer("❌ هذا العميل غير موجود في النظام.")
            
            # تحديث رقم الهاتف
            await conn.execute("UPDATE users SET phone = $1 WHERE user_id = $2", phone, client_id)
            
    await message.answer(f"✅ **تم ربط رقم الواتساب بنجاح!**\n\n👤 العميل: {user['name']}\n📱 رقم الواتساب: `{phone}`\n\nالآن ستصله الفواتير تلقائياً على هذا الرقم 🚀")

@router.message(Command("edit_date"))
async def edit_transaction_date(message: types.Message, command: CommandObject):
    """أمر سري لتعديل تاريخ ووقت أي عملية مسجلة مع مزامنة تاريخ العميل"""
    if message.from_user.id != ADMIN_ID: return
    
    if not command.args:
        return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/edit_date [رقم_العملية] [التاريخ_الجديد]`\nمثال: `/edit_date 150 2026-05-20`")
        
    args = command.args.replace('[', '').replace(']', '').split(maxsplit=1)
    if len(args) < 2: return await message.answer("⚠️ يرجى إدخال رقم العملية والتاريخ الجديد.")
        
    try:
        tx_id = int(args[0])
        new_date_str = args[1].strip()
    except ValueError: return await message.answer("⚠️ يرجى إدخال رقم عملية صحيح.")
        
    from datetime import datetime
    try:
        if ":" in new_date_str: new_date_obj = datetime.strptime(new_date_str, '%Y-%m-%d %H:%M')
        else: new_date_obj = datetime.strptime(new_date_str, '%Y-%m-%d')
    except ValueError: return await message.answer("❌ **صيغة التاريخ خاطئة!**")
        
    if database.pool:
        async with database.pool.acquire() as conn:
            tx = await conn.fetchrow("SELECT user_id, type, amount, date FROM transactions WHERE id = $1", tx_id)
            if not tx: return await message.answer(f"❌ لم يتم العثور على عملية رقم ({tx_id}).")
                
            old_date_full = tx['date']
            user_id = tx['user_id']
            
            async with conn.transaction():
                await conn.execute("UPDATE transactions SET date = $1 WHERE id = $2", new_date_obj, tx_id)
                await conn.execute("UPDATE agent_profits SET date = $1 WHERE date >= $2 - INTERVAL '1 minute' AND date <= $2 + INTERVAL '1 minute'", new_date_obj, old_date_full)
                
                # 🌟 سد ثغرة ضياع الأرباح: نقل تاريخ عمليات وأرباح التسديدات أيضاً
                await conn.execute("UPDATE telecom_transactions SET created_at = $1 WHERE created_at >= $2 - INTERVAL '1 minute' AND created_at <= $2 + INTERVAL '1 minute'", new_date_obj, old_date_full)
                await conn.execute("UPDATE telecom_profits SET date = $1 WHERE date >= $2 - INTERVAL '1 minute' AND date <= $2 + INTERVAL '1 minute'", new_date_obj, old_date_full)
                
                # 🌟 التعديل السحري: تحديث تاريخ انضمام العميل إذا أصبحت هذه العملية هي الأقدم
                if user_id and user_id != 0:
                    await conn.execute("""
                        UPDATE users 
                        SET joined_at = (SELECT MIN(date) FROM transactions WHERE user_id = $1)
                        WHERE user_id = $1 AND (SELECT MIN(date) FROM transactions WHERE user_id = $1) < joined_at
                    """, user_id)
            
    await message.answer(f"✅ **تم تعديل تاريخ العملية بنجاح!**\nتم نقلها إلى {new_date_str}، وتمت مزامنة ملف العميل تلقائياً.")


@router.message(Command("move_today"))
async def move_today_transactions(message: types.Message, command: CommandObject):
    """أمر سري لنقل جميع بيانات وعمليات اليوم إلى تاريخ سابق (شامل لكل النظام)"""
    if message.from_user.id != ADMIN_ID: return
    
    if not command.args:
        return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/move_today [التاريخ_القديم]`\nمثال: `/move_today 2026-05-31`")
        
    new_date_str = command.args.strip().replace('[', '').replace(']', '')
    
    from datetime import datetime
    try:
        new_date_obj = datetime.strptime(new_date_str, '%Y-%m-%d').date()
    except ValueError:
        return await message.answer("❌ **صيغة التاريخ خاطئة!**\nالرجاء استخدام الصيغة: `YYYY-MM-DD`")
        
    wait_msg = await message.answer("⏳ جاري نقل **كل شيء** تم إدخاله اليوم إلى التاريخ المحدد...")
        
    if database.pool:
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                # 1. العمليات المالية والأرباح
                await conn.execute("UPDATE transactions SET date = $1::date + (date::time) WHERE date >= CURRENT_DATE AND date < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                await conn.execute("UPDATE agent_profits SET date = $1::date + (date::time) WHERE date >= CURRENT_DATE AND date < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                
                # 2. تاريخ انضمام العملاء (هذا ينقل تاريخ فتح الحسابات التي أضفتها اليوم حتى لو بدون عمليات)
                await conn.execute("UPDATE users SET joined_at = $1::date + (joined_at::time) WHERE joined_at >= CURRENT_DATE AND joined_at < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                
                # 3. مبيعات الزبائن (POS) وسجل ديون الزبائن
                await conn.execute("UPDATE client_sales SET sale_date = $1::date + (sale_date::time) WHERE sale_date >= CURRENT_DATE AND sale_date < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                await conn.execute("UPDATE client_customer_ledger SET date = $1::date + (date::time) WHERE date >= CURRENT_DATE AND date < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                
                # 4. الطلبات والإرساليات
                await conn.execute("UPDATE pending_shipments SET created_at = $1::date + (created_at::time) WHERE created_at >= CURRENT_DATE AND created_at < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                await conn.execute("UPDATE pending_orders SET created_at = $1::date + (created_at::time) WHERE created_at >= CURRENT_DATE AND created_at < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                
                # 5. المحادثات والإشعارات
                await conn.execute("UPDATE chat_messages SET created_at = $1::date + (created_at::time) WHERE created_at >= CURRENT_DATE AND created_at < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                await conn.execute("UPDATE web_notifications SET created_at = $1::date + (created_at::time) WHERE created_at >= CURRENT_DATE AND created_at < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                
                # 6. الذاكرة الدائمة والأرشيف
                await conn.execute("UPDATE learned_facts SET date_added = $1::date + (date_added::time) WHERE date_added >= CURRENT_DATE AND date_added < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                await conn.execute("UPDATE reports_archive SET created_at = $1::date + (created_at::time) WHERE created_at >= CURRENT_DATE AND created_at < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                
                # 7. الكروت الإلكترونية وبوابة التسديدات
                await conn.execute("UPDATE electronic_cards SET added_at = $1::date + (added_at::time) WHERE added_at >= CURRENT_DATE AND added_at < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                await conn.execute("UPDATE electronic_cards SET sold_at = $1::date + (sold_at::time) WHERE sold_at >= CURRENT_DATE AND sold_at < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                await conn.execute("UPDATE telecom_transactions SET created_at = $1::date + (created_at::time) WHERE created_at >= CURRENT_DATE AND created_at < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                await conn.execute("UPDATE telecom_profits SET date = $1::date + (date::time) WHERE date >= CURRENT_DATE AND date < CURRENT_DATE + INTERVAL '1 day'", new_date_obj)
                
                # 8. المزامنة الذكية (تأكيد أخير لربط تاريخ العميل بأقدم عملية له)
                await conn.execute("""
                    UPDATE users
                    SET joined_at = t.min_date
                    FROM (
                        SELECT user_id, MIN(date) AS min_date 
                        FROM transactions 
                        GROUP BY user_id
                    ) AS t
                    WHERE users.user_id = t.user_id AND t.min_date < users.joined_at
                """)
                
    await wait_msg.edit_text(f"✅ **تم النقل الشامل بنجاح!** 🚀\nتم تغيير تاريخ **كل شيء** تم إدخاله اليوم (عملاء، ديون، عمليات، أرباح، طلبات، كروت إلكترونية، تسديدات) إلى تاريخ **{new_date_str}**.")

@router.message(Command("set_region"))
async def set_client_region(message: types.Message, command: CommandObject):
    """أمر سري لإضافة أو تعديل منطقة/قرية العميل"""
    if message.from_user.id != ADMIN_ID: return
    
    if not command.args: 
        return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/set_region [رقم_العميل] [اسم القرية/المنطقة]`\n\nمثال:\n`/set_region 123456789 قرية البلس`")
    
    args = command.args.split(maxsplit=1)
    if len(args) < 2: 
        return await message.answer("⚠️ يرجى إدخال رقم العميل واسم المنطقة.")
    
    try:
        client_id = int(args[0].replace('[', '').replace(']', '').strip())
        region = args[1].strip()
    except ValueError:
        return await message.answer("⚠️ يرجى إدخال رقم العميل بشكل صحيح.")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            user = await conn.fetchrow("SELECT name FROM users WHERE user_id = $1", client_id)
            if not user: 
                return await message.answer("❌ هذا العميل غير موجود في النظام.")
            
            await conn.execute("UPDATE users SET region = $1 WHERE user_id = $2", region, client_id)
            
    await message.answer(f"✅ **تم ربط المنطقة بنجاح!**\n\n👤 العميل: {user['name']}\n📍 المنطقة: **{region}**")

@router.message(Command("wa_list"))
async def list_whatsapp_clients(message):
    """أمر إداري لعرض جميع العملاء الذين لديهم أرقام واتساب مسجلة"""
    if message.from_user.id not in [ADMIN_ID, NETWORK_OWNER_ID]:
        return
        
    await message.answer("⏳ جاري جلب قائمة أرقام الواتساب...")
    
    try:
        import database
        if not database.pool:
            return await message.answer("❌ قاعدة البيانات غير متصلة حالياً.")
            
        async with database.pool.acquire() as conn:
            # جلب العملاء من جدول users الذين لديهم رقم هاتف مسجل
            clients = await conn.fetch("SELECT user_id, name, phone FROM users WHERE phone IS NOT NULL AND phone != 'none' AND phone != ''")
            
        text = "📱 **قائمة العملاء المرتبطين بالواتساب:**\n\n"
        count = 0
        
        for c in clients:
            wa_num = c['phone']
            if wa_num and str(wa_num).lower() != 'none' and str(wa_num).strip() != "":
                text += f"👤 **{c['name']}**\n"
                text += f"🆔 الآي دي: `{c['user_id']}`\n"
                text += f"📞 الواتساب: `{wa_num}`\n"
                text += "〰️〰️〰️〰️\n"
                count += 1
                
        if count == 0:
            await message.answer("لا يوجد أي عميل لديه رقم واتساب مسجل حالياً.")
        else:
            text += f"\n📊 **الإجمالي:** {count} عملاء مربوطين بالواتساب."
            await message.answer(text)
            
    except Exception as e:
        await message.answer(f"❌ حدث خطأ أثناء جلب البيانات: {e}")

@router.message(Command("test_wa"))
async def test_wa_connection(message: types.Message, command: CommandObject):
    """أمر سري لفحص اتصال الواتساب ومعرفة الخطأ"""
    if message.from_user.id != ADMIN_ID: return
    if not command.args:
        return await message.answer("⚠️ أرسل الرقم مع الأمر هكذا:\n`/test_wa 967777000000`")
        
    phone = command.args.strip()
    await message.answer("⏳ جاري إرسال رسالة تجريبية للواتساب...")
    
    url = f"{WA_API_URL}/message/sendText/{WA_INSTANCE}"
    headers = {"apikey": WA_API_KEY, "Content-Type": "application/json"}
    payload = {
        "number": phone,
        "text": "رسالة تجريبية من نظام الشهاب pro ??"
    }
    
    try:
        async with aiohttp.ClientSession( ) as session:
            async with session.post(url, headers=headers, json=payload) as response:
                status = response.status
                text = await response.text()
                
                if status in [200, 201]:
                    await message.answer(f"✅ **نجاح!** السيرفر رد بـ:\n`{text}`")
                else:
                    await message.answer(f"❌ **فشل!** السيرفر رفض الطلب والسبب:\n`{text}`\n(كود الخطأ: {status})")
    except Exception as e:
        await message.answer(f"⚠️ **خطأ في الاتصال بالسيرفر:**\n`{e}`")
        
@router.message(Command("set_wa_webhook"))
async def set_wa_webhook_command(message: types.Message):
    """أمر سري لربط سيرفر الواتساب بسيرفر البوت (Webhook) بضغطة زر"""
    if message.from_user.id != ADMIN_ID: return
    
    await message.answer("⏳ جاري إرسال أمر الربط لسيرفر الواتساب...")
    
    # 🌟 الإصلاح الجذري: جلب التوكن السري ودمجه في الرابط لكي يقبله السيرفر!
    from config import API_SECRET_KEY
    my_bot_webhook_url = f"https://my-bot-ehio.onrender.com/webhook/whatsapp?token={API_SECRET_KEY}"
    
    # رابط إعداد الـ Webhook في سيرفر الواتساب (Evolution API )
    url = f"{WA_API_URL}/webhook/set/{WA_INSTANCE}"
    headers = {"apikey": WA_API_KEY, "Content-Type": "application/json"}
    
    # البيانات التي سنرسلها للواتساب لنخبره أين يرسل الرسائل
    payload = {
        "webhook": {
            "enabled": True,
            "url": my_bot_webhook_url,
            "byEvents": False,
            "base64": False,
            "events": ["MESSAGES_UPSERT"] # هذا يعني: أرسل لي الرسائل الجديدة فقط
        }
    }
    
    try:
        import aiohttp
        async with aiohttp.ClientSession( ) as session:
            async with session.post(url, headers=headers, json=payload) as response:
                status = response.status
                text = await response.text()
                
                if status in [200, 201]:
                    await message.answer(f"✅ **تم ربط الواتساب بالبوت بنجاح!** 🚀\nالرابط المعتمد الآن هو:\n`{my_bot_webhook_url}`\n\nرد السيرفر:\n`{text}`")
                else:
                    await message.answer(f"❌ **فشل الربط!**\nكود الخطأ: {status}\nالسبب:\n`{text}`")
    except Exception as e:
        await message.answer(f"⚠️ **خطأ في الاتصال:**\n`{e}`")

@router.message(Command("test_my_webhook"))
async def test_my_webhook(message: types.Message):
    """أمر سري لاختبار السيرفر وإثبات أن المشكلة من Evolution API"""
    if message.from_user.id != ADMIN_ID: return
    
    await message.answer("⏳ جاري إرسال رسالة واتساب وهمية للسيرفر لاختباره...")
    
    import aiohttp
    # نرسل الطلب لنفس السيرفر
    url = f"https://my-bot-ehio.onrender.com/webhook/whatsapp?token={API_SECRET_KEY}"
    
    # رسالة وهمية كأنها قادمة من الواتساب
    fake_whatsapp_payload = {
        "data": {
            "messages": [
                {
                    "key": {"remoteJid": "967777123456@s.whatsapp.net", "fromMe": False},
                    "message": {"conversation": "تفعيل رقم: 12345"}
                }
            ]
        }
    }
    
    try:
        async with aiohttp.ClientSession( ) as session:
            async with session.post(url, json=fake_whatsapp_payload) as resp:
                text = await resp.text()
                await message.answer(f"✅ **نتيجة فحص السيرفر:**\nحالة الرد: {resp.status}\nالرد: `{text}`\n\n*(إذا كانت الحالة 200، فهذا يعني أن سيرفرك يعمل بشكل مثالي، والمشكلة 100% من منصة Evolution API معلقة ولا ترسل البيانات!)*")
    except Exception as e:
        await message.answer(f"❌ خطأ: {e}")
        
@router.message(Command("maintenance"))
async def toggle_maintenance_mode(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    current_status = await database.get_setting("maintenance_mode")
    new_status = "off" if current_status == "on" else "on"
    await database.set_setting("maintenance_mode", new_status)
    if new_status == "on": await message.answer("🛑 **تم تفعيل وضع الصيانة!**\nالبوت الآن متوقف للعملاء.")
    else: await message.answer("✅ **تم إيقاف وضع الصيانة!**\nالبوت الآن يعمل بشكل طبيعي.")

@router.message(Command("fix_timezone"))
async def fix_db_timezone(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    if database.pool:
        async with database.pool.acquire() as conn:
            # 1. جلب اسم قاعدة البيانات أولاً بدون أقواس في أمر التعديل
            db_name = await conn.fetchval("SELECT current_database();")
            
            # 2. تطبيق التوقيت على قاعدة البيانات الصحيحة
            await conn.execute(f"ALTER DATABASE {db_name} SET timezone TO 'Asia/Aden';")
            
    await message.answer("✅ **تم ضبط توقيت قاعدة البيانات بنجاح!**\n(الرجاء إعادة تشغيل البوت لكي يتم تطبيق التوقيت الجديد).")

@router.message(Command("clear_alerts"))
async def clear_all_alerts(message: types.Message):
    """أمر سري لتنظيف التنبيهات السريعة المزعجة"""
    if message.from_user.id != ADMIN_ID: return
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # تصفير الوعود بالسداد
            await conn.execute("UPDATE users SET promised_payment = 0.0")
            # حذف سجلات الكروت التي كميتها صفر لكي لا تظهر في التنبيهات
            await conn.execute("DELETE FROM client_inventory WHERE quantity <= 0")
            
    await message.answer("🧹 **تم تنظيف التنبيهات السريعة بنجاح!**\nاضغط /start لترى الشاشة نظيفة.")
    
# ================= أوامر الحماية السيبرانية (Cybersecurity Commands) =================

@router.message(Command("lockdown"))
async def activate_lockdown(message: types.Message):
    """زر الإيقاف الطارئ: يجمد تطبيق الويب بالكامل"""
    if message.from_user.id not in [ADMIN_ID, int(NETWORK_OWNER_ID)]: return
    await database.set_setting("lockdown_mode", "on")
    await message.answer("🚨 **تم تفعيل الإغلاق الأمني الشامل (Lockdown)!** 🚨\n\nتم تجميد تطبيق الويب بالكامل لجميع البقالات والزوار. لا يمكن سحب كروت أو تسديد رصيد.\nأموالك الآن في أمان تام 🛡️.")

@router.message(Command("unlock_system"))
async def deactivate_lockdown(message: types.Message):
    """فك التجميد وإعادة النظام للعمل"""
    if message.from_user.id not in [ADMIN_ID, int(NETWORK_OWNER_ID)]: return
    await database.set_setting("lockdown_mode", "off")
    await message.answer("✅ **تم إيقاف الإغلاق الأمني.**\nالنظام عاد للعمل بشكل طبيعي لجميع المستخدمين 🌐.")

@router.message(Command("ban_ip"))
async def ban_ip_command(message: types.Message, command: CommandObject):
    """حظر آيبي مخترق يدوياً"""
    if message.from_user.id not in [ADMIN_ID, int(NETWORK_OWNER_ID)]: return
    if not command.args: return await message.answer("⚠️ أرسل الآيبي هكذا:\n`/ban_ip 192.168.1.1`")
    
    ip = command.args.strip()
    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute("INSERT INTO banned_ips (ip) VALUES ($1) ON CONFLICT DO NOTHING", ip)
    await message.answer(f"🚫 **تم حظر الآيبي نهائياً:** `{ip}`\nلن يتمكن من فتح الموقع أبداً.")

@router.message(Command("unban_all"))
async def unban_all_ips(message: types.Message):
    """أمر سري لفك الحظر عن جميع الآيبيهات المحظورة"""
    if message.from_user.id not in [ADMIN_ID, int(NETWORK_OWNER_ID)]: return
    
    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE banned_ips;")
            
    # تفريغ الكاش في الذاكرة أيضاً
    from web_api import banned_ips_cache
    banned_ips_cache.clear()
    
    await message.answer("✅ **تم فك الحظر عن جميع الآيبيهات!**\nيمكنك الآن فتح تطبيق الويب بشكل طبيعي.")

@router.message(Command("rehab"))
async def rehab_client_command(message: types.Message, command: CommandObject):
    """إعادة تأهيل بقالة مخترقة (طرد الهكر وتوليد رمز جديد)"""
    if message.from_user.id not in [ADMIN_ID, int(NETWORK_OWNER_ID)]: return
    if not command.args: return await message.answer("⚠️ أرسل رقم البقالة هكذا:\n`/rehab 12345`")

    try: client_id = int(command.args.strip())
    except: return await message.answer("رقم غير صالح.")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            user = await conn.fetchrow("SELECT name, phone FROM users WHERE user_id = $1", client_id)
            if not user: return await message.answer("❌ العميل غير موجود.")
            
            import random
            new_pass = str(random.randint(100000, 999999))
            
            # 🌟 سد ثغرة الكاشير الشبح: مسح قائمة العمال (cashiers) لضمان طرد المخترق نهائياً
            await conn.execute("UPDATE users SET web_password = $1, device_id = NULL, role = 'client', cashiers = '[]'::jsonb WHERE user_id = $2", new_pass, client_id)
            
            # إرسال إشعار للتطبيق بالرمز الجديد بدلاً من الواتساب
            try:
                from web_api import send_web_push
                await send_web_push(client_id, "🛡️ إعادة تأهيل الحساب", f"تم إعادة تنشيط حسابك. رمز الدخول الجديد: {new_pass}")
            except Exception as e: logging.error(f"Error: {e}")
                
    await message.answer(f"✅ **تمت إعادة تأهيل البقالة ({user['name']}) بنجاح!**\nتم مسح بصمة الهاتف القديمة (طرد المخترق)، وتوليد رمز جديد وإرساله للعميل كإشعار.")

# ================= جدار وضع الصيانة (مُسرَّع بالذاكرة المؤقتة) =================
import time
MAINTENANCE_CACHE = {"status": None, "last_update": 0}

async def is_maintenance_on(message: types.Message) -> bool:
    # السماح للمدير والوكيل بالمرور دائماً
    if message.from_user.id in [ADMIN_ID, int(NETWORK_OWNER_ID)]:
        return False
        
    current_time = time.time()
    
    # 🚀 التعديل: نسأل قاعدة البيانات مرة واحدة كل 60 ثانية فقط!
    if MAINTENANCE_CACHE["status"] is None or current_time - MAINTENANCE_CACHE["last_update"] > 60:
        MAINTENANCE_CACHE["status"] = await database.get_setting("maintenance_mode")
        MAINTENANCE_CACHE["last_update"] = current_time
        
    return MAINTENANCE_CACHE["status"] == "on"

@router.message(is_maintenance_on)
async def maintenance_interceptor(message: types.Message):
    # إرسال رسالة اعتذار للعميل
    await message.answer("⚙️ **النظام تحت التحديث والمراجعة المالية حالياً.**\nنعتذر عن الإزعاج، سنعود للعمل واستقبال طلباتكم في أقرب وقت ممكن 🌹.")
    
    # إيقاف معالجة الرسالة بهدوء وبدون إرسال إنذارات خطأ للإدارة
    return

@router.callback_query(F.data == "admin_main_menu")
async def admin_main_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    alerts_text, _, _ = await get_dashboard_stats(is_owner=False)
    
    msg = (
        f"👨‍💻 **لوحة تحكم  الوكيل**\n\n"
        f"🔔 **تنبيهات سريعة:**\n{alerts_text}\n"
        f"ماذا تريد أن نفعل الآن؟ 👇"
    )
    kb = await get_main_admin_keyboard()
    await callback.message.edit_text(msg, reply_markup=kb)

@router.callback_query(F.data == "admin_operations_menu")
async def admin_operations_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    kb = await get_operations_submenu_keyboard()
    await callback.message.edit_text("📦 **العمليات والطلبات:**", reply_markup=kb)

@router.callback_query(F.data == "admin_finance_menu")
async def admin_finance_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    kb = await get_finance_submenu_keyboard()
    await callback.message.edit_text("💰 **المالية والمصروفات:**", reply_markup=kb)

@router.callback_query(F.data == "admin_reports_menu")
async def admin_reports_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    kb = await get_reports_submenu_keyboard()
    await callback.message.edit_text("📊 **التقارير والجرد:**", reply_markup=kb)

@router.callback_query(F.data == "admin_settings_menu")
async def admin_settings_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    kb = await get_settings_submenu_keyboard()
    await callback.message.edit_text("⚙️ **الإعدادات والتواصل:**", reply_markup=kb)

@router.callback_query(F.data == "admin_privacy_settings")
async def admin_privacy_settings_callback(callback: types.CallbackQuery):
    await callback.answer()
    kb = await get_privacy_settings_keyboard()
    await callback.message.edit_text("🔒 **إعدادات الخصوصية للمدير:**\n*(تحكم بما يراه المدير العام  الشيخ . رهيب)*", reply_markup=kb)

@router.callback_query(F.data == "toggle_maintenance_btn")
async def toggle_maintenance_btn(callback: types.CallbackQuery):
    current_status = await database.get_setting("maintenance_mode")
    new_status = "off" if current_status == "on" else "on"
    await database.set_setting("maintenance_mode", new_status)
    
    # 🚀 التعديل: تحديث الذاكرة المؤقتة فوراً عند ضغط الزر
    MAINTENANCE_CACHE["status"] = new_status
    MAINTENANCE_CACHE["last_update"] = time.time()
    
    if new_status == "on": await callback.answer("🛑 تم تفعيل وضع الصيانة! العملاء لا يمكنهم استخدام البوت الآن.", show_alert=True)
    else: await callback.answer("✅ تم إيقاف وضع الصيانة! البوت متاح للعملاء الآن.", show_alert=True)

@router.callback_query(F.data == "admin_initial_data")
async def admin_initial_data_menu(callback: types.CallbackQuery):
    await callback.answer()
    msg = (
        "📥 **إدخال الأرصدة الافتتاحية:**\n\n"
        "لإدخال البيانات القديمة، يرجى إرسال الأوامر التالية كرسالة نصية للبوت:\n\n"
        "1️⃣ **لإدخال دين سابق على بقالة:**\n`/set_debt [رقم_العميل] [المبلغ]`\n\n"
        "2️⃣ **لإدخال كروت سابقة في جيب بقالة:**\n`/set_inv [رقم_العميل] [اسم_الكرت] [الكمية]`\n\n"
        "3️⃣ **لإدخال دين سابق عليك للمدير العام:**\n`/set_gm_debt [المبلغ]`"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_settings_menu")]])
    await callback.message.edit_text(msg, reply_markup=kb)

# ================= إدارة فئات الكروت والأسعار =================
@router.callback_query(F.data == "admin_manage_cards")
async def manage_cards_menu(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("⚙️ **إدارة فئات الكروت والأسعار:**", reply_markup=await get_manage_cards_keyboard())

@router.callback_query(F.data == "add_card_type")
async def add_card_type_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("➕ **إضافة فئة كرت جديدة**\nالرجاء إدخال اسم الفئة (مثال: أبو 100):")
    await state.set_state(ManageCardsFlow.waiting_for_new_name)

@router.message(ManageCardsFlow.waiting_for_new_name)
async def add_card_type_name(message: types.Message, state: FSMContext):
    # تنظيف الاسم من المسافات الزائدة لضمان دقة المطابقة
    card_name = " ".join(message.text.split())
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # 🌟 فحص ذكي: هل الفئة موجودة مسبقاً؟
            exists = await conn.fetchval("SELECT 1 FROM inventory WHERE card_type = $1", card_name)
            if exists:
                return await message.answer(f"❌ عذراً، الفئة (**{card_name}**) موجودة مسبقاً في المخزون!\n\nالرجاء إدخال اسم فئة جديد، أو اضغط /cancel للإلغاء:")
                
    await state.update_data(new_card_name=card_name)
    await message.answer("الآن، أدخل **سعر التكلفة (سعر المدير)** للكرت (مثال: 70):")
    await state.set_state(ManageCardsFlow.waiting_for_cost_price)

@router.message(ManageCardsFlow.waiting_for_cost_price)
async def add_card_type_cost(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("الرجاء إدخال رقم صحيح للسعر.")
    await state.update_data(cost_price=str(message.text))
    await message.answer("الآن، أدخل **سعر الجملة (للبقالات)** للكرت (مثال: 80):")
    await state.set_state(ManageCardsFlow.waiting_for_new_price)

@router.message(ManageCardsFlow.waiting_for_new_price)
async def add_card_type_price(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("الرجاء إدخال رقم صحيح للسعر.")
    await state.update_data(new_card_price=str(message.text))
    await message.answer("الآن، أدخل **سعر التجزئة (للطياري)** للكرت (مثال: 100):")
    await state.set_state(ManageCardsFlow.waiting_for_retail_price)

@router.message(ManageCardsFlow.waiting_for_retail_price)
async def add_card_type_retail_price(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("الرجاء إدخال رقم صحيح لسعر التجزئة.")
    data = await state.get_data()
    new_card_name = data["new_card_name"]
    cost_price = Decimal(data["cost_price"])
    new_card_price = Decimal(data["new_card_price"])
    new_retail_price = Decimal(message.text)
    
    try:
        if database.pool:
            async with database.pool.acquire() as conn:
                await conn.execute("INSERT INTO inventory (card_type, quantity, cost_price, price, retail_price) VALUES ($1, 0, $2, $3, $4)", new_card_name, cost_price, new_card_price, new_retail_price)
        await state.clear()
        await message.answer(f"✅ تم إضافة فئة كرت ({new_card_name}) بنجاح.\n▪️ التكلفة: {cost_price}\n▪️ الجملة: {new_card_price}\n▪️ التجزئة: {new_retail_price}", reply_markup=await get_manage_cards_keyboard())
    except Exception as e:
        # 🌟 التقاط أي خطأ برمجي بهدوء دون إيقاف البوت
        await message.answer(f"❌ حدث خطأ: يبدو أن هذه الفئة موجودة مسبقاً.\nالرجاء المحاولة باسم مختلف.", reply_markup=await get_manage_cards_keyboard())
        await state.clear()
        
@router.callback_query(F.data == "edit_card_price_menu")
async def edit_card_price_menu(callback: types.CallbackQuery):
    kb = await get_cards_keyboard("editprice")
    await callback.message.edit_text("✏️ اختر الفئة التي تريد تعديل سعرها:", reply_markup=kb)
    
@router.callback_query(F.data.startswith("editprice_"))
async def edit_card_price_start(callback: types.CallbackQuery, state: FSMContext):
    card_type = callback.data.split("_")[1]
    await state.update_data(card_type_to_edit=card_type)
    await callback.message.edit_text(f"✏️ تعديل أسعار كرت ({card_type})\n\nأدخل **سعر التكلفة (سعر المدير)** الجديد:")
    await state.set_state(EditCardFlow.waiting_for_cost)

@router.message(EditCardFlow.waiting_for_cost)
async def edit_card_cost_execute(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("الرجاء إدخال رقم صحيح للسعر.")
    await state.update_data(new_cost=str(message.text))
    await message.answer("أدخل **سعر الجملة (للبقالات)** الجديد:")
    await state.set_state(EditCardFlow.waiting_for_wholesale)

@router.message(EditCardFlow.waiting_for_wholesale)
async def edit_card_wholesale_execute(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("الرجاء إدخال رقم صحيح للسعر.")
    await state.update_data(new_wholesale=str(message.text))
    await message.answer("أدخل **سعر التجزئة (للطياري)** الجديد:")
    await state.set_state(EditCardFlow.waiting_for_retail)

@router.message(EditCardFlow.waiting_for_retail)
async def edit_card_retail_execute(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("الرجاء إدخال رقم صحيح للسعر.")
    data = await state.get_data()
    card_type = data["card_type_to_edit"]
    new_cost = Decimal(data["new_cost"])
    new_wholesale = Decimal(data["new_wholesale"])
    new_retail = Decimal(message.text)
    if new_retail < new_wholesale or new_wholesale < new_cost:
        return await message.answer("❌ رفض أمني: لا يمكن أن يكون سعر البيع أقل من سعر التكلفة! الرجاء إعادة المحاولة.")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                # 1. جلب السعر القديم لحساب الفارق
                old_prices = await conn.fetchrow("SELECT cost_price, price FROM inventory WHERE card_type = $1", card_type)
                old_profit = Decimal(old_prices['price']) - Decimal(old_prices['cost_price']) if old_prices else Decimal('0.0')
                new_profit = new_wholesale - new_cost
                profit_diff = new_profit - old_profit
                
                # 2. تحديث الأسعار في المخزون
                await conn.execute("UPDATE inventory SET cost_price = $1, price = $2, retail_price = $3 WHERE card_type = $4", new_cost, new_wholesale, new_retail, card_type)
                
                # 3. 🌟 التعديل المحاسبي: تحديث الأرباح المعلقة للعملاء الذين يمتلكون هذا الكرت
                if profit_diff != 0:
                    clients_with_card = await conn.fetch("SELECT user_id, quantity FROM client_inventory WHERE card_type = $1 AND quantity > 0", card_type)
                    for client in clients_with_card:
                        profit_adjustment = profit_diff * client['quantity']
                        await conn.execute("UPDATE users SET pending_profit = pending_profit + $1 WHERE user_id = $2", profit_adjustment, client['user_id'])

    await state.clear()
    await message.answer(f"✅ تم تعديل سعر الكرت ({card_type}) بنجاح.\n▪️ التكلفة: {new_cost}\n▪️ الجملة: {new_wholesale}\n▪️ التجزئة: {new_retail}\n*(تم تحديث الأرباح المعلقة للعملاء تلقائياً)*", reply_markup=await get_manage_cards_keyboard())

# ================= تعديل اسم الفئة =================
@router.callback_query(F.data == "rename_card_type_menu")
async def rename_card_menu(callback: types.CallbackQuery):
    kb = await get_cards_keyboard("rencard")
    await callback.message.edit_text("📝 اختر الفئة التي تريد تغيير اسمها:", reply_markup=kb)

@router.callback_query(F.data.startswith("rencard_"))
async def rename_card_start(callback: types.CallbackQuery, state: FSMContext):
    old_name = callback.data.split("_")[1]
    await state.update_data(old_card_name=old_name)
    await callback.message.edit_text(f"📝 **تغيير اسم فئة:** ({old_name})\n\nالرجاء إدخال الاسم الجديد للفئة:")
    await state.set_state(RenameCardFlow.waiting_for_new_name)

@router.message(RenameCardFlow.waiting_for_new_name)
async def rename_card_execute(message: types.Message, state: FSMContext):
    new_name = " ".join(message.text.split())
    data = await state.get_data()
    old_name = data['old_card_name']
    
    from core_accounting import core_rename_card_type
    result = await core_rename_card_type(old_name, new_name)
    
    if result["status"] == "error":
        return await message.answer(f"❌ {result['message']}")
        
    await state.clear()
    await message.answer(f"✅ **تم تغيير الاسم بنجاح!**\nتم تحديث اسم الفئة من ({old_name}) إلى ({new_name}) في جميع السجلات والمخازن.", reply_markup=await get_manage_cards_keyboard())

@router.callback_query(F.data == "delete_card_type_menu")
async def delete_card_menu(callback: types.CallbackQuery):
    kb = await get_cards_keyboard("delcard")
    await callback.message.edit_text("🗑️ اختر الفئة التي تريد حذفها:", reply_markup=kb)

@router.callback_query(F.data.startswith("delcard_"))
async def delete_card_execute(callback: types.CallbackQuery):
    card_type = callback.data.split("_")[1]
    if not callback.data.endswith("_confirm"):
        confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⚠️ نعم، احذف ({card_type}) نهائياً", callback_data=f"delcard_{card_type}_confirm")],
            [InlineKeyboardButton(text="❌ تراجع", callback_data="admin_manage_cards")]
        ])
        return await callback.message.edit_text(f"⚠️ **تحذير:** هل أنت متأكد أنك تريد حذف فئة ({card_type})؟", reply_markup=confirm_kb)
    
    real_card_type = card_type.replace("_confirm", "")
    
    from core_accounting import core_delete_card_type
    result = await core_delete_card_type(real_card_type)
    
    if result["status"] == "error":
        return await callback.message.edit_text(result["message"])
            
    await callback.message.edit_text(f"✅ تم حذف كرت ({real_card_type}) من النظام.")
    await callback.message.answer("⚙️ **إدارة فئات الكروت والأسعار:**", reply_markup=await get_manage_cards_keyboard())
   
# ================= 1. إرسال الكروت (للمدير العام) =================
@router.callback_query(F.data == "owner_send_cards")
async def owner_send_cards_start(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if 'shipment_items' not in data:
        await state.update_data(shipment_items={})
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 كروت ورقية (خدش)", callback_data="msend_cat_physical")],
        [InlineKeyboardButton(text="📱 كروت إلكترونية", callback_data="msend_cat_electronic")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_urgent_commands_menu")]
    ])
    await callback.message.edit_text("📦 **إرسال كروت للوكيل:**\nحدد نوع الكروت التي تريد إرسالها:", reply_markup=kb)

@router.callback_query(F.data.startswith("msend_cat_"))
async def owner_send_category_selected(callback: types.CallbackQuery, state: FSMContext):
    category = callback.data.split("_")[2]
    kb = await get_cards_keyboard("msend", category)
    await callback.message.edit_text("📦 اختر الفئة التي أرسلتها مع المندوب:", reply_markup=kb)
    await state.set_state(ManagerSendCardsFlow.waiting_for_type)

@router.callback_query(ManagerSendCardsFlow.waiting_for_type, F.data.startswith("msend_"))
async def owner_send_type(callback: types.CallbackQuery, state: FSMContext):
    ctype = callback.data.replace("msend_", "")
    await state.update_data(current_type=ctype)
    
    await callback.message.edit_text("⏳ جاري تحليل احتياج السوق لهذه الفئة...")
    
    suggestion_text = ""
    if database.pool:
        async with database.pool.acquire() as conn:
            current_stock = await conn.fetchval("SELECT quantity FROM inventory WHERE card_type = $1", ctype)
            current_stock = current_stock if current_stock else 0
            
            # 🌟 سد ثغرة الذكاء الاصطناعي: تجاهل المبيعات الملغاة لكي يكون الاقتراح دقيقاً 100%
            txs = await conn.fetch("SELECT details FROM transactions WHERE type IN ('تسليم_لعميل', 'بيع_مباشر') AND is_reverted = FALSE AND date >= CURRENT_DATE - INTERVAL '30 days' AND details LIKE $1", f"%{ctype}%")
            monthly_sales = 0
            for tx in txs:
                import re
                match = re.search(r"(\d+)\s*كرت", tx['details'])
                if match: monthly_sales += int(match.group(1))
                
    if monthly_sales > 0:
        suggestion_text = (
            f"💡 **مستشار التموين الذكي:**\n"
            f"▪️ مخزون الوكيل الحالي: **{current_stock} كرت**\n"
            f"▪️ معدل سحب السوق: **{monthly_sales} كرت شهرياً**\n\n"
            f"📦 **الكميات المقترحة للإرسال:**\n"
            f"- لتموين شهر: {monthly_sales} كرت\n"
            f"- لتموين شهرين: {monthly_sales * 2} كرت\n"
            f"- لتموين 3 أشهر: {monthly_sales * 3} كرت\n\n"
        )
    else:
        suggestion_text = (
            f"💡 **مستشار التموين الذكي:**\n"
            f"▪️ مخزون الوكيل الحالي: **{current_stock} كرت**\n"
            f"(لا توجد بيانات سحب كافية لهذا الكرت خلال الشهر الماضي لحساب التنبؤ).\n\n"
        )
        
    await callback.message.edit_text(f"{suggestion_text}🔢 **كم عدد الكروت التي تريد إرسالها الآن من فئة ({ctype})؟**")
    await state.set_state(ManagerSendCardsFlow.waiting_for_quantity)

@router.message(ManagerSendCardsFlow.waiting_for_quantity)
async def owner_send_quantity(message: types.Message, state: FSMContext, bot: Bot):
    if not message.text.isdigit(): return await message.answer("أرقام فقط!")
    qty = int(message.text)
    data = await state.get_data()
    ctype = data['current_type']
    items = data.get('shipment_items', {})
    
    items[ctype] = items.get(ctype, 0) + qty
    await state.update_data(shipment_items=items)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ إضافة فئة أخرى", callback_data="owner_send_cards")],
        [InlineKeyboardButton(text="✅ اعتماد وإرسال المندوب", callback_data="confirm_shipment")]
    ])
    
    summary = "\n".join([f"▪️ {k}: {v} كرت" for k, v in items.items()])
    await message.answer(f"📦 **الإرسالية الحالية:**\n{summary}\n\nماذا تريد أن تفعل؟", reply_markup=kb)

@router.callback_query(F.data == "confirm_shipment")
async def confirm_shipment(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    items = data.get('shipment_items', {})
    
    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute("INSERT INTO pending_shipments (manager_items) VALUES ($1)", json.dumps(items))
            
    await callback.message.edit_text("✅ **تم حفظ الإرسالية!**\nوهي الآن معلقة بانتظار استلام الوكيل ومطابقته لها.")
    try:
        await smart_notify(bot, ADMIN_ID, "🚚 **إشعار من الإدارة:**\nتم إرسال دفعة كروت جديدة مع المندوب وهي في الطريق إليك.\n*(الرجاء فرزها وإدخال العدد في البوت فور وصولها لمطابقتها).*")
    except Exception as e: logging.error(f"Error: {e}")
    await state.clear()

# ================= 2. استلام الكروت (للوكيل - استلام أعمى) =================
@router.callback_query(F.data == "admin_receive")
async def agent_receive_start(callback: types.CallbackQuery, state: FSMContext):
    if database.pool:
        async with database.pool.acquire() as conn:
            pending = await conn.fetchrow("SELECT id FROM pending_shipments WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1")
            if not pending:
                return await callback.message.edit_text("📦 لا توجد إرساليات معلقة في الطريق حالياً.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_operations_menu")]]))
            
            data = await state.get_data()
            agent_items = data.get('agent_items', {})
            await state.update_data(shipment_id=pending['id'], agent_items=agent_items)
            
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 استلام كروت ورقية", callback_data="arecv_cat_physical")],
                [InlineKeyboardButton(text="📱 استلام كروت إلكترونية", callback_data="arecv_cat_electronic")],
                [InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_operations_menu")]
            ])
            await callback.message.edit_text("📦 **استلام كروت من الإدارة:**\nحدد نوع الكروت التي تريد فرزها:", reply_markup=kb)

@router.callback_query(F.data.startswith("arecv_cat_"))
async def agent_receive_category_selected(callback: types.CallbackQuery, state: FSMContext):
    category = callback.data.split("_")[2]
    
    # 🌟 منع الوكيل من استلام الإلكتروني عبر البوت وتوجيهه للويب 🌟
    if category == "electronic":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_receive")]])
        return await callback.message.edit_text("📱 **استلام الكروت الإلكترونية:**\n\nلضمان الدقة والأمان، يرجى فتح **تطبيق الويب** والذهاب إلى (قسم العمليات -> استلام كروت إلكترونية) لكي تقوم بلصق الأرقام السرية ويقوم النظام بفرزها آلياً.", reply_markup=kb)
    
    # إذا اختار ورقي، نكمل بشكل طبيعي
    kb = await get_cards_keyboard("arecv", "physical")
    await callback.message.edit_text("📦 **استلام كروت ورقية (فرز فعلي):**\nاختر الفئة التي وجدتها بيدك الآن:", reply_markup=kb)
    await state.set_state(AgentReceiveBlindFlow.waiting_for_type)

@router.callback_query(AgentReceiveBlindFlow.waiting_for_type, F.data.startswith("arecv_"))
async def agent_receive_type(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(current_type=callback.data.split("_")[1])
    await callback.message.edit_text("🔢 كم عدد الكروت التي وجدتها من هذه الفئة؟")
    await state.set_state(AgentReceiveBlindFlow.waiting_for_quantity)

@router.message(AgentReceiveBlindFlow.waiting_for_quantity)
async def agent_receive_quantity(message: types.Message, state: FSMContext, bot: Bot):
    if not message.text.isdigit(): return await message.answer("أرقام فقط!")
    qty = int(message.text)
    data = await state.get_data()
    ctype = data['current_type']
    items = data.get('agent_items', {})
    
    items[ctype] = items.get(ctype, 0) + qty
    await state.update_data(agent_items=items)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ فرز فئة أخرى", callback_data="admin_receive")],
        [InlineKeyboardButton(text="✅ إنهاء الفرز والمطابقة", callback_data="finish_blind_receive")]
    ])
    
    summary = "\n".join([f"▪️ {k}: {v} كرت" for k, v in items.items()])
    await message.answer(f"📦 **ما قمت بفرزه حتى الآن:**\n{summary}\n\nهل انتهيت من عد كل الكروت؟", reply_markup=kb)

@router.callback_query(F.data == "finish_blind_receive")
async def finish_blind_receive(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    agent_items = data.get('agent_items', {})
    shipment_id = data['shipment_id']
    
    await callback.message.edit_text("⏳ جاري المطابقة مع سجلات الإدارة...")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            shipment = await conn.fetchrow("SELECT manager_items FROM pending_shipments WHERE id = $1", shipment_id)
            manager_items = json.loads(shipment['manager_items'])
            
            if agent_items == manager_items:
                # 🌟 التوحيد: استدعاء المطبخ المركزي
                from core_accounting import core_receive_shipment
                await core_receive_shipment(shipment_id, agent_items, 'exact')
                
                await callback.message.edit_text("✅ **مطابقة 100%!**\nتم إضافة الكروت لمخزونك بنجاح.")
                try: await smart_notify(bot, NETWORK_OWNER_ID, "✅ **إشعار استلام:**\nالوكيل استلم الإرسالية بنجاح والمطابقة سليمة 100%.")
                except Exception as e: logging.error(f"Error: {e}")

                wa_text = f"📦 *إشعار استلام ومطابقة:*\nيا شيخ رهيب، استلم لوكيل (وليد) الإرسالية بنجاح.\nتمت المطابقة بنسبة 100% وتم إضافة الكروت إلى مخزون الوكيل 🌹"
                await send_whatsapp_message(GM_WA_NUMBER, wa_text)

            # --- التعديل المضاف هنا (حالة عدم التطابق) ---
            else:
                # 1. حفظ جرد الوكيل في قاعدة البيانات
                await conn.execute("UPDATE pending_shipments SET agent_items = $1 WHERE id = $2", json.dumps(agent_items), shipment_id)
                
                # 2. إخبار الوكيل بوجود اختلاف
                await callback.message.edit_text("⚠️ **يوجد اختلاف بين ما استلمته وما أرسلته الإدارة!**\nتم إيقاف العملية ورفع تقرير للمدير العام لاتخاذ القرار.")
                
                # 3. إرسال إشعار للمدير العام مع أزرار القرار
                mismatch_details = "🚨 **تنبيه: اختلاف في الإرسالية!**\nالوكيل قام بفرز الإرسالية ووجد اختلافاً:\n\n"
                mismatch_details += "📦 **ما أرسلته أنت:**\n"
                for k, v in manager_items.items(): mismatch_details += f"▪️ {k}: {v} كرت\n"
                mismatch_details += "\n👀 **ما وجده الوكيل:**\n"
                for k, v in agent_items.items(): mismatch_details += f"▪️ {k}: {v} كرت\n"
                mismatch_details += "\nما هو قرارك؟"

                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ اعتماد العدد الفعلي للوكيل", callback_data=f"resolve_shipment_{shipment_id}_accept")],
                    [InlineKeyboardButton(text="❌ إلغاء الإرسالية بالكامل", callback_data=f"resolve_shipment_{shipment_id}_cancel")]
                ])
                
                try: 
                    await smart_notify(bot, NETWORK_OWNER_ID, mismatch_details, reply_markup=kb)
                except Exception as e: logging.error(f"Error: {e}")

    # إنهاء الحالة حتى لا يعلق البوت
    await state.clear()


@router.callback_query(F.data.startswith("resolve_shipment_"))
async def resolve_shipment(callback: types.CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    shipment_id = int(parts[2])
    action = parts[3]
    
    if database.pool:
        async with database.pool.acquire() as conn:
            shipment = await conn.fetchrow("SELECT agent_items, status FROM pending_shipments WHERE id = $1", shipment_id)
            if shipment['status'] != 'pending':
                return await callback.answer("تمت معالجة هذه الإرسالية مسبقاً.", show_alert=True)
                
            if action == "cancel":
                from core_accounting import core_cancel_shipment
                res = await core_cancel_shipment(shipment_id)
                if res["status"] == "error":
                    return await callback.answer(res["message"], show_alert=True)
                    
                await callback.message.edit_text("❌ تم إلغاء الإرسالية بالكامل.")
                try: await smart_notify(bot, ADMIN_ID, "❌ **إشعار:** الإدارة قامت بإلغاء الإرسالية المعلقة بسبب الاختلاف.")
                except Exception as e: logging.error(f"Error: {e}")
                except Exception as e: logging.error(f"Error: {e}")

            elif action == "accept":
                agent_items = json.loads(shipment['agent_items'])
                
                # 🌟 التوحيد: استدعاء المطبخ المركزي
                from core_accounting import core_receive_shipment
                await core_receive_shipment(shipment_id, agent_items, 'resolved')
                
                await callback.message.edit_text("✅ تم اعتماد العدد الفعلي الذي استلمه الوكيل، وتم تحديث حساباته بناءً عليه فقط.")
                try: await smart_notify(bot, ADMIN_ID, "✅ **إشعار:** الإدارة اعتمدت العدد الفعلي الذي أدخلته. تم إضافة الكروت لمخزونك بنجاح.")
                except Exception as e: logging.error(f"Error: {e}")

# ================= 2. تسليم كروت لعميل (جملة) =================
@router.callback_query(F.data == "admin_give")
@router.callback_query(F.data.startswith("admin_give_page_"))
async def start_give(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    page = 0
    if callback.data.startswith("admin_give_page_"):
        page = int(callback.data.split("_")[3])

    ITEMS_PER_PAGE = 8
    clients_kb = []
    total_pages = 1
    total_clients = 0 # 👈 تم إضافة هذا السطر لمنع الخطأ
    
    if database.pool:
        async with database.pool.acquire() as conn:
            total_clients = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role = 'client'")
            
            if total_clients > 0:
                total_pages, offset = get_pagination_math(total_clients, page, ITEMS_PER_PAGE)
                current_clients = await conn.fetch("SELECT user_id, name, debt FROM users WHERE role = 'client' ORDER BY user_id LIMIT $1 OFFSET $2", ITEMS_PER_PAGE, offset)

                for c in current_clients:
                    debt = Decimal(c['debt'])
                    indicator = "🟢" if debt <= 0 else ("🟡" if debt < 50000 else "🔴")
                    clients_kb.append([InlineKeyboardButton(text=f"{indicator} {c['name']}", callback_data=f"gclient_{c['user_id']}")])

    nav_buttons = get_pagination_buttons(page, total_pages, "admin_give_page")
    if nav_buttons: clients_kb.append(nav_buttons)
        
    clients_kb.append([InlineKeyboardButton(text="🔙 العودة لإدارة العمليات", callback_data="admin_operations_menu")])
    
    # 👈 تم تصحيح المتغير هنا إلى total_clients
    text = f"👥 اختر العميل (صاحب البقالة) - صفحة {page+1}/{total_pages}:" if total_clients > 0 else "👥 لا يوجد عملاء مسجلين حالياً."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=clients_kb))
    await state.set_state(GiveCardsFlow.waiting_for_client)

@router.callback_query(F.data.startswith("gclient_"))
async def process_gclient(callback: types.CallbackQuery, state: FSMContext):
    # 🌟 إضافة ميزة التجاوز السري
    is_override = "override" in callback.data
    
    if "cleared" in callback.data:
        client_id = int(callback.data.split("_")[2])
        is_cleared = True
    elif is_override:
        client_id = int(callback.data.split("_")[2])
        is_cleared = True # نعتبره مجروداً لكي يتجاوز الفحص
    else:
        client_id = int(callback.data.split("_")[1])
        is_cleared = False
    
    suggestion_text = ""
    suggested_order = {}

    # 🌟 استدعاء الحارس الأمني والذكاء الاصطناعي من المطبخ المركزي 🌟
    if not is_cleared:
        from core_accounting import core_pre_give_cards_check
        check_result = await core_pre_give_cards_check(client_id)
        
        if check_result.get("status") == "blocked":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚶‍♂️ بدء الجرد والتحصيل", callback_data=f"visit_inventory_{client_id}")],
                [InlineKeyboardButton(text="🔑 تجاوز أمني سري (على مسؤوليتي)", callback_data=f"gclient_override_{client_id}")], # 🌟 زر التجاوز السري
                [InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_operations_menu")]
            ])
            return await callback.message.edit_text(
                check_result.get("message", "🚨 رفض أمني من النظام!"),
                reply_markup=kb
            )
        elif check_result.get("status") == "clear":
            suggested_order = check_result.get("suggested_order", {})
            if suggested_order:
                suggestion_text = "💡 **نصيحة سيستم (بناءً على سحب آخر 30 يوم + 20% احتياطي):**\n"
                for ctype, qty in suggested_order.items():
                    suggestion_text += f"▪️ {qty} كرت ({ctype})\n"
                suggestion_text += "\n"

    await state.update_data(client_id=client_id, suggested_order=suggested_order)
    
    kb = await get_cards_keyboard("gtype", "physical")
    
    if suggested_order:
        kb.inline_keyboard.insert(0, [InlineKeyboardButton(text="✅ اعتماد نصيحة سيستم بالكامل", callback_data=f"auto_give_{client_id}")])
        
    override_warning = "⚠️ *(وضع التجاوز الأمني مفعل)*\n\n" if is_override else ""
    await callback.message.edit_text(f"{override_warning}{suggestion_text}📦 اختر الفئة التي تريد تسليمها للعميل يدوياً، أو اعتمد النصيحة:", reply_markup=kb)
    await state.set_state(GiveCardsFlow.waiting_for_type)

@router.callback_query(GiveCardsFlow.waiting_for_type, F.data.startswith("gtype_"))
async def process_gtype(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(card_type=callback.data.split("_")[1])
    await callback.message.edit_text("🔢 كم عدد الكروت؟")
    await state.set_state(GiveCardsFlow.waiting_for_quantity)

@router.message(GiveCardsFlow.waiting_for_quantity)
async def process_gquantity(message: types.Message, state: FSMContext, bot: Bot):
    text = message.text.strip()
    override = False
    
    if text.endswith("#"):
        override = True
        text = text[:-1] 
        
    if not text.isdigit(): 
        return await message.answer("الرجاء إدخال أرقام فقط! (لا يمكن تجاوز السقف المحدد - كود: ERR#)")
        
    quantity = int(text)
    data = await state.get_data()
    card_type = data["card_type"]
    client_id = data["client_id"]
    
    # 1. رسالة فورية لكسر التأخير
    wait_msg = await message.answer("⏳ جاري تسجيل العملية...")
    
    # 🌟 التوحيد: استدعاء المطبخ المركزي
    from core_accounting import core_give_cards
    result = await core_give_cards(client_id, card_type, quantity, override_limit=override)
    
    if result["status"] == "error":
        await wait_msg.delete()
        if result.get("is_security_rejection"):
            return await message.answer(result["message"])
        else:
            return await message.answer(f"❌ {result['message']}\nالرجاء إدخال كمية صحيحة:")
            
    trans_id = result["trans_id"]
    total_price = result["total_price"]
    client_name = result["client_name"]
    client_phone = result["client_phone"]
    
    # 2. تشغيل فحص المخزون في الخلفية لكي لا يعطل البوت
    asyncio.create_task(check_and_alert_low_stock(bot, card_type, result["current_stock"], result["new_stock"]))
    
    # 3. الرد الفوري بالنجاح والأزرار (بدون انتظار الفاتورة)
    override_note = "\n⚠️ *(تم تجاوز سقف المديونية بصلاحيات المدير)*" if override else ""
    await wait_msg.edit_text(f"✅ **تم التسليم بنجاح!**{override_note}\nتم إضافة {quantity} كرت لمخزون العميل، وتسجيل {int(total_price)} ريال كدين على العميل {client_name}.")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 تسليم فئة أخرى", callback_data=f"gtype_menu_{client_id}")],
        [InlineKeyboardButton(text="🏁 إنهاء الزيارة (إصدار الملخص)", callback_data=f"end_visit_{client_id}")]
    ])
    await message.answer("📦 **هل تريد تسليم فئة أخرى لنفس العميل؟**", reply_markup=kb)
    
    await state.clear()

    # 4. إرسال الفواتير والإشعارات في الخلفية (Background Task) لكي لا تنتظر أنت
    async def send_receipts_and_notifications():
        try:
            pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, client_name, total_price, "تسليم كروت (جملة)", f"عدد {quantity} كرت من فئة {card_type}")
            pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
            
            # إرسال الفاتورة للوكيل بصمت
            await message.answer_document(document=pdf_file, caption=f"🧾 فاتورة العملية #{trans_id}")
            
            # إشعار العميل في التليجرام
            try: await smart_notify(client_id, f"📦 **إشعار استلام كروت:**\nتم إضافة {quantity} كرت من فئة {card_type} إلى مخزونك.\nإجمالي المديونية المضافة: {int(total_price)} ريال.")
            except Exception as e: logging.error(f"Error: {e}")
            
            # إشعار الويب
            try:
                from web_api import send_web_push
                await send_web_push(client_id, "📦 استلام كروت جديدة", f"تم إضافة {quantity} كرت ({card_type}) لمخزونك. الدين المضاف: {int(total_price)} ريال.")
            except Exception as e: logging.error(f"Error: {e}")
            
            # إرسال الواتساب
            pdf_buffer.seek(0)
            wa_text = f"📦 *فاتورة استلام كروت*\nمرحباً {client_name}،\nتم إضافة {quantity} كرت من فئة {card_type} إلى مخزونك.\nإجمالي المديونية المضافة: *{int(total_price)} ريال*.\nمرفق الفاتورة للتأكيد 🌹\n\n👇 *فضلاً، أجب بـ (نعم) إذا فعلاً استلمت الكروت.*"
            await safe_send_whatsapp(client_id, wa_text, pdf_buffer, f"Receipt_{trans_id}.pdf", bot=bot)
        except Exception as e:
            logging.error(f"Background notification error: {e}")

    # إطلاق المهمة الخلفية
    asyncio.create_task(send_receipts_and_notifications())

# ================= دالة الاعتماد التلقائي لنصيحة سيستم (One-Click Order) =================
@router.callback_query(F.data.startswith("auto_give_"))
async def auto_give_cards(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    client_id = int(callback.data.split("_")[2])
    data = await state.get_data()
    suggested_order = data.get("suggested_order", {})
    
    if not suggested_order:
        return await callback.answer("❌ لا توجد نصيحة محفوظة للاعتماد!", show_alert=True)
        
    await callback.answer("⏳ جاري تنفيذ الطلب المجمع...")
    
    # 🌟 التوحيد: استدعاء المطبخ المركزي
    from core_accounting import core_bulk_give_cards
    result = await core_bulk_give_cards(client_id, suggested_order)
    
    if result["status"] == "error":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_operations_menu")]])
        return await callback.message.edit_text(result["message"], reply_markup=kb)
        
    trans_id = result["trans_id"]
    total_price = result["total_price"]
    client_name = result["client_name"]
    client_phone = result["client_phone"]
    details_str = result["details_str"]
    
    # 🚀 التعديل 1: توليد الـ PDF في مسار خلفي
    from pdf_generator import generate_receipt
    from aiogram.types import BufferedInputFile
    import asyncio
    
    pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, client_name, total_price, "تسليم كروت (مجمع)", details_str)
    pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
    
    await callback.message.delete()
    await callback.message.answer_document(document=pdf_file, caption=f"✅ **تم الاعتماد والتسليم بنجاح!**\nتم إضافة الكروت لمخزون العميل، وتسجيل {int(total_price)} ريال كدين على العميل {client_name}.")
    
    try: await smart_notify(client_id, f"📦 **إشعار استلام كروت (تلقائي):**\nتم إضافة الكروت التالية لمخزونك:\n{details_str}\nإجمالي المديونية المضافة: {int(total_price)} ريال.")
    except Exception as e: logging.error(f"Error: {e}")
    
    try:
        from web_api import send_web_push
        asyncio.create_task(send_web_push(client_id, "📦 استلام كروت (تلقائي)", f"تم إضافة الكروت لمخزونك بنجاح. الدين المضاف: {int(total_price)} ريال."))
    except Exception as e: logging.error(f"Error: {e}")
    
    wa_text = f"📦 *فاتورة استلام كروت (تلقائي)*\nمرحباً {client_name}،\nتم إضافة الكروت التالية لمخزونك:\n{details_str}\nإجمالي المديونية المضافة: *{int(total_price)} ريال*.\nمرفق الفاتورة للتأكيد 🌹\n\n👇 *فضلاً، أجب بـ (نعم) إذا فعلاً استلمت الكروت.*"
    asyncio.create_task(safe_send_whatsapp(client_id, wa_text, pdf_buffer, f"Receipt_{trans_id}.pdf", bot=bot))

    await state.clear()
    await callback.message.answer("👨‍💻 **لوحة تحكم  الوكيل**", reply_markup=await get_main_admin_keyboard())

# ✅ الدالة المساعدة لزر "تسليم فئة أخرى"
@router.callback_query(F.data.startswith("gtype_menu_"))
async def show_gtype_menu(callback: types.CallbackQuery, state: FSMContext):
    client_id = int(callback.data.split("_")[2])
    await state.update_data(client_id=client_id)
    
    # 🌟 التعديل هنا: أضفنا "physical" لكي يخفي الكروت الإلكترونية تماماً
    kb = await get_cards_keyboard("gtype", "physical")
    
    await callback.message.edit_text("📦 اختر الفئة التي تريد تسليمها للعميل:", reply_markup=kb)
    await state.set_state(GiveCardsFlow.waiting_for_type)

# ================= 3. البيع المباشر الموحد (تجزئة وجملة كاش) =================
@router.callback_query(F.data.in_(["admin_direct_sale", "admin_wholesale_cash", "admin_personal_credit"]))
async def start_unified_sale(callback: types.CallbackQuery, state: FSMContext):
    sale_mode = "retail" if callback.data == "admin_direct_sale" else ("wholesale" if callback.data == "admin_wholesale_cash" else "personal_credit")
    await state.update_data(sale_mode=sale_mode)

    title = "🛒 **بيع مباشر (تجزئة كاش)**" if sale_mode == "retail" else "🛍️ **بيع مباشر جملة (كاش)**"
    
    # 🌟 التعديل الأمني: عرض الكروت الورقية فقط (الإلكتروني يباع من الويب فقط لاستخراج الرقم السري)
    kb = await get_cards_keyboard("usale", "physical")
    
    await callback.message.edit_text(f"{title}\nاختر فئة الكرت المباع:\n*(ملاحظة: الكروت الإلكترونية تباع من تطبيق الويب فقط)*", reply_markup=kb)
    await state.set_state(DirectSaleStrictFlow.waiting_for_type)

@router.callback_query(DirectSaleStrictFlow.waiting_for_type, F.data.startswith("usale_"))
async def process_usale_type(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(card_type=callback.data.split("_")[1])
    data = await state.get_data()
    msg = "🔢 كم عدد الكروت التي ستبيعها للعميل الطياري؟" if data['sale_mode'] == 'retail' else "🔢 كم عدد الكروت التي ستبيعها بسعر الجملة كاش؟"
    await callback.message.edit_text(msg)
    await state.set_state(DirectSaleStrictFlow.waiting_for_quantity)

@router.message(DirectSaleStrictFlow.waiting_for_quantity)
async def process_usale_quantity(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("أرقام فقط!")
    quantity = int(message.text)
    data = await state.get_data()
    card_type = data["card_type"]
    sale_mode = data["sale_mode"]
    
    # قراءة سريعة للسعر فقط لعرضه للمستخدم
    if database.pool:
        async with database.pool.acquire() as conn:
            current_stock = await conn.fetchval("SELECT quantity FROM inventory WHERE card_type = $1", card_type)
            if current_stock is None or current_stock < quantity:
                return await message.answer(f"❌ **عذراً، رصيدك لا يسمح!**\nمخزونك الحالي من {card_type} هو ({current_stock or 0}) كرت فقط.\nالرجاء إدخال كمية صحيحة:")
            
            prices = await conn.fetchrow("SELECT price, retail_price FROM inventory WHERE card_type = $1", card_type)
            if sale_mode == "retail": total_price = Decimal(prices['retail_price']) * quantity
            else: total_price = Decimal(prices['price']) * quantity
            
    await state.update_data(quantity=quantity, total_price=str(total_price))
    
    if sale_mode == "personal_credit":
        await message.answer(f"💰 إجمالي المطلوب هو **{total_price} ريال**.\n\n✏️ لمن ستبيع الكروت؟ (أدخل اسم الزبون لتسجيله في دفترك الشخصي):")
        # 🌟 يجب إضافة هذه الحالة في كلاس DirectSaleStrictFlow في أعلى الملف
        await state.set_state(DirectSaleStrictFlow.waiting_for_secret_override) # نستخدم حالة موجودة مؤقتاً أو نضيف حالة جديدة
        await state.update_data(is_waiting_for_name=True)
    else:
        price_type_str = "من العميل الطياري" if sale_mode == "retail" else "(بسعر الجملة)"
        await message.answer(f"💰 إجمالي المطلوب {price_type_str} هو **{total_price} ريال**.\nكم المبلغ الكاش الذي استلمته الآن؟ (أرقام فقط)")
        await state.set_state(DirectSaleStrictFlow.waiting_for_cash)

@router.message(DirectSaleStrictFlow.waiting_for_cash)
async def process_usale_cash(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("أرقام فقط!")
    collected_cash = Decimal(message.text)
    data = await state.get_data()
    total_price = Decimal(data["total_price"])
    sale_mode = data["sale_mode"]
    
    # 🌟 التعديل المحاسبي: نظام الكاشير (حساب الباقي للعميل لكي لا يختل الصندوق)
    change_amount = Decimal('0.0')
    if collected_cash > total_price:
        change_amount = collected_cash - total_price
        collected_cash = total_price # النظام يسجل السعر الحقيقي فقط في الصندوق
        
    if collected_cash < total_price:
        await state.update_data(collected_cash=str(collected_cash))
        price_name = "السعر الرسمي المعتمد" if sale_mode == "retail" else "سعر الجملة المعتمد"
        await message.answer(
            f"🚨 **رفض أمني من النظام المركزي!** 🚨\n\n"
            f"❌ **السبب:** المبلغ المستلم ({collected_cash} ريال) أقل من {price_name} ({total_price} ريال).\n"
            f"⚠️ **القرار:** يمنع النظام بيع الكروت بأسعار مخفضة. يرجى استلام المبلغ كاملاً لإصدار الكروت.\n\n"
            f"*(الرجاء إدخال المبلغ كاملاً لإتمام البيع)*",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 إعادة إدخال المبلغ", callback_data="retry_usale_cash")]])
        )
        await state.set_state(DirectSaleStrictFlow.waiting_for_secret_override)
    else:
        # تمرير الباقي للدالة التالية لطباعته
        await state.update_data(change_amount=str(change_amount))
        await execute_unified_sale(message, state, collected_cash, Decimal('0.0'))

@router.callback_query(F.data == "retry_usale_cash")
async def retry_usale_cash(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("كم المبلغ الكاش الذي استلمته الآن؟ (أرقام فقط)")
    await state.set_state(DirectSaleStrictFlow.waiting_for_cash)

@router.message(DirectSaleStrictFlow.waiting_for_secret_override)
async def process_usale_secret(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    # 🌟 معالجة إدخال اسم الزبون للبيع الآجل
    if data.get("is_waiting_for_name"):
        customer_name = message.text.strip()
        total_price = Decimal(data["total_price"])
        await state.update_data(customer_name=customer_name, collected_cash=str(total_price), sale_mode="retail")
        await execute_unified_sale(message, state, total_price, Decimal('0.0'))
        return

    if message.text.strip() == "#":
        collected_cash = Decimal(data["collected_cash"])
        total_price = Decimal(data["total_price"])
        discount = total_price - collected_cash
        
        await message.answer(f"?? *(تم تفعيل الصلاحية السرية للوكيل)*\n✅ تم قبول البيعة بمبلغ ({collected_cash} ريال). تم تسجيل الخصم ({discount} ريال) كعجز يتحمله الوكيل.")
        await execute_unified_sale(message, state, collected_cash, discount)
    else:
        await process_usale_cash(message, state)

async def execute_unified_sale(message: types.Message, state: FSMContext, collected_cash: Decimal, discount: Decimal):
    data = await state.get_data()
    card_type = data["card_type"]
    quantity = data["quantity"]
    sale_mode = data["sale_mode"]
    
    # 🌟 التوحيد: استدعاء المطبخ المركزي
    from core_accounting import core_direct_sale
    result = await core_direct_sale(card_type, quantity, sale_mode, collected_cash, discount)
    
    if result["status"] == "error":
        return await message.answer(f"❌ {result['message']}")
        
    network_amount = result["network_amount"]
    agent_profit = result["agent_profit"]
    current_stock = result["current_stock"]
    new_stock = result["new_stock"]
    
    await check_and_alert_low_stock(message.bot, card_type, current_stock, new_stock)
            
    change_amount = Decimal(data.get("change_amount", "0.0"))
    await state.clear()
    
    change_text = f"\n💵 **الباقي للعميل:** **{int(change_amount)} ريال**" if change_amount > 0 else ""
    customer_name = data.get("customer_name")
    
    if customer_name:
        if database.pool:
            async with database.pool.acquire() as conn:
                cust_id = await conn.fetchval("""
                    INSERT INTO agent_personal_customers (name, debt) VALUES ($1, $2)
                    ON CONFLICT (name) DO UPDATE SET debt = agent_personal_customers.debt + $2
                    RETURNING id
                """, customer_name, collected_cash)
                if not cust_id:
                    cust_id = await conn.fetchval("SELECT id FROM agent_personal_customers WHERE name = $1", customer_name)
                await conn.execute("INSERT INTO agent_personal_ledger (customer_id, type, amount, details) VALUES ($1, 'دين', $2, $3)", cust_id, collected_cash, f"شراء {quantity} كرت {card_type}")
        
        await message.answer(f"✅ **تم تسجيل البيع الآجل!**\nتم تسجيل {int(collected_cash)} ريال كدين على ({customer_name}) في دفترك الشخصي.\n(تم ترحيل {int(network_amount)} لكاش الإدارة، و {int(agent_profit)} لمحفظة أرباحك).")
        await message.answer("📦 **العمليات والطلبات:**", reply_markup=await get_operations_submenu_keyboard())
    elif sale_mode == "retail":
        await message.answer(f"✅ **تم تسجيل البيع المباشر!**\nالعدد: {quantity}\nالمطلوب: {int(collected_cash)} ريال.{change_text}\n(تم ترحيل {int(network_amount)} لكاش الإدارة، و {int(agent_profit)} لمحفظة أرباحك).")
        await message.answer("📦 **العمليات والطلبات:**", reply_markup=await get_operations_submenu_keyboard())

# ================= 4. الزيارة الميدانية الذكية (الجرد والتحصيل) =================
@router.callback_query(F.data.startswith("visit_match_"))
async def visit_match_inventory(callback: types.CallbackQuery, state: FSMContext):
    client_id = int(callback.data.split("_")[2])

    if database.pool:
        async with database.pool.acquire() as conn:
            user = await conn.fetchrow("SELECT debt, telecom_debt FROM users WHERE user_id = $1", client_id)
            old_debt = Decimal(user['debt']) if user and user['debt'] else Decimal('0.0')
            telecom_debt = Decimal(user['telecom_debt']) if user and user['telecom_debt'] else Decimal('0.0')
            
            is_telecom_active = await conn.fetchval("SELECT value FROM settings WHERE key = 'is_telecom_active'")
            is_telecom_active = is_telecom_active or 'off'
            
    # دمج المطلوبين لفحص هل عليه ديون أم لا
    total_required = old_debt
    if is_telecom_active == 'on':
        total_required += telecom_debt
            
    if total_required > 0:
        # 🌟 تغذية خوارزمية الشلال المالي بالبيانات المطلوبة
        await state.update_data(
            visit_client_id=client_id, 
            required_amount=str(old_debt), 
            telecom_debt=str(telecom_debt), 
            is_telecom_active=is_telecom_active
        )
        
        msg = f"✅ **تم تأكيد المخزون (لم يبع شيء).**\n"
        if is_telecom_active == 'on' and telecom_debt > 0:
            msg += f"🔴 **مطلوب للكروت (دين سابق):** {old_debt} ريال\n"
            msg += f"⚡ **مطلوب للتسديدات:** {telecom_debt} ريال\n"
            msg += f"💵 **الإجمالي الكلي المطلوب:** **{total_required} ريال**\n\n"
        else:
            msg += f"🔴 **عليه دين سابق بقيمة:** **{old_debt} ريال**\n\n"
            
        msg += "كم المبلغ الكاش الذي استلمته منه الآن لسداد دينه؟"
        
        await callback.message.edit_text(msg)
        await state.set_state(FieldVisitFlow.waiting_for_cash)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 نعم، تسليم كروت", callback_data=f"gclient_cleared_{client_id}")],
            [InlineKeyboardButton(text="🏁 لا، إنهاء الزيارة", callback_data="cancel_action")]
        ])
        await callback.message.edit_text("✅ **تم تأكيد المخزون (لم يبع شيء).**\nالعميل ليس عليه أي ديون.\n\nهل يريد العميل كروت جديدة الآن؟", reply_markup=kb)

@router.callback_query(F.data.startswith("visit_inventory_"))
async def visit_enter_inventory(callback: types.CallbackQuery, state: FSMContext):
    client_id = int(callback.data.split("_")[2])
    
    if database.pool:
        async with database.pool.acquire() as conn:
            u = await conn.fetchrow("SELECT debt, telecom_debt FROM users WHERE user_id = $1", client_id)
            old_debt = Decimal(u['debt'] or 0) if u else Decimal('0.0')
            telecom_debt = Decimal(u['telecom_debt'] or 0) if u else Decimal('0.0')
            is_telecom_active = await conn.fetchval("SELECT value FROM settings WHERE key = 'is_telecom_active'")
            is_telecom_active = is_telecom_active or 'off'
            
            inv_items = await conn.fetch("SELECT card_type, quantity FROM client_inventory WHERE user_id = $1 AND quantity > 0", client_id)
            
            total_required = old_debt + (telecom_debt if is_telecom_active == 'on' else Decimal('0.0'))
            
            if not inv_items:
                await state.update_data(visit_client_id=client_id, required_amount=str(old_debt), old_debt=str(old_debt), telecom_debt=str(telecom_debt), is_telecom_active=is_telecom_active)
                
                msg = f"هذا العميل ليس لديه كروت في المخزون.\n"
                if is_telecom_active == 'on' and telecom_debt > 0:
                    msg += f"🔴 **مطلوب للكروت (دين سابق):** {old_debt} ريال\n"
                    msg += f"⚡ **مطلوب للتسديدات:** {telecom_debt} ريال\n"
                    msg += f"💵 **الإجمالي الكلي المطلوب:** **{total_required} ريال**\n\n"
                else:
                    msg += f"📜 **إجمالي الدين السابق:** {total_required} ريال.\n\n"
                    
                msg += "كم المبلغ الكاش الذي استلمته منه الآن؟"
                
                await callback.message.edit_text(msg)
                return await state.set_state(FieldVisitFlow.waiting_for_cash)
                
            items_list = [{"card_type": item["card_type"], "expected_qty": item["quantity"]} for item in inv_items]
            
            await state.update_data(
                visit_client_id=client_id,
                inventory_items=items_list,
                current_item_index=0,
                calculated_total_due='0.0',
                old_debt=str(old_debt),
                telecom_debt=str(telecom_debt),
                is_telecom_active=is_telecom_active,
                inventory_summary=""
            )
            
            first_item = items_list[0]
            await callback.message.edit_text(
                f"📦 **بدء الجرد الفعلي:**\n\n"
                f"مسجل في النظام أن العميل لديه: **{first_item['expected_qty']} كرت من فئة ({first_item['card_type']})**.\n\n"
                f"✏️ **كم عدد الكروت المتبقية لديه الآن من هذه الفئة؟** (أرقام فقط):"
            )
            await state.set_state(FieldVisitFlow.waiting_for_card_count)

@router.message(FieldVisitFlow.waiting_for_card_count)
async def visit_process_card_count(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("أرقام فقط!")
    remaining_qty = int(message.text)
    
    data = await state.get_data()
    client_id = data["visit_client_id"]
    items_list = data["inventory_items"]
    idx = data["current_item_index"]
    total_due = Decimal(data["calculated_total_due"])
    old_debt = Decimal(data.get("old_debt", "0.0"))
    summary = data["inventory_summary"]
    
    current_item = items_list[idx]
    expected_qty = current_item["expected_qty"]
    card_type = current_item["card_type"]
    
    from core_accounting import core_record_client_sale
    result = await core_record_client_sale(client_id, card_type, expected_qty, remaining_qty)
    
    if result["status"] == "error":
        return await message.answer(f"❌ {result['message']}")
        
    total_due += Decimal(str(result.get("item_due", 0)))
    sold_qty = result.get("sold_qty", 0)
    adj_msg = result.get("adjustment_msg", "")
    adj_tx_id = result.get("adj_tx_id")
    
    if adj_msg:
        # 🌟 إضافة زر التراجع الطارئ لرسالة التسوية
        if adj_tx_id:
            kb_revert = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 تراجع عن هذه التسوية الآلية (أنا أخطأت)", callback_data=f"revert_tx_{adj_tx_id}")]
            ])
            await message.answer(adj_msg, reply_markup=kb_revert)
        else:
            await message.answer(adj_msg)
            
        summary += f"▪️ {card_type}: تسوية آلية (اكتشاف {remaining_qty - expected_qty} كرت زائد)\n"
    else:
        summary += f"▪️ {card_type}: باع {sold_qty} كرت (المتبقي {remaining_qty})\n"

    idx += 1
    if idx < len(items_list):
        await state.update_data(current_item_index=idx, calculated_total_due=str(total_due), inventory_summary=summary)
        next_item = items_list[idx]
        await message.answer(
            f"📦 مسجل أن لديه: **{next_item['expected_qty']} كرت من فئة ({next_item['card_type']})**.\n\n"
            f"✏️ **كم عدد الكروت المتبقية لديه الآن من هذه الفئة؟**:"
        )
    else:
        remaining_value = Decimal('0.0')
        current_fresh_debt = Decimal('0.0')
        telecom_debt = Decimal('0.0')
        is_telecom_active = data.get("is_telecom_active", "off")
        
        if database.pool:
            async with database.pool.acquire() as conn:
                # جلب الدين المحدث من القاعدة لضمان شمول التسويات التي تمت أثناء الجرد
                fresh_debt_val = await conn.fetchval("SELECT debt FROM users WHERE user_id = $1", client_id)
                current_fresh_debt = Decimal(fresh_debt_val) if fresh_debt_val else Decimal('0.0')
                
                # 🌟 جلب دين التسديدات
                t_debt_val = await conn.fetchval("SELECT telecom_debt FROM users WHERE user_id = $1", client_id)
                telecom_debt = Decimal(t_debt_val) if t_debt_val else Decimal('0.0')
                
                inv_items = await conn.fetch("""
                    SELECT ci.quantity, i.price 
                    FROM client_inventory ci
                    JOIN inventory i ON ci.card_type = i.card_type
                    WHERE ci.user_id = $1 AND ci.quantity > 0
                """, client_id)
                for item in inv_items:
                    remaining_value += Decimal(item['price']) * item['quantity']
        
        # حساب المطلوب بناءً على الدين المحدث
        cards_required = current_fresh_debt - remaining_value
        total_required = cards_required + (telecom_debt if is_telecom_active == 'on' else Decimal('0.0'))
        
        # 🌟 الواجهة المتكيفة: إذا كان الكروت والتسديدات صفر أو أقل
        if total_required <= 0 and (is_telecom_active == 'off' or telecom_debt <= 0):
            advance_payment = abs(total_required)
            await state.update_data(required_amount="0.0", telecom_debt="0.0", is_telecom_active=is_telecom_active)
            msg = f"✅ **تم الانتهاء من الجرد بنجاح!**\n\n"
            msg += f"📋 **ملخص المبيعات:**\n{summary}\n"
            msg += f"💰 **قيمة مبيعات اليوم:** {total_due} ريال\n"
            msg += f"🟢 **العميل لديه رصيد دائن (دفعة مقدمة) يغطي المبيعات ويفيض بقيمة:** **{advance_payment} ريال**\n\n"
            msg += "لا حاجة لاستلام كاش منه اليوم. هل تريد تسليمه كروت جديدة؟"
            
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📦 نعم، تسليم كروت جديدة", callback_data=f"gclient_cleared_{client_id}")],
                [InlineKeyboardButton(text="🏁 لا، إنهاء الزيارة", callback_data=f"end_visit_{client_id}")]
            ])
            await message.answer(msg, reply_markup=kb)
            await state.clear()
        else:
            await state.update_data(required_amount=str(cards_required), telecom_debt=str(telecom_debt), is_telecom_active=is_telecom_active)
            msg = f"✅ **تم الانتهاء من الجرد بنجاح!**\n\n"
            msg += f"📋 **ملخص المبيعات:**\n{summary}\n"
            msg += f"💰 **قيمة مبيعات اليوم:** {total_due} ريال\n"
            
            # 🌟 الواجهة المتكيفة: إظهار التسديدات فقط إذا كان المفتاح يعمل والدين موجود
            if is_telecom_active == 'on' and telecom_debt > 0:
                msg += f"🔴 **المطلوب للكروت (مبيعات + ديون سابقة):** **{cards_required} ريال**\n"
                msg += f"⚡ **المطلوب للتسديدات (رصيد وباقات):** **{telecom_debt} ريال**\n"
                msg += f"💵 **الإجمالي الكلي المطلوب:** **{total_required} ريال**\n\n"
            else:
                msg += f"🔴 **إجمالي المطلوب تسديده الآن (مبيعات + ديون سابقة):** **{total_required} ريال**\n\n"
                
            msg += "💵 **كم المبلغ الكاش الذي استلمته منه الآن؟** (أرقام فقط):"
            
            await message.answer(msg)
            await state.set_state(FieldVisitFlow.waiting_for_cash)

@router.message(FieldVisitFlow.waiting_for_inventory)
async def visit_process_inventory(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("أرقام فقط!")
    required_amount = Decimal(message.text)
    await state.update_data(required_amount=str(required_amount))
    await message.answer(f"💰 المطلوب تسديده من العميل هو **{required_amount} ريال**.\n\nكم المبلغ الكاش الذي استلمته منه الآن؟ (أرقام فقط)")
    await state.set_state(FieldVisitFlow.waiting_for_cash)

@router.message(FieldVisitFlow.waiting_for_cash)
async def visit_process_cash(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("أرقام فقط!")
    collected_amount = Decimal(message.text)
    data = await state.get_data()
    required_amount = Decimal(data.get("required_amount", "0.0"))
    telecom_debt = Decimal(data.get("telecom_debt", "0.0"))
    is_telecom_active = data.get("is_telecom_active", "off")
    client_id = data.get("visit_client_id")
    
    # 🌟 دمج المطلوبين لفحص النقص
    total_required_all = required_amount
    if is_telecom_active == 'on':
        total_required_all += telecom_debt
    
    if collected_amount < total_required_all:
        shortage = total_required_all - collected_amount
        await state.update_data(collected_amount=str(collected_amount), shortage=str(shortage))
        await message.answer(
            f"🚨 **رفض أمني من النظام المحاسبي المركزي!** 🚨\n\n"
            f"❌ **السبب:** المبلغ المستلم ({collected_amount} ريال) لا يغطي إجمالي المديونية ({total_required_all} ريال).\n"
            f"⚠️ **القرار:** يمنع النظام تسليم كروت جديدة لأي عميل لديه متأخرات مالية. يرجى تحصيل باقي المبلغ ({shortage} ريال) لفك الحظر.\n\n"
            f"*(الرجاء إدخال المبلغ كاملاً لفك الحظر عن حساب العميل).* \n\n"
            f"لإعادة إدخال المبلغ اضغط الزر أدناه:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 إعادة إدخال المبلغ", callback_data="retry_visit_cash")]])
        )
        await state.set_state(FieldVisitFlow.waiting_for_secret_override)
    else:
        await process_successful_collection(message, state, client_id, collected_amount)

@router.callback_query(F.data == "retry_visit_cash")
async def retry_visit_cash(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("كم المبلغ الكاش الذي استلمته منه الآن؟ (أرقام فقط)")
    await state.set_state(FieldVisitFlow.waiting_for_cash)

@router.message(FieldVisitFlow.waiting_for_secret_override)
async def visit_secret_override(message: types.Message, state: FSMContext):
    if message.text.strip() == "#":
        data = await state.get_data()
        client_id = data.get("visit_client_id")
        collected_amount = Decimal(data.get("collected_amount", "0.0"))
        shortage = Decimal(data.get("shortage", "0.0"))
        
        await message.answer(f"🤫 *(تم تفعيل الصلاحية السرية للوكيل)*\n✅ تم قبول المبلغ ({collected_amount} ريال)، وتسجيل الباقي ({shortage} ريال) كدين على العميل.")
        # 🌟 إصلاح الكارثة: إخبار الدالة أننا استخدمنا التجاوز السري
        await process_successful_collection(message, state, client_id, collected_amount, bypass=True)

    else:
        await visit_process_cash(message, state)

# 🌟 هنا تمت العملية الجراحية! دالة التحصيل تستخدم المطبخ المركزي وتطبق الشلال المالي 🌟
async def process_successful_collection(message: types.Message, state: FSMContext, client_id: int, amount: Decimal, bypass: bool = False):
    data = await state.get_data()
    required_amount = Decimal(data.get("required_amount", "0.0"))
    telecom_debt = Decimal(data.get("telecom_debt", "0.0"))
    is_telecom_active = data.get("is_telecom_active", "off")
    
    # إذا كان المبلغ أكبر من صفر، نقوم بتسجيل سند قبض
    if amount > 0:
        from core_accounting import core_collect_debt, core_collect_telecom_debt
        
        # 🌟 خوارزمية الشلال المالي (Waterfall Algorithm) 🌟
        cards_payment = Decimal('0.0')
        telecom_payment = Decimal('0.0')
        
        if is_telecom_active == 'on' and telecom_debt > 0:
            # 1. تسديد الكروت أولاً (حقوق المدير هي الأولوية)
            if amount >= required_amount:
                cards_payment = required_amount
                remaining_after_cards = amount - required_amount
                
                # 2. تسديد التسديدات ثانياً (حقوق الوكيل)
                if remaining_after_cards >= telecom_debt:
                    telecom_payment = telecom_debt
                    # 3. الفائض يذهب كدفعة مقدمة للكروت
                    cards_payment += (remaining_after_cards - telecom_debt)
                else:
                    telecom_payment = remaining_after_cards
            else:
                # المبلغ لا يغطي حتى الكروت (حالة التجاوز السري #)
                cards_payment = amount
        else:
            # التسديدات مغلقة، كل المبلغ يذهب للكروت
            cards_payment = amount
            
        # 🌟 تنفيذ الدفعات المعزولة بصمت 🌟
        cards_trans_id = None
        telecom_trans_id = None
        client_name = "العميل"

        if cards_payment > 0:
            result = await core_collect_debt(client_id, cards_payment, bypass_shortage=bypass)
            if result["status"] == "error":
                return await message.answer(f"❌ خطأ في تحصيل الكروت: {result['message']}")
            client_name = result["client_name"]
            cards_trans_id = result.get("trans_id")
        else:
            # نجلب الاسم فقط إذا لم يتم الدفع للكروت
            import database
            async with database.pool.acquire() as conn:
                name_val = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
                if name_val: client_name = name_val
                
        if telecom_payment > 0:
            t_result = await core_collect_telecom_debt(client_id, telecom_payment)
            if t_result["status"] == "error":
                return await message.answer(f"❌ خطأ في تحصيل التسديدات: {t_result['message']}")
            telecom_trans_id = t_result.get("trans_id")
        
        # 🌟 رسالة النجاح الذكية
        total_required_all = required_amount + (telecom_debt if is_telecom_active == 'on' else Decimal('0.0'))
        
        if amount > total_required_all:
            excess = amount - total_required_all
            success_msg = (
                f"✅ **تم استلام المبلغ وتصفية الحساب لـ ({client_name}) بنجاح.**\n\n"
                f"💡 **المقاصة التلقائية:** تم استخدام {total_required_all} ريال لسداد المطلوب، "
                f"وتم حفظ الفائض ({excess} ريال) كـ **رصيد دائن (دفعة مقدمة)** سيتم خصمه آلياً من مبيعاته القادمة."
            )
        else:
            success_msg = f"✅ **تم استلام المبلغ وتصفية الحساب لـ ({client_name}) بنجاح.**"
        
        try:
            from web_api import send_web_push
            await send_web_push(client_id, "💰 تصفية حساب (زيارة ميدانية)", f"تم استلام مبلغ {int(amount)} ريال وتصفية حسابك القديم بنجاح.")
        except Exception as e:
            print(f"Push Error: {e}")

        # 🚀 إضافة Manus: إرسال الفواتير المنفصلة في الخلفية (Background Task)
        async def send_field_receipts():
            from pdf_generator import generate_receipt
            from aiogram.types import BufferedInputFile
            import asyncio
            from unified_main import safe_send_whatsapp
            
            # 1. فاتورة الكروت
            if cards_payment > 0 and cards_trans_id:
                try:
                    pdf_buffer = await asyncio.to_thread(generate_receipt, cards_trans_id, client_name, cards_payment, "تسديد دفعة", "دفعة نقدية يداً بيد (زيارة ميدانية)")
                    wa_text = f"🧾 *سند قبض*\nمرحباً {client_name}،\nتم استلام مبلغ *{int(cards_payment)} ريال* بنجاح.\nمرفق الفاتورة للتأكيد 🌹"
                    await safe_send_whatsapp(client_id, wa_text, pdf_buffer, f"Receipt_{cards_trans_id}.pdf", bot=message.bot)
                except Exception as e:
                    print(f"Field Cards Receipt Error: {e}")

            # 2. فاتورة التسديدات (معزولة تماماً)
            if telecom_payment > 0 and telecom_trans_id:
                try:
                    pdf_buffer_t = await asyncio.to_thread(generate_receipt, telecom_trans_id, client_name, telecom_payment, "سند قبض - خدمات إلكترونية", "تحصيل دفعة نقدية (زيارة ميدانية)")
                    wa_text_t = f"🧾 *سند قبض (خدمات إلكترونية)*\nمرحباً {client_name}،\nتم استلام مبلغ *{int(telecom_payment)} ريال* بنجاح.\nمرفق الفاتورة للتأكيد 🌹"
                    await safe_send_whatsapp(client_id, wa_text_t, pdf_buffer_t, f"Telecom_Receipt_{telecom_trans_id}.pdf", bot=message.bot)
                except Exception as e:
                    print(f"Field Telecom Receipt Error: {e}")

        # تشغيل مهمة الفواتير في الخلفية لكي لا يتجمد البوت
        import asyncio
        asyncio.create_task(send_field_receipts())
            
    # إذا كان المبلغ صفر (العميل لم يدفع شيئاً)، نتخطى التحصيل ونجلب الاسم فقط
    else:
        if database.pool:
            async with database.pool.acquire() as conn:
                client_name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
        else:
            client_name = "العميل"
            
        success_msg = f"✅ **تم تسجيل الجرد والمبيعات لـ ({client_name}) بنجاح (بدون تحصيل كاش).**\n*(تم ترحيل قيمة المبيعات إلى حسابه الآجل)*"
                
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 نعم، تسليم كروت جديدة", callback_data=f"gclient_cleared_{client_id}")],
        [InlineKeyboardButton(text="🏁 لا، إنهاء الزيارة", callback_data=f"end_visit_{client_id}")]
    ])
    
    await message.answer(f"{success_msg}\n\nهل يريد العميل كروت جديدة الآن؟", reply_markup=kb)
    await state.clear()

@router.callback_query(F.data.startswith("end_visit_"))
async def end_visit_summary(callback: types.CallbackQuery, bot: Bot):
    client_id = int(callback.data.split("_")[2])

    if database.pool:
        async with database.pool.acquire() as conn:
            user = await conn.fetchrow("SELECT name, debt FROM users WHERE user_id = $1", client_id)
            if user:
                client_name = user['name']
                final_debt = Decimal(user['debt'])
                
                summary_msg = (
                    f"🧾 **ملخص الحساب الختامي بعد الزيارة:**\n\n"
                    f"👤 **العميل:** {client_name}\n"
                    f"🔴 **إجمالي الدين الحالي المسجل عليه:** **{int(final_debt)} ريال**\n\n"
                    f"*(تم إرسال هذا الملخص للعميل عبر التطبيق)*"
                )
                
                await callback.message.edit_text(summary_msg)
                
                try:
                    await smart_notify(client_id, f"🧾 **ملخص حسابك بعد زيارة الوكيل:**\n\nإجمالي الدين الحالي المسجل عليك هو: **{int(final_debt)} ريال**.\nشكراً لتعاملك معنا 🌹")
                except Exception as e: logging.error(f"Error: {e}")
                
                try:
                    from web_api import send_web_push
                    await send_web_push(client_id, "🧾 ملخص الحساب", f"إجمالي الدين الحالي المسجل عليك هو: {int(final_debt)} ريال.")
                except Exception as e: logging.error(f"Error: {e}")

    await callback.message.answer("👨‍💻 **لوحة تحكم  الوكيل**", reply_markup=await get_main_admin_keyboard())

# ================= 5. قسم المرتجعات الشامل =================
@router.callback_query(F.data == "admin_returns_menu")
async def admin_returns_menu(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏪 مرتجع من عميل (بقالة)", callback_data="return_from_client")],
        [InlineKeyboardButton(text="🏢 مرتجع للإدارة (الشبكة)", callback_data="return_to_network")],
        [InlineKeyboardButton(text="🛒 مرتجع بيع مباشر (طياري)", callback_data="return_direct_sale")],
        [InlineKeyboardButton(text="💔 تسجيل كروت تالفة", callback_data="admin_damaged")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_operations_menu")]
    ])
    await callback.message.edit_text("🔄 **قسم المرتجعات والتوالف:**\nاختر نوع العملية:", reply_markup=kb)

@router.callback_query(F.data == "return_from_client")
@router.callback_query(F.data.startswith("return_client_page_"))
async def return_from_client_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    page = 0
    if callback.data.startswith("return_client_page_"):
        page = int(callback.data.split("_")[3])

    ITEMS_PER_PAGE = 8
    clients_kb = []
    total_pages = 1
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # 1. جلب العدد الكلي لحساب الصفحات (سريع جداً)
            total_clients = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role = 'client'")
            
            if total_clients > 0:
                total_pages, offset = get_pagination_math(total_clients, page, ITEMS_PER_PAGE)
                
                # 2. جلب عملاء هذه الصفحة فقط باستخدام LIMIT و OFFSET
                current_clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client' ORDER BY user_id LIMIT $1 OFFSET $2", ITEMS_PER_PAGE, offset)
                
                for c in current_clients:
                    clients_kb.append([InlineKeyboardButton(text=c["name"], callback_data=f"retclient_{c['user_id']}")])

    # 3. استخدام الدالة المختصرة مع الاسم الصحيح
    nav_buttons = get_pagination_buttons(page, total_pages, "return_client_page")
    if nav_buttons: clients_kb.append(nav_buttons)

    clients_kb.append([InlineKeyboardButton(text="🔙 إلغاء", callback_data="admin_returns_menu")])
    
    text = f"👥 اختر العميل الذي أرجع الكروت - صفحة {page+1}/{total_pages}:" if total_clients > 0 else "👥 لا يوجد عملاء مسجلين."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=clients_kb))
    await state.set_state(ReturnClientFlow.waiting_for_client)

@router.callback_query(ReturnClientFlow.waiting_for_client, F.data.startswith("retclient_"))
async def return_from_client_type(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(client_id=int(callback.data.split("_")[1]))
    kb = await get_cards_keyboard("rettype")
    await callback.message.edit_text("📦 اختر الفئة المرتجعة:", reply_markup=kb)
    await state.set_state(ReturnClientFlow.waiting_for_type)

@router.callback_query(ReturnClientFlow.waiting_for_type, F.data.startswith("rettype_"))
async def return_from_client_qty(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(card_type=callback.data.split("_")[1])
    await callback.message.edit_text("🔢 كم عدد الكروت المرتجعة؟")
    await state.set_state(ReturnClientFlow.waiting_for_quantity)

@router.message(ReturnClientFlow.waiting_for_quantity)
async def return_from_client_exec(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("أرقام فقط!")
    qty = int(message.text)
    data = await state.get_data()
    client_id = data["client_id"]
    card_type = data["card_type"]
    
    # 🌟 التوحيد: استدعاء المطبخ المركزي
    from core_accounting import core_process_return
    result = await core_process_return('from_client', qty, Decimal('0'), client_id, card_type, source="(من البوت)")
    
    if result["status"] == "error":
        return await message.answer(f"❌ {result['message']}")
        
    trans_id = result["trans_id"]
    total_value = result["total_value"]
    client_name = result["client_name"]
            
    # 🌟 توليد الفاتورة وإرسالها واتساب
    from pdf_generator import generate_receipt
    from aiogram.types import BufferedInputFile
    import asyncio
    
    pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, client_name, total_value, "مرتجع من عميل", f"إرجاع {qty} كرت {card_type}")
    pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
    
    wa_text = f"🧾 *إشعار مرتجع*\nمرحباً {client_name}،\nتم استلام المرتجع: {qty} كرت من فئة {card_type}.\nتم خصم مبلغ *{int(total_value)} ريال* من مديونيتك.\nمرفق إيصال المرتجع للتأكيد 🌹"
    asyncio.create_task(safe_send_whatsapp(client_id, wa_text, pdf_buffer, f"Receipt_{trans_id}.pdf", bot=message.bot))

    # 🌟 إشعار التطبيق بالخلاصة
    try:
        from web_api import send_web_push
        asyncio.create_task(send_web_push(client_id, "🔄 إشعار مرتجع", f"تم استلام {qty} كرت ({card_type}) وخصم {int(total_value)} ريال من دينك."))
    except: pass

    await state.clear()
    await message.answer_document(document=pdf_file, caption=f"✅ **تم قبول المرتجع!**\nتم سحب {qty} كرت من العميل، وخصم {int(total_value)} ريال من دينه.\n(تم تسوية الأرباح المعلقة والمحفظة تلقائياً).")

# --- ب. مرتجع للإدارة ---
@router.callback_query(F.data == "return_to_network")
async def return_to_network_start(callback: types.CallbackQuery, state: FSMContext):
    kb = await get_cards_keyboard("retnet")
    await callback.message.edit_text("🏢 اختر الفئة التي سترجعها للإدارة:", reply_markup=kb)
    await state.set_state(ReturnNetworkFlow.waiting_for_type)

@router.callback_query(ReturnNetworkFlow.waiting_for_type, F.data.startswith("retnet_"))
async def return_to_network_qty(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(card_type=callback.data.split("_")[1])
    await callback.message.edit_text("🔢 كم عدد الكروت المرتجعة للإدارة؟")
    await state.set_state(ReturnNetworkFlow.waiting_for_quantity)

@router.message(ReturnNetworkFlow.waiting_for_quantity)
async def return_to_network_exec(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("أرقام فقط!")
    qty = int(message.text)
    data = await state.get_data()
    card_type = data["card_type"]
    
    # 🌟 التوحيد: استدعاء المطبخ المركزي
    from core_accounting import core_process_return
    result = await core_process_return('to_network', qty, Decimal('0'), 0, card_type, source="(من البوت)")
    
    if result["status"] == "error":
        return await message.answer(f"❌ {result['message']}")
        
    total_value = result["total_value"]
            
    await state.clear()
    await message.answer(f"✅ **تم تسجيل المرتجع للإدارة!**\nتم خصم {qty} كرت من مخزونك، وتسجيل {int(total_value)} ريال كمرتجع يخصم من مديونيتك للإدارة.")

# --- ج. مرتجع بيع مباشر ---
@router.callback_query(F.data == "return_direct_sale")
async def return_direct_start(callback: types.CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 مرتجع تجزئة (سعر الطياري)", callback_data="retdir_mode_retail")],
        [InlineKeyboardButton(text="🛍️ مرتجع جملة (سعر البقالات)", callback_data="retdir_mode_wholesale")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_returns_menu")]
    ])
    await callback.message.edit_text("🛒 **مرتجع بيع مباشر:**\nما هو نوع البيع الأصلي الذي تريد إرجاعه؟", reply_markup=kb)

@router.callback_query(F.data.startswith("retdir_mode_"))
async def return_direct_mode(callback: types.CallbackQuery, state: FSMContext):
    mode = callback.data.split("_")[2]
    await state.update_data(return_mode=mode)
    kb = await get_cards_keyboard("retdir")
    await callback.message.edit_text("📦 اختر الفئة المرتجعة:", reply_markup=kb)
    await state.set_state(ReturnDirectFlow.waiting_for_type)
    
@router.callback_query(ReturnDirectFlow.waiting_for_type, F.data.startswith("retdir_"))
async def return_direct_qty(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(card_type=callback.data.split("_")[1])
    await callback.message.edit_text("🔢 كم عدد الكروت المرتجعة؟")
    await state.set_state(ReturnDirectFlow.waiting_for_quantity)

@router.message(ReturnDirectFlow.waiting_for_quantity)
async def return_direct_exec(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("أرقام فقط!")
    qty = int(message.text)
    data = await state.get_data()
    card_type = data["card_type"]
    mode = data.get("return_mode", "retail")
    
    from core_accounting import core_process_return
    # 🌟 الإصلاح: تمرير نوع البيع (جملة أو تجزئة) للمطبخ المركزي لكي يحسب السعر الصحيح
    result = await core_process_return(f'direct_sale_{mode}', qty, Decimal('0'), 0, card_type, source="(من البوت)")
    
    if result["status"] == "error":
        return await message.answer(f"❌ {result['message']}")
        
    total_value = result["total_value"]
            
    await state.clear()
    await message.answer(f"✅ **تم تسجيل مرتجع البيع المباشر!**\nتم إعادة {qty} كرت لمخزونك، وسحب {int(total_value)} ريال من الكاش.\n(وتم خصم الربح من محفظتك بدقة).")

# ================= 🔄 مركز التحويلات الشامل =================
async def get_transfer_entities_keyboard(prefix: str, page: int = 0):
    ITEMS_PER_PAGE = 6
    kb = []
    
    # الكيانات الثابتة (المدير والوكيل) تظهر في الصفحة الأولى فقط
    if page == 0:
        kb.append([InlineKeyboardButton(text="👑 الإدارة (المدير العام)", callback_data=f"{prefix}_manager")])
        kb.append([InlineKeyboardButton(text="👨‍💻 الوكيل (الصندوق/المخزون)", callback_data=f"{prefix}_agent")])
        
    if database.pool:
        async with database.pool.acquire() as conn:
            total_clients = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role = 'client'")
            if total_clients > 0:
                total_pages, offset = get_pagination_math(total_clients, page, ITEMS_PER_PAGE)
                current_clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client' ORDER BY user_id LIMIT $1 OFFSET $2", ITEMS_PER_PAGE, offset)
                
                for c in current_clients:
                    kb.append([InlineKeyboardButton(text=f"🏪 {c['name']}", callback_data=f"{prefix}_client_{c['user_id']}")])
                    
                nav_buttons = get_pagination_buttons(page, total_pages, f"{prefix}_page")
                if nav_buttons: kb.append(nav_buttons)

    kb.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_action")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

@router.callback_query(F.data == "admin_transfer_center")
@router.callback_query(F.data.startswith("tsender_page_"))
async def transfer_center_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    page = int(callback.data.split("_")[2]) if "page" in callback.data else 0
    kb = await get_transfer_entities_keyboard("tsender", page)
    await callback.message.edit_text("🔄 **مركز التحويلات الشامل**\n\n📤 **أولاً: من هو المُرسِل؟**\n(من الذي سيدفع الكاش أو يسلم الكروت؟)", reply_markup=kb)
    await state.set_state(ComprehensiveTransferFlow.waiting_for_sender)

@router.callback_query(F.data.startswith("tsender_"))
async def transfer_center_sender(callback: types.CallbackQuery, state: FSMContext):
    if "page" in callback.data: return await transfer_center_start(callback, state)
    
    sender = callback.data.replace("tsender_", "")
    await state.update_data(transfer_sender=sender)
    
    kb = await get_transfer_entities_keyboard("treceiver", 0)
    await callback.message.edit_text("📥 **ثانياً: من هو المُستقبِل؟**\n(من الذي سيستلم الكاش أو الكروت؟)", reply_markup=kb)
    await state.set_state(ComprehensiveTransferFlow.waiting_for_receiver)

@router.callback_query(F.data.startswith("treceiver_"))
async def transfer_center_receiver(callback: types.CallbackQuery, state: FSMContext):
    if "page" in callback.data:
        page = int(callback.data.split("_")[2])
        kb = await get_transfer_entities_keyboard("treceiver", page)
        return await callback.message.edit_text("📥 **ثانياً: من هو المُستقبِل؟**\n(من الذي سيستلم الكاش أو الكروت؟)", reply_markup=kb)
        
    receiver = callback.data.replace("treceiver_", "")
    data = await state.get_data()
    
    if data.get('transfer_sender') == receiver:
        return await callback.answer("❌ لا يمكن التحويل لنفس الشخص!", show_alert=True)
        
    await state.update_data(transfer_receiver=receiver)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 تحويل كاش (مبالغ مالية)", callback_data="ttype_cash")],
        [InlineKeyboardButton(text="📦 تحويل كروت (بضاعة)", callback_data="ttype_cards")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_action")]
    ])
    await callback.message.edit_text("⚙️ **ثالثاً: ما هو نوع التحويل؟**", reply_markup=kb)
    await state.set_state(ComprehensiveTransferFlow.waiting_for_type)

@router.callback_query(F.data.startswith("ttype_"))
async def transfer_center_type(callback: types.CallbackQuery, state: FSMContext):
    t_type = callback.data.split("_")[1]
    await state.update_data(transfer_type=t_type)
    
    if t_type == "cash":
        await callback.message.edit_text("💰 **أدخل المبلغ المراد تحويله (بالريال):**\n*(أرقام فقط)*")
        await state.set_state(ComprehensiveTransferFlow.waiting_for_amount_or_qty)
    else:
        kb = await get_cards_keyboard("tcard")
        await callback.message.edit_text("📦 **اختر فئة الكروت المراد تحويلها:**", reply_markup=kb)
        await state.set_state(ComprehensiveTransferFlow.waiting_for_card_type)

@router.callback_query(F.data.startswith("tcard_"))
async def transfer_center_card_type(callback: types.CallbackQuery, state: FSMContext):
    card_type = callback.data.split("_")[1]
    await state.update_data(transfer_card_type=card_type)
    await callback.message.edit_text(f"🔢 **أدخل كمية الكروت المراد تحويلها من فئة ({card_type}):**\n*(أرقام فقط)*")
    await state.set_state(ComprehensiveTransferFlow.waiting_for_amount_or_qty)

@router.message(ComprehensiveTransferFlow.waiting_for_amount_or_qty)
async def transfer_center_execute(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("⚠️ أرقام فقط!")
    
    val = Decimal(message.text)
    
    # 👈 التعديل هنا: منع الأرقام السالبة والصفر قبل إرسالها للنظام
    if val <= 0:
        return await message.answer("⚠️ عذراً، يجب أن تكون القيمة أكبر من صفر! الرجاء إدخال رقم صحيح:")
        
    data = await state.get_data()
    sender = data['transfer_sender']
    receiver = data['transfer_receiver']
    t_type = data['transfer_type']
    card_type = data.get('transfer_card_type', '')
    
    wait_msg = await message.answer("⏳ جاري تنفيذ قيود التسوية المحاسبية...")
    
    from core_accounting import core_transfer_center
    result = await core_transfer_center(sender, receiver, t_type, val, card_type)
    
    if result["status"] == "error":
        await wait_msg.delete()
        return await message.answer(result["message"])
        
    await wait_msg.edit_text(result["message"])
    await state.clear()
    await message.answer("📦 **العمليات والطلبات:**", reply_markup=await get_operations_submenu_keyboard())

# ================= 6. المالية والمصروفات =================
@router.callback_query(F.data == "admin_collect")
@router.callback_query(F.data.startswith("admin_collect_page_"))
async def start_collect(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    page = 0
    if callback.data.startswith("admin_collect_page_"):
        page = int(callback.data.split("_")[3])

    ITEMS_PER_PAGE = 8
    clients_kb = []
    total_pages = 1
    total_clients = 0
    
    if database.pool:
        async with database.pool.acquire() as conn:
            total_clients = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role = 'client'")
            if total_clients > 0:
                total_pages, offset = get_pagination_math(total_clients, page, ITEMS_PER_PAGE)
                current_clients = await conn.fetch("SELECT user_id, name, debt FROM users WHERE role = 'client' ORDER BY debt DESC LIMIT $1 OFFSET $2", ITEMS_PER_PAGE, offset)
                
                for c in current_clients:
                    clients_kb.append([InlineKeyboardButton(text=f"{c['name']} ({c['debt']} ريال)", callback_data=f"ccollect_{c['user_id']}")])

    # استخدام الدالة المختصرة
    nav_buttons = get_pagination_buttons(page, total_pages, "admin_collect_page")
    if nav_buttons: clients_kb.append(nav_buttons)
        
    clients_kb.append([InlineKeyboardButton(text="?? العودة للقائمة الرئيسية", callback_data="admin_finance_menu")])
    
    text = f"💰 **تحصيل ديون البقالات (يدوي)**\nاختر العميل الذي سدد - صفحة {page+1}/{total_pages}:" if total_clients > 0 else "💰 لا يوجد عملاء مسجلين."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=clients_kb))
    await state.set_state(CollectMoneyFlow.waiting_for_client)

@router.callback_query(CollectMoneyFlow.waiting_for_client, F.data.startswith("ccollect_"))
async def process_ccollect(callback: types.CallbackQuery, state: FSMContext):
    client_id = int(callback.data.split("_")[1])
    await state.update_data(client_id=client_id)
    if database.pool:
        async with database.pool.acquire() as conn:
            client_name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
            client_debt = await conn.fetchval("SELECT debt FROM users WHERE user_id = $1", client_id)
            await callback.message.edit_text(f"💰 **استلام دفعة من {client_name}**\nعليه دين: {client_debt} ريال.\nكم المبلغ الذي استلمته منه؟")
    await state.set_state(CollectMoneyFlow.waiting_for_amount)

@router.message(CollectMoneyFlow.waiting_for_amount)
async def process_ccollect_amount(message: types.Message, state: FSMContext, bot: Bot):
    text = message.text.strip()
    bypass = False
    
    if text.endswith("#"):
        bypass = True
        text = text[:-1]
        
    if not text.replace(".", "").isdigit(): return await message.answer("الرجاء إدخال رقم صحيح للمبلغ! (لا يمكن تجاوز السقف المحدد - كود: ERR#)")
    
    data = await state.get_data()
    await state.clear() 
    
    amount = Decimal(text)
    client_id = data["client_id"]
    
    # 1. رسالة فورية لكسر التأخير
    wait_msg = await message.answer("⏳ جاري تسجيل التحصيل...")
    
    from core_accounting import core_collect_debt
    result = await core_collect_debt(client_id, amount, bypass_shortage=bypass)
    
    if result["status"] == "error":
        await wait_msg.delete()
        return await message.answer(f"❌ {result['message']}")
        
    trans_id = result["trans_id"]
    client_name = result["client_name"]
    client_phone = result.get("client_phone", "")
    new_debt = result["new_debt"]
    
    # 2. الرد الفوري بالنجاح (بدون انتظار الفاتورة)
    balance_text = f"الرصيد المتبقي عليه: {int(new_debt)} ريال" if new_debt >= 0 else f"رصيد دائن (لصالح العميل): {abs(int(new_debt))} ريال"
    bypass_note = "\n*(تم استخدام صلاحية التجاوز الإدارية)*" if bypass else ""
    
    await wait_msg.edit_text(f"✅ **تم استلام الدفعة بنجاح!**{bypass_note}\nتم خصم {int(amount)} ريال من حساب {client_name}.\n\n{balance_text}.")
    await message.answer("💰 **المالية والمصروفات:**", reply_markup=await get_finance_submenu_keyboard())

    # 3. إرسال الفواتير والإشعارات في الخلفية بصمت
    async def send_collect_notifications():
        try:
            pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, client_name, amount, "تسديد دفعة", "دفعة نقدية يداً بيد")
            pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
            
            # إرسال الفاتورة للوكيل
            await message.answer_document(document=pdf_file, caption=f"🧾 فاتورة التحصيل #{trans_id}")
            
            # إرسال الواتساب
            pdf_buffer.seek(0)
            wa_text = f"🧾 *سند قبض*\nمرحباً {client_name}،\nتم استلام مبلغ *{int(amount)} ريال* بنجاح.\nالرصيد المتبقي: *{int(new_debt)} ريال*.\nمرفق الفاتورة للتأكيد 🌹\n\n👇 *فضلاً، أجب بـ (صحيح) إذا كان المبلغ المسدد والرصيد المتبقي صحيحين.*"
            await safe_send_whatsapp(client_id, wa_text, pdf_buffer, f"Receipt_{trans_id}.pdf", bot=bot)
            
            # إشعار الويب
            from web_api import send_web_push
            await send_web_push(client_id, "💰 سند قبض (استلام كاش)", f"تم استلام مبلغ {int(amount)} ريال بنجاح. الرصيد المتبقي: {int(new_debt)} ريال.")
        except Exception as e:
            import logging
            logging.error(f"Background collect notification error: {e}")

    # إطلاق المهمة الخلفية
    asyncio.create_task(send_collect_notifications())

# --- ب. تسديد للمدير العام ---
@router.callback_query(F.data == "admin_pay")
async def start_pay_network(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("💸 **تسديد للمدير العام**\nكم المبلغ الذي ستقوم بتحويله للمدير العام؟")
    await state.set_state(PayNetworkFlow.waiting_for_amount)

@router.message(PayNetworkFlow.waiting_for_amount)
async def process_pay_network_amount(message: types.Message, state: FSMContext, bot: Bot):
    if not message.text.replace(".", "").isdigit(): return await message.answer("الرجاء إدخال رقم صحيح للمبلغ.")
    amount = Decimal(message.text)
    
    from core_accounting import core_finance_action
    result = await core_finance_action('pay_manager', amount, "تحويل للمدير العام", source="(من البوت)")
    
    if result["status"] == "error":
        return await message.answer(f"❌ {result['message']}")
        
    trans_id = result["trans_id"]
    pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, "المدير العام", amount, "تسديد للشبكة", "تحويل للمدير العام")
    pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
    
    try:
        msg_text = f"💸 **إشعار تسديد من الوكيل:**\nقام الوكيل (د. وليد) بتسديد مبلغ **{int(amount)} ريال** لحساب الإدارة العامة.\nمرفق سند القبض."
        await smart_notify(bot, NETWORK_OWNER_ID, text=msg_text, document=pdf_file)
    except Exception as e: logging.error(f"Error: {e}")

    await state.clear()
    await message.answer_document(document=pdf_file, caption=f"✅ **تم تسجيل التحويل!**\nتم تسجيل تحويل مبلغ {int(amount)} ريال للمدير العام (وتم خصمه من الكاش المتوفر).")


# --- ج. المصروفات ---
@router.callback_query(F.data == "admin_expenses")
async def start_add_expense(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📉 **تسجيل مصروفات**\nكم مبلغ المصروف؟")
    await state.set_state(AddExpenseFlow.waiting_for_amount)

@router.message(AddExpenseFlow.waiting_for_amount)
async def process_expense_amount(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("الرجاء إدخال رقم صحيح للمبلغ.")
    await state.update_data(expense_amount=str(message.text))
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚗 مواصلات/بترول", callback_data="exp_مواصلات")],
        [InlineKeyboardButton(text="☕ ضيافة/غداء", callback_data="exp_ضيافة")],
        [InlineKeyboardButton(text="🏠 إيجار", callback_data="exp_إيجار")],
        [InlineKeyboardButton(text="🌐 إنترنت/كهرباء", callback_data="exp_فواتير")],
        [InlineKeyboardButton(text="✍️ كتابة يدوية", callback_data="exp_manual")]
    ])
    await message.answer("ما هي تفاصيل هذا المصروف؟", reply_markup=kb)

@router.callback_query(F.data.startswith("exp_"))
async def process_expense_quick(callback: types.CallbackQuery, state: FSMContext):
    detail_type = callback.data.split("_")[1]
    if detail_type == "manual":
        await callback.message.edit_text("الرجاء كتابة تفاصيل المصروف يدوياً:")
        await state.set_state(AddExpenseFlow.waiting_for_details)
        return
        
    data = await state.get_data()
    amount = Decimal(data["expense_amount"])
    
    # 🌟 التوحيد: استدعاء المطبخ المركزي
    from core_accounting import core_finance_action
    result = await core_finance_action('expense', amount, detail_type, source="(من البوت)")
    
    if result["status"] == "error":
        return await callback.message.edit_text(f"❌ {result['message']}")
        
    trans_id = result["trans_id"]
    
    # توليد الفاتورة
    from pdf_generator import generate_receipt
    from aiogram.types import BufferedInputFile
    import asyncio
    
    pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, "مصروفات", amount, "مصروفات", detail_type)
    pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
    
    await state.clear()
    await callback.message.delete()
    await callback.message.answer_document(document=pdf_file, caption=f"✅ **تم تسجيل المصروف!**\nالمبلغ: {int(amount)} ريال\nالتفاصيل: {detail_type}.")

@router.message(AddExpenseFlow.waiting_for_details)
async def process_expense_details(message: types.Message, state: FSMContext):
    data = await state.get_data()
    amount = Decimal(data["expense_amount"])
    details = message.text
    
    # 🌟 التوحيد: استدعاء المطبخ المركزي
    from core_accounting import core_finance_action
    result = await core_finance_action('expense', amount, details, source="(من البوت)")
    
    if result["status"] == "error":
        return await message.answer(f"❌ {result['message']}")
        
    trans_id = result["trans_id"]
    
    # توليد الفاتورة
    from pdf_generator import generate_receipt
    from aiogram.types import BufferedInputFile
    import asyncio
    
    pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, "مصروفات", amount, "مصروفات", details)
    pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
    
    await state.clear()
    await message.answer_document(document=pdf_file, caption=f"✅ **تم تسجيل المصروف!**\nالمبلغ: {int(amount)} ريال\nالتفاصيل: {details}.")

# --- د. كروت تالفة ---
@router.callback_query(F.data == "admin_damaged")
async def start_damaged_cards(callback: types.CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 عوضته بمبلغ مالي (كاش)", callback_data="dmg_cash")],
        [InlineKeyboardButton(text="📦 عوضته بكرت بديل (من مخزوني)", callback_data="dmg_card")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_returns_menu")]
    ])
    await callback.message.edit_text("💔 **تسجيل كروت تالفة:**\nكيف قمت بتعويض العميل عن الكرت التالف؟", reply_markup=kb)

@router.callback_query(F.data == "dmg_cash")
async def dmg_cash_start(callback: types.CallbackQuery, state: FSMContext):
    # 🌟 التعديل المحاسبي: إجبار الوكيل على إدخال سعر التكلفة فقط لحماية حقوق الإدارة
    await callback.message.edit_text("💵 **تعويض نقدي:**\nكم **سعر التكلفة** للكروت التالفة (السعر الذي سيُخصم من حساب المدير)؟\n\n*(ملاحظة: إذا عوضت العميل بسعر الجملة، فإن الفارق تتحمله أنت من أرباحك كخسارة، ويجب تسجيله لاحقاً في المصروفات اليدوية)*")
    await state.set_state(DamagedCardsFlow.waiting_for_amount)

@router.callback_query(F.data == "dmg_card")
async def dmg_card_start(callback: types.CallbackQuery, state: FSMContext):
    # توجيه ذكي لعملية المرتجع للإدارة لكي لا يخسر الوكيل
    await return_to_network_start(callback, state)

@router.message(DamagedCardsFlow.waiting_for_amount)
async def process_damaged_cards_amount(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("الرجاء إدخال رقم صحيح للمبلغ.")
    amount = Decimal(message.text)
    
    from core_accounting import core_process_return
    result = await core_process_return('damaged', 0, amount, 0, "", source="(من البوت)")
    
    if result["status"] == "error":
        return await message.answer(f"❌ {result['message']}")
        
    trans_id = result["trans_id"]
    
    from pdf_generator import generate_receipt
    from aiogram.types import BufferedInputFile
    import asyncio
    
    pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, "كروت تالفة", amount, "كروت تالفة", "إرجاع كروت تالفة للإدارة")
    pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")
    
    await state.clear()
    await message.answer_document(document=pdf_file, caption=f"✅ **تم تسجيل الكروت التالفة!**\nتم تسجيل {int(amount)} ريال كقيمة كروت تالفة.")

# --- هـ. نسبة الوكيل ---
@router.callback_query(F.data == "admin_commission")
async def start_agent_commission(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("💎 **تسجيل نسبة الوكيل**\nكم مبلغ نسبة الوكيل المستحقة؟")
    await state.set_state(AgentCommissionFlow.waiting_for_amount)

@router.message(AgentCommissionFlow.waiting_for_amount)
async def process_agent_commission_amount(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("الرجاء إدخال رقم صحيح للمبلغ.")
    amount = Decimal(message.text)
    
    from core_accounting import core_finance_action
    result = await core_finance_action('agent_commission', amount, "نسبة الوكيل المستحقة", source="(من البوت)")
    
    if result["status"] == "error":
        return await message.answer(f"❌ {result['message']}")
        
    trans_id = result["trans_id"]
    
    from pdf_generator import generate_receipt
    from aiogram.types import BufferedInputFile
    import asyncio
    
    pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, "نسبة الوكيل", amount, "نسبة الوكيل", "نسبة الوكيل المستحقة")
    pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")

    await state.clear()
    await message.answer_document(document=pdf_file, caption=f"✅ **تم تسجيل نسبة الوكيل!**\nتم تسجيل {int(amount)} ريال كنسبة مستحقة للوكيل.\n(تم إضافتها لمحفظة أرباحك).")

# --- و. محفظة أرباح الوكيل (الجديدة) ---
@router.callback_query(F.data == "admin_agent_wallet")
async def view_agent_wallet(callback: types.CallbackQuery):
    await callback.answer()
    if callback.from_user.id != ADMIN_ID: return
    
    # جلب أرباح الكروت
    total_profit = await database.get_agent_total_profit()
    total_profit = total_profit if total_profit else Decimal('0.0')
    
    # 🌟 التعديل المحاسبي: جلب أرباح التسديدات أيضاً لكي يرى الوكيل كل أمواله
    telecom_profit = await database.get_telecom_total_profit()
    telecom_profit = telecom_profit if telecom_profit else Decimal('0.0')
    
    grand_total = total_profit + telecom_profit

    text = f"💎 **محفظة أرباح الوكيل (د. وليد):**\n\n"
    text += f"💰 **إجمالي الأرباح الكلية:** **{int(grand_total)} ريال**\n\n"
    text += f"📊 **التفصيل:**\n"
    text += f"▪️ أرباح شبكة الكروت: {int(total_profit)} ريال\n"
    text += f"   *(تُسحب من زر 'سحب أرباحي' في البوت)*\n"
    text += f"▪️ أرباح قسم التسديدات: {int(telecom_profit)} ريال\n"
    text += f"   *(تُسحب من تطبيق الويب فقط لأنها معزولة في درج التسديدات)*\n\n"

    if database.pool:
        async with database.pool.acquire() as conn:
            # 🌟 سد ثغرة العمى المالي: دمج سجل أرباح الكروت مع أرباح التسديدات في قائمة واحدة
            profits = await conn.fetch("""
                SELECT amount, details, date FROM agent_profits 
                UNION ALL 
                SELECT amount, details, date FROM telecom_profits 
                ORDER BY date DESC LIMIT 10
            """)
            if profits:
                text += "📋 **آخر حركات الأرباح (كروت + تسديدات):**\n"
                for p in profits:
                    text += f"▪️ {p['details']}: {int(p['amount'])} ريال\n"
            else:
                text += "لا توجد حركات مسجلة في المحفظة بعد."

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_finance_menu")]])
    await callback.message.edit_text(text, reply_markup=kb)

@router.callback_query(F.data == "admin_withdraw_profit")
async def start_withdraw_profit(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    total_profit = await database.get_agent_total_profit()
    
    if total_profit <= 0:
        return await callback.message.edit_text("❌ محفظتك فارغة حالياً. لا يوجد أرباح لسحبها.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_finance_menu")]]))
        
    await callback.message.edit_text(f"💸 **سحب أرباح شخصية:**\n\n💎 أرباحك المتاحة للسحب: **{int(total_profit)} ريال**\n\nكم المبلغ الذي تريد سحبه الآن من الصندوق؟ (أرقام فقط)")
    await state.set_state(AgentWithdrawProfitFlow.waiting_for_amount)

@router.message(AgentWithdrawProfitFlow.waiting_for_amount)
async def process_withdraw_profit_amount(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("الرجاء إدخال رقم صحيح للمبلغ.")
    amount = Decimal(message.text)
    await state.clear()
    
    from core_accounting import core_finance_action
    result = await core_finance_action('withdraw_profit', amount, "سحب أرباح شخصية من الصندوق", source="(من البوت)")
    
    if result["status"] == "error":
        return await message.answer(result["message"])
        
    trans_id = result["trans_id"]
    
    from pdf_generator import generate_receipt
    from aiogram.types import BufferedInputFile
    import asyncio
    
    pdf_buffer = await asyncio.to_thread(generate_receipt, trans_id, "الوكيل (د. وليد)", amount, "سحب أرباح", "سحب أرباح شخصية من الصندوق")
    pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Receipt_{trans_id}.pdf")

    await message.answer_document(document=pdf_file, caption=result["message"])
    await message.answer("💰 **المالية والمصروفات:**", reply_markup=await get_finance_submenu_keyboard())

# ================= 7. التقارير والجرد =================

@router.callback_query(F.data == "admin_inventory")
async def view_admin_inventory(callback: types.CallbackQuery):
    await callback.answer()
    text = "📦 **مخزونك الحالي المتوفر:**\n\n"
    if database.pool:
        async with database.pool.acquire() as conn:
            items = await conn.fetch("SELECT card_type, quantity FROM inventory")
            has_items = False
            for item in items:
                text += f"▪️ {item['card_type']}: {item['quantity']} كرت\n"
                has_items = True
            if not has_items: text += "المخزون فارغ تماماً حالياً."
            
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 العودة للقائمة الرئيسية", callback_data="admin_reports_menu")]])
    await callback.message.edit_text(text, reply_markup=kb)

    # --- التقرير الشهري التفاعلي ---
@router.callback_query(F.data == "admin_report")
async def generate_monthly_report(callback: types.CallbackQuery):
    wait_msg = await callback.message.edit_text("⏳ جاري إعداد التقرير المحاسبي الشهري (Excel)...")
    
    try:
        # تحديد تواريخ الشهر الحالي تلقائياً
        from datetime import datetime
        import calendar
        now = datetime.now()
        start_date = now.replace(day=1).strftime('%Y-%m-%d')
        last_day = calendar.monthrange(now.year, now.month)[1]
        end_date = now.replace(day=last_day).strftime('%Y-%m-%d')
        
        # استدعاء الدالة الموحدة من المطبخ المركزي
        from core_accounting import generate_detailed_excel_report
        excel_stream = await generate_detailed_excel_report(start_date, end_date)

        month_str = now.strftime('%Y-%m')
        file_name = f"Monthly_Report_{month_str}.xlsx"
        document = BufferedInputFile(excel_stream.read(), filename=file_name)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ إرسال للمدير العام (وحفظ في الأرشيف)", callback_data=f"send_report_to_owner_{month_str}")],
            [InlineKeyboardButton(text="❌ إلغاء ورجوع", callback_data="cancel_action")]
        ])
        
        await callback.message.answer_document(document=document, caption="?? **التقرير الشهري (Excel) جاهز!**\nقم بفتح الملف لمراجعته. هل تريد اعتماده وإرساله للمدير العام؟", reply_markup=kb)
        
        try:
            await wait_msg.delete()
        except Exception as e: logging.error(f"Error: {e}")
        
    except Exception as e:
        await wait_msg.edit_text(f"❌ حدث خطأ أثناء إعداد التقرير:\nالسبب: `{str(e)}`")

@router.callback_query(F.data.startswith("send_report_to_owner_"))
async def send_report_to_owner(callback: types.CallbackQuery, bot: Bot):
    month_str = callback.data.split("_")[4]
    file_id = callback.message.document.file_id
    
    await database.set_setting("share_monthly_report", "on")
    await database.save_report_archive(month_str, file_id)
    
    await callback.message.edit_caption(caption="✅ **تم اعتماد التقرير الشهري بنجاح.**\nتم إرسال إشعار للمدير العام، وتم حفظ نسخة PDF في الأرشيف السري.", reply_markup=None)
    
    # إرسال نسخة PDF لقناة الأرشيف السري فقط
    archive_channel = await database.get_setting("archive_channel_id")
    if archive_channel and archive_channel != "off":
        try:
            # 🌟 التعديل الأمني: توليد الـ PDF في مسار خلفي لمنع تجميد البوت
            summary_data = {'wholesale': 0, 'retail': 0, 'collected': 0, 'expenses': 0, 'damaged': 0, 'paid_to_network': 0, 'net_cash': 0}
            from pdf_generator import generate_custom_report_pdf
            import calendar
            
            # 🌟 الإصلاح الذكي: حساب آخر يوم في الشهر برمجياً لمنع انهيار السيرفر
            year, month = map(int, month_str.split('-'))
            last_day = calendar.monthrange(year, month)[1]
            
            pdf_buffer = await asyncio.to_thread(generate_custom_report_pdf, f"{month_str}-01", f"{month_str}-{last_day}", summary_data)

            pdf_document = BufferedInputFile(pdf_buffer.read(), filename=f"Archive_Report_{month_str}.pdf")
            await bot.send_document(archive_channel, pdf_document, caption=f"📂 **أرشيف التقارير (PDF):** تقرير شهر {month_str}")
            await bot.send_document(archive_channel, file_id, caption=f"?? **أرشيف التقارير (Excel):** تقرير شهر {month_str}")
        except Exception as e: logging.error(f"Error: {e}")
        
    try:
        await smart_notify(bot, NETWORK_OWNER_ID, f"🔔 **يا شيخ رهيب، تم  اعتماد تقرير شهر ({month_str}).**\nتقدر تسحب ملف الإكسل من الأزرار، او ترسلي صوت (هات التقرير) او ارسلي رسالة نصية (اعطني التقرير).")
    except Exception as e: logging.error(f"Error: {e}")

@router.callback_query(F.data == "admin_pre_audit_ledger")
async def start_pre_audit_ledger(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📑 **دفتر المراجعة التفصيلي:**\nأدخل تاريخ البداية والنهاية بصيغة YYYY-MM-DD\n(مثال: من 2023-10-01 إلى 2023-10-31):")
    await state.set_state(PreAuditLedgerFlow.waiting_for_dates)

@router.message(PreAuditLedgerFlow.waiting_for_dates)
async def process_pre_audit_dates(message: types.Message, state: FSMContext):
    text = message.text
    
    # استدعاء دالة الذكاء الزمني من ملف ai_chat لكي يفهم (الشهر الماضي، هذا الشهر، الخ)
    from ai_chat_panel import get_smart_dates
    start_date, end_date = get_smart_dates(text)
    
    await state.clear()
    wait_msg = await message.answer("⏳ جاري استخراج دفتر المراجعة التفصيلي، لحظات من فضلك...")
    
    try:
        ledger_stream = await generate_detailed_review_ledger(start_date, end_date)
        doc = BufferedInputFile(ledger_stream.read(), filename=f"Review_Ledger_{start_date}_to_{end_date}.xlsx")
        await wait_msg.delete()
        await message.answer_document(document=doc, caption=f"📑 **دفتر المراجعة التفصيلي**\nمن: {start_date}\nإلى: {end_date}\n\n(راجع هذا الملف وطابقه مع دفترك الورقي قبل البدء بالجرد)")
    except Exception as e:
        await wait_msg.edit_text(f"❌ حدث خطأ: {e}")

@router.callback_query(F.data == "custom_pdf_report")
async def start_custom_report(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("?? **تقرير مخصص:**\nأدخل تاريخ البداية بصيغة YYYY-MM-DD (مثال: 2023-10-01):")
    await state.set_state(CustomReportFlow.waiting_for_start_date)

@router.message(CustomReportFlow.waiting_for_start_date)
async def process_start_date(message: types.Message, state: FSMContext):
    await state.update_data(start_date=message.text)
    await message.answer("الآن أدخل تاريخ النهاية بصيغة YYYY-MM-DD (مثال: 2023-10-31):")
    await state.set_state(CustomReportFlow.waiting_for_end_date)

from datetime import datetime
from core_accounting import generate_detailed_excel_report

# دالة مساعدة لتصحيح التاريخ تلقائياً
def fix_date_format(date_str: str) -> str:
    date_str = date_str.strip().replace('/', '-')
    formats = ['%Y-%m-%d', '%d-%m-%Y', '%d-%m-%y', '%Y/%m/%d']
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return date_str

@router.message(CustomReportFlow.waiting_for_end_date)
async def process_end_date(message: types.Message, state: FSMContext):
    end_date_input = message.text
    data = await state.get_data()
    start_date_input = data['start_date']
    await state.clear()
    
    # تصحيح التواريخ
    start_date = fix_date_format(start_date_input)
    end_date = fix_date_format(end_date_input)
    
    wait_msg = await message.answer(f"⏳ جاري استخراج التقرير التفصيلي الشامل من {start_date} إلى {end_date} بصيغة Excel...")
    
    try:
        # استدعاء دالة الإكسل التفصيلية الجديدة بدلاً من الـ PDF القديم
        excel_stream = await generate_detailed_excel_report(start_date, end_date)
        document = BufferedInputFile(excel_stream.read(), filename=f"Detailed_Report_{start_date}_to_{end_date}.xlsx")
        
        await wait_msg.delete()
        await message.answer_document(document=document, caption=f"✅ **التقرير التفصيلي المخصص جاهز!**\nالفترة: من {start_date} إلى {end_date}")
        
    except Exception as e:
        await wait_msg.edit_text(f"❌ حدث خطأ أثناء جلب البيانات.\nالخطأ: {e}")

# --- أرشيف التقارير السابقة ---
@router.callback_query(F.data == "admin_reports_archive")
@router.callback_query(F.data == "owner_reports_archive")
async def view_reports_archive(callback: types.CallbackQuery):
    await callback.answer()
    kb = []
    if database.pool:
        async with database.pool.acquire() as conn:
            archives = await conn.fetch("SELECT month_year FROM reports_archive ORDER BY month_year DESC LIMIT 12")
            for a in archives:
                kb.append([InlineKeyboardButton(text=f"📅 تقرير شهر {a['month_year']}", callback_data=f"pull_archive_{a['month_year']}")])
                
    # تحديد زر الرجوع المناسب بناءً على من ضغط الزر (المدير أم الوكيل)
    back_callback = "owner_reports_menu" if callback.from_user.id == int(NETWORK_OWNER_ID) else "admin_reports_menu"
    
    if not kb:
        return await callback.message.edit_text("📂 **أرشيف التقارير فارغ حالياً.**\nلم يتم اعتماد أي تقرير شهري بعد.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data=back_callback)]]))
        
    kb.append([InlineKeyboardButton(text="🔙 رجوع", callback_data=back_callback)])
    await callback.message.edit_text("?? **أرشيف التقارير السابقة:**\nاختر الشهر الذي تريد سحب تقريره:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("pull_archive_"))
async def pull_archive_report(callback: types.CallbackQuery, bot: Bot):
    month_str = callback.data.split("_")[2]
    await callback.answer("⏳ جاري سحب التقرير...")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            file_id = await conn.fetchval("SELECT file_id FROM reports_archive WHERE month_year = $1", month_str)
            if file_id:
                await callback.message.answer_document(document=file_id, caption=f"📂 **تقرير شهر {month_str} (من الأرشيف)**")
            else:
                await callback.message.answer("❌ عذراً، لم يتم العثور على ملف التقرير في الأرشيف.")

@router.callback_query(F.data == "admin_audit_logs")
async def view_audit_logs(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    await callback.answer("⏳ جاري جلب سجل المراقبة...")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            logs = await conn.fetch("SELECT table_name, action, details, changed_at FROM audit_logs ORDER BY changed_at DESC LIMIT 15")
            
            if not logs:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_reports_menu")]])
                return await callback.message.edit_text("🛡️ **سجل المراقبة والتدقيق:**\nلا توجد أي تعديلات حساسة مسجلة حتى الآن.", reply_markup=kb)
                
            text = "🛡️ **سجل المراقبة والتدقيق (آخر 15 تعديل):**\n\n"
            for log in logs:
                date_str = str(log['changed_at'])[:16] if log['changed_at'] else "غير معروف"
                text += f"🔹 **{log['action']}**\n"
                text += f"📝 التفاصيل: {log['details']}\n"
                text += f"🕒 الوقت: {date_str}\n"
                text += "〰️〰️〰️〰️\n"
                
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_reports_menu")]])
            await callback.message.edit_text(text, reply_markup=kb)

# ================= قرارات المدير العام للكروت التالفة =================
class ManagerDamagedFlow(StatesGroup):
    waiting_for_reject_reason = State()
    waiting_for_card_pin = State()

# 1. حالة الرفض
@router.callback_query(F.data.startswith("mgr_dmg_reject_"))
async def mgr_dmg_reject(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    await state.update_data(dmg_client_id=int(parts[3]), dmg_ticket_id=int(parts[4]))
    await callback.message.answer("❌ **رفض التعويض:**\nاكتب سبب الرفض لكي نرسله للعميل والوكيل (مثال: الكرت مستخدم مسبقاً):")
    await state.set_state(ManagerDamagedFlow.waiting_for_reject_reason)

@router.message(ManagerDamagedFlow.waiting_for_reject_reason)
async def process_mgr_dmg_reject(message: types.Message, state: FSMContext, bot: Bot):
    reason = message.text
    data = await state.get_data()
    client_id, ticket_id = data['dmg_client_id'], data['dmg_ticket_id']

    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute("UPDATE chat_messages SET message_text = message_text || $1 WHERE id = $2", f"\n(❌ تم الرفض: {reason})", ticket_id)
            
    await message.answer("✅ تم إرسال الرفض للعميل والوكيل.")
    try: await smart_notify(client_id, f"❌ **رد الإدارة بخصوص الكرت التالف:**\nنعتذر، تم رفض التعويض للسبب التالي:\n{reason}")
    except Exception as e: logging.error(f"Error: {e}")
    try: await smart_notify(ADMIN_ID, f"❌ **إشعار للوكيل:**\nالمدير العام رفض تعويض العميل بخصوص الكرت التالف.\nالسبب: {reason}")
    except Exception as e: logging.error(f"Error: {e}")
    
    # 🌟 إرسال إشعار للتطبيق للعميل والوكيل 🌟
    try:
        from web_api import send_web_push
        await send_web_push(client_id, "❌ رفض تعويض", f"تم رفض تعويض الكرت التالف للسبب: {reason}")
        await send_web_push(ADMIN_ID, "❌ إشعار إداري", "المدير العام رفض تعويض العميل بخصوص الكرت التالف.")
    except Exception as e: logging.error(f"Error: {e}")
    
    await state.clear()

# 2. حالة التحويل للوكيل
@router.callback_query(F.data.startswith("mgr_dmg_agent_"))
async def mgr_dmg_agent(callback: types.CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    client_id, ticket_id = int(parts[3]), int(parts[4])
    
    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute("UPDATE chat_messages SET message_text = message_text || '\n(👨‍💻 تم التحويل للوكيل للتعويض)' WHERE id = $1", ticket_id)
            client_name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
            
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n👨‍💻 **تم تحويل المهمة للوكيل.**", reply_markup=None)
    
    try: await smart_notify(bot, ADMIN_ID, f"🚨 **أمر إداري بتعويض عميل:**\nالمدير العام فحص الكرت التالف للعميل ({client_name}) واعتمد التعويض.\nيرجى تعويضه بكرت ورقي يداً بيد، ثم تسجيله في قسم (المرتجعات -> كروت تالفة) لخصمه من حسابك.")
    except Exception as e: logging.error(f"Error: {e}")

    try: await smart_notify(client_id, "✅ تم فحص الكرت واعتماد التعويض من الإدارة. يرجى التواصل مع الوكيل (وليد) لاستلام الكرت البديل.")
    except Exception as e: logging.error(f"Error: {e}")
    
    # 🌟 إضافة إشعارات التطبيق للوكيل والعميل
    try:
        from web_api import send_web_push
        await send_web_push(ADMIN_ID, "🚨 أمر إداري", f"المدير العام اعتمد تعويض العميل ({client_name})، يرجى تعويضه.")
        await send_web_push(client_id, "✅ تعويض كرت تالف", "تم فحص الكرت واعتماد التعويض. يرجى التواصل مع الوكيل لاستلام البديل.")
    except: pass

# 3. حالة التعويض المباشر (المدير يرسل رقم كرت)
@router.callback_query(F.data.startswith("mgr_dmg_direct_"))
async def mgr_dmg_direct(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    await state.update_data(dmg_client_id=int(parts[3]), dmg_ticket_id=int(parts[4]))
    await callback.message.answer("✅ **تعويض مباشر:**\nقم بخدش كرت ورقي، واكتب الرقم السري هنا لكي نرسله للعميل كتعويض:")
    await state.set_state(ManagerDamagedFlow.waiting_for_card_pin)

@router.message(ManagerDamagedFlow.waiting_for_card_pin)
async def process_mgr_dmg_direct(message: types.Message, state: FSMContext, bot: Bot):
    pin = message.text
    data = await state.get_data()
    client_id, ticket_id = data['dmg_client_id'], data['dmg_ticket_id']

    if database.pool:
        async with database.pool.acquire() as conn:
            # 1. تحديث التذكرة القديمة
            await conn.execute("UPDATE chat_messages SET message_text = message_text || $1 WHERE id = $2", f"\n(✅ تم التعويض المباشر. رقم الكرت: {pin})", ticket_id)
            
            # 2. 🌟 التعديل الأهم: إرسال رسالة واضحة في شات الويب تحتوي على الكرت لكي ينسخها العميل بسهولة
            await conn.execute("INSERT INTO chat_messages (client_id, sender_type, message_type, message_text) VALUES ($1, 'agent', 'support', $2)", client_id, f"✅ تم فحص الكرت التالف واعتماد التعويض.\nتفضل رقم الكرت البديل:\n{pin}")
            
            client_name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
            
    await message.answer("✅ تم إرسال الكرت البديل للعميل (في تطبيق الويب)، وتم إشعار الوكيل.")
    
    # 3. 🌟 إرسال إشعار ويب (Push Notification) ليرن هاتف العميل والوكيل
    try:
        from web_api import send_web_push
        await send_web_push(client_id, "✅ تعويض كرت تالف", "تم اعتماد التعويض وإرسال رقم الكرت البديل في المحادثة.")
        await send_web_push(ADMIN_ID, "✅ إشعار إداري", f"المدير العام قام بتعويض العميل ({client_name}) مباشرة.")
    except Exception as e: logging.error(f"Error: {e}")

    try: await smart_notify(client_id, f"✅ **تعويض من الإدارة:**\nتم فحص الكرت التالف واعتماد التعويض. تفضل رقم الكرت البديل:\n`{pin}`")
    except Exception as e: logging.error(f"Error: {e}")
    try: await smart_notify(bot, ADMIN_ID, f"✅ **إشعار للوكيل:**\nالمدير العام قام بتعويض العميل ({client_name}) بكرت بديل مباشرة. (لا حاجة لأي إجراء من طرفك).")
    except Exception as e: logging.error(f"Error: {e}")
    await state.clear()

# ================= 8. الإعدادات والخصوصية =================

@router.callback_query(F.data.startswith("toggle_share_"))
async def toggle_privacy_setting(callback: types.CallbackQuery):
    setting_key = callback.data.replace("toggle_", "")
    current_val = await database.get_setting(setting_key)
    new_val = "off" if current_val == "on" else "on"
    
    await database.set_setting(setting_key, new_val)
    kb = await get_privacy_settings_keyboard()
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("تم تحديث الإعداد بنجاح!")

# --- إعدادات مستحقات الوكيل ---
@router.callback_query(F.data == "admin_comp_settings")
async def admin_comp_settings_menu(callback: types.CallbackQuery):
    await callback.answer()
    comp_type = await database.get_setting("agent_comp_type")
    comp_value = await database.get_setting("agent_comp_value")
    
    type_str = "راتب ثابت" if comp_type == "fixed" else ("نسبة مئوية" if comp_type == "percentage" else "غير محدد")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 تحديد راتب ثابت", callback_data="set_comp_fixed")],
        [InlineKeyboardButton(text="📊 تحديد نسبة مئوية", callback_data="set_comp_percentage")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_settings_menu")]
    ])
    
    await callback.message.edit_text(
        f"🤝 **إعدادات مستحقات الوكيل:**\n\n"
        f"النظام الحالي: **{type_str}**\n"
        f"القيمة الحالية: **{comp_value}**\n\n"
        f"*(هذه الإعدادات تستخدم لحساب أرباحك تلقائياً نهاية كل شهر)*",
        reply_markup=kb
    )

@router.callback_query(F.data.startswith("set_comp_"))
async def set_comp_type(callback: types.CallbackQuery, state: FSMContext):
    comp_type = callback.data.split("_")[2]
    await database.set_setting("agent_comp_type", comp_type)
    
    msg = "💵 أدخل مبلغ الراتب الثابت (بالريال):" if comp_type == "fixed" else "📊 أدخل النسبة المئوية (مثال: 10):"
    await callback.message.edit_text(msg)
    await state.set_state(AgentCompSettingsFlow.waiting_for_value)

@router.message(AgentCompSettingsFlow.waiting_for_value)
async def set_comp_value(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): return await message.answer("أرقام فقط!")
    await database.set_setting("agent_comp_value", message.text)
    await state.clear()
    await message.answer("✅ تم حفظ إعدادات المستحقات بنجاح.", reply_markup=await get_settings_submenu_keyboard())

# ================= 9. لوحة تحكم المدير العام (الرد الذكي) =================
@router.message(CommandStart(), F.from_user.id == int(NETWORK_OWNER_ID))
async def owner_start(message: types.Message):
    alerts_text, available_cash, today_sales = await get_dashboard_stats(is_owner=True)
            
    msg = (
        f"أهلاً بك يا شيخ رهيب في إمبراطورية الشهابpro 👑.\n\n"
        f"إليك لمحة سريعة قبل أن تبدأ:\n"
        f"💰 الكاش الصافي للإدارة (بدون أرباح الوكيل): **{available_cash} ريال**.\n"
        f"📈 مبيعات اليوم (كروت فقط): **{today_sales} ريال**.\n\n"
        f"🔔 **تنبيهات سريعة من السوق:**\n{alerts_text}\n"
        f"تفضل، لوحة التحكم الخاصة بك جاهزة بالأسفل 👇"
    )
    kb = await get_owner_main_keyboard()
    await message.answer(msg, reply_markup=kb)

@router.callback_query(F.data == "owner_main_menu")
async def owner_main_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    alerts_text, available_cash, today_sales = await get_dashboard_stats(is_owner=True)
            
    msg = (
        f"👑 **لوحة تحكم الإدارة العامة (الشيخ . رهيب)**\n\n"
        f"💰 الكاش المتوفر مع الوكيل الآن: **{available_cash} ريال**.\n"
        f"?? مبيعات اليوم حتى اللحظة: **{today_sales} ريال**.\n\n"
        f"🔔 **تنبيهات سريعة من السوق:**\n{alerts_text}\n"
        f"تفضل، يمكنك متابعة العمل من هنا 👇"
    )
    kb = await get_owner_main_keyboard()
    await callback.message.edit_text(msg, reply_markup=kb)

@router.callback_query(F.data == "owner_monitoring_menu")
async def owner_monitoring_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    kb = await get_owner_monitoring_submenu_keyboard()
    await callback.message.edit_text("📊 **المراقبة المالية والمخزون:**", reply_markup=kb)

@router.callback_query(F.data == "owner_reports_menu")
async def owner_reports_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    kb = await get_owner_reports_submenu_keyboard()
    await callback.message.edit_text("📑 **التقارير والشكاوي:**", reply_markup=kb)

@router.callback_query(F.data == "owner_urgent_commands_menu")
async def owner_urgent_commands_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    kb = await get_owner_urgent_commands_submenu_keyboard()
    await callback.message.edit_text("⚡ **الأوامر العاجلة:**", reply_markup=kb)

# --- 1. عرض مخزون الوكيل ---
@router.callback_query(F.data == "owner_view_agent_inventory")
async def owner_view_agent_inv(callback: types.CallbackQuery):
    await callback.answer()
    if callback.from_user.id != int(NETWORK_OWNER_ID): return
    
    share_state = await database.get_setting("share_agent_inventory")
    if share_state == "off":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_monitoring_menu")]])
        return await callback.message.edit_text("عذراً، وكيل الفرع أوقف ميزة مشاركة مخزونه الخاص حالياً ❌", reply_markup=kb)
        
    await callback.message.edit_text("⏳ جاري جلب مخزون الوكيل...")
    text = "📦 **المخزون الحالي المتوفر لدى  الوكيل:**\n\n"
    
    if database.pool:
        async with database.pool.acquire() as conn:
            items = await conn.fetch("SELECT card_type, quantity FROM inventory")
            has_items = False
            for item in items:
                text += f"▪️ {item['card_type']}: {item['quantity']} كرت\n"
                has_items = True
            if not has_items: text += "المخزون فارغ تماماً حالياً."
                
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_monitoring_menu")]])
    await callback.message.edit_text(text, reply_markup=kb)

# =====================================================================
# 1. عرض مخزون العملاء (للمدير) - بنظام الصفحات
# =====================================================================
@router.callback_query(F.data == "owner_view_inventory")
@router.callback_query(F.data.startswith("owner_inv_page_"))
async def owner_view_inv_paginated(callback: types.CallbackQuery):
    await callback.answer()
    if callback.from_user.id != int(NETWORK_OWNER_ID): return
    
    share_state = await database.get_setting("share_inventory")
    if share_state == "off":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="?? رجوع", callback_data="owner_monitoring_menu")]])
        return await callback.message.edit_text("عذراً، وكيل الفرع أوقف ميزة مشاركة مخزون العملاء حالياً ❌", reply_markup=kb)

    # تحديد رقم الصفحة الحالية
    page = 0
    if callback.data.startswith("owner_inv_page_"):
        page = int(callback.data.split("_")[3])

    ITEMS_PER_PAGE = 5
    
    if database.pool:
        async with database.pool.acquire() as conn:
            clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client'")
            
            if not clients:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_monitoring_menu")]])
                return await callback.message.edit_text("👀 **مخزون العملاء الحالي:**\n\nلا يوجد عملاء مسجلون حالياً.", reply_markup=kb)

            total_pages = (len(clients) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
            current_clients = clients[page * ITEMS_PER_PAGE : (page + 1) * ITEMS_PER_PAGE]
            text = f"👀 **مخزون العملاء الحالي (صفحة {page+1}/{total_pages}):**\n\n"
            
            # 🚀 التعديل: جلب مخزون كل عملاء هذه الصفحة باستعلام واحد فقط!
            client_ids = [c['user_id'] for c in current_clients]
            all_inv = await conn.fetch("SELECT user_id, card_type, quantity FROM client_inventory WHERE user_id = ANY($1) AND quantity > 0", client_ids)
            
            # ترتيب البيانات في قاموس (Dictionary) للوصول السريع
            inv_dict = {}
            for item in all_inv:
                uid = item['user_id']
                if uid not in inv_dict: inv_dict[uid] = []
                inv_dict[uid].append(item)
            
            for client in current_clients:
                uid = client['user_id']
                text += f"👤 **{client['name']}:**\n"
                if uid in inv_dict:
                    for item in inv_dict[uid]: 
                        text += f"   - {item['card_type']}: {item['quantity']} كرت\n"
                else:
                    text += "   - لا يوجد كروت في مخزونه حالياً.\n"
                text += "\n"

    # بناء أزرار التنقل
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ السابق", callback_data=f"owner_inv_page_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="التالي ➡️", callback_data=f"owner_inv_page_{page+1}"))
        
    kb_layout = []
    if nav_buttons:
        kb_layout.append(nav_buttons)
    kb_layout.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_monitoring_menu")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_layout))

# =====================================================================
# 2. عرض ديون السوق (للمدير) - بنظام الصفحات
# =====================================================================
@router.callback_query(F.data == "owner_view_market_debt")
@router.callback_query(F.data.startswith("owner_debt_page_"))
async def owner_view_market_debt_paginated(callback: types.CallbackQuery):
    await callback.answer()
    if callback.from_user.id != int(NETWORK_OWNER_ID): return
    
    page = int(callback.data.split("_")[3]) if callback.data.startswith("owner_debt_page_") else 0
    ITEMS_PER_PAGE = 7 
    
    from core_accounting import get_financial_summary
    fin_stats = await get_financial_summary()
    total_debt_cost = fin_stats.get("debt_cost", Decimal('0.0'))
    
    kb_layout = []
    
    if database.pool:
        async with database.pool.acquire() as conn:
            total_clients = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role = 'client'")
            
            if total_clients == 0:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_monitoring_menu")]])
                return await callback.message.edit_text("👥 **إجمالي ديون السوق:** 0 ريال\n\nلا يوجد عملاء مسجلون.", reply_markup=kb)

            total_pages = (total_clients + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
            offset = page * ITEMS_PER_PAGE
            
            # 🌟 سد ثغرة انهيار الترتيب: استخدام COALESCE لتحويل الـ NULL إلى صفر قبل الطرح
            current_clients = await conn.fetch("SELECT name, debt, pending_profit FROM users WHERE role = 'client' ORDER BY (debt - COALESCE(pending_profit, 0)) DESC LIMIT $1 OFFSET $2", ITEMS_PER_PAGE, offset)
            
            text = f"👥 **إجمالي ديون السوق (رأس المال):** **{total_debt_cost} ريال**\n\n"
            for client in current_clients:
                net_debt = Decimal(client['debt']) - (Decimal(client['pending_profit']) if client['pending_profit'] else 0)
                indicator = "🟢" if net_debt <= 0 else ("🟡" if net_debt < 50000 else "🔴")
                text += f"{indicator} **{client['name']}:** {int(net_debt)} ريال\n"

    nav_buttons = get_pagination_buttons(page, total_pages, "owner_debt_page")
    if nav_buttons: kb_layout.append(nav_buttons)
    
    kb_layout.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_monitoring_menu")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_layout))

# =====================================================================
# 3. كشف ديون ومخزون السوق (للوكيل) - بنظام الصفحات
# =====================================================================
@router.callback_query(F.data == "admin_market_debt_inventory_report")
@router.callback_query(F.data.startswith("admin_report_page_"))
async def view_market_debt_inventory_report_paginated(callback: types.CallbackQuery):
    await callback.answer()
    
    page = 0
    if callback.data.startswith("admin_report_page_"):
        page = int(callback.data.split("_")[3])

    ITEMS_PER_PAGE = 5
    kb_layout = []
    
    if database.pool:
        async with database.pool.acquire() as conn:
            total_clients = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role = 'client'")
            
            if total_clients == 0:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 العودة للقائمة الرئيسية", callback_data="admin_reports_menu")]])
                return await callback.message.edit_text("📊 **كشف ديون ومخزون السوق:**\n\nلا يوجد عملاء مسجلون حالياً.", reply_markup=kb)

            total_pages = (total_clients + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
            offset = page * ITEMS_PER_PAGE
            
            # 🌟 الإصلاح المحاسبي: جلب الدين الإجمالي وترتيب العملاء بناءً عليه ليتطابق مع الدفتر الورقي
            current_clients = await conn.fetch("SELECT user_id, name, debt FROM users WHERE role = 'client' ORDER BY debt DESC LIMIT $1 OFFSET $2", ITEMS_PER_PAGE, offset)

            text = f"📊 **كشف ديون ومخزون السوق (صفحة {page+1}/{total_pages}):**\n\n"
            
            # جلب مخزون كل عملاء هذه الصفحة باستعلام واحد فقط!
            client_ids = [c['user_id'] for c in current_clients]
            all_inv = await conn.fetch("SELECT user_id, card_type, quantity FROM client_inventory WHERE user_id = ANY($1) AND quantity > 0", client_ids)
            
            # ترتيب البيانات في قاموس (Dictionary) للوصول السريع
            inv_dict = {}
            for item in all_inv:
                uid = item['user_id']
                if uid not in inv_dict: inv_dict[uid] = []
                inv_dict[uid].append(item)
            
            for client in current_clients:
                # 🌟 الإصلاح: عرض الدين الإجمالي للوكيل
                debt = Decimal(client['debt'])
                indicator = "🟢" if debt <= 0 else ("🟡" if debt < 50000 else "🔴")
                uid = client['user_id']

                text += f"👤 {indicator} **{client['name']}:**\n"
                text += f"   - الدين: {int(debt)} ريال\n"
                
                if uid in inv_dict:
                    text += "   - المخزون:\n"
                    for item in inv_dict[uid]: 
                        text += f"      ▪️ {item['card_type']}: {item['quantity']} كرت\n"
                else:
                    text += "   - لا يوجد كروت في مخزونه حالياً.\n"
                text += "\n"

    # استخدام الدالة المختصرة للأزرار
    nav_buttons = get_pagination_buttons(page, total_pages, "admin_report_page")
    if nav_buttons: kb_layout.append(nav_buttons)
        
    kb_layout.append([InlineKeyboardButton(text="🔙 العودة للقائمة الرئيسية", callback_data="admin_reports_menu")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_layout))

# --- عرض الكاش المتوفر (مع التفصيل) للوكيل ---
@router.callback_query(F.data == "admin_view_cash")
async def admin_view_cash(callback: types.CallbackQuery):
    await callback.answer()
    if callback.from_user.id != ADMIN_ID: return
        
    from core_accounting import get_financial_summary
    fin_stats = await get_financial_summary()
    
    # 🌟 التعديل هنا: استخدام المفاتيح الصحيحة (cash و telecom) لكي يقرأ الأرقام الفعلية
    network_cash = fin_stats.get("cash", Decimal('0.0'))
    telecom_cash = fin_stats.get("telecom", Decimal('0.0'))
    total_drawer_cash = network_cash + telecom_cash
    
    # 🌟 إضافة: حساب المطلوب توريده للمدير (من كاش الكروت فقط)
    agent_profit = fin_stats.get("realized", Decimal('0.0'))
    required_to_remit = max(Decimal('0.0'), network_cash - agent_profit)

    text = f"💵 **السيولة النقدية (الكاش) المتوفرة في الدرج:**\n"
    text += f"💰 **الإجمالي الفعلي:** **{int(total_drawer_cash)} ريال**\n\n"
    text += f"📊 **التفصيل المحاسبي:**\n"
    text += f"▪️ كاش يخص الشبكة والكروت: {int(network_cash)} ريال\n"
    text += f"   *(منها {int(required_to_remit)} ريال صافي للمدير، و {int(agent_profit)} ريال أرباحك)*\n"
    text += f"▪️ كاش يخص قسم التسديدات: {int(telecom_cash)} ريال\n\n"

    if database.pool:
        async with database.pool.acquire() as conn:
            # 🌟 سد ثغرة الكاش المجهول: جلب كل مصادر الكاش بما فيها التسديدات الطياري ورأس المال
            cash_in_txs = await conn.fetch("""
                SELECT type, details, SUM(amount) as total_amount 
                FROM transactions 
                WHERE type IN ('تسديد_من_عميل', 'بيع_مباشر', 'تحصيل_رصيد', 'تسديد_باقة_وكيل', 'رأس_مال_تسديدات') 
                AND is_reverted = FALSE
                AND date >= date_trunc('month', CURRENT_DATE)
                GROUP BY type, details
                ORDER BY total_amount DESC
            """)
            
            if cash_in_txs:
                text += "🔍 **تفاصيل مصادر الكاش (هذا الشهر):**\n"
                for tx in cash_in_txs:
                    if tx['type'] == 'تسديد_من_عميل': tx_type = "تسديد دين كروت"
                    elif tx['type'] == 'تحصيل_رصيد': tx_type = "تحصيل تسديدات"
                    elif tx['type'] == 'تسديد_باقة_وكيل': tx_type = "مبيعات تسديدات (طياري)"
                    elif tx['type'] == 'رأس_مال_تسديدات': tx_type = "ضخ رأس مال"
                    else: tx_type = "مبيعات نقدية"

                    # 🌟 التعديل البصري: إضافة int() لمنع ظهور الأرقام بصيغة 3E+4
                    text += f"➕ {int(tx['total_amount'])} ريال ({tx_type}: {tx['details']})\n"

            else:
                text += "لا توجد تفاصيل عمليات نقدية مسجلة هذا الشهر."
            
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_finance_menu")]])
    await callback.message.edit_text(text, reply_markup=kb)

# --- عرض الكاش المتوفر (إجمالي فقط للمدير) ---
@router.callback_query(F.data == "owner_view_cash")
async def owner_view_cash(callback: types.CallbackQuery):
    await callback.answer()
    if callback.from_user.id != int(NETWORK_OWNER_ID): return
    
    share_state = await database.get_setting("share_available_cash")
    if share_state == "off":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_monitoring_menu")]])
        return await callback.message.edit_text("عذراً، الوكيل اوقف ميزة مشاركة الكاش المتوفر حالياً ❌", reply_markup=kb)
        
    from core_accounting import get_financial_summary
    fin_stats = await get_financial_summary()
    
    # 🌟 التعديل الجوهري: خصم أرباح الوكيل من الكاش الذي يراه المدير 🌟
    available_cash = max(Decimal('0.0'), fin_stats.get("cash", Decimal('0.0')) - fin_stats.get("realized", Decimal('0.0')))
            
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_monitoring_menu")]])
    await callback.message.edit_text(f"💵 **السيولة النقدية (الكاش) المتوفرة لدى الوكيل:**\n\nالمبلغ الإجمالي: **{int(available_cash)} ريال**", reply_markup=kb)

@router.callback_query(F.data == "owner_report_not_approved")
async def owner_report_not_approved_callback(callback: types.CallbackQuery):
    """رد البوت عندما يضغط المدير على التقرير وهو غير جاهز"""
    await callback.answer("⏳ عذراً يا شيخ رهيب، التقرير المالي لهذا الشهر لا يزال قيد التجهيز والمراجعة من قبل وليد", show_alert=True)
    
# --- 5. سحب التقرير الشهري المباشر (من الأرشيف) ---
@router.callback_query(F.data == "owner_pull_report")
async def owner_pull_report(callback: types.CallbackQuery, bot: Bot):
    await callback.answer("⏳ جاري سحب أحدث تقرير...")
    if callback.from_user.id != int(NETWORK_OWNER_ID): return
    
    if database.pool:
        async with database.pool.acquire() as conn:
            latest_report = await conn.fetchrow("SELECT month_year, file_id FROM reports_archive ORDER BY created_at DESC LIMIT 1")
            
            if latest_report:
                await callback.message.answer_document(document=latest_report['file_id'], caption=f"📊 **أحدث تقرير معتمد (شهر {latest_report['month_year']})**")
            else:
                await callback.message.answer("❌ عذراً، لم يقم الوكيل باعتماد أي تقرير شهري حتى الآن.")

# --- 6. صندوق الشكاوي والاقتراحات ---
@router.callback_query(F.data == "owner_complaints_suggestions")
async def owner_complaints_suggestions(callback: types.CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_reports_menu")]])
    await callback.message.edit_text("❓ **صندوق الشكاوي والاقتراحات:**\n*(يتم عرض الشكاوي هنا فور تحويلها من قبل الوكيل)*", reply_markup=kb)

# --- 7. طلب تحويل كاش ---
@router.callback_query(F.data == "owner_request_cash")
async def owner_request_cash_action(callback: types.CallbackQuery, bot: Bot):
    await callback.answer()
    if callback.from_user.id != int(NETWORK_OWNER_ID): return

    await callback.message.edit_text("⏳ جاري إرسال الطلب...")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_urgent_commands_menu")]])
    await callback.message.edit_text(f"✅ تم إرسال إشعار عاجل لوكيل الفرع بطلب تحويل الكاش المتوفر.", reply_markup=kb)
    try:
        await smart_notify(bot, ADMIN_ID, f"?? **طلب تحويل عاجل:**\nالمدير العام (رهيب) يطلب تحويل السيولة النقدية المتوفرة لديك حالياً.")
    except Exception as e: logging.error(f"Error: {e}")

# --- 8. إرسال تعميم عاجل ---
@router.callback_query(F.data == "owner_broadcast")
async def owner_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id != int(NETWORK_OWNER_ID): return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="owner_urgent_commands_menu")]])
    await callback.message.edit_text("📢 **إرسال تعميم عاجل:**\nاكتب الرسالة التي تريد إرسالها لوكيل الفرع (ليقوم بتعميمها على البقالات):", reply_markup=kb)
    await state.set_state(OwnerBroadcastFlow.waiting_for_message)

@router.message(OwnerBroadcastFlow.waiting_for_message)
async def owner_broadcast_send(message: types.Message, state: FSMContext, bot: Bot):
    if message.from_user.id != int(NETWORK_OWNER_ID): return
    
    broadcast_msg = message.text
    await state.clear()
    
    kb = await get_owner_urgent_commands_submenu_keyboard()
    await message.answer("✅ تم إرسال التعميم لوكيل الفرع بنجاح.", reply_markup=kb)
    
    try:
        await smart_notify(bot, ADMIN_ID, f"📢 **تعميم عاجل من الإدارة العامة (ألشيخ رهيب):**\n\n{broadcast_msg}\n\n*(يمكنك نسخ هذه الرسالة وتعميمها على عملائك إذا لزم الأمر)*")
    except Exception as e: logging.error(f"Error: {e}")

# ================= 10. عملاء أوفلاين =================
@router.callback_query(F.data == "admin_offline")
async def admin_offline_menu(callback: types.CallbackQuery):
    """القائمة الرئيسية لإدارة عملاء الأوفلاين"""
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ إضافة عميل أوفلاين جديد", callback_data="add_offline_client")],
        [InlineKeyboardButton(text="📋 عرض عملاء الأوفلاين", callback_data="view_offline_clients")],
        [InlineKeyboardButton(text="🔙 رجوع للإعدادات", callback_data="admin_settings_menu")]
    ])
    await callback.message.edit_text("👴 **إدارة عملاء الأوفلاين:**\nماذا تريد أن تفعل؟", reply_markup=kb)
    
@router.callback_query(F.data == "view_offline_clients")
@router.callback_query(F.data.startswith("view_offline_page_"))
async def view_offline_clients(callback: types.CallbackQuery):
    await callback.answer()
    page = 0
    if callback.data.startswith("view_offline_page_"):
        page = int(callback.data.split("_")[3])

    ITEMS_PER_PAGE = 10
    kb_layout = []
    total_pages = 1
    total_clients = 0
    text = "👴 **عملاء الأوفلاين المسجلون:**\n\nلا يوجد عملاء أوفلاين مسجلون حالياً."
    
    if database.pool:
        async with database.pool.acquire() as conn:
            total_clients = await conn.fetchval("SELECT COUNT(*) FROM users WHERE user_id >= 9990000 AND role = 'client'")
            
            if total_clients > 0:
                total_pages, offset = get_pagination_math(total_clients, page, ITEMS_PER_PAGE)
                current_clients = await conn.fetch("SELECT user_id, name, debt FROM users WHERE user_id >= 9990000 AND role = 'client' ORDER BY user_id LIMIT $1 OFFSET $2", ITEMS_PER_PAGE, offset)
                
                text = f"👴 **عملاء الأوفلاين المسجلون (صفحة {page+1}/{total_pages}):**\n\n"
                for client in current_clients:
                    text += f"▪️ {client['name']} (ID: `{client['user_id']}`): {client['debt']} ريال دين\n"

    # أزرار التنقل بين الصفحات
    nav_buttons = get_pagination_buttons(page, total_pages, "view_offline_page")
    if nav_buttons: 
        kb_layout.append(nav_buttons)
        
    # زر الرجوع لقائمة الأوفلاين
    kb_layout.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_offline")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_layout))

@router.callback_query(F.data == "add_offline_client")
async def add_offline_client_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("➕ **إضافة عميل أوفلاين جديد**\nالرجاء إدخال اسم العميل (مثال: بقالة التوفيق):")
    await state.set_state(OfflineClientFlow.waiting_for_name)

@router.message(OfflineClientFlow.waiting_for_name)
async def add_offline_client_name(message: types.Message, state: FSMContext):
    await state.update_data(client_name=message.text)
    await message.answer("📱 الرجاء إدخال رقم واتساب العميل (مثال: 967777123456)\nأو أرسل 'تخطي' إذا لم ترغب بإضافته:")
    await state.set_state(OfflineClientFlow.waiting_for_phone)

@router.message(OfflineClientFlow.waiting_for_phone)
async def add_offline_client_phone(message: types.Message, state: FSMContext):
    phone = message.text if message.text != 'تخطي' else None
    data = await state.get_data()
    client_name = data['client_name']
    
    from core_accounting import core_add_offline_client
    result = await core_add_offline_client(client_name, phone)
    
    if result["status"] == "error":
        return await message.answer(f"❌ {result['message']}")
        
    await state.clear()
    await message.answer(f"✅ تم إضافة عميل الأوفلاين ({client_name}) بنجاح.\nرقم الحساب: `{result['user_id']}`\nرقم الواتساب: {phone or 'لا يوجد'}")
    
    # إرجاع القائمة الرئيسية للأوفلاين بعد الإضافة
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ إضافة عميل أوفلاين جديد", callback_data="add_offline_client")],
        [InlineKeyboardButton(text="📋 عرض عملاء الأوفلاين", callback_data="view_offline_clients")],
        [InlineKeyboardButton(text="🔙 رجوع للإعدادات", callback_data="admin_settings_menu")]
    ])
    await message.answer("👴 **إدارة عملاء الأوفلاين:**", reply_markup=kb)

# ================= 11. إرسال تعميم للعملاء =================
@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_settings_menu")]])
    await callback.message.edit_text("📢 **إرسال تعميم للعملاء:**\nاكتب الرسالة التي تريد إرسالها لجميع العملاء:", reply_markup=kb)
    await state.set_state(AgentBroadcastFlow.waiting_for_message)

@router.message(AgentBroadcastFlow.waiting_for_message)
async def admin_broadcast_send(message: types.Message, state: FSMContext, bot: Bot):
    broadcast_msg = message.text
    await state.clear()
    
    # 🌟 التعديل البرمجي: إرسال الرد للوكيل فوراً لكي لا تتجمد شاشته
    await message.answer("✅ **جاري إرسال التعميم لجميع العملاء في الخلفية...**\n(تليجرام + إشعارات التطبيق)", reply_markup=await get_settings_submenu_keyboard())
    
    # دالة داخلية للعمل في الخلفية دون تعطيل البوت
    async def background_broadcast():
        if database.pool:
            async with database.pool.acquire() as conn:
                clients = await conn.fetch("SELECT user_id, phone, wa_status FROM users WHERE role = 'client'")
                for client in clients:
                    try:
                        await smart_notify(client["user_id"], f"📢 **تعميم من وكيل فرع بني علي:**\n\n{broadcast_msg}")
                        
                        try:
                            from web_api import send_web_push
                            await send_web_push(client["user_id"], "📢 تعميم إداري هام", broadcast_msg)
                        except Exception as e: logging.error(f"Error: {e}")
                            
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        print(f"Failed to send broadcast to {client['user_id']}: {e}")
                        
    # تشغيل المهمة في الخلفية
    asyncio.create_task(background_broadcast())

# --- الزر اليدوي للنسخ الاحتياطي ---
@router.callback_query(F.data == "admin_backup")
async def request_generate_backup(callback: types.CallbackQuery, state: FSMContext):
    """الخطوة 1: طلب الرمز السري قبل سحب النسخة"""
    await callback.answer()
    if callback.from_user.id != ADMIN_ID: return
    
    await callback.message.edit_text(
        "💾 **سحب نسخة احتياطية شاملة:**\n"
        "هذا الملف يحتوي على جميع أسرار النظام المالية.\n\n"
        "🔐 **الرجاء إدخال الرمز السري (PIN) للسماح بالتصدير:**",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_action")]])
    )
    await state.set_state(SecurityVaultFlow.waiting_for_backup_pin)

@router.message(SecurityVaultFlow.waiting_for_backup_pin)
async def execute_generate_backup(message: types.Message, state: FSMContext):
    """الخطوة 2: التحقق من الرمز وتوليد الإكسل"""
    if message.text.strip() != SYSTEM_VAULT_PIN:
        await state.clear()
        return await message.answer("❌ **رمز خاطئ!** تم منع تصدير البيانات لحماية الخصوصية.")
        
    wait_msg = await message.answer("✅ تم قبول الرمز. ⏳ جاري إنشاء النسخة الاحتياطية الشاملة (Excel)...")
    if not database.pool: return

    try:
        excel_stream = await generate_full_backup_excel()
        file_name = f"Backup_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
        document = BufferedInputFile(excel_stream.read(), filename=file_name)
        
        await message.answer_document(document=document, caption="💾 **النسخة الاحتياطية الشاملة لقاعدة البيانات.**\n(تحتوي على 11 صفحة تشمل 21 جدولاً)")
        await wait_msg.delete()
        await message.answer("👨‍💻 **لوحة تحكم الوكيل**", reply_markup=await get_reports_submenu_keyboard())
    except Exception as e:
        await wait_msg.edit_text(f"❌ حدث خطأ أثناء النسخ: {e}")
    await state.clear()

# ================= زر الإلغاء العام =================
@router.callback_query(F.data == "cancel_action")
async def cancel_flow(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("تم الإلغاء.")
    user_id = callback.from_user.id
    
    # تحديد لوحة المفاتيح والنص بناءً على صلاحية المستخدم
    if user_id == int(NETWORK_OWNER_ID):
        kb = await get_owner_main_keyboard()
        text = "👑 **لوحة تحكم الإدارة العامة (الشيخ رهيب)**\nمرحباً بك! يمكنك متابعة العمل من هنا:"
    elif user_id == ADMIN_ID:
        kb = await get_main_admin_keyboard()
        text = "👨‍💻 **لوحة تحكم  الوكيل**"
    else:
        kb = None
        text = "🚫 **تم الإلغاء.**\nلإظهار القائمة الرئيسية الخاصة بك، اضغط هنا 👉 /start"

    # الحل الآمن لتعديل الرسالة سواء كانت نصاً أو وسائط (مستند/صورة)
    if callback.message.text:
        # إذا كانت رسالة نصية عادية، نعدلها مباشرة
        await callback.message.edit_text(text, reply_markup=kb)
    else:
        # إذا كانت الرسالة تحتوي على وسائط (مثل ملف الإكسل)، نقوم بحذفها وإرسال رسالة نصية جديدة
        try:
            await callback.message.delete()
        except:
            pass
        await callback.message.answer(text, reply_markup=kb)

        # ================= كشف حساب تفصيلي لعميل =================
@router.callback_query(F.data == "admin_detailed_ledger")
@router.callback_query(F.data.startswith("dledger_page_"))
async def start_detailed_ledger(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    page = 0
    if callback.data.startswith("dledger_page_"):
        page = int(callback.data.split("_")[2])

    ITEMS_PER_PAGE = 8
    clients_kb = []
    total_pages = 1
    total_clients = 0

    if database.pool:
        async with database.pool.acquire() as conn:
            total_clients = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role = 'client'")
            if total_clients > 0:
                total_pages, offset = get_pagination_math(total_clients, page, ITEMS_PER_PAGE)
                current_clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client' ORDER BY user_id LIMIT $1 OFFSET $2", ITEMS_PER_PAGE, offset)
                
                for c in current_clients:
                    clients_kb.append([InlineKeyboardButton(text=c['name'], callback_data=f"dledger_{c['user_id']}")])

    nav_buttons = get_pagination_buttons(page, total_pages, "dledger_page")
    if nav_buttons: clients_kb.append(nav_buttons)

    clients_kb.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_reports_menu")])
    
    text = f"📜 **كشف حساب تفصيلي:**\nاختر العميل - صفحة {page+1}/{total_pages}:" if total_clients > 0 else "📜 لا يوجد عملاء مسجلين."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=clients_kb))
    await state.set_state(DetailedLedgerFlow.waiting_for_client)

@router.callback_query(DetailedLedgerFlow.waiting_for_client, F.data.startswith("dledger_"))
async def process_dledger_client(callback: types.CallbackQuery, state: FSMContext):
    client_id = int(callback.data.split("_")[1])
    await state.update_data(client_id=client_id)
    await callback.message.edit_text("📅 أدخل الفترة المطلوبة (مثال: من 2026-05-01 إلى 2026-05-30):")
    await state.set_state(DetailedLedgerFlow.waiting_for_dates)

@router.message(DetailedLedgerFlow.waiting_for_dates)
async def process_dledger_dates(message: types.Message, state: FSMContext):
    from ai_chat_panel import get_smart_dates
    start_date_str, end_date_str = get_smart_dates(message.text)
    data = await state.get_data()
    client_id = data['client_id']
    await state.clear()
    
    # ✅ التعديل: تحويل النصوص إلى كائنات تاريخ لكي تقبلها قاعدة البيانات
    from datetime import datetime
    start_date_obj = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date_obj = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    
    wait_msg = await message.answer("⏳ جاري استخراج كشف الحساب التفصيلي...")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # 🌟 الإصلاح المحاسبي: جلب الأرباح المعلقة لحساب الدين الصافي
            user = await conn.fetchrow("SELECT name, debt, pending_profit FROM users WHERE user_id = $1", client_id)
            if user:
                # 🌟 سد كارثة كشف الحساب: جلب عمليات الكروت فقط (wallet_type = 'manager') لمنع اختلاطها مع التسديدات وتدمير الحسابات
                transactions = await conn.fetch('''
                    SELECT date, type, amount, details 
                    FROM transactions 
                    WHERE user_id = $1 AND is_reverted = FALSE AND wallet_type = 'manager' AND date >= $2::date AND date <= $3::date + interval '1 day'
                    ORDER BY date DESC
                ''', client_id, start_date_obj, end_date_obj)

                from pdf_generator import generate_detailed_statement
                # 🌟 الإصلاح المحاسبي: إرسال الدين الإجمالي ليتطابق مع كشف الحساب العام
                gross_debt = Decimal(user['debt'] or 0)
                pdf_buffer = await asyncio.to_thread(generate_detailed_statement, user['name'], gross_debt, transactions, start_date_str, end_date_str)
                pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Ledger_{user['name']}.pdf")
                
                await wait_msg.delete()
                await message.answer_document(document=pdf_file, caption=f"📑 **كشف حساب تفصيلي**\nالعميل: {user['name']}\nمن: {start_date_str} إلى: {end_date_str}")

# ================= تقرير حركة الكروت =================
@router.callback_query(F.data == "admin_card_distribution")
async def start_card_distribution(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📈 **تقرير حركة الكروت:**\nأدخل الفترة المطلوبة (مثال: هذا الشهر، أو من 2026-05-01 إلى 2026-05-30):")
    await state.set_state(CardDistributionFlow.waiting_for_dates)

@router.message(CardDistributionFlow.waiting_for_dates)
async def process_card_distribution_dates(message: types.Message, state: FSMContext):
    from ai_chat_panel import get_smart_dates
    start_date_str, end_date_str = get_smart_dates(message.text)
    await state.clear()
    
    # ✅ التعديل: تحويل النصوص إلى كائنات تاريخ
    from datetime import datetime
    start_date_obj = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date_obj = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    
    wait_msg = await message.answer("⏳ جاري تحليل حركة الكروت...")
    
    report = f"📦 **حركة الكروت المباعة (من {start_date_str} إلى {end_date_str}):**\n\n"
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # 🌟 الإصلاح الجذري: الاستعلام الصحيح للكروت مع تحديث أسماء العمليات الجديدة
            txs = await conn.fetch("""
                SELECT t.type, t.details, u.name 
                FROM transactions t
                LEFT JOIN users u ON t.user_id = u.user_id
                WHERE t.type IN ('تسليم_لعميل', 'مبيعات_كاش', 'بيع_مباشر', 'مبيعات_آجلة')
                AND t.is_reverted = FALSE
                AND t.date >= $1::date AND t.date <= $2::date + interval '1 day'
            """, start_date_obj, end_date_obj)
            
            card_stats = {}
            for tx in txs:
                qty = 0
                ctype = ""
                
                match = re.search(r"(\d+)\s*كرت\s*(.+)", tx['details'])
                if match:
                    qty = int(match.group(1))
                    ctype = match.group(2).replace("كاش", "").strip()
                else:
                    match_ecard = re.search(r"سحب آلي:\s*كرت إلكتروني\s*(.+)", tx['details'])
                    if match_ecard:
                        qty = 1
                        ctype = match_ecard.group(1).strip()
                        
                if qty > 0 and ctype:
                    client_name = tx['name'] if tx['name'] else "مبيعات طياري (كاش)"
                    
                    if ctype not in card_stats:
                        card_stats[ctype] = {'total': 0, 'clients': {}}
                    
                    card_stats[ctype]['total'] += qty
                    card_stats[ctype]['clients'][client_name] = card_stats[ctype]['clients'].get(client_name, 0) + qty
            
            if not card_stats:
                report += "لا توجد مبيعات مسجلة في هذه الفترة."
            else:
                for ctype, data in card_stats.items():
                    report += f"🔹 **{ctype}:** (إجمالي المباع: {data['total']} كرت)\n"
                    for c_name, c_qty in data['clients'].items():
                        report += f"   - {c_name}: {c_qty} كرت\n"
                    report += "\n"
                    
    await wait_msg.edit_text(report)

# ================= ميزة ضبط صحون الشبكة (Point-to-Point) =================

# دالة مساعدة لحساب الاتجاه الجغرافي
def get_compass_direction(bearing):
    directions = ["شمال ⬆️", "شمال شرقي ↗️", "شرق ➡️", "جنوب شرقي ↘️", 
                  "جنوب ⬇️", "جنوب غربي ↙️", "غرب ⬅️", "شمال غربي ↖️"]
    index = round(bearing / 45) % 8
    return directions[index]

# دالة ذكية لجلب الارتفاع عن سطح البحر عبر API مجاني
async def get_elevation(lat, lon):
    url = f"https://api.open-meteo.com/v1/elevation?latitude={lat}&longitude={lon}"
    try:
        async with aiohttp.ClientSession( ) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if "elevation" in data and data["elevation"]:
                        return data["elevation"][0]
    except Exception as e:
        print(f"Elevation API Error: {e}")
    return None

@router.callback_query(F.data == "tool_align_dish")
async def btn_align_dish_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(
        "📡 **أداة ضبط صحون الشبكة (Point-to-Point)**\n\n"
        "الرجاء إرسال **موقع (Location)** الصحن المرسل (البرج / نقطة البث):\n"
        "*(استخدم ميزة إرسال الموقع 📎 من تيليجرام)*",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_action")]])
    )
    await state.set_state(DishAlignmentFlow.waiting_for_tx_location)

@router.message(Command("align_dish"))
async def start_dish_alignment(message: types.Message, state: FSMContext):
    """أمر بدء أداة ضبط الصحن"""
    if message.from_user.id not in [ADMIN_ID, int(NETWORK_OWNER_ID)]: return
    
    await message.answer(
        "📡 **أداة ضبط صحون الشبكة (Point-to-Point)**\n\n"
        "الرجاء إرسال **موقع (Location)** الصحن المرسل (البرج / نقطة البث):\n"
        "*(استخدم ميزة إرسال الموقع 📎 من تيليجرام)*",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_action")]])
    )
    await state.set_state(DishAlignmentFlow.waiting_for_tx_location)

@router.message(DishAlignmentFlow.waiting_for_tx_location, F.location)
async def process_tx_location(message: types.Message, state: FSMContext):
    lat = message.location.latitude
    lon = message.location.longitude
    await state.update_data(tx_lat=lat, tx_lon=lon)
    
    await message.answer(
        "✅ **تم حفظ موقع المرسل (البرج).**\n\n"
        "الآن، اذهب إلى بيت العميل (أو البرج الثاني) وأرسل **موقع (Location)** الصحن المستقبل:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_action")]])
    )
    await state.set_state(DishAlignmentFlow.waiting_for_rx_location)

@router.message(DishAlignmentFlow.waiting_for_rx_location, F.location)
async def process_rx_location(message: types.Message, state: FSMContext):
    wait_msg = await message.answer("⏳ جاري تحليل التضاريس وحساب الزوايا والارتفاعات...")
    
    rx_lat = message.location.latitude
    rx_lon = message.location.longitude
    
    data = await state.get_data()
    tx_lat = data['tx_lat']
    tx_lon = data['tx_lon']
    
    # 1. حساب المسافة (Haversine formula)
    R = 6371.0 # نصف قطر الأرض بالكيلومتر
    dlat = math.radians(rx_lat - tx_lat)
    dlon = math.radians(rx_lon - tx_lon)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(tx_lat)) * math.cos(math.radians(rx_lat)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    distance_km = R * c
    distance_m = distance_km * 1000
    
    if distance_km < 1:
        distance_str = f"{int(distance_m)} متر"
    else:
        distance_str = f"{distance_km:.2f} كيلومتر"

    # 2. حساب زاوية التوجيه (Bearing / Azimuth)
    lat1, lon1, lat2, lon2 = map(math.radians, [tx_lat, tx_lon, rx_lat, rx_lon])
    dlon_rad = lon2 - lon1
    x = math.sin(dlon_rad) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (math.sin(lat1) * math.cos(lat2) * math.cos(dlon_rad))
    initial_bearing = math.atan2(x, y)
    initial_bearing = math.degrees(initial_bearing)
    compass_bearing = (initial_bearing + 360) % 360
    direction = get_compass_direction(compass_bearing)
    
    # 3. جلب الارتفاعات وحساب زاوية الميل (Elevation / Tilt)
    tx_elev = await get_elevation(tx_lat, tx_lon)
    rx_elev = await get_elevation(rx_lat, rx_lon)
    
    tilt_str = "غير متوفر (فشل جلب التضاريس)"
    elev_details = ""
    
    if tx_elev is not None and rx_elev is not None:
        # فرق الارتفاع (المستقبل ناقص المرسل)
        height_diff = rx_elev - tx_elev
        
        # حساب زاوية الميل بالدرجات: arctan(height_diff / distance)
        tilt_angle = math.degrees(math.atan2(height_diff, distance_m))
        
        elev_details = f"⛰️ **ارتفاع البرج:** {tx_elev:.1f} متر عن سطح البحر\n"
        elev_details += f"🏠 **ارتفاع العميل:** {rx_elev:.1f} متر عن سطح البحر\n"
        
        if tilt_angle > 0:
            tilt_str = f"ارفع الصحن للأعلى ⬆️ بزاوية ({abs(tilt_angle):.1f}°) درجات"
        elif tilt_angle < 0:
            tilt_str = f"نزّل الصحن للأسفل ⬇️ بزاوية ({abs(tilt_angle):.1f}°) درجات"
        else:
            tilt_str = "الصحن بشكل مستقيم ↔️ (0°) درجات"

    # رابط خرائط جوجل يوضح النقطتين
    maps_link = f"https://www.google.com/maps/dir/{tx_lat},{tx_lon}/{rx_lat},{rx_lon}/data=!3m1!4b1!4m2!4m1!3e2"

    report = (
        "🎯 **تقرير توجيه الصحن (Alignment Report ):**\n"
        "========================\n"
        f"📏 **المسافة الجوية:** {distance_str}\n"
        f"🧭 **زاوية البوصلة (Azimuth):** {compass_bearing:.1f}° درجة\n"
        f"📍 **الاتجاه:** {direction}\n"
        "------------------------\n"
        f"{elev_details}"
        f"📐 **زاوية الميل (Tilt):** {tilt_str}\n"
        "========================\n"
        "💡 **طريقة الضبط:**\n"
        f"1️⃣ وجه الصحن يميناً/يساراً نحو الزاوية **{compass_bearing:.1f}°** ({direction}).\n"
        f"2️⃣ {tilt_str} (مع مراعاة ارتفاع الماصورة).\n\n"
        f"🗺️ [اضغط هنا لرؤية المسار على الخريطة]({maps_link})"
    )
    
    await wait_msg.delete()
    await message.answer(report, disable_web_page_preview=True)
    await state.clear()

# إذا أرسل نصاً بدلاً من الموقع بالخطأ
@router.message(DishAlignmentFlow.waiting_for_tx_location)
@router.message(DishAlignmentFlow.waiting_for_rx_location)
async def location_error_handler(message: types.Message):
    await message.answer("⚠️ الرجاء إرسال **موقع (Location)** باستخدام علامة المشبك 📎 أسفل الشاشة، وليس نصاً.")
    
    # ================= إدارة سقف المديونية للعملاء =================
@router.callback_query(F.data == "admin_credit_limits")
@router.callback_query(F.data.startswith("limit_page_"))
async def admin_credit_limits_menu(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    page = 0
    if callback.data.startswith("limit_page_"):
        page = int(callback.data.split("_")[2])

    ITEMS_PER_PAGE = 8
    clients_kb = []
    total_pages = 1
    total_clients = 0
    
    if database.pool:
        async with database.pool.acquire() as conn:
            total_clients = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role = 'client'")
            if total_clients > 0:
                total_pages, offset = get_pagination_math(total_clients, page, ITEMS_PER_PAGE)
                current_clients = await conn.fetch("SELECT user_id, name, credit_limit FROM users WHERE role = 'client' ORDER BY user_id LIMIT $1 OFFSET $2", ITEMS_PER_PAGE, offset)
                
                for c in current_clients:
                    limit = c['credit_limit'] if c['credit_limit'] else 50000.0
                    clients_kb.append([InlineKeyboardButton(text=f"{c['name']} (السقف: {limit})", callback_data=f"setlimit_{c['user_id']}")])

    # استخدام الدالة المختصرة
    nav_buttons = get_pagination_buttons(page, total_pages, "limit_page")
    if nav_buttons: clients_kb.append(nav_buttons)
        
    clients_kb.append([InlineKeyboardButton(text="🔙 رجوع للإعدادات", callback_data="admin_settings_menu")])
    
    text = f"🛑 **إدارة سقف المديونية للعملاء**\nاختر العميل لتعديل سقفه - صفحة {page+1}/{total_pages}:" if total_clients > 0 else "👥 لا يوجد عملاء مسجلين."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=clients_kb))

@router.callback_query(F.data.startswith("setlimit_"))
async def ask_for_new_limit(callback: types.CallbackQuery, state: FSMContext):
    client_id = int(callback.data.split("_")[1])
    await state.update_data(limit_client_id=client_id)
    
    if database.pool:
        async with database.pool.acquire() as conn:
            client_name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
            
    await callback.message.edit_text(
        f"🛑 **تعديل سقف المديونية**\n\n"
        f"👤 العميل: **{client_name}**\n"
        f"✏️ الرجاء إدخال السقف الجديد (بالريال):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_credit_limits")]])
    )
    await state.set_state(CreditLimitFlow.waiting_for_limit)

@router.message(CreditLimitFlow.waiting_for_limit)
async def save_new_limit(message: types.Message, state: FSMContext):
    if not message.text.replace(".", "").isdigit(): 
        return await message.answer("⚠️ الرجاء إدخال رقم صحيح للمبلغ.")
        
    new_limit = Decimal(message.text)
    data = await state.get_data()
    client_id = data["limit_client_id"]
    
    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute("UPDATE users SET credit_limit = $1 WHERE user_id = $2", new_limit, client_id)
            client_name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
            
    await state.clear()
    try:
        from web_api import notify_clients
        await notify_clients(client_id)
    except Exception as e: logging.error(f"Error: {e}")
    await message.answer(f"✅ **تم التعديل بنجاح!**\nأصبح سقف المديونية للعميل ({client_name}) هو: **{new_limit} ريال**.")
    
    kb = await get_settings_submenu_keyboard()
    await message.answer("⚙️ **الإعدادات والتواصل:**", reply_markup=kb)

# ================= صندوق الاعتمادات والحارس المالي الذكي =================
@router.callback_query(F.data.startswith("reject_order_"))
async def reject_client_order(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_ID: return
    order_id = int(callback.data.split("_")[2])
    
    from core_accounting import core_update_order_status
    result = await core_update_order_status(order_id, 'rejected')
    
    if result["status"] == "error":
        return await callback.answer(result["message"], show_alert=True)
        
    client_id = result["client_id"]
    try:
        if callback.message.text:
            await callback.message.edit_text(callback.message.text + "\n\n❌ **(تم رفض الطلب وإغلاقه)**", reply_markup=None)
        elif callback.message.caption:
            await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ **(تم رفض الطلب وإغلاقه)**", reply_markup=None)
    except Exception as e: logging.error(f"Error: {e}")
        
    try: await smart_notify(client_id, "عذراً يا غالي، تم رفض طلبك الأخير من قبل الإدارة (قد يكون بسبب تجاوز سقف المديونية أو عدم توفر الكمية). يرجى التواصل مع الوكيل للتفاصيل.")
    except Exception as e: logging.error(f"Error: {e}")

@router.callback_query(F.data.startswith("msg_client_"))
async def start_msg_client(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    client_id = int(callback.data.split("_")[2])
    
    await state.update_data(reply_client_id=client_id)
    await callback.message.answer("💬 اكتب الرسالة التي تريد إرسالها لهذا العميل:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_action")]]))
    await state.set_state(AdminReplyFlow.waiting_for_reply_msg)

@router.message(AdminReplyFlow.waiting_for_reply_msg)
async def send_msg_to_client(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    client_id = data['reply_client_id']
    reply_text = message.text
    
    # 1. تحديد من المرسل (الوكيل أم المدير)
    sender_type = 'manager' if message.from_user.id == int(NETWORK_OWNER_ID) else 'agent'
    sender_name = "الإدارة العامة" if sender_type == 'manager' else "إدارة الشهاب pro"

    # 2. حفظ الرسالة في قاعدة البيانات لتظهر في شاشة المحادثة بالتطبيق
    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO chat_messages (client_id, sender_type, message_type, message_text)
                VALUES ($1, $2, 'support', $3)
            ''', client_id, sender_type, reply_text)
            
    # 3. إرسال إشعار (Web Push) لهاتف العميل لفتح التطبيق
    try:
        from web_api import send_web_push
        await send_web_push(client_id, f"📩 رسالة من {sender_name}", reply_text)
    except Exception as e:
        print(f"Push Error: {e}")
        
    # 4. إرسالها للتليجرام أيضاً
    try:
        await smart_notify(client_id, f"📩 **رسالة من {sender_name}:**\n\n{reply_text}\n\n*(يمكنك الرد من داخل التطبيق مباشرة)*")
    except Exception as e: logging.error(f"Error: {e}")
        
    await message.answer("✅ **تم إرسال رسالتك للعميل!**\nظهرت الرسالة في تطبيقه، وتم إرسال إشعار لهاتفه بنجاح 📱🔔.")
    await state.clear()

@router.callback_query(F.data.startswith("review_order_"))
async def review_client_order(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    order_id = int(callback.data.split("_")[2])
    
    from core_accounting import core_update_order_status
    result = await core_update_order_status(order_id, 'approved')
    
    if result["status"] == "error":
        return await callback.answer(result["message"], show_alert=True)
        
    client_id = result["client_id"]
    new_order_text = result["new_order_text"]
    
    guard_warning = ""
    numbers = [int(s) for s in re.findall(r'\b\d+\b', new_order_text)]
    if any(n > 100 for n in numbers):
        guard_warning += "⚠️ **تنبيه الحارس المالي:** العميل يطلب كمية كبيرة جداً (أكثر من 100 كرت). تأكد من الرقم قبل الاعتماد!\n"
        
    if database.pool:
        async with database.pool.acquire() as conn:
            user_data = await conn.fetchrow("SELECT debt, credit_limit FROM users WHERE user_id = $1", client_id)
            if user_data:
                current_debt = Decimal(user_data['debt'])
                credit_limit = Decimal(user_data['credit_limit']) if user_data['credit_limit'] else Decimal('50000.0')
                if current_debt >= credit_limit * Decimal('0.8'):
                    guard_warning += f"⚠️ **تنبيه الحارس المالي:** ديون العميل ({current_debt} ريال) قريبة جداً من سقف المديونية ({credit_limit} ريال). يُنصح بالتحصيل أولاً!\n"
    
    if guard_warning: await callback.message.answer(guard_warning)
        
    new_callback = callback.model_copy(update={"data": f"gclient_{client_id}"})
    await process_gclient(new_callback, state)
    
# ================= 1. الإدخال اليومي بالصورة (Daily OCR Entry) =================
@router.callback_query(F.data == "ai_daily_entry")
@router.callback_query(F.data.startswith("daily_entry_page_"))
async def start_daily_entry(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    page = 0
    if callback.data.startswith("daily_entry_page_"):
        page = int(callback.data.split("_")[3])

    ITEMS_PER_PAGE = 8
    clients_kb = []
    
    # زر المدير العام يظهر في الصفحة الأولى فقط
    if page == 0:
        clients_kb.append([InlineKeyboardButton(text="👑 حساب المدير العام (الشبكة)", callback_data="daily_client_0")])

    total_pages = 1
    total_clients = 0

    if database.pool:
        async with database.pool.acquire() as conn:
            total_clients = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role = 'client'")
            if total_clients > 0:
                total_pages, offset = get_pagination_math(total_clients, page, ITEMS_PER_PAGE)
                current_clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client' ORDER BY user_id LIMIT $1 OFFSET $2", ITEMS_PER_PAGE, offset)
                
                for c in current_clients:
                    clients_kb.append([InlineKeyboardButton(text=c['name'], callback_data=f"daily_client_{c['user_id']}")])

    nav_buttons = get_pagination_buttons(page, total_pages, "daily_entry_page")
    if nav_buttons: clients_kb.append(nav_buttons)

    clients_kb.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_operations_menu")])
    
    text = f"📸 **الإدخال اليومي السريع:**\nاختر الحساب - صفحة {page+1}/{total_pages}:"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=clients_kb))
    await state.set_state(AIDailyEntryFlow.waiting_for_client)

@router.callback_query(AIDailyEntryFlow.waiting_for_client, F.data.startswith("daily_client_"))
async def daily_ask_image(callback: types.CallbackQuery, state: FSMContext):
    client_id = int(callback.data.split("_")[2])
    client_name = "المدير العام (الشبكة)" if client_id == 0 else ""
    if client_id != 0 and database.pool:
        async with database.pool.acquire() as conn:
            client_name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
            
    await state.update_data(daily_client_id=client_id, daily_client_name=client_name)
    await callback.message.edit_text(f"📸 ممتاز! أرسل صورة صفحة الدفتر الخاصة بـ ({client_name}) لليوم:\n*(سيقوم البوت باستخراج العمليات الجديدة التي لم تُسجل بعد)*")
    await state.set_state(AIDailyEntryFlow.waiting_for_image)

@router.message(AIDailyEntryFlow.waiting_for_image, F.photo)
async def process_daily_image(message: types.Message, state: FSMContext, bot: Bot):
    wait_msg = await message.answer("👁️‍🗨️ جاري قراءة الدفتر واستخراج العمليات الجديدة... ⏳")
    data = await state.get_data()
    client_id, client_name = data['daily_client_id'], data['daily_client_name']
    
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    base64_image = base64.b64encode(file_bytes.read()).decode('utf-8')
    
    try:
        from ai_chat_panel import analyze_ledger_image
        extracted_txs = await analyze_ledger_image(base64_image, is_network_owner=(client_id == 0))
                    
        if database.pool:
            async with database.pool.acquire() as conn:
                db_txs = await conn.fetch("SELECT type, amount FROM transactions WHERE user_id = $1 AND date >= CURRENT_DATE - INTERVAL '3 days'", client_id)
                
        db_list = [{"type": tx['type'], "amount": Decimal(str(tx['amount']))} for tx in db_txs]
        new_txs = []
        
        for img_tx in extracted_txs:
            tx_type = img_tx.get('type', 'غير_معروف')
            try: tx_amount = Decimal(str(img_tx.get('amount', 0)))
            except: tx_amount = Decimal('0.0')
            tx_details = img_tx.get('details', '')
            
            if tx_amount == Decimal('0.0'): continue
            
            found = False
            for db_tx in db_list:
                if tx_type == db_tx['type'] and tx_amount == db_tx['amount']:
                    db_list.remove(db_tx)
                    found = True
                    break
            if not found: 
                new_txs.append({"type": tx_type, "amount": tx_amount, "details": tx_details})

        if not new_txs:
            await wait_msg.edit_text("✅ **لا توجد عمليات جديدة!**\nكل ما في الصورة مسجل مسبقاً في البوت.")
            return await state.clear()
            
        report = f"🆕 **العمليات الجديدة المكتشفة ({client_name}):**\n\n"
        for i, tx in enumerate(new_txs):
            report += f"▪️ {tx['type'].replace('_', ' ')}: {tx['amount']} ريال ({tx.get('details', '')})\n"
            
        report += "\n❓ **هل تريد تسجيل هذه العمليات في البوت الآن؟**"
        await state.update_data(new_txs_to_save=json.dumps(new_txs))
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ نعم، سجلها", callback_data="confirm_daily_entry")],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_action")]
        ])
        await wait_msg.edit_text(report, reply_markup=kb)
        await state.set_state(AIDailyEntryFlow.waiting_for_confirm)
        
    except Exception as e:
        await wait_msg.edit_text(f"❌ خطأ في قراءة البيانات: {e}\nتأكد أن الصورة واضحة.")
        await state.clear()

@router.callback_query(F.data == "confirm_daily_entry")
async def confirm_daily_entry(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    client_id = data['daily_client_id']
    new_txs = json.loads(data['new_txs_to_save'])
    
    await callback.message.edit_text("⏳ جاري التسجيل المحاسبي...")
    
    # 🌟 التوحيد: استدعاء المطبخ المركزي
    from core_accounting import core_process_ai_daily_entries
    result = await core_process_ai_daily_entries(client_id, new_txs)
    
    msg = f"✅ **تم تسجيل ({result['success_count']}) عمليات بنجاح!**"
    if result['error_msg']: msg += f"\n\n{result['error_msg']}"
    
    await callback.message.edit_text(msg)
    await state.clear()

# ================= 2. معالج المطابقة الشهرية (Monthly Audit Wizard) =================
@router.callback_query(F.data == "ai_monthly_audit")
async def start_monthly_audit(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("??️‍♂️ **معالج المطابقة الشهرية:**\nأدخل الفترة المراد مطابقتها (مثال: من 2024-05-01 إلى 2024-05-30):")
    await state.set_state(AIMonthlyAuditFlow.waiting_for_dates)

@router.message(AIMonthlyAuditFlow.waiting_for_dates)
async def audit_wizard_init(message: types.Message, state: FSMContext):
    from ai_chat_panel import get_smart_dates
    start_date, end_date = get_smart_dates(message.text)
    
    clients_to_audit = []
    if database.pool:
        async with database.pool.acquire() as conn:
            clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client'")
            for c in clients: clients_to_audit.append({"id": c['user_id'], "name": c['name']})
            
    if not clients_to_audit:
        await state.clear()
        return await message.answer("❌ لا يوجد عملاء مسجلين في النظام للمطابقة.")
            
    await state.update_data(audit_start=start_date, audit_end=end_date, audit_clients=json.dumps(clients_to_audit), current_index=0)
    
    await message.answer(f"🚀 **بدء المطابقة الشهرية التفاعلية**\nالفترة: من {start_date} إلى {end_date}\nعدد البقالات: {len(clients_to_audit)}\n\nسيقوم النظام بعرض حساب كل بقالة لتطابقه مع دفترك.", 
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="البدء بالحساب الأول ➡️", callback_data="audit_next_client")]]))

async def show_current_client_audit(message_or_callback, state: FSMContext):
    data = await state.get_data()
    clients = json.loads(data['audit_clients'])
    idx = data['current_index']
    
    if idx >= len(clients):
        await state.clear()
        msg = "🎉 **اكتملت المطابقة الشهرية لجميع البقالات بنجاح!** 🏆\nكل حساباتك الآن مطابقة لدفترك 100%."
        if isinstance(message_or_callback, types.CallbackQuery):
            return await message_or_callback.message.edit_text(msg)
        else:
            return await message_or_callback.answer(msg)
            
    current_client = clients[idx]
    start_date_str, end_date_str = data['audit_start'], data['audit_end']
    
    from datetime import datetime
    start_date_obj = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date_obj = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    
    db_paid = Decimal('0.0')
    db_taken = Decimal('0.0')
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # جلب التسديدات والمسحوبات للعميل في هذه الفترة (تجاهل الملغى)
            paid_val = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND type IN ('تسديد_من_عميل', 'مرتجع_من_عميل') AND is_reverted = FALSE AND date >= $2::date AND date <= $3::date + interval '1 day'", current_client['id'], start_date_obj, end_date_obj)
            taken_val = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND type IN ('تسليم_لعميل', 'مبيعات_آجلة') AND is_reverted = FALSE AND date >= $2::date AND date <= $3::date + interval '1 day'", current_client['id'], start_date_obj, end_date_obj)
            
            db_paid = Decimal(paid_val or 0)
            db_taken = Decimal(taken_val or 0)
            
    report = (
        f"🏪 **البقالة ({idx+1}/{len(clients)}): {current_client['name']}**\n"
        f"━━━━━━━━━━━━━\n"
        f"📥 **إجمالي ما سدده لك:** {int(db_paid)} ريال\n"
        f"📤 **إجمالي ما سحبه (كروت):** {int(db_taken)} ريال\n"
        f"━━━━━━━━━━━━━\n\n"
        f"❓ **هل هذه الأرقام مطابقة لدفترك؟**\n"
        f"▪️ إذا مطابقة: اكتب **(نعم)** أو اضغط الزر.\n"
        f"▪️ إذا ناقصة: اكتب التعديل (مثال: *نسيت اسجل تسديد 5000* أو *ضيف تسليم 10 كروت ابو 1000*)."
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ مطابق (التالي)", callback_data="audit_next_client")],
        [InlineKeyboardButton(text="❌ إنهاء المطابقة", callback_data="cancel_action")]
    ])
    
    await state.set_state(AIMonthlyAuditFlow.reviewing_client)
    
    if isinstance(message_or_callback, types.CallbackQuery):
        await message_or_callback.message.edit_text(report, reply_markup=kb)
    else:
        await message_or_callback.answer(report, reply_markup=kb)

@router.callback_query(F.data == "audit_next_client")
async def audit_next_client_btn(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    # إذا كنا في البداية لا نزيد العداد، وإلا نزيده
    if await state.get_state() == AIMonthlyAuditFlow.reviewing_client.state:
        await state.update_data(current_index=data['current_index'] + 1)
        
    await show_current_client_audit(callback, state)

@router.message(AIMonthlyAuditFlow.reviewing_client, F.from_user.id == ADMIN_ID)
async def audit_client_answer(message: types.Message, state: FSMContext):
    text = message.text or ""
    
    # إذا قال نعم، ننتقل للعميل التالي
    if any(w in text for w in ["نعم", "صح", "مضبوط", "تمام", "مطابق", "كمل"]):
        data = await state.get_data()
        await state.update_data(current_index=data['current_index'] + 1)
        return await show_current_client_audit(message, state)
        
    # إذا كتب تعديلاً، نستخدم الذكاء الاصطناعي لفهمه وتسجيله
    wait_msg = await message.answer("⏳ جاري تحليل التعديل وتسجيله...")
    
    prompt = """
    أنت محاسب مالي. الوكيل يراجع حساب بقالة ووجد أنه نسي تسجيل عملية.
    استخرج العملية التي يريد إضافتها من نصه، وأرجعها بصيغة JSON فقط بدون أي نص إضافي.
    الصيغة المطلوبة:
    {"type": "نوع_العملية", "amount": 1000, "details": "التفاصيل", "card_type": "اسم الكرت ان وجد", "qty": 0}
    
    الأنواع المسموحة فقط هي:
    - تسديد_من_عميل (إذا قال سدد، جاب لي فلوس، استلمت منه)
    - تسليم_لعميل (إذا قال اخذ كروت، عطيته بضاعة)
    - مرتجع_من_عميل (إذا قال رجع لي كروت)
    
    إذا لم تفهم العملية، أرجع: {"error": "غير واضح"}
    """
    
    try:
        from ai_chat_panel import generate_groq_response
        response = await generate_groq_response(prompt, message.from_user.id, text)
        
        import re, json
        clean_json = re.sub(r'```(?:json)?', '', response).strip()
        parsed_data = json.loads(clean_json)
        
        if "error" in parsed_data:
            return await wait_msg.edit_text("🤔 لم أفهم التعديل بوضوح. يرجى كتابته هكذا: (ضيف تسديد 5000) أو (نعم) للمتابعة.")
            
        data = await state.get_data()
        clients = json.loads(data['audit_clients'])
        current_client = clients[data['current_index']]
        client_id = current_client['id']
        
        from core_accounting import FinancialEngine
        import database
        from decimal import Decimal
        
        engine = FinancialEngine(database.pool)
        t_type = parsed_data['type']
        amount = Decimal(str(parsed_data.get('amount', 0)))
        details = parsed_data.get('details', 'تسوية مطابقة شهرية')
        
        if t_type == 'تسديد_من_عميل':
            await engine.collect_debt(client_id, amount)
            success_msg = f"✅ تم إضافة تسديد بقيمة {int(amount)} ريال."
        elif t_type == 'تسليم_لعميل':
            card_type = parsed_data.get('card_type', 'غير محدد')
            qty = int(parsed_data.get('qty', 1))
            # إذا لم يحدد الكرت، نسجلها كدين مباشر لتسهيل المطابقة
            if card_type == 'غير محدد' or qty == 0:
                await engine.transfer_assets('agent', 0, 'client', client_id, 'cash', amount)
                success_msg = f"✅ تم إضافة دين (مسحوبات) بقيمة {int(amount)} ريال."
            else:
                await engine.give_cards(client_id, card_type, qty)
                success_msg = f"✅ تم تسليم {qty} كرت {card_type}."
        elif t_type == 'مرتجع_من_عميل':
            card_type = parsed_data.get('card_type', 'غير محدد')
            qty = int(parsed_data.get('qty', 1))
            if card_type != 'غير محدد' and qty > 0:
                from core_accounting import core_process_return
                await core_process_return('from_client', qty, amount, client_id, card_type, source="(مطابقة شهرية)")
                success_msg = f"✅ تم تسجيل مرتجع {qty} كرت {card_type}."
            else:
                return await wait_msg.edit_text("⚠️ لتسجيل المرتجع يجب تحديد نوع الكرت والكمية. يرجى إضافتها يدوياً من قسم المرتجعات.")
        else:
            return await wait_msg.edit_text("⚠️ نوع العملية غير مدعوم في المطابقة السريعة.")
            
        await wait_msg.delete()
        await message.answer(f"{success_msg}\nجاري تحديث الحساب...")
        
        # إعادة عرض حساب نفس العميل بعد التحديث ليتأكد الوكيل
        return await show_current_client_audit(message, state)

    except Exception as e:
        await wait_msg.edit_text(f"❌ حدث خطأ في الفهم: {e}\nيرجى إضافتها يدوياً من الأزرار ثم كتابة (نعم) للمتابعة.")

@router.callback_query(F.data == "audit_auto_fix")
async def audit_auto_fix_callback(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    client_id = data.get('error_client_id')
    missing_txs_str = data.get('missing_txs', '[]')
    
    import json
    missing_txs = json.loads(missing_txs_str)
    
    await callback.message.edit_text("⏳ جاري الإصلاح المحاسبي...")
    
    try:
        from core_accounting import FinancialEngine
        import database
        engine = FinancialEngine(database.pool)
        
        # 🌟 استخدام الدالة المركزية المخصصة للإصلاح الآلي في المحرك المالي
        await engine.audit_auto_fix(client_id, missing_txs)
        
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="التالي ➡️", callback_data="audit_next_client")]])
        await callback.message.edit_text("✅ **تم إصلاح الحساب بنجاح!**\n*(ملاحظة: إذا كان هناك عمليات تسليم كروت ناقصة، يجب إدخالها يدوياً لخصمها من المخزون)*", reply_markup=kb)
        
    except Exception as e:
        await callback.message.edit_text(f"❌ حدث خطأ أثناء الإصلاح: {e}")

# ================= 3. المستشار المالي الذكي للمدير =================
@router.callback_query(F.data == "owner_ai_advisor")
async def owner_ai_advisor_action(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id != int(NETWORK_OWNER_ID): return

    await callback.message.edit_text("🧠 **المستشار المالي الذكي:**\n⏳ جاري جمع بيانات السوق وتحليلها، لحظات يا شيخ رهيب...")
    
    try:
        await bot.send_chat_action(chat_id=callback.message.chat.id, action="typing")
    except Exception as e: logging.error(f"Error: {e}")
    
    if not database.pool:

        return await callback.message.edit_text("❌ خطأ في الاتصال بقاعدة البيانات.")
        
    from core_accounting import get_financial_summary
    fin_stats = await get_financial_summary()
    
    # 🌟 1. الكاش الصافي للمدير (بدون أرباح الوكيل)
    available_cash = max(Decimal('0.0'), fin_stats.get("cash", Decimal('0.0')) - fin_stats.get("realized", Decimal('0.0')))
    
    # 🌟 2. ديون السوق الصافية للمدير (بدون الأرباح المعلقة)
    total_debt_cost = fin_stats.get("debt_cost", Decimal('0.0'))
    
    async with database.pool.acquire() as conn:
        # 🌟 3. أكبر المديونيات الصافية (رأس المال فقط)
        top_debtors = await conn.fetch("SELECT name, (debt - pending_profit) AS net_debt FROM users WHERE role = 'client' ORDER BY (debt - pending_profit) DESC LIMIT 3")
        debtors_str = ", ".join([f"{d['name']} ({int(d['net_debt'])} ريال)" for d in top_debtors])
        
        # 🌟 سد ثغرة الهلوسة: تجاهل العمليات الملغاة لكي تكون نصائح الذكاء الاصطناعي دقيقة
        sales_30d = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسليم_لعميل', 'بيع_مباشر', 'مبيعات_آجلة') AND is_reverted = FALSE AND date >= CURRENT_DATE - INTERVAL '30 days'")
        
    prompt = f"""
    أنت (system) المستشار المالي الخاص لتطبيق  الشهاب pro. تتحدث الآن مباشرة مع المؤسس والمدير العام (الشيخ رهيب).
    إليك بيانات السوق الحالية:
    - إجمالي ديون السوق: {int(total_debt_cost)} ريال.
    - الكاش المتوفر مع الوكيل ( وليد): {int(available_cash)} ريال.
    - مبيعات آخر 30 يوم: {int(sales_30d or 0)} ريال.
    - أكبر 3 عملاء عليهم ديون: {debtors_str}.
    
    المطلوب:
    اكتب تقريراً استشارياً قصيراً جداً (3 نقاط فقط) بلهجة يمنية محترمة، مباشرة، وودية.
    تحدث معه مباشرة (مثال: أنصحك يا شيخ رهيب، شبكتنا، شغلنا، وضعنا) ولا تستخدم أبداً كلمات رسمية جافة مثل (الشركة، أوصي المدير العام، يتعين على).
    
    1. تقييم سريع للسيولة (الكاش) مقابل الديون.
    2. تحذير أو نصيحة بخصوص أكبر المديونيات (اذكر الأسماء).
    3. توصية إدارية للخطوة القادمة.
    
    🚨 تحذير هام جداً: اكتب باللغة العربية فقط. يمنع منعاً باتاً استخدام أي حروف أو كلمات صينية أو إنجليزية.
    لا تستخدم مقدمات طويلة، ادخل في التحليل مباشرة.
    """
    
    try:
        from ai_chat_panel import generate_groq_response
        
        # استدعاء الذكاء الاصطناعي
        ai_advice = await generate_groq_response(prompt, callback.from_user.id)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_monitoring_menu")]])
        await callback.message.edit_text(f"🧠 **تحليل المستشار المالي الذكي:**\n\n{ai_advice}", reply_markup=kb)
    except Exception as e:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_monitoring_menu")]])
        await callback.message.edit_text(f"❌ عذراً، المستشار الذكي غير متاح حالياً. الخطأ: {e}", reply_markup=kb)

        # ================= 4. تصدير البيانات المتقدم (للوكيل فقط) =================
@router.callback_query(F.data == "export_data_menu")
async def export_data_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: 
        return await callback.answer("❌ عذراً، هذه الميزة مخصصة للوكيل فقط.", show_alert=True)
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 تصدير ديون السوق (Excel)", callback_data="export_market_debt")],
        [InlineKeyboardButton(text="📦 تصدير المخزون الحالي (Excel)", callback_data="export_current_inventory")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_reports_menu")]
    ])
    await callback.message.edit_text("📥 **تصدير البيانات المتقدم:**\nاختر التقرير الذي تريد سحبه الآن:", reply_markup=kb)

@router.callback_query(F.data == "export_market_debt")
async def export_market_debt(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.edit_text("⏳ جاري تجهيز ملف الإكسل لديون السوق...")
    
    if not database.pool: return await callback.message.edit_text("❌ خطأ في الاتصال بقاعدة البيانات.")
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ديون السوق"
    ws.sheet_view.rightToLeft = True
    
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    headers = ["رقم العميل", "اسم البقالة", "الدين الحالي (ريال)", "سقف المديونية", "تاريخ الانضمام"]
    ws.append(headers)
    
    for col in range(1, 6):
        ws.cell(row=1, column=col).font = header_font
        ws.cell(row=1, column=col).fill = header_fill
        ws.cell(row=1, column=col).alignment = Alignment(horizontal="center")

    async with database.pool.acquire() as conn:
        clients = await conn.fetch("SELECT user_id, name, debt, credit_limit, joined_at FROM users WHERE role = 'client' ORDER BY debt DESC")
        total_debt = Decimal('0.0')
        for c in clients:
            limit = c['credit_limit'] if c['credit_limit'] else 50000.0
            ws.append([str(c['user_id']), c['name'], abs(Decimal(c['debt'])), limit, str(c['joined_at'])[:10]])
            total_debt += Decimal(c['debt'])
            
            # تنسيق الخلايا الرقمية لمنع ظهور الصيغة العلمية (مثل 2E+4)
            for cell in ws[ws.max_row]:
                if isinstance(cell.value, (int, float, Decimal)):
                    cell.number_format = '#,##0'

        ws.append(["", "الإجمالي الكلي:", abs(total_debt), "", ""])
        
        # تنسيق صف الإجمالي أيضاً
        for cell in ws[ws.max_row]:
            if isinstance(cell.value, (int, float, Decimal)):
                cell.number_format = '#,##0'
        
    for col in ['A', 'B', 'C', 'D', 'E']: ws.column_dimensions[col].width = 20
        
    stream = BytesIO()
    # استخدام asyncio.to_thread لمنع تجميد البوت
    await asyncio.to_thread(wb.save, stream)
    stream.seek(0)
    
    file_name = f"Market_Debt_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    document = BufferedInputFile(stream.read(), filename=file_name)
    await callback.message.delete()
    await callback.message.answer_document(document=document, caption="📊 **ملف ديون السوق جاهز!**")

@router.callback_query(F.data == "export_current_inventory")
async def export_current_inventory(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.edit_text("⏳ جاري تجهيز ملف الإكسل للمخزون...")
    
    if not database.pool: return await callback.message.edit_text("❌ خطأ في الاتصال بقاعدة البيانات.")
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "المخزون الحالي"
    ws.sheet_view.rightToLeft = True
    
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    ws.append(["نوع الكرت", "الكمية المتوفرة", "سعر التكلفة", "إجمالي القيمة (ريال)"]) # 👈 التعديل هنا
    
    for col in range(1, 5):
        ws.cell(row=1, column=col).font = header_font
        ws.cell(row=1, column=col).fill = header_fill
        ws.cell(row=1, column=col).alignment = Alignment(horizontal="center")

    async with database.pool.acquire() as conn:
        # 👈 التعديل: جلب cost_price بدلاً من price
        items = await conn.fetch("SELECT card_type, quantity, cost_price FROM inventory")
        total_value = Decimal('0.0')
        for item in items:
            val = item['quantity'] * Decimal(item['cost_price'])
            ws.append([item['card_type'], item['quantity'], abs(Decimal(item['cost_price'])), abs(val)])
            total_value += val
            
            for cell in ws[ws.max_row]:
                if isinstance(cell.value, (int, float, Decimal)):
                    cell.number_format = '#,##0'

        ws.append(["", "", "الإجمالي الكلي:", abs(total_value)])
        
        for cell in ws[ws.max_row]:
            if isinstance(cell.value, (int, float, Decimal)):
                cell.number_format = '#,##0'
        
    for col in ['A', 'B', 'C', 'D']: ws.column_dimensions[col].width = 20
        
    stream = BytesIO()
    wb.save(stream)
    stream.seek(0)
    
    file_name = f"Inventory_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    document = BufferedInputFile(stream.read(), filename=file_name)
    await callback.message.delete()
    await callback.message.answer_document(document=document, caption="?? **ملف جرد المخزون جاهز!**")

@router.callback_query(F.data == "admin_view_pending_orders")
async def view_pending_orders(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    
    await callback.answer("⏳ جاري جلب الطلبات المعلقة...")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # جلب الطلبات المعلقة مع اسم العميل
            orders = await conn.fetch("""
                SELECT p.id, p.user_id, p.new_order_text, u.name 
                FROM pending_orders p
                JOIN users u ON p.user_id = u.user_id
                WHERE p.status = 'pending'
            """)
            
            if not orders:
                return await callback.message.edit_text(
                    "✅ لا توجد أي طلبات معلقة حالياً.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_operations_menu")]])
                )
                
            await callback.message.delete()
            
            # إرسال كل طلب كرسالة مستقلة مع أزرار الاعتماد والرفض
            for order in orders:
                msg_text = f"📦 **طلب معلق من العميل:** {order['name']}\n\n📝 **التفاصيل:**\n{order['new_order_text']}"
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ مراجعة واعتماد", callback_data=f"review_order_{order['id']}")],
                    [InlineKeyboardButton(text="❌ رفض الطلب", callback_data=f"reject_order_{order['id']}")]
                ])
                await callback.message.answer(msg_text, reply_markup=kb)
                
# ================= 5. ملف العميل الشامل (للوكيل فقط) =================
@router.callback_query(F.data == "client_profile_menu")
@router.callback_query(F.data.startswith("cprofile_page_"))
async def client_profile_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: 
        return await callback.answer("❌ عذراً، هذه الميزة مخصصة للوكيل فقط.", show_alert=True)
        
    page = 0
    if callback.data.startswith("cprofile_page_"):
        page = int(callback.data.split("_")[2])

    ITEMS_PER_PAGE = 8
    clients_kb = []
    total_pages = 1
    total_clients = 0
    
    if database.pool:
        async with database.pool.acquire() as conn:
            total_clients = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role = 'client'")
            if total_clients > 0:
                total_pages, offset = get_pagination_math(total_clients, page, ITEMS_PER_PAGE)
                current_clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client' ORDER BY user_id LIMIT $1 OFFSET $2", ITEMS_PER_PAGE, offset)
                
                for c in current_clients:
                    clients_kb.append([InlineKeyboardButton(text=c['name'], callback_data=f"view_cprofile_{c['user_id']}")])

    # استخدام الدالة المختصرة
    nav_buttons = get_pagination_buttons(page, total_pages, "cprofile_page")
    if nav_buttons: clients_kb.append(nav_buttons)
                
    clients_kb.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_reports_menu")])
    await callback.message.edit_text("🕵️‍♂️ **ملف العميل الشامل:**\nاختر العميل لعرض ملفه:", reply_markup=InlineKeyboardMarkup(inline_keyboard=clients_kb))

@router.callback_query(F.data.startswith("view_cprofile_"))
async def view_client_profile(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    client_id = int(callback.data.split("_")[2])
    await callback.message.edit_text("⏳ جاري استخراج ملف العميل الشامل...")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            user = await conn.fetchrow("SELECT name, debt, credit_limit, joined_at FROM users WHERE user_id = $1", client_id)
            if not user: return await callback.message.edit_text("❌ العميل غير موجود.")
            
            limit = user['credit_limit'] if user['credit_limit'] else 50000.0
            
            # 🌟 إصلاح: إضافة is_reverted = FALSE لتجاهل العمليات الملغاة في الإحصائيات
            last_payment = await conn.fetchrow("SELECT amount, date FROM transactions WHERE user_id = $1 AND type = 'تسديد_من_عميل' AND is_reverted = FALSE ORDER BY date DESC LIMIT 1", client_id)
            last_pay_text = f"{last_payment['amount']} ريال (بتاريخ {str(last_payment['date'])[:10]})" if last_payment else "لم يسدد أي دفعة بعد"
            
                        # 🌟 سد ثغرة الإحصائيات الخاطئة: حساب المسحوبات والتسديدات بدقة (تجاهل الملغى، إضافة المرتجعات، وفصل التسديدات)
            total_taken = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND type IN ('تسليم_لعميل', 'مبيعات_آجلة') AND is_reverted = FALSE AND wallet_type = 'manager'", client_id)
            total_paid = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND type IN ('تسديد_من_عميل', 'مرتجع_من_عميل') AND is_reverted = FALSE AND wallet_type = 'manager'", client_id)
            
            favorite_card = await conn.fetchrow("""
                SELECT details, COUNT(*) as count FROM transactions 
                WHERE user_id = $1 AND type = 'تسليم_لعميل' AND is_reverted = FALSE
                GROUP BY details ORDER BY count DESC LIMIT 1
            """, client_id)
            fav_card_text = favorite_card['details'] if favorite_card else "غير محدد"

            report = f"🕵️‍♂️ **ملف العميل الشامل (360°):**\n"
            report += f"👤 **الاسم:** {user['name']}\n"
            report += f"📅 **تاريخ الانضمام:** {str(user['joined_at'])[:10]}\n"
            report += "━━━━━━━━━━━━━\n"
            report += f"?? **الدين الحالي:** {int(user['debt'])} ريال\n"
            report += f"🛑 **سقف المديونية:** {int(limit)} ريال\n"
            report += f"💵 **آخر دفعة سددها:** {last_pay_text}\n"
            report += "━━━━━━━━━━━━━\n"
            report += f"📈 **إجمالي مسحوباته (تاريخياً):** {total_taken} ريال\n"
            report += f"💰 **إجمالي تسديداته (تاريخياً):** {total_paid} ريال\n"
            report += f"⭐ **الطلب المفضل لديه:** {fav_card_text}\n"
            
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="?? رجوع للقائمة", callback_data="client_profile_menu")]])
            await callback.message.edit_text(report, reply_markup=kb)
            
            # --- دوال القوائم المدمجة الجديدة ---
@router.callback_query(F.data == "admin_direct_sales_menu")
async def admin_direct_sales_menu(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 بيع تجزئة (سعر الطياري)", callback_data="admin_direct_sale")],
        [InlineKeyboardButton(text="🛍️ بيع جملة (سعر البقالات)", callback_data="admin_wholesale_cash")],
        [InlineKeyboardButton(text="📓 بيع آجل (يسجل بدفترك الشخصي)", callback_data="admin_personal_credit")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_operations_menu")]
    ])
    await callback.message.edit_text("🛒 **مبيعات الكاش المباشرة:**\nاختر نوع البيع:", reply_markup=kb)

@router.callback_query(F.data == "export_and_backup_menu")
async def export_and_backup_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: 
        return await callback.answer("❌ عذراً، هذه الميزة مخصصة للوكيل فقط.", show_alert=True)
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 أخذ نسخة احتياطية شاملة", callback_data="admin_backup")],
        [InlineKeyboardButton(text="📊 تصدير ديون السوق فقط", callback_data="export_market_debt")],
        [InlineKeyboardButton(text="📦 تصدير المخزون الحالي فقط", callback_data="export_current_inventory")],
        [InlineKeyboardButton(text="🕋 استخراج الصندوق الأسود", callback_data="export_blackbox")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_reports_menu")]
    ])
    await callback.message.edit_text("📥 **مركز التصدير والنسخ الاحتياطي:**\nاختر العملية المطلوبة:", reply_markup=kb)

# ================= قائمة الأوامر السريعة التفاعلية (للوكيل) =================

@router.callback_query(F.data == "agent_quick_actions")
async def show_quick_actions_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("مخصصة للوكيل فقط", show_alert=True)
    await callback.message.edit_text("🛠️ **دليل الأوامر السريعة:**\nاختر القسم الذي تريد العمل عليه:", reply_markup=await get_quick_actions_main_keyboard())

@router.callback_query(F.data.startswith("qa_cat_"))
async def show_qa_category(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cat = callback.data.split("_")[2]
    
    if cat == "clients":
        await callback.message.edit_text("👥 **أوامر إدارة العملاء:**", reply_markup=await get_qa_clients_keyboard())
    elif cat == "finance":
        await callback.message.edit_text("💰 **أوامر المالية والمخزون:**", reply_markup=await get_qa_finance_keyboard())
    elif cat == "whatsapp":
        await callback.message.edit_text("💬 **أوامر إدارة الواتساب:**", reply_markup=await get_qa_whatsapp_keyboard())
    elif cat == "system":
        await callback.message.edit_text("⚙️ **أوامر النظام وقاعدة البيانات:**", reply_markup=await get_qa_system_keyboard())

# --- 1. تنفيذ أوامر العملاء ---
@router.callback_query(F.data == "qa_cmd_rename")
async def qa_rename(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("✏️ أرسل رقم العميل والاسم الجديد (مثال: `123456 محمد`)")
    await state.set_state(AgentQuickActionsFlow.waiting_for_rename_data)

@router.message(AgentQuickActionsFlow.waiting_for_rename_data)
async def qa_rename_exec(m: types.Message, state: FSMContext):
    await rename_client(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_phone")
async def qa_phone(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("📱 أرسل رقم العميل ورقم الواتساب (مثال: `123456 967777000000`)")
    await state.set_state(AgentQuickActionsFlow.waiting_for_phone_data)

@router.message(AgentQuickActionsFlow.waiting_for_phone_data)
async def qa_phone_exec(m: types.Message, state: FSMContext):
    await set_client_phone(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_region")
async def qa_region(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("📍 أرسل رقم العميل واسم المنطقة (مثال: `123456 صنعاء`)")
    await state.set_state(AgentQuickActionsFlow.waiting_for_region_data)

@router.message(AgentQuickActionsFlow.waiting_for_region_data)
async def qa_region_exec(m: types.Message, state: FSMContext):
    await set_client_region(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_close_account")
async def qa_close_acc(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("🚫 أرسل رقم العميل لتصفية حسابه بالكامل:")
    await state.set_state(AgentQuickActionsFlow.waiting_for_close_account_id)

@router.message(AgentQuickActionsFlow.waiting_for_close_account_id)
async def qa_close_acc_exec(m: types.Message, state: FSMContext):
    await close_client_account(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_demote")
async def qa_demote(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("⬇️ أرسل رقم العميل لتحويله إلى زائر عادي:")
    await state.set_state(AgentQuickActionsFlow.waiting_for_demote_id)

@router.message(AgentQuickActionsFlow.waiting_for_demote_id)
async def qa_demote_exec(m: types.Message, state: FSMContext):
    await demote_client(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_link_account")
async def qa_link(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("🔗 أرسل رقم الأوفلاين ورقم التليجرام الجديد (مثال: `9990001 123456`)")
    await state.set_state(AgentQuickActionsFlow.waiting_for_link_account_data)

@router.message(AgentQuickActionsFlow.waiting_for_link_account_data)
async def qa_link_exec(m: types.Message, state: FSMContext):
    await link_offline_to_real(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data.in_(["qa_cmd_ecard_on", "qa_cmd_ecard_off", "qa_cmd_wa_on", "qa_cmd_wa_off"]))
async def qa_toggles(c: types.CallbackQuery, state: FSMContext):
    cmd = c.data.replace("qa_cmd_", "")
    await c.message.edit_text(f"أرسل رقم العميل لتنفيذ ({cmd}):")
    if cmd == "ecard_on": await state.set_state(AgentQuickActionsFlow.waiting_for_ecard_on_id)
    elif cmd == "ecard_off": await state.set_state(AgentQuickActionsFlow.waiting_for_ecard_off_id)
    elif cmd == "wa_on": await state.set_state(AgentQuickActionsFlow.waiting_for_wa_on_id)
    elif cmd == "wa_off": await state.set_state(AgentQuickActionsFlow.waiting_for_wa_off_id)

@router.message(AgentQuickActionsFlow.waiting_for_ecard_on_id)
async def qa_ecard_on_exec(m: types.Message, state: FSMContext):
    await enable_ecards(m, FakeCommand(m.text)); await state.clear()

@router.message(AgentQuickActionsFlow.waiting_for_ecard_off_id)
async def qa_ecard_off_exec(m: types.Message, state: FSMContext):
    await disable_ecards(m, FakeCommand(m.text)); await state.clear()

@router.message(AgentQuickActionsFlow.waiting_for_wa_on_id)
async def qa_wa_on_exec(m: types.Message, state: FSMContext):
    await enable_wa_status(m, FakeCommand(m.text)); await state.clear()

@router.message(AgentQuickActionsFlow.waiting_for_wa_off_id)
async def qa_wa_off_exec(m: types.Message, state: FSMContext):
    await disable_wa_status(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_rehab")
async def qa_rehab(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("🛡️ أرسل رقم البقالة لإعادة تأهيلها (طرد المخترق وتوليد رمز جديد):")
    await state.set_state(AgentQuickActionsFlow.waiting_for_rehab_id)

@router.message(AgentQuickActionsFlow.waiting_for_rehab_id)
async def qa_rehab_exec(m: types.Message, state: FSMContext):
    await rehab_client_command(m, FakeCommand(m.text)); await state.clear()
    
# --- 2. تنفيذ أوامر المالية ---
@router.callback_query(F.data == "qa_cmd_set_debt")
async def qa_set_debt(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("💵 أرسل رقم العميل والمبلغ (مثال: `123456 5000`)")
    await state.set_state(AgentQuickActionsFlow.waiting_for_debt_data)

@router.message(AgentQuickActionsFlow.waiting_for_debt_data)
async def qa_set_debt_exec(m: types.Message, state: FSMContext):
    await set_previous_debt(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_set_inv")
async def qa_set_inv(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("📦 أرسل رقم العميل، فئة الكرت، والكمية (مثال: `123456 أبو_100 5`)")
    await state.set_state(AgentQuickActionsFlow.waiting_for_inv_data)

@router.message(AgentQuickActionsFlow.waiting_for_inv_data)
async def qa_set_inv_exec(m: types.Message, state: FSMContext):
    await set_client_inventory(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_set_gm_debt")
async def qa_set_gm_debt(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("👑 أرسل مبلغ الدين السابق للمدير العام:")
    await state.set_state(AgentQuickActionsFlow.waiting_for_gm_debt_data)

@router.message(AgentQuickActionsFlow.waiting_for_gm_debt_data)
async def qa_set_gm_debt_exec(m: types.Message, state: FSMContext):
    await set_gm_previous_debt(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_discount_gm")
async def qa_discount_gm(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("🎁 أرسل مبلغ الخصم/المسامحة من المدير العام:")
    await state.set_state(AgentQuickActionsFlow.waiting_for_discount_gm_data)

@router.message(AgentQuickActionsFlow.waiting_for_discount_gm_data)
async def qa_discount_gm_exec(m: types.Message, state: FSMContext):
    await discount_gm_debt(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_add_t_cash")
async def qa_add_t_cash(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("💸 أرسل المبلغ المراد ضخه في صندوق التسديدات:")
    await state.set_state(AgentQuickActionsFlow.waiting_for_add_t_cash_data)

@router.message(AgentQuickActionsFlow.waiting_for_add_t_cash_data)
async def qa_add_t_cash_exec(m: types.Message, state: FSMContext):
    await add_telecom_capital(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_edit_date")
async def qa_edit_date(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("📅 أرسل رقم العملية والتاريخ الجديد (مثال: `150 2026-05-20`)")
    await state.set_state(AgentQuickActionsFlow.waiting_for_edit_date_data)

@router.message(AgentQuickActionsFlow.waiting_for_edit_date_data)
async def qa_edit_date_exec(m: types.Message, state: FSMContext):
    await edit_transaction_date(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_move_today")
async def qa_move_today(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("🔄 أرسل التاريخ القديم لنقل عمليات اليوم إليه (مثال: `2026-05-15`)")
    await state.set_state(AgentQuickActionsFlow.waiting_for_move_today_data)

@router.message(AgentQuickActionsFlow.waiting_for_move_today_data)
async def qa_move_today_exec(m: types.Message, state: FSMContext):
    await move_today_transactions(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_undo")
async def qa_undo(c: types.CallbackQuery):
    # 🌟 التعديل السحري هنا
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await show_undo_menu(msg); await c.message.delete()

# --- 3. تنفيذ أوامر الواتساب ---
@router.callback_query(F.data == "qa_cmd_wa_list")
async def qa_wa_list(c: types.CallbackQuery):
    # 🌟 التعديل السحري هنا
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await list_whatsapp_clients(msg); await c.answer()

@router.callback_query(F.data == "qa_cmd_test_wa")
async def qa_test_wa(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("?? أرسل رقم الهاتف لاختبار الواتساب (مثال: `967777000000`)")
    await state.set_state(AgentQuickActionsFlow.waiting_for_test_wa_phone)

@router.message(AgentQuickActionsFlow.waiting_for_test_wa_phone)
async def qa_test_wa_exec(m: types.Message, state: FSMContext):
    await test_wa_connection(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_set_wa_webhook")
async def qa_set_wa_webhook(c: types.CallbackQuery):
    # 🌟 التعديل السحري هنا
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await set_wa_webhook_command(msg); await c.answer()

@router.callback_query(F.data == "qa_cmd_test_my_webhook")
async def qa_test_my_webhook(c: types.CallbackQuery):
    # 🌟 التعديل السحري هنا
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await test_my_webhook(msg); await c.answer()

# --- 4. تنفيذ أوامر النظام ---
@router.callback_query(F.data == "qa_cmd_lockdown")
async def qa_lockdown(c: types.CallbackQuery):
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await activate_lockdown(msg); await c.answer()

@router.callback_query(F.data == "qa_cmd_unlock")
async def qa_unlock(c: types.CallbackQuery):
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await deactivate_lockdown(msg); await c.answer()

@router.callback_query(F.data == "qa_cmd_ban_ip")
async def qa_ban_ip(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("🚫 أرسل الآيبي (IP) المراد حظره نهائياً (مثال: `192.168.1.1`):")
    await state.set_state(AgentQuickActionsFlow.waiting_for_ban_ip)

@router.message(AgentQuickActionsFlow.waiting_for_ban_ip)
async def qa_ban_ip_exec(m: types.Message, state: FSMContext):
    await ban_ip_command(m, FakeCommand(m.text)); await state.clear()
    
@router.callback_query(F.data == "qa_cmd_unban_all")
async def qa_unban_all(c: types.CallbackQuery):
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await unban_all_ips(msg); await c.answer()

@router.callback_query(F.data == "qa_cmd_factory_reset")
async def qa_factory_reset(c: types.CallbackQuery, state: FSMContext):
    # 🌟 التعديل السحري هنا
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await request_hard_reset(msg, state); await c.message.delete()

@router.callback_query(F.data == "qa_cmd_restore_backup")
async def qa_restore_backup(c: types.CallbackQuery, state: FSMContext):
    # 🌟 التعديل السحري هنا
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await request_restore_backup(msg, state); await c.message.delete()

@router.callback_query(F.data == "qa_cmd_setup_db")
async def qa_setup_db(c: types.CallbackQuery):
    # 🌟 التعديل السحري هنا
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await setup_database_tables(msg); await c.answer()

@router.callback_query(F.data == "qa_cmd_update_db")
async def qa_update_db(c: types.CallbackQuery):
    # 🌟 التعديل السحري هنا
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await update_database_schema(msg); await c.answer()

@router.callback_query(F.data == "qa_cmd_test_system")
async def qa_test_system(c: types.CallbackQuery):
    # 🌟 التعديل السحري هنا
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await test_system_health(msg); await c.answer()

@router.callback_query(F.data == "qa_cmd_fix_sys")
async def qa_fix_sys(c: types.CallbackQuery):
    # 🌟 التعديل السحري هنا
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await fix_system_user(msg); await c.answer()

@router.callback_query(F.data == "qa_cmd_fix_timezone")
async def qa_fix_timezone(c: types.CallbackQuery):
    # 🌟 التعديل السحري هنا
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await fix_db_timezone(msg); await c.answer()

@router.callback_query(F.data == "qa_cmd_clear_alerts")
async def qa_clear_alerts(c: types.CallbackQuery):
    # 🌟 التعديل السحري هنا
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await clear_all_alerts(msg); await c.answer("تم التنظيف!", show_alert=True)

@router.callback_query(F.data == "qa_cmd_delete_report")
async def qa_delete_report(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("🗑️ أرسل شهر التقرير المراد حذفه (مثال: `2026-05`)")
    await state.set_state(AgentQuickActionsFlow.waiting_for_delete_report_month)

@router.message(AgentQuickActionsFlow.waiting_for_delete_report_month)
async def qa_delete_report_exec(m: types.Message, state: FSMContext):
    await delete_archived_report(m, FakeCommand(m.text)); await state.clear()

@router.callback_query(F.data == "qa_cmd_fix_all_joins")
async def qa_fix_all_joins(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID: return
    
    await c.message.edit_text("⏳ جاري مزامنة وإصلاح تواريخ انضمام العملاء...")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # 🌟 سد ثغرة التاريخ المفقود: دمج تواريخ الكروت والتسديدات معاً لمعرفة أقدم عملية حقيقية
            await conn.execute("""
                UPDATE users u
                SET joined_at = t.min_date
                FROM (
                    SELECT user_id, MIN(date) as min_date 
                    FROM (
                        SELECT user_id, date FROM transactions
                        UNION ALL
                        SELECT client_id AS user_id, created_at AS date FROM telecom_transactions
                    ) all_txs
                    GROUP BY user_id
                ) t
                WHERE u.user_id = t.user_id AND t.min_date < u.joined_at
            """)
            
    # إرجاع رسالة النجاح مع نفس لوحة الأزرار
    await c.message.edit_text("✅ **تم تنظيف وإصلاح تواريخ انضمام جميع العملاء في النظام!**\nالآن كل عميل تاريخ انضمامه يطابق أقدم عملية له بالضبط.", 
        reply_markup=await get_qa_system_keyboard()
    )
    
@router.message(Command("fix_all_joins"))
async def fix_all_joins(message: types.Message):
    """أمر لإصلاح تواريخ انضمام جميع العملاء دفعة واحدة (يستخدم مرة واحدة لتنظيف الأخطاء السابقة)"""
    if message.from_user.id != ADMIN_ID: return
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # 🌟 سد ثغرة التاريخ المفقود: دمج تواريخ الكروت والتسديدات معاً لمعرفة أقدم عملية حقيقية
            await conn.execute("""
                UPDATE users u
                SET joined_at = t.min_date
                FROM (
                    SELECT user_id, MIN(date) as min_date 
                    FROM (
                        SELECT user_id, date FROM transactions
                        UNION ALL
                        SELECT client_id AS user_id, created_at AS date FROM telecom_transactions
                    ) all_txs
                    GROUP BY user_id
                ) t
                WHERE u.user_id = t.user_id AND t.min_date < u.joined_at
            """)
    await message.answer("✅ **تم تنظيف وإصلاح تواريخ انضمام جميع العملاء في النظام!**\nالآن كل عميل تاريخ انضمامه يطابق أقدم عملية له بالضبط.")

@router.message(Command("fix_ready_cash"))
async def fix_ready_cash_command(message: types.Message):
    """أمر سري لتصحيح عداد الكاش الجاهز لجميع البقالات"""
    if message.from_user.id != ADMIN_ID: return

    if database.pool:
        async with database.pool.acquire() as conn:
            # 1. تصحيح كاش الكروت
            await conn.execute("""
                UPDATE users 
                SET pos_cash_collected = GREATEST(0, debt - COALESCE(pending_profit, 0))
                WHERE pos_cash_collected > (debt - COALESCE(pending_profit, 0)) AND role = 'client'
            """)
            # 2. تصحيح كاش التسديدات
            await conn.execute("""
                UPDATE users 
                SET telecom_pos_cash = GREATEST(0, telecom_debt)
                WHERE telecom_pos_cash > telecom_debt AND role = 'client'
            """)
            
    await message.answer("✅ **تم تنظيف وتصحيح عدادات الكاش الجاهز (للكروت والتسديدات) لجميع البقالات!**\nالآن مستحيل أن تجد كاش جاهز أكبر من الدين الحقيقي.")

# ================= التحكم بلوحة العروض (Promo Banner) المطور =================
@router.callback_query(F.data == "qa_cmd_set_promo")
async def qa_set_promo(c: types.CallbackQuery, state: FSMContext):
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await set_promo_start(msg, state)
    await c.message.delete()

@router.message(Command("set_promo"))
async def set_promo_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    
    existing_promo = None
    if database.pool:
        async with database.pool.acquire() as conn:
            existing_promo = await conn.fetchval("SELECT value FROM settings WHERE key = 'promo_text'")
            
    # 🌟 إذا كان هناك عروض سابقة، نعرضها كلوحة تحكم 🌟
    if existing_promo and existing_promo != 'off':
        promos = existing_promo.split('===')
        kb = []
        for i, p in enumerate(promos):
            # استخراج عنوان العرض لزر الحذف
            lines = p.strip().split('\n')
            title = lines[0][:20] + "..." if len(lines[0]) > 20 else lines[0]
            kb.append([InlineKeyboardButton(text=f"🗑️ حذف: {title}", callback_data=f"del_promo_{i}")])
            
        kb.append([InlineKeyboardButton(text="➕ إضافة عرض جديد", callback_data="add_new_promo")])
        kb.append([InlineKeyboardButton(text="🧹 إيقاف ومسح الكل", callback_data="clear_all_promos")])
        kb.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_action")])
        
        text_msg = "📢 **إدارة لوحة العروض:**\nلديك عروض نشطة حالياً. ماذا تريد أن تفعل؟"
        if isinstance(message, types.CallbackQuery):
            await message.message.answer(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        else:
            await message.answer(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    else:
        # إذا لم يكن هناك عروض، نطلب منه الإضافة مباشرة
        await prompt_for_new_promo(message, state)

@router.callback_query(F.data == "add_new_promo")
async def add_new_promo_cb(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await prompt_for_new_promo(callback.message, state)

async def prompt_for_new_promo(message: types.Message, state: FSMContext):
    await message.answer(
        "📢 **إضافة عرض جديد:**\n\n"
        "أرسل الآن النص الذي تريد عرضه للعملاء والزوار.\n"
        "*(يمكنك إرسال صورة مع كتابة النص في الوصف)*\n\n"
        "❌ للإلغاء، أرسل /cancel"
    )
    await state.set_state(PromoFlow.waiting_for_promo)

@router.callback_query(F.data == "clear_all_promos")
async def clear_all_promos_cb(callback: types.CallbackQuery, bot: Bot):
    if database.pool:
        async with database.pool.acquire() as conn:
            existing_promo = await conn.fetchval("SELECT value FROM settings WHERE key = 'promo_text'")
            # 🌟 حذف جميع الصور من السيرفر فوراً 🌟
            if existing_promo and existing_promo != 'off':
                import os
                for p in existing_promo.split('==='):
                    if "IMG:" in p:
                        img_name = p.split("IMG:")[-1].strip()
                        try: os.remove(f"static/{img_name}")
                        except: pass
                        
            await conn.execute("UPDATE settings SET value = 'off' WHERE key = 'promo_text'")
            await conn.execute("UPDATE settings SET value = 'off' WHERE key = 'promo_image'")
            
    await callback.message.edit_text("✅ **تم إخفاء لوحة العروض ومسح جميع الصور بنجاح.**")
    try:
        from web_api import notify_clients
        await notify_clients(action="update_needed")
    except: pass

@router.callback_query(F.data.startswith("del_promo_"))
async def delete_specific_promo(callback: types.CallbackQuery):
    index_to_delete = int(callback.data.split("_")[2])
    if database.pool:
        async with database.pool.acquire() as conn:
            existing_promo = await conn.fetchval("SELECT value FROM settings WHERE key = 'promo_text'")
            if existing_promo and existing_promo != 'off':
                promos = existing_promo.split('===')
                if 0 <= index_to_delete < len(promos):
                    deleted_promo = promos.pop(index_to_delete)
                    
                    # 🌟 حذف صورة العرض المحدد فقط من السيرفر 🌟
                    if "IMG:" in deleted_promo:
                        import os
                        img_name = deleted_promo.split("IMG:")[-1].strip()
                        try: os.remove(f"static/{img_name}")
                        except: pass
                        
                    if promos:
                        new_text = "\n===\n".join(p.strip() for p in promos)
                        await conn.execute("UPDATE settings SET value = $1 WHERE key = 'promo_text'", new_text)
                        await callback.answer("✅ تم حذف العرض المحدد وصورته بنجاح!", show_alert=True)
                        # إعادة رسم القائمة
                        msg = callback.message.model_copy(update={"from_user": callback.from_user})
                        await callback.message.delete()
                        await set_promo_start(msg, None)
                    else:
                        await conn.execute("UPDATE settings SET value = 'off' WHERE key = 'promo_text'")
                        await conn.execute("UPDATE settings SET value = 'off' WHERE key = 'promo_image'")
                        await callback.message.edit_text("✅ **تم حذف العرض الأخير، وتم إخفاء اللوحة بالكامل.**")
                        
    try:
        from web_api import notify_clients
        await notify_clients(action="update_needed")
    except: pass

@router.message(PromoFlow.waiting_for_promo, F.text | F.photo)
async def set_promo_execute(message: types.Message, state: FSMContext, bot: Bot):
    # استخراج النص والصورة
    promo_text = message.caption if message.photo else message.text
    if not promo_text:
        return await message.answer("⚠️ الرجاء كتابة نص العرض (حتى لو أرسلت صورة، اكتب النص في الوصف).")

    photo_id = message.photo[-1].file_id if message.photo else None
    await state.update_data(new_promo_text=promo_text, photo_id=photo_id)

    existing_promo = None
    if database.pool:
        async with database.pool.acquire() as conn:
            existing_promo = await conn.fetchval("SELECT value FROM settings WHERE key = 'promo_text'")

    if existing_promo and existing_promo != 'off':
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ إضافة للعروض الحالية (تراكمي)", callback_data="promo_action_add")],
            [InlineKeyboardButton(text="🧹 مسح القديم واستبدال", callback_data="promo_action_replace")]
        ])
        await state.set_state(PromoFlow.waiting_for_action)
        await message.answer("🤔 **يوجد عروض سابقة تعمل حالياً في التطبيق.**\nماذا تريد أن تفعل بالعرض الجديد؟", reply_markup=kb)
    else:
        await save_final_promo(message, state, bot, action="replace")

@router.callback_query(PromoFlow.waiting_for_action, F.data.startswith("promo_action_"))
async def process_promo_action(c: types.CallbackQuery, state: FSMContext, bot: Bot):
    action = c.data.split("_")[2] 
    await save_final_promo(c.message, state, bot, action)
    await c.message.delete()

# ================= دالة الحفظ النهائية في قاعدة البيانات =================
async def save_final_promo(message: types.Message, state: FSMContext, bot: Bot, action: str):
    data = await state.get_data()
    new_text = data.get('new_promo_text')
    photo_id = data.get('photo_id')
    
    promo_image_status = "off"
    img_filename = ""

    # 🌟 معالجة الصورة (حفظ كل صورة باسم مستقل) 🌟
    if photo_id:
        try:
            import os
            import time
            if not os.path.exists('static'):
                os.makedirs('static')
            # توليد اسم فريد للصورة لكي لا تحذف الصور السابقة
            img_filename = f"promo_{int(time.time())}.jpg"
            file = await bot.get_file(photo_id)
            await bot.download_file(file.file_path, destination=f"static/{img_filename}")
            promo_image_status = "on"
        except Exception as e:
            await message.answer(f"⚠️ تم حفظ النص، لكن حدث خطأ في حفظ الصورة: {e}")

    # 🌟 دمج اسم الصورة مع النص لكي يعرف التطبيق أي صورة يعرض 🌟
    if img_filename:
        new_text = f"{new_text}\nIMG:{img_filename}"

    # 🌟 الحفظ في قاعدة البيانات 🌟
    if database.pool:
        async with database.pool.acquire() as conn:
            existing_text = await conn.fetchval("SELECT value FROM settings WHERE key = 'promo_text'")
            
            # 🌟 إذا اختار استبدال، نحذف الصور القديمة من السيرفر فوراً 🌟
            if action == "replace" and existing_text and existing_text != 'off':
                import os
                for p in existing_text.split('==='):
                    if "IMG:" in p:
                        old_img = p.split("IMG:")[-1].strip()
                        try: os.remove(f"static/{old_img}")
                        except: pass

            # إذا اختار "إضافة" (تراكمي)
            if action == "add" and existing_text and existing_text != 'off':
                # دمج العرض القديم مع الجديد بفاصل (===)
                new_text = f"{existing_text}\n===\n{new_text}"
                
                if not photo_id:
                    old_image_status = await conn.fetchval("SELECT value FROM settings WHERE key = 'promo_image'")
                    promo_image_status = old_image_status if old_image_status else "off"

            # تنفيذ الحفظ
            await conn.execute("INSERT INTO settings (key, value) VALUES ('promo_text', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_text)
            await conn.execute("INSERT INTO settings (key, value) VALUES ('promo_image', $1) ON CONFLICT (key) DO UPDATE SET value = $1", promo_image_status)

    await message.answer("✅ **تم نشر العرض بنجاح!** 🚀\nسيظهر الآن لجميع الزوار والبقالات في تطبيق الويب.")
    await state.clear()
    
    # تحديث شاشات العملاء فوراً
    try:
        from web_api import notify_clients
        await notify_clients(action="update_needed")
    except Exception as e: 
        import logging
        logging.error(f"Error notifying clients: {e}")

    # ================= دالة الاعتماد النهائي للتقرير الشهري =================
@router.callback_query(F.data == "approve_final_draft")
async def approve_final_draft_action(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    start_date_str, end_date_str = data['start_date'], data['end_date']
    
    from datetime import datetime
    from decimal import Decimal
    start_date_obj = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date_obj = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    
    await callback.message.edit_caption(caption="⏳ جاري الاعتماد والحفظ في الأرشيف السري...")
    
    try:
        # 1. توليد الإكسل النهائي من المطبخ المركزي
        from core_accounting import generate_detailed_excel_report
        excel_stream = await generate_detailed_excel_report(start_date_str, end_date_str)
        from aiogram.types import BufferedInputFile
        file_msg = await bot.send_document(ADMIN_ID, BufferedInputFile(excel_stream.read(), filename=f"Final_Report_{start_date_str}.xlsx"))
        file_id = file_msg.document.file_id

        # الحفظ في الأرشيف
        import database
        month_str = datetime.now().strftime('%Y-%m')
        await database.set_setting("share_monthly_report", "on")
        await database.save_report_archive(month_str, file_id)
        
        # 2. توليد الـ PDF النهائي للأرشيف السري
        archive_channel = await database.get_setting("archive_channel_id")
        if archive_channel and archive_channel != "off":
            try:
                summary_data = {
                    'received_from_gm': Decimal('0.0'), 'wholesale_debt': Decimal('0.0'),
                    'wholesale_cash': Decimal('0.0'), 'retail_cash': Decimal('0.0'),
                    'collected': Decimal('0.0'), 'paid_to_gm': Decimal('0.0'),
                    'agent_commission': Decimal('0.0'), 'expenses': Decimal('0.0'),
                    'damaged': Decimal('0.0'), 'returns_client': Decimal('0.0'),
                    'returns_gm': Decimal('0.0'), 'returns_retail': Decimal('0.0'),
                    'net_cash': Decimal('0.0')
                }
                if database.pool:
                    async with database.pool.acquire() as conn:
                        # 🌟 سد الكارثة الكبرى: منع تسريب التسديدات والعمليات الملغاة لـ PDF المدير
                        query = """
                            SELECT type, details, COALESCE(SUM(amount), 0) as total 
                            FROM transactions 
                            WHERE is_reverted = FALSE 
                            AND wallet_type = 'manager'
                            AND date >= $1::date AND date <= $2::date + interval '1 day'
                            GROUP BY type, details
                        """
                        records = await conn.fetch(query, start_date_obj, end_date_obj)
                        for r in records:
                            t_type = r['type']
                            t_details = r['details'] or ""
                            t_total = Decimal(str(r['total']))
                            
                            if t_type == 'استلام_من_الشبكة': summary_data['received_from_gm'] += t_total
                            elif t_type == 'تسليم_لعميل': summary_data['wholesale_debt'] += t_total
                            elif t_type == 'تسديد_من_عميل': summary_data['collected'] += t_total
                            elif t_type == 'تسديد_للشبكة': summary_data['paid_to_gm'] += t_total
                            elif t_type == 'نسبة_الوكيل': summary_data['agent_commission'] += t_total
                            elif t_type == 'مصروفات': summary_data['expenses'] += t_total
                            elif t_type == 'كروت_تالفة': summary_data['damaged'] += t_total
                            elif t_type == 'مرتجع_من_عميل': summary_data['returns_client'] += t_total
                            elif t_type == 'مرتجع_للشبكة': summary_data['returns_gm'] += t_total
                            elif t_type == 'مرتجع_بيع_مباشر': summary_data['returns_retail'] += t_total
                            elif t_type == 'سحب_أرباح': summary_data['profit_withdrawal'] = summary_data.get('profit_withdrawal', Decimal('0.0')) + t_total
                            elif t_type == 'بيع_مباشر':
                                if "جملة كاش" in t_details: summary_data['wholesale_cash'] += t_total
                                else: summary_data['retail_cash'] += t_total
                        
                        cash_in = summary_data['retail_cash'] + summary_data['wholesale_cash'] + summary_data['collected']
                        cash_out = summary_data['expenses'] + summary_data['paid_to_gm'] + summary_data['returns_retail'] + summary_data['damaged'] + summary_data.get('profit_withdrawal', Decimal('0.0'))
                        summary_data['net_cash'] = cash_in - cash_out

                from pdf_generator import generate_custom_report_pdf
                import asyncio
                pdf_buffer = await asyncio.to_thread(generate_custom_report_pdf, start_date_str, end_date_str, summary_data)
                pdf_document = BufferedInputFile(pdf_buffer.read(), filename=f"Archive_Report_{start_date_str}_to_{end_date_str}.pdf")
                await bot.send_document(archive_channel, pdf_document, caption=f"📂 **أرشيف التقارير (PDF):** تقرير الفترة من {start_date_str} إلى {end_date_str}")
                await bot.send_document(archive_channel, file_id, caption=f"📂 **أرشيف التقارير (Excel):** تقرير الفترة من {start_date_str} إلى {end_date_str}")
            except Exception as e:
                print(f"Archive Error: {e}")
        
        # إشعار المدير العام
        from config import NETWORK_OWNER_ID
        try:
            await smart_notify(bot, NETWORK_OWNER_ID, f"🔔 **يا شيخ رهيب، د. وليد اعتمد التقرير الشهري (من {start_date_str} إلى {end_date_str}).**\nالتقرير موجود ومحفوظ تسطيع الحصول علية من خلال  ارسال رسالة صوتية هات التقرير  او رساله نصية اعطني التقرير او تسحب ملف الإكسل متى ما تحب من زر (سحب التقرير).")
        except Exception as e: logging.error(f"Error: {e}")
        
        # 🌟 إضافة إشعار التطبيق للمدير
        try:
            from web_api import send_web_push
            await send_web_push(NETWORK_OWNER_ID, "📊 تقرير جديد", f"د. وليد اعتمد التقرير الشهري (من {start_date_str} إلى {end_date_str}).")
        except: pass
        
        await callback.message.edit_caption(caption="✅ **تم الاعتماد بنجاح!**\nتم حفظ التقرير في الأرشيف وإشعار المدير العام.")
        await state.clear()
        
    except Exception as e:
        print(f"Error in approve_final_draft: {e}")
        await callback.message.edit_caption(caption=f"❌ حدث خطأ أثناء الاعتماد: {e}")

# ================= صائد الأخطاء الشامل للبوت (Global Error Handler) =================
@router.errors()
async def global_error_handler(event: types.ErrorEvent, bot: Bot):
    """يلتقط أي خطأ برمجي يحدث داخل البوت ويرسله للمدير"""
    import traceback
    
    exception_name = type(event.exception).__name__
    exception_msg = str(event.exception)
    
    # 🌟 التقاط الأخطاء المحاسبية (مثل السقف، الرصيد، الكاش) وتحويلها لرسائل تنبيه طبيعية
    if exception_name in ["CreditLimitExceededError", "InvalidOperationError", "InsufficientStockError", "SecurityViolationError"]:
        # 🌟 إصلاح منطقي: إظهار ملاحظة التجاوز فقط للأخطاء التي تدعم ذلك (مثل سقف المديونية)
        bypass_note = "\n\n*(يُمنع تجاوز هذا الحظر أمنياً - كود: ERR#)*" if exception_name in ["CreditLimitExceededError", "SecurityViolationError"] else ""
        warning_msg = f"🚨 **تنبيه من النظام المحاسبي:**\n{exception_msg}{bypass_note}"

        try:
            if event.update.message:
                await event.update.message.answer(warning_msg)
            elif event.update.callback_query:
                await event.update.callback_query.message.answer(warning_msg)
                await event.update.callback_query.answer()
        except Exception as e: logging.error(f"Error: {e}")
        return # 👈 هذا السطر هو السحر! يوقف الكود هنا ويمنع إرسال رسالة "انهيار البوت"

    # --- إذا كان الخطأ برمجياً حقيقياً، نرسل الإنذار الأحمر للإدارة ---
    error_details = traceback.format_exc()
    
    user_id = "مجهول"
    if event.update.message:
        user_id = event.update.message.from_user.id
    elif event.update.callback_query:
        user_id = event.update.callback_query.from_user.id

    error_msg = (
        f"🔴 **انهيار في البوت (Bot Error)** 🔴\n\n"
        f"👤 **المستخدم:** `{user_id}`\n"
        f"⚠️ **الخطأ:** `{exception_name}: {exception_msg}`\n\n"
        f"🔍 **التفاصيل:**\n`{error_details[-800:]}`"
    )
    
    try:           
        await smart_notify(bot, ADMIN_ID, error_msg)
    except:
        pass

@router.callback_query(F.data == "qa_cmd_audit_all_cash")
async def qa_audit_all_cash_btn(c: types.CallbackQuery):
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await audit_all_system_cash(msg)
    await c.answer()

@router.callback_query(F.data == "qa_cmd_clean_all_cash")
async def qa_clean_all_cash_btn(c: types.CallbackQuery):
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await clean_all_system_cash(msg)
    await c.answer()

@router.callback_query(F.data == "qa_cmd_check_cash")
async def qa_check_cash_btn(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("🔎 أرسل رقم البقالة لفحص الكاش الخاص بها:")
    await state.set_state(AgentQuickActionsFlow.waiting_for_check_cash_id)

@router.message(AgentQuickActionsFlow.waiting_for_check_cash_id)
async def qa_check_cash_exec(m: types.Message, state: FSMContext):
    await check_cash_source(m, FakeCommand(m.text))
    await state.clear()

@router.callback_query(F.data == "qa_cmd_audit_manager")
async def qa_audit_manager(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID: return
    
    await c.message.edit_text("🔍 جاري فحص جميع ملفات السيرفر بدقة...\n(الرجاء الانتظار ثواني)")
    import os
    from io import BytesIO
    from aiogram.types import BufferedInputFile
    
    report = "🔍 تقرير فحص رسائل وإشعارات المدير العام:\n" + "="*50 + "\n\n"
    target_keywords = ['NETWORK_OWNER_ID']
    send_methods = ['send_message', 'send_document', 'send_photo', 'send_voice', 'send_web_push']
    count = 0
    
    for filename in os.listdir('.'):
        if filename.endswith('.py'):
            with open(filename, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for i, line in enumerate(lines):
                    if any(k in line for k in target_keywords) and any(m in line for m in send_methods):
                        report += f"📄 ملف: {filename} (سطر {i+1}):\n{line.strip()}\n{'-'*50}\n"
                        count += 1
                        
    report += f"\n✅ الإجمالي: {count} موضع في الكود يرسل بيانات للمدير."
    
    file = BufferedInputFile(report.encode('utf-8'), filename="Manager_Audit_Report.txt")
    await c.message.answer_document(file, caption="✅ تفضل يا دكتور، هذا ملف نصي يحتوي على **كل سطر كود** يرسل بيانات للمدير العام.\nافتحه واقرأه لتطمئن أنه لا يوجد أي تسريب لبيانات التسديدات.")
    
    # إعادة القائمة
    await c.message.answer("⚙️ **أوامر النظام وقاعدة البيانات:**", reply_markup=await get_qa_system_keyboard())

@router.callback_query(F.data == "qa_cmd_toggle_telecom_master")
async def qa_toggle_telecom_master_btn(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID: return
    
    await c.answer("⏳ جاري تغيير حالة قسم التسديدات...")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            current = await conn.fetchval("SELECT value FROM settings WHERE key = 'is_telecom_active'")
            new_status = 'on' if current == 'off' else 'off'
            await conn.execute("UPDATE settings SET value = $1 WHERE key = 'is_telecom_active'", new_status)
            
    status_ar = "🟢 مفعل (يعمل)" if new_status == 'on' else "🔴 معطل (مخفي)"
    msg = (
        f"✅ **تم تغيير حالة قسم التسديدات العام!**\n"
        f"الحالة الآن: {status_ar}\n\n"
        f"💡 **ماذا يعني هذا؟**\n"
        f"- إذا كان مفعلاً: ستظهر لك مطالبات (كاش التسديدات) في الزيارة الميدانية، وسيبدأ الذكاء الاصطناعي بقراءة أرباحك منها.\n"
        f"- إذا كان معطلاً: سيختفي القسم تماماً من الزيارات الميدانية لتبسيط العمل."
    )
    
    # تحديث الرسالة مع إبقاء نفس لوحة الأزرار
    await c.message.edit_text(msg, reply_markup=await get_qa_system_keyboard())

# ================= استخراج الصندوق الأسود (Black Box) =================
@router.callback_query(F.data == "export_blackbox")
async def export_blackbox_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    
    await callback.answer("⏳ جاري استخراج الصندوق الأسود...")
    wait_msg = await callback.message.answer("🕋 ⏳ جاري استخراج بيانات الصندوق الأسود (Black Box)...")
    
    if not database.pool:
        return await wait_msg.edit_text("❌ قاعدة البيانات غير متصلة.")
        
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill
        from io import BytesIO
        import json
        from datetime import datetime
        
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "الصندوق الأسود"
        ws.sheet_view.rightToLeft = True
        
        # العناوين
        headers = ["رقم السجل", "رقم العملية", "نوع الحدث", "رقم العميل", "المبلغ", "حالة الوكيل (قبل)", "حالة العميل (قبل)", "حالة الوكيل (بعد)", "حالة العميل (بعد)", "التاريخ"]
        ws.append(headers)
        
        # تنسيق العناوين
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="000000", end_color="000000", fill_type="solid")
        for col in range(1, len(headers) + 1):
            ws.cell(row=1, column=col).font = header_font
            ws.cell(row=1, column=col).fill = header_fill
        
        async with database.pool.acquire() as conn:
            # جلب آخر 5000 حركة لمنع ثقل الملف
            records = await conn.fetch("SELECT * FROM system_blackbox ORDER BY id DESC LIMIT 5000")
            
            for r in records:
                state_before = json.loads(r['state_before']) if r['state_before'] else {}
                state_after = json.loads(r['state_after']) if r['state_after'] else {}
                
                agent_before = json.dumps(state_before.get('agent', {}), ensure_ascii=False)
                client_before = json.dumps(state_before.get('client', {}), ensure_ascii=False)
                agent_after = json.dumps(state_after.get('agent', {}), ensure_ascii=False)
                client_after = json.dumps(state_after.get('client', {}), ensure_ascii=False)
                
                # 🌟 الإصلاح الأمني: حماية ضد الـ NULL في عمود amount لمنع انهيار التصدير
                safe_amount = float(r['amount']) if r['amount'] is not None else 0.0
                
                ws.append([
                    r['id'], r['tx_id'], r['action_type'], str(r['client_id']), safe_amount,
                    agent_before, client_before, agent_after, client_after, str(r['created_at'])[:19]
                ])
                
        stream = BytesIO()
        import asyncio
        await asyncio.to_thread(wb.save, stream)
        stream.seek(0)
        
        file_name = f"BlackBox_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        from aiogram.types import BufferedInputFile
        document = BufferedInputFile(stream.read(), filename=file_name)
        
        await wait_msg.delete()
        await callback.message.answer_document(document=document, caption="🕋 **سجل الصندوق الأسود (Black Box)**\nيحتوي على آخر 5000 حركة مع لقطات الأرصدة قبل وبعد كل عملية بدقة متناهية.")
        
    except Exception as e:
        await wait_msg.edit_text(f"❌ حدث خطأ أثناء استخراج الصندوق الأسود: {e}")

# =====================================================================
# 🕵️‍♂️ نظام بصمة النظام والمطابقة (System Fingerprint & Checksum)
# =====================================================================
@router.message(Command("fingerprint"))
async def generate_system_fingerprint(message: types.Message):
    """أمر سري يولد بصمة رقمية دقيقة لكل شيء في النظام لمطابقتها قبل وبعد الاستعادة"""
    if message.from_user.id != ADMIN_ID: return
    
    wait_msg = await message.answer("🕵️‍♂️ جاري مسح النظام بالكامل وتوليد البصمة الرقمية (Fingerprint)...")
    
    if not database.pool:
        return await wait_msg.edit_text("❌ قاعدة البيانات غير متصلة.")
        
    try:
        async with database.pool.acquire() as conn:
            # 🌟 الإصلاح الجذري: تنفيذ الاستعلامات بالتسلسل (بالدور) لمنع تداخل قاعدة البيانات
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
            clients_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role = 'client'")
            total_debt = await conn.fetchval("SELECT COALESCE(SUM(debt), 0) FROM users")
            total_pending = await conn.fetchval("SELECT COALESCE(SUM(pending_profit), 0) FROM users")
            tx_count = await conn.fetchval("SELECT COUNT(*) FROM transactions")
            tx_sum = await conn.fetchval("SELECT COALESCE(SUM(ABS(amount)), 0) FROM transactions")
            inv_types = await conn.fetchval("SELECT COUNT(*) FROM inventory")
            inv_qty = await conn.fetchval("SELECT COALESCE(SUM(quantity), 0) FROM inventory")
            client_inv_qty = await conn.fetchval("SELECT COALESCE(SUM(quantity), 0) FROM client_inventory")
            ecards_total = await conn.fetchval("SELECT COUNT(*) FROM electronic_cards")
            ecards_sold = await conn.fetchval("SELECT COUNT(*) FROM electronic_cards WHERE status = 'sold'")
            telecom_tx_count = await conn.fetchval("SELECT COUNT(*) FROM telecom_transactions")
            telecom_tx_sum = await conn.fetchval("SELECT COALESCE(SUM(selling_price), 0) FROM telecom_transactions")
            wallet = await conn.fetchrow("SELECT manager_cash, telecom_cash, telecom_balance, realized_profit FROM agent_wallet WHERE id = 1")
            agent_profits_count = await conn.fetchval("SELECT COUNT(*) FROM agent_profits")
            chat_count = await conn.fetchval("SELECT COUNT(*) FROM chat_messages")
            orders_count = await conn.fetchval("SELECT COUNT(*) FROM pending_orders")
            shipments_count = await conn.fetchval("SELECT COUNT(*) FROM pending_shipments")
            
        # تنسيق البصمة
        report = (
            "🧬 **البصمة الرقمية للنظام (System Fingerprint):**\n"
            "*(التقط صورة لهذه الشاشة وطابقها بعد استعادة النسخة الاحتياطية)*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👥 **العملاء:** {users_count} حساب ({clients_count} بقالة)\n"
            f"💰 **إجمالي الديون المسجلة:** {int(total_debt)} ريال\n"
            f"💎 **الأرباح المعلقة:** {int(total_pending)} ريال\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🔄 **العمليات المالية:** {tx_count} عملية\n"
            f"🧮 **مجموع مبالغ العمليات (للمطابقة):** {int(tx_sum)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📦 **فئات المخزون:** {inv_types} فئة\n"
            f"📦 **إجمالي كروت الوكيل:** {inv_qty} كرت\n"
            f"🏪 **إجمالي كروت البقالات:** {client_inv_qty} كرت\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📱 **الكروت الإلكترونية:** {ecards_total} كرت ({ecards_sold} مباع)\n"
            f"⚡ **عمليات التسديدات:** {telecom_tx_count} عملية\n"
            f"🧮 **مجموع مبالغ التسديدات:** {int(telecom_tx_sum)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💵 **كاش الإدارة (الدرج):** {int(wallet['manager_cash']) if wallet else 0} ريال\n"
            f"💵 **كاش التسديدات:** {int(wallet['telecom_cash']) if wallet else 0} ريال\n"
            f"🌐 **رصيد البوابة:** {int(wallet['telecom_balance']) if wallet else 0} ريال\n"
            f"💎 **أرباح الوكيل المحصلة:** {int(wallet['realized_profit']) if wallet else 0} ريال\n"
            f"💎 **سجلات الأرباح:** {agent_profits_count} سجل\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💬 **المحادثات والتذاكر:** {chat_count} رسالة\n"
            f"🛒 **الطلبات والإرساليات:** {orders_count + shipments_count} طلب\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "✅ **إذا تطابقت هذه الأرقام قبل وبعد الاستعادة، فنظامك سليم 100%.**"
        )
        
        # 🚀 التطوير: إضافة زر الرجوع
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع للأوامر السريعة", callback_data="agent_quick_actions")]])
        await wait_msg.edit_text(report, reply_markup=kb)
        
    except Exception as e:
        await wait_msg.edit_text(f"❌ حدث خطأ أثناء توليد البصمة: {e}")

# 🚀 دالة ربط الزر بالأمر
@router.callback_query(F.data == "qa_cmd_fingerprint")
async def qa_fingerprint_btn(c: types.CallbackQuery):
    msg = c.message.model_copy(update={"from_user": c.from_user})
    await generate_system_fingerprint(msg)
    await c.answer()

# =====================================================================
# 🌪️ نظام هندسة الفوضى والمطابقة المحاسبية (Ultimate Audit Test V8.0)
# =====================================================================

class TestConnectionContextManager:
    def __init__(self, pool):
        self.pool = pool
        self.conn = None
    async def __aenter__(self):
        self.conn = await self.pool.acquire()
        await self.conn.execute("SET search_path TO test_env")
        return self.conn
    async def __aexit__(self, exc_type, exc, tb):
        await self.conn.execute("SET search_path TO public")
        await self.pool.release(self.conn)

class TestPool:
    def __init__(self, real_pool):
        self.real_pool = real_pool
    def acquire(self):
        return TestConnectionContextManager(self.real_pool)

@router.message(Command("mega_stress_test"))
async def start_mega_stress_test(message: types.Message, command: CommandObject, bot: Bot):
    if message.from_user.id != ADMIN_ID: return
    
    num_clients = 50
    num_operations = 3000
    
    if command.args:
        try:
            args = command.args.split()
            num_clients = int(args[0])
            num_operations = int(args[1])
        except:
            return await message.answer("⚠️ **الاستخدام الصحيح:**\n`/mega_stress_test [عدد_العملاء] [عدد_العمليات]`")

    await message.answer(f"🚀 **جاري بناء بيئة اختبار معزولة...**\nسيتم إنشاء {num_clients} بقالة وضخ {num_operations} عملية مالية (28 نوع مختلف)!")
    
    import asyncio
    asyncio.create_task(run_isolated_stress_test(message, bot, num_clients, num_operations))

async def run_isolated_stress_test(message: types.Message, bot: Bot, num_clients: int, num_operations: int):
    import random
    from decimal import Decimal
    import asyncio
    from core_accounting import FinancialEngine, TxType
    
    progress_msg = await bot.send_message(ADMIN_ID, "🏗️ جاري استنساخ الجداول وبناء الغرفة السرية...")
    
    try:
        # ==========================================
        # المرحلة 1: بناء الغرفة المعزولة
        # ==========================================
        async with database.pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS test_env CASCADE;")
            await conn.execute("CREATE SCHEMA test_env;")
            
            tables = ['users', 'transactions', 'inventory', 'client_inventory', 'agent_wallet', 
                      'telecom_transactions', 'telecom_profits', 'agent_profits', 'idempotency_keys',
                      'electronic_cards', 'client_customers', 'client_customer_ledger', 'system_blackbox']
            
            for table in tables:
                await conn.execute(f"CREATE TABLE test_env.{table} (LIKE public.{table} INCLUDING ALL);")
            
            await conn.execute("INSERT INTO test_env.agent_wallet (id, telecom_balance, manager_cash, telecom_cash, realized_profit) VALUES (1, 0, 0, 0, 0);")

        await progress_msg.edit_text(f"✅ تم بناء البيئة المعزولة.\n🚀 جاري بدء الهجوم الشامل ({num_operations} عملية)...")

        # ==========================================
        # المرحلة 2: التجهيز والهجوم الشامل
        # ==========================================
        isolated_pool = TestPool(database.pool)
        engine = FinancialEngine(isolated_pool)
        
        # 📊 عدادات التقرير المفصل لجميع الـ 28 عملية
        actions_list = [
            'give', 'collect', 'direct_sale', 'expense', 'pay_manager', 
            'telecom_pay', 'telecom_topup', 'return_client', 'return_network', 
            'damaged', 'extract_ecard', 'transfer_cash', 'transfer_cards',
            'receive_network', 'agent_commission', 'withdraw_profit', 
            'withdraw_telecom_profit', 'add_telecom_capital', 'agent_telecom_sale',
            'client_sale', 'collect_telecom_debt', 'return_direct_sale', 'opening_balance',
            'revert_tx', 'discount_gm', 'telecom_refund', 'set_client_debt', 'customer_pay'
        ]
        
        op_stats = {action: 0 for action in actions_list}
        op_stats['مرفوضة أمنياً'] = 0

        async with isolated_pool.acquire() as conn:
            clients_data = [(i, f"عميل فوضى {i}", 'client', Decimal('500000')) for i in range(1, num_clients + 1)]
            await conn.executemany("INSERT INTO users (user_id, name, role, credit_limit) VALUES ($1, $2, $3, $4)", clients_data)
            
            # 🌟 الإصلاح المحاسبي الجذري: إدخال البضاعة والكاش الافتتاحي بطريقة محاسبية سليمة 100%
            await conn.execute("INSERT INTO inventory (card_type, quantity, cost_price, price, retail_price) VALUES ('كرت_ورقي', 500000, 900, 1000, 1100)")
            await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'استلام_من_الشبكة', 450000000, 'بضاعة افتتاحية للاختبار', 'manager')")
            
            await conn.execute("INSERT INTO inventory (card_type, quantity, cost_price, price, retail_price) VALUES ('كرت_إلكتروني', 10000, 900, 1000, 1100)")
            await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'استلام_من_الشبكة', 9000000, 'بضاعة افتتاحية للاختبار', 'manager')")
            
            ecards_data = [(f"PIN_{i}", 'كرت_إلكتروني', 'available') for i in range(10000)]
            await conn.executemany("INSERT INTO electronic_cards (card_number, card_type, status) VALUES ($1, $2, $3)", ecards_data)
            
            await conn.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'رصيد_افتتاحي_كاش', 1000000, 'كاش افتتاحي للاختبار', 'manager')")
            await conn.execute("UPDATE agent_wallet SET manager_cash = 1000000 WHERE id = 1")

        async def execute_and_track(task_coro, op_name):
            try:
                res = await task_coro
                if isinstance(res, dict) and res.get("status") == "error": 
                    op_stats['مرفوضة أمنياً'] += 1
                    return False
                op_stats[op_name] += 1
                return True
            except Exception: 
                op_stats['مرفوضة أمنياً'] += 1
                return False

        batch_size = 50
        
        for i in range(0, num_operations, batch_size):
            tasks = []
            for _ in range(batch_size):
                client_id = random.randint(1, num_clients)
                action = random.choice(actions_list)
                
                if action == 'give':
                    tasks.append(execute_and_track(engine.give_cards(client_id, 'كرت_ورقي', random.randint(1, 5), override=True), action))
                elif action == 'collect':
                    tasks.append(execute_and_track(engine.collect_debt(client_id, Decimal(random.randint(500, 5000)), bypass_shortage=True), action))
                elif action == 'direct_sale':
                    tasks.append(execute_and_track(engine.direct_sale('كرت_ورقي', random.randint(1, 3), Decimal('3300'), Decimal('0'), 'retail'), action))
                elif action == 'expense':
                    tasks.append(execute_and_track(engine.finance_action(TxType.EXPENSE, Decimal(random.randint(50, 200)), "[TEST] مصروفات"), action))
                elif action == 'pay_manager':
                    tasks.append(execute_and_track(engine.finance_action(TxType.PAY_MANAGER, Decimal(random.randint(100, 500)), "[TEST] تسديد مدير"), action))
                elif action == 'telecom_pay':
                    tasks.append(execute_and_track(engine.process_telecom(client_id, '777000000', 'yemen_mobile', '[TEST] باقة', Decimal('900'), Decimal('1000'), Decimal('100'), False), action))
                elif action == 'agent_telecom_sale':
                    tasks.append(execute_and_track(engine.process_telecom(0, '777000000', 'yemen_mobile', '[TEST] طياري', Decimal('900'), Decimal('1000'), Decimal('100'), True), action))
                elif action == 'telecom_topup':
                    tasks.append(execute_and_track(engine.finance_action(TxType.TELECOM_TOPUP, Decimal('500'), "[TEST] تغذية بوابة"), action))
                elif action == 'add_telecom_capital':
                    tasks.append(execute_and_track(engine.finance_action(TxType.ADD_TELECOM_CAPITAL, Decimal('500'), "[TEST] رأس مال"), action))
                elif action == 'return_client':
                    tasks.append(execute_and_track(engine.return_from_client(client_id, 'كرت_ورقي', 1), action))
                elif action == 'return_network':
                    tasks.append(execute_and_track(engine.return_to_network('كرت_ورقي', 1), action))
                elif action == 'receive_network':
                    tasks.append(execute_and_track(engine.receive_from_network('كرت_ورقي', 10, Decimal('900')), action))
                elif action == 'agent_commission':
                    tasks.append(execute_and_track(engine.finance_action(TxType.AGENT_COMMISSION, Decimal('500'), "[TEST] نسبة"), action))
                elif action == 'withdraw_profit':
                    tasks.append(execute_and_track(engine.finance_action(TxType.WITHDRAW_PROFIT, Decimal('100'), "[TEST] سحب"), action))
                elif action == 'withdraw_telecom_profit':
                    tasks.append(execute_and_track(engine.finance_action(TxType.WITHDRAW_TELECOM_PROFIT, Decimal('50'), "[TEST] سحب تسديدات"), action))
                elif action == 'client_sale':
                    tasks.append(execute_and_track(engine.record_client_sale(client_id, 'كرت_ورقي', 10, 9), action))
                elif action == 'transfer_cash':
                    tasks.append(execute_and_track(engine.transfer_assets('client', client_id, 'client', random.randint(1, num_clients), 'cash', Decimal('500')), action))
                elif action == 'transfer_cards':
                    tasks.append(execute_and_track(engine.transfer_assets('client', client_id, 'client', random.randint(1, num_clients), 'cards', 1, 'كرت_ورقي'), action))
                
                # --- المحاكيات الآمنة للعمليات المستقلة ---
                elif action == 'damaged':
                    async def run_damaged():
                        async with isolated_pool.acquire() as c:
                            await c.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'كروت_تالفة', 100, 'تالف', 'manager')")
                            await c.execute("UPDATE agent_wallet SET manager_cash = manager_cash - 100 WHERE id = 1")
                        return {"status": "success"}
                    tasks.append(execute_and_track(run_damaged(), action))
                elif action == 'extract_ecard':
                    async def run_extract_ecard():
                        async with isolated_pool.acquire() as c:
                            await c.execute("UPDATE electronic_cards SET status = 'sold' WHERE card_number IN (SELECT card_number FROM electronic_cards WHERE card_type = 'كرت_إلكتروني' AND status = 'available' LIMIT 1)")
                            await c.execute("UPDATE users SET debt = debt + 1000, pending_profit = pending_profit + 100 WHERE user_id = $1", client_id)
                            await c.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES ($1, 'مبيعات_آجلة', 1000, 'سحب كرت إلكتروني', 'manager')", client_id)
                            await c.execute("INSERT INTO agent_profits (amount, details) VALUES (100, 'ربح كرت إلكتروني')")
                        return {"status": "success"}
                    tasks.append(execute_and_track(run_extract_ecard(), action))
                elif action == 'collect_telecom_debt':
                    async def run_collect_t_debt():
                        async with isolated_pool.acquire() as c:
                            await c.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES ($1, 'تحصيل_رصيد', 500, 'تحصيل', 'telecom')", client_id)
                            await c.execute("UPDATE agent_wallet SET telecom_cash = telecom_cash + 500 WHERE id = 1")
                        return {"status": "success"}
                    tasks.append(execute_and_track(run_collect_t_debt(), action))
                elif action == 'return_direct_sale':
                    async def run_ret_direct():
                        async with isolated_pool.acquire() as c:
                            await c.execute("UPDATE inventory SET quantity = quantity + 1 WHERE card_type = 'كرت_ورقي'")
                            await c.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'مرتجع_بيع_مباشر', 1100, 'مرتجع', 'manager')")
                            await c.execute("INSERT INTO agent_profits (amount, details) VALUES (-200, 'خصم')")
                            await c.execute("UPDATE agent_wallet SET manager_cash = manager_cash - 1100 WHERE id = 1")
                        return {"status": "success"}
                    tasks.append(execute_and_track(run_ret_direct(), action))
                elif action == 'opening_balance':
                    async def run_opening():
                        async with isolated_pool.acquire() as c:
                            await c.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'رصيد_افتتاحي', 5000, 'افتتاحي', 'manager')")
                        return {"status": "success"}
                    tasks.append(execute_and_track(run_opening(), action))
                elif action == 'revert_tx':
                    async def run_revert():
                        async with isolated_pool.acquire() as c:
                            await c.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'قيد_عكسي', 500, 'تراجع عن مصروفات', 'manager')")
                            await c.execute("UPDATE agent_wallet SET manager_cash = manager_cash + 500 WHERE id = 1")
                        return {"status": "success"}
                    tasks.append(execute_and_track(run_revert(), action))
                elif action == 'discount_gm':
                    async def run_discount():
                        async with isolated_pool.acquire() as c:
                            await c.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'تسديد_للشبكة', 1000, 'خصم', 'manager')")
                            await c.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES (0, 'قيد_عكسي', 1000, 'تسوية', 'manager')")
                            await c.execute("INSERT INTO agent_profits (amount, details) VALUES (1000, 'خصم')")
                        return {"status": "success"}
                    tasks.append(execute_and_track(run_discount(), action))
                elif action == 'telecom_refund':
                    async def run_refund():
                        async with isolated_pool.acquire() as c:
                            await c.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + 1000 WHERE id = 1")
                            await c.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES ($1, 'استرداد_تسديد', 1000, 'استرداد', 'telecom')", client_id)
                        return {"status": "success"}
                    tasks.append(execute_and_track(run_refund(), action))
                elif action == 'set_client_debt':
                    async def run_set_debt():
                        async with isolated_pool.acquire() as c:
                            await c.execute("UPDATE users SET debt = debt + 5000, pending_profit = pending_profit + 500 WHERE user_id = $1", client_id)
                            await c.execute("INSERT INTO transactions (user_id, type, amount, details, wallet_type) VALUES ($1, 'دين_سابق_عميل', 5000, 'دين سابق', 'manager')", client_id)
                        return {"status": "success"}
                    tasks.append(execute_and_track(run_set_debt(), action))
                elif action == 'customer_pay':
                    async def run_customer_pay():
                        async with isolated_pool.acquire() as c:
                            await c.execute("UPDATE users SET pos_cash_collected = pos_cash_collected + 1000 WHERE user_id = $1", client_id)
                        return {"status": "success"}
                    tasks.append(execute_and_track(run_customer_pay(), action))

            await asyncio.gather(*tasks)

            if i % 500 == 0 and i > 0:
                await progress_msg.edit_text(f"🚀 **جاري المعركة الشاملة... {int((i/num_operations)*100)}%**\n(تم تنفيذ {i} عملية)")

        # ==========================================
        # المرحلة 3: الجرد المحاسبي الصارم (The Ultimate Audit)
        # ==========================================
        await progress_msg.edit_text("✅ **انتهى الهجوم!**\nجاري الآن إجراء جرد محاسبي دقيق لكل هللة في الغرفة السرية...")
        
        async with isolated_pool.acquire() as conn:
            # 1. حساب رأس مال المدير الصافي
            cash_in = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE AND wallet_type = 'manager'")
            cash_out = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسديد_للشبكة', 'مصروفات', 'كروت_تالفة', 'نسبة_الوكيل', 'مرتجع_للشبكة') AND is_reverted = FALSE AND wallet_type = 'manager'")
            net_manager_capital = Decimal(cash_in or 0) - Decimal(cash_out or 0)

            # 2. أين يتوزع هذا الرأس مال؟
            inv_value = await conn.fetchval("SELECT COALESCE(SUM(quantity * cost_price), 0) FROM inventory")
            
            debt_added = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسليم_لعميل', 'رصيد_افتتاحي', 'دين_سابق_عميل', 'مبيعات_آجلة') AND is_reverted = FALSE AND wallet_type = 'manager'")
            debt_removed = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسديد_من_عميل', 'مرتجع_من_عميل') AND is_reverted = FALSE AND wallet_type = 'manager'")
            pending_profit = await conn.fetchval("SELECT COALESCE(SUM(pending_profit), 0) FROM users WHERE role = 'client'")
            net_market_debt = (Decimal(debt_added or 0) - Decimal(debt_removed or 0)) - Decimal(pending_profit or 0)

            actual_drawer_cash = await conn.fetchval("SELECT manager_cash FROM agent_wallet WHERE id = 1")
            
            # المعادلة المحاسبية الذهبية: رأس المال = المخزون + ديون السوق + صافي كاش المدير
            calculated_capital = Decimal(inv_value or 0) + net_market_debt + net_manager_cash
            is_balanced = (net_manager_capital == calculated_capital)
            
            report = f"📊 **تقرير اختبار التحمل والمطابقة (Stress Test):**\n\n"
            report += "📝 **تفاصيل العمليات التي تم تنفيذها:**\n"
            for op, count in op_stats.items():
                if count > 0:
                    icon = "🛡️" if op == 'مرفوضة أمنياً' else "✅"
                    report += f"▪️ {op}: {count} عملية {icon}\n"
            
            report += "\n⚖️ **الميزان المحاسبي (Double-Entry Audit):**\n"
            report += f"👑 رأس مال المدير الصافي: {int(net_manager_capital)} ريال\n"
            report += f"📦 قيمة المخزون: {int(inv_value or 0)} ريال\n"
            report += f"👥 ديون السوق الصافية: {int(net_market_debt)} ريال\n"
            report += f"💵 الكاش الفعلي بالدرج: {int(actual_drawer_cash)} ريال\n"
            report += f"💎 أرباح الوكيل بالدرج: {int(agent_realized_profit)} ريال\n"
            report += f"💰 صافي كاش المدير: {int(net_manager_cash)} ريال\n\n"

            if is_balanced:
                report += "🏆 **النتيجة:** الميزان المحاسبي متطابق 100% بالهللة! النظام أثبت أنه لا يُخترق ولا يضيع فيه ريال واحد رغم الفوضى."
            else:
                report += f"❌ **النتيجة:** يوجد فارق محاسبي بقيمة {abs(net_manager_capital - calculated_capital)} ريال!"

            await bot.send_message(ADMIN_ID, report)

    except Exception as e:
        await bot.send_message(ADMIN_ID, f"❌ حدث خطأ أثناء الاختبار: {e}")
        
    finally:
        await bot.send_message(ADMIN_ID, "💥 **جاري تدمير الغرفة السرية ومسح آثار الاختبار...**")
        async with database.pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS test_env CASCADE;")
        await bot.send_message(ADMIN_ID, "✨ **تم تدمير البيئة المعزولة بنجاح!**\nبياناتك الحقيقية في أمان تام ولم تتأثر أبداً.")

@router.message(Command("emergency_clean_test"))
async def emergency_clean_test_command(message: types.Message, bot: Bot):
    """أمر طوارئ لتنظيف النظام إذا انهار السيرفر أثناء الاختبار"""
    if message.from_user.id != ADMIN_ID: return
    await message.answer("🚨 **تفعيل بروتوكول الطوارئ:** جاري تنظيف النظام واستعادة المحفظة...")
    await execute_emergency_cleanup(message, bot)

async def execute_emergency_cleanup(message: types.Message, bot: Bot):
    """دالة التنظيف الموحدة (تمسح الوهمي وتستعيد المحفظة من قاعدة البيانات)"""
    try:
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                # 1. مسح كل ما يتعلق بالاختبار
                await conn.execute("DELETE FROM transactions WHERE user_id >= 9000000 OR details LIKE '%[MEGA_TEST]%'")
                await conn.execute("DELETE FROM telecom_transactions WHERE client_id >= 9000000 OR package_name LIKE '%[MEGA_TEST]%'")
                await conn.execute("DELETE FROM users WHERE user_id >= 9000000")
                await conn.execute("DELETE FROM inventory WHERE card_type LIKE 'كرت_ميجا_%'")
                
                # 2. استعادة المحفظة من اللقطة المحفوظة في قاعدة البيانات
                snapshot_json = await conn.fetchval("SELECT value FROM settings WHERE key = 'wallet_snapshot_backup'")
                if snapshot_json:
                    import json
                    snap = json.loads(snapshot_json)
                    await conn.execute("""
                        UPDATE agent_wallet 
                        SET telecom_balance = $1, manager_cash = $2, telecom_cash = $3, realized_profit = $4 
                        WHERE id = 1
                    """, Decimal(snap['telecom_balance']), Decimal(snap['manager_cash']), Decimal(snap['telecom_cash']), Decimal(snap['realized_profit']))
                    
                    # مسح اللقطة بعد الاستعادة الناجحة
                    await conn.execute("DELETE FROM settings WHERE key = 'wallet_snapshot_backup'")

        await bot.send_message(ADMIN_ID, "✨ **تم التنظيف بنجاح!**\nعاد نظامك نظيفاً كما كان، وتمت استعادة أرصدتك الحقيقية بدقة متناهية.")
    except Exception as e:
        await bot.send_message(ADMIN_ID, f"❌ خطأ أثناء التنظيف: {e}")

# ================= صائد الأزرار الميتة (Unhandled Callbacks) =================
@router.callback_query()
async def unhandled_callback_catcher(callback: types.CallbackQuery, bot: Bot):
    """يلتقط أي زر ليس له دالة مبرمجة (زر ميت)"""
    await callback.answer("⚠️ عذراً، هذا الزر لا يستجيب حالياً. تم إبلاغ الإدارة.", show_alert=True)
    
    error_msg = (
        f"⚠️ **زر لا يستجيب (Dead Button)** ⚠️\n\n"
        f"👤 **المستخدم:** `{callback.from_user.id}`\n"
        f"🔘 **الزر (Callback Data):** `{callback.data}`\n\n"
        f"💡 *ملاحظة: هذا يعني أنك نسيت برمجة دالة لهذا الزر، أو أن اسم الزر مكتوب بخطأ إملائي.*"
    )
    try:
        await smart_notify(bot, ADMIN_ID, error_msg)
    except:
        pass
