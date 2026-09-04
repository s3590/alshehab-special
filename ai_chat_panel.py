import os
import asyncio
import aiohttp
import json
import re
import difflib
import random
from io import BytesIO
from aiogram import Router, F, types, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from config import ADMIN_ID, NETWORK_OWNER_ID
import database
import edge_tts
import tempfile
from decimal import Decimal, InvalidOperation

# 🌟 التعديل: استيراد المحرك المالي الجديد
from core_accounting import FinancialEngine, TxType, FinancialError

router = Router()

from datetime import datetime, timedelta
import calendar

def get_smart_dates(text: str) -> tuple[str, str]:
    """دالة ذكية لاستخراج تواريخ الجرد بدقة مع مراعاة الأشهر (28, 30, 31)"""
    now = datetime.now()
    text_clean = text.replace("إلى", "الى") # توحيد الكلمة لتسهيل البحث
    
    # حالة 1: جرد الشهر الماضي (حتى لو تأخرنا ودخلنا في شهر جديد)
    if any(word in text_clean for word in ["الماضي", "السابق", "شهر ماضي"]):
        first_day_of_current = now.replace(day=1)
        last_day_of_prev = first_day_of_current - timedelta(days=1)
        first_day_of_prev = last_day_of_prev.replace(day=1)
        return first_day_of_prev.strftime('%Y-%m-%d'), last_day_of_prev.strftime('%Y-%m-%d')
        
    # حالة 2: جرد هذا الشهر
    elif any(word in text_clean for word in ["هذا الشهر", "الحالي", "الشهر ده"]):
        first_day = now.replace(day=1)
        return first_day.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d')
        
    # حالة 3 (تحديد شهر معين، أو من شهر معين إلى اليوم)
    month_match = re.search(r'شهر\s*(\d{1,2})', text_clean)
    if month_match:
        month = int(month_match.group(1))
        if 1 <= month <= 12:
            year = now.year
            if month > now.month: # إذا طلب شهر 12 ونحن في شهر 1، يقصد السنة الماضية
                year -= 1
            
            first_day = datetime(year, month, 1)
            
            # إذا قال "إلى اليوم"
            if "الى اليوم" in text_clean or "حتى اليوم" in text_clean:
                return first_day.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d')
            # إذا قال فقط "شهر 6" (نعطيه الشهر كامل)
            else:
                import calendar
                last_day = calendar.monthrange(year, month)[1]
                end_date = datetime(year, month, last_day)
                return first_day.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')
        
    # حالة 4: تواريخ محددة يدوياً (تم التعديل هنا 🚀)
    # التعبير النمطي الجديد يدعم: 1-6-2026 و 01-06-2026 و 2026-06-01 وحتى 1/6/2026
    dates = re.findall(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4}', text_clean)
    if len(dates) >= 2:
        def parse_to_standard(date_str):
            date_str = date_str.replace('/', '-')
            # محاولة قراءة التاريخ بجميع الصيغ المحتملة وتحويله لصيغة قاعدة البيانات YYYY-MM-DD
            for fmt in ['%Y-%m-%d', '%d-%m-%Y', '%m-%d-%Y']:
                try:
                    return datetime.strptime(date_str, fmt).strftime('%Y-%m-%d')
                except ValueError:
                    continue
            return date_str

        return parse_to_standard(dates[0]), parse_to_standard(dates[1])
            
    # الافتراضي: آخر 30 يوم
    return (now - timedelta(days=30)).strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d')

async def send_delayed_reminder(bot: Bot, user_id: int, delay_seconds: int, text: str):
    """دالة لجدولة التذكيرات الذكية"""
    await asyncio.sleep(delay_seconds)
    try:
        await bot.send_message(user_id, f"🔔 **تذكير مبرمج:**\n\nيا دكتور وليد، طلبت مني أذكرك بهذا الموضوع:\n{text}")
    except: pass
    
# ================= الحالات الجديدة (FSM) =================
class AuditFlow(StatesGroup):
    waiting_for_cash = State()
    waiting_for_inventory = State()
    resolving_discrepancy = State()

class ManagerReportFlow(StatesGroup):
    step_1_previous = State()
    step_2_received_paid = State()
    step_3_expenses = State()
    step_4_net_total = State()
    step_5_details = State()
    final_approval = State()

class AgentInteractiveAudit(StatesGroup):
    waiting_for_dates = State()
    reviewing_ledger = State()
    step_1_prev = State()
    step_2_received = State()
    step_3_paid = State()
    step_4_expenses = State()
    step_5_net = State()
    step_6_details = State()
    waiting_for_correction = State()
    waiting_for_draft_approval = State()
    
class BatchProcessingFlow(StatesGroup):
    waiting_for_approval = State()
    
    
# متغيرات عامة للتحكم في حالة التقرير والجرد
report_status = {
    "is_ready": False,
    "expected_cash": Decimal('0'),
    "expected_inventory": {},
    "actual_cash": Decimal('0'),
    "actual_inventory": {},
    "meeting_minutes": []
}
# ================= الذاكرة قصيرة المدى (RAM) =================
user_memory = {}
MAX_MEMORY_LENGTH = 10

def update_memory(user_id: int, role: str, content: str):
    if user_id not in user_memory:
        user_memory[user_id] = []
        
    # 🌟 الحماية من تجاوز الذاكرة (Context Overflow): قص النص إذا تجاوز 500 حرف
    safe_content = content[:500] + "..." if len(content) > 500 else content
    
    user_memory[user_id].append({"role": role, "content": safe_content})
    if len(user_memory[user_id]) > MAX_MEMORY_LENGTH:
        user_memory[user_id].pop(0)
        

# ================= الحالات (FSM) للمساعد الصوتي =================
class SmartVoiceFlow(StatesGroup):
    waiting_for_confirmation = State()

# ================= تجميع مفاتيح Groq المتاحة =================
GROQ_API_KEYS = []
for i in range(1, 11):
    key_name = "GROQ_API_KEY" if i == 1 else f"GROQ_API_KEY_{i}"
    key_val = os.getenv(key_name)
    if key_val:
        GROQ_API_KEYS.append(key_val)

# ================= نظام إدارة المفاتيح الذكي (Round-Robin & Rate Limit) =================
import time
current_key_index = 0
key_cooldowns = {}

def get_smart_groq_keys():
    """نظام ذكي يوزع الضغط بالتساوي ويتجاوز المفاتيح المحظورة مؤقتاً"""
    global current_key_index
    now = time.time()
    
    # تصفية المفاتيح المتاحة (التي انتهت فترة حظرها)
    available_keys = [k for k in GROQ_API_KEYS if key_cooldowns.get(k, 0) < now]
    
    # إذا تم حظر كل المفاتيح، نستخدمها كلها ونحاول
    if not available_keys:
        available_keys = GROQ_API_KEYS
        
    ordered_keys = []
    for _ in range(len(available_keys)):
        ordered_keys.append(available_keys[current_key_index % len(available_keys)])
        current_key_index += 1
        
    return ordered_keys

def mark_key_rate_limited(key):
    """حظر المفتاح المستهلك لمدة 60 ثانية"""
    key_cooldowns[key] = time.time() + 60

# ================= دالة تنظيف وتوحيد الأسماء العربية =================
def normalize_arabic_name(text: str) -> str:
    if not text: return ""
    text = re.sub(r'[أإآ]', 'ا', text)
    text = text.replace('ة', 'ه')
    text = text.replace('ى', 'ي')
    text = re.sub(r'\bعبد\s+', 'عبد', text)
    text = re.sub(r'\bابو\s+', 'ابو', text)
    return text.strip()

# ================= دالة تحويل النص إلى صوت =================
async def text_to_voice(text: str) -> tuple[BytesIO, str]:
    clean_text = text.replace("*", "").replace("_", "").replace("`", "").replace("#", "").replace("▪️", "").strip()
    
    # 👈 فلتر تصحيح النطق (إضافة التشكيل الإجباري للكلمات الشائعة)
    clean_text = clean_text.replace("الله", "اللَّه")
    clean_text = clean_text.replace("سيستم", "سِيسْتِم")
    clean_text = clean_text.replace("رهيب", "رَهِيب")
    clean_text = clean_text.replace("وليد", "وَلِيد")
    
    if not clean_text:
        clean_text = "عذراً، لا يوجد نص لنطقه."

    try:
        # جلب الصوت المفضل من قاعدة البيانات (وإذا لم يوجد نضع اليمني كافتراضي)
        voice = await database.get_setting("bot_voice")
        if not voice or voice == "none" or voice == "off":
            voice = "ar-YE-SalehNeural" 
            
        communicate = edge_tts.Communicate(clean_text, voice, rate="+10%")
        
        audio_data = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data += chunk["data"]
                
        fp = BytesIO(audio_data)
        fp.seek(0)
        return fp, "mp3"

    except Exception as e:
        print(f"Edge TTS failed, falling back to gTTS. Error: {e}")
        from gtts import gTTS
        def _generate_gtts():
            tts = gTTS(text=clean_text, lang='ar', slow=False)
            fp = BytesIO()
            tts.write_to_fp(fp)
            fp.seek(0)
            return fp
            
        fp = await asyncio.to_thread(_generate_gtts)
        return fp, "ogg"

# ================= إعداد جلسة اتصال دائمة =================
groq_session = None

async def init_ai_session():
    global groq_session
    if groq_session is None:
        groq_session = aiohttp.ClientSession()
        print("✅ تم فتح جلسة اتصال دائمة مع الذكاء الاصطناعي (Groq).")

async def close_ai_session():
    global groq_session
    if groq_session:
        await groq_session.close()
        print("🛑 تم إغلاق جلسة الذكاء الاصطناعي.")

# ================= نظام العرض التفاعلي (للمدير العام) =================
@router.message(ManagerReportFlow.step_1_previous, F.from_user.id == int(NETWORK_OWNER_ID))
async def manager_step_1(message: types.Message, state: FSMContext):
    is_voice = False
    if message.voice:
        is_voice = True
        file = await message.bot.get_file(message.voice.file_id)
        downloaded_file = await message.bot.download_file(file.file_path)
        user_text = await transcribe_audio(downloaded_file.read())
    else:
        user_text = message.text or ""
        
    report_status["meeting_minutes"].append(f"المدير: {user_text}")
    
    user_text_clean = user_text.replace(".", "").replace("،", "").strip()
    negative_words = ["لا", "غلط", "خطا", "وقف", "لحظه", "راجع"]
    approval_words = ["صح", "موافق", "نعم", "اقرا", "كمل", "تمام", "مضبوط", "اوكي", "ايوه", "اعتمد", "بعده", "التالي"]
    is_approved = False
    if not any(w in user_text_clean for w in negative_words):
        if any(w in user_text_clean for w in approval_words) or len(user_text_clean.split()) <= 3:
            is_approved = True
    
    if is_approved:
        received_breakdown_text = ""
        total_received = Decimal('0.0')
        if database.pool:
            async with database.pool.acquire() as conn:
                # 👈 التعديل هنا: استخدام تواريخ التقرير المعتمد بدلاً من الشهر الحالي
                start_date_obj = report_status.get("start_date")
                end_date_obj = report_status.get("end_date")
                
                if start_date_obj and end_date_obj:
                    received_txs = await conn.fetch("SELECT type, amount, details FROM transactions WHERE type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
                else:
                    received_txs = await conn.fetch("SELECT type, amount, details FROM transactions WHERE type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)")
                
                received_breakdown_text = ""
                opening_balances_text = ""
                total_received = Decimal('0.0')
                total_opening = Decimal('0.0')
                
                received_breakdown = {}
                for tx in received_txs:
                    if tx['type'] in ('رصيد_افتتاحي', 'رصيد_افتتاحي_كاش'):
                        total_opening += Decimal(tx['amount'])
                        opening_balances_text += f"▪️ {tx['details']} ({int(abs(Decimal(tx['amount'])))} ريال)\n"
                    else:
                        total_received += Decimal(tx['amount'])
                        match = re.search(r"استلام (\d+) كرت (.+)", tx['details'])
                        if match:
                            qty = int(match.group(1))
                            ctype = match.group(2).strip()
                            if ctype not in received_breakdown:
                                received_breakdown[ctype] = {'qty': 0, 'amount': Decimal(0)}
                            received_breakdown[ctype]['qty'] += qty
                            received_breakdown[ctype]['amount'] += Decimal(tx['amount'])
                        else:
                            ctype = tx['details']
                            if ctype not in received_breakdown:
                                received_breakdown[ctype] = {'qty': 0, 'amount': Decimal(0)}
                            received_breakdown[ctype]['amount'] += Decimal(tx['amount'])
                
                if received_breakdown:
                    for ctype, data in received_breakdown.items():
                        if data['qty'] > 0:
                            received_breakdown_text += f"▪️ {ctype} | العدد: {data['qty']} ({int(abs(data['amount']))} ريال)\n"
                        else:
                            received_breakdown_text += f"▪️ {ctype} ({int(abs(data['amount']))} ريال)\n"
                else:
                    received_breakdown_text = "▪️ لا توجد كروت مستلمة\n"
                    
                if not opening_balances_text:
                    opening_balances_text = "▪️ لا توجد أرصدة افتتاحية\n"
        
        report_status["received_cards_value"] = total_received + total_opening
        
        month_str = report_status.get("month_str", "هذا الشهر")
        reply_text = f"ممتاز. **الخطوة 2 (الأرصدة والكروت لشهر {month_str}):**\n\n📦 **الأرصدة الافتتاحية (ديون سابقة):**\n{opening_balances_text}الإجمالي: {int(abs(total_opening))} ريال\n\n📦 **الكروت المستلمة:**\n{received_breakdown_text}الإجمالي: {int(abs(total_received))} ريال\n\nإجمالي المضاف لحساب الإدارة: **{int(abs(total_received + total_opening))} ريال**.\nهل ننتقل للخطوة التالية؟"
        await state.set_state(ManagerReportFlow.step_2_received_paid)
    else:
        prompt = "المدير يعترض على رصيد الشهر السابق. رد عليه بلهجة يمنية محترمة ومختصرة جداً (سطرين). طمئنه أن الرصيد مرحل بدقة، واسأله: هل ننتقل للخطوة التالية؟"
        reply_text = await generate_groq_response(prompt, message.from_user.id, user_text)
        
    report_status["meeting_minutes"].append(f"شهاب: {reply_text}")
    
    if is_voice:
        try:
            voice_buffer, file_ext = await text_to_voice(reply_text.replace("**", ""))
            voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
            await message.answer_voice(voice=voice_file, caption="🎙️ (رد شهاب)")
        except:
            await message.answer(reply_text)
    else:
        await message.answer(reply_text)

@router.message(ManagerReportFlow.step_2_received_paid, F.from_user.id == int(NETWORK_OWNER_ID))
async def manager_step_2(message: types.Message, state: FSMContext):
    is_voice = False
    if message.voice:
        is_voice = True
        file = await message.bot.get_file(message.voice.file_id)
        downloaded_file = await message.bot.download_file(file.file_path)
        user_text = await transcribe_audio(downloaded_file.read())
    else:
        user_text = message.text or ""
        
    report_status["meeting_minutes"].append(f"المدير: {user_text}")
    
    user_text_clean = user_text.replace(".", "").replace("،", "").strip()
    negative_words = ["لا", "غلط", "خطا", "وقف", "لحظه", "راجع"]
    approval_words = ["صح", "موافق", "نعم", "اقرا", "كمل", "تمام", "مضبوط", "اوكي", "ايوه", "اعتمد", "بعده", "التالي"]
    is_approved = False
    if not any(w in user_text_clean for w in negative_words):
        if any(w in user_text_clean for w in approval_words) or len(user_text_clean.split()) <= 3:
            is_approved = True
    
    if is_approved:
        paid_breakdown_text = ""
        total_paid = Decimal('0.0')
        if database.pool:
            async with database.pool.acquire() as conn:
                # 👈 التعديل هنا: استخدام تواريخ التقرير المعتمد
                start_date_obj = report_status.get("start_date")
                end_date_obj = report_status.get("end_date")
                
                if start_date_obj and end_date_obj:
                    paid_txs = await conn.fetch("SELECT amount, details FROM transactions WHERE type = 'تسديد_للشبكة' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
                else:
                    paid_txs = await conn.fetch("SELECT amount, details FROM transactions WHERE type = 'تسديد_للشبكة' AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)")
                    
                if paid_txs:
                    for tx in paid_txs:
                        total_paid += Decimal(tx['amount'])
                        paid_breakdown_text += f"▪️ {tx['details']} ({int(abs(Decimal(tx['amount'])))} ريال)\n"
                else:
                    paid_breakdown_text = "▪️ لا توجد دفعات مسجلة\n"
        
        report_status["paid_to_manager"] = total_paid
        
        reply_text = f"ممتاز. **الخطوة 3 (الدفعات المسددة للإدارة):**\n{paid_breakdown_text}\nإجمالي الدفعات المسددة: **{int(abs(total_paid))} ريال**.\nنكمل؟"
        await state.set_state(ManagerReportFlow.step_3_expenses)
    else:
        prompt = "المدير يعترض على الكروت المستلمة. رد عليه بلهجة يمنية محترمة ومختصرة جداً (سطرين). طمئنه أن الأرقام مسجلة بدقة، واسأله: هل ننتقل للخطوة التالية؟"
        reply_text = await generate_groq_response(prompt, message.from_user.id, user_text)
        
    report_status["meeting_minutes"].append(f"شهاب: {reply_text}")
    
    if is_voice:
        try:
            voice_buffer, file_ext = await text_to_voice(reply_text.replace("**", ""))
            voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
            await message.answer_voice(voice=voice_file, caption="🎙️ (رد شهاب)")
        except:
            await message.answer(reply_text)
    else:
        await message.answer(reply_text)

@router.message(ManagerReportFlow.step_3_expenses, F.from_user.id == int(NETWORK_OWNER_ID))
async def manager_step_3(message: types.Message, state: FSMContext):
    is_voice = False
    if message.voice:
        is_voice = True
        file = await message.bot.get_file(message.voice.file_id)
        downloaded_file = await message.bot.download_file(file.file_path)
        user_text = await transcribe_audio(downloaded_file.read())
    else:
        user_text = message.text or ""

    report_status["meeting_minutes"].append(f"المدير: {user_text}")
    
    user_text_clean = user_text.replace(".", "").replace("،", "").strip()
    negative_words = ["لا", "غلط", "خطا", "وقف", "لحظه", "راجع"]
    approval_words = ["صح", "موافق", "نعم", "اقرا", "كمل", "تمام", "مضبوط", "اوكي", "ايوه", "اعتمد", "بعده", "التالي"]
    is_approved = False
    if not any(w in user_text_clean for w in negative_words):
        if any(w in user_text_clean for w in approval_words) or len(user_text_clean.split()) <= 3:
            is_approved = True
    
    if is_approved:
        expenses_breakdown_text = ""
        total_expenses = Decimal('0.0')
        damaged = Decimal('0.0')
        agent_dues = Decimal('0.0')
        returned_net = Decimal('0.0')
        
        if database.pool:
            async with database.pool.acquire() as conn:
                # 👈 التعديل هنا: استخدام تواريخ التقرير المعتمد
                start_date_obj = report_status.get("start_date")
                end_date_obj = report_status.get("end_date")
                
                if start_date_obj and end_date_obj:
                    exp_txs = await conn.fetch("SELECT amount, details FROM transactions WHERE wallet_type = 'manager' AND type = 'مصروفات' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
                    dmg_val = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'كروت_تالفة' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
                    agent_val = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'نسبة_الوكيل' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
                    ret_val = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'مرتجع_للشبكة' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
                else:
                    exp_txs = await conn.fetch("SELECT amount, details FROM transactions WHERE wallet_type = 'manager' AND type = 'مصروفات' AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)")
                    dmg_val = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'كروت_تالفة' AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)")
                    agent_val = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'نسبة_الوكيل' AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)")
                    ret_val = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'مرتجع_للشبكة' AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)")
                    
                if exp_txs:
                    for tx in exp_txs:
                        total_expenses += Decimal(tx['amount'])
                        expenses_breakdown_text += f"▪️ {tx['details']} ({int(abs(Decimal(tx['amount'])))} ريال)\n"
                else:
                    expenses_breakdown_text = "▪️ لا توجد مصروفات مسجلة\n"
                
                damaged = Decimal(dmg_val) if dmg_val else Decimal('0.0')
                agent_dues = Decimal(agent_val) if agent_val else Decimal('0.0')
                returned_net = Decimal(ret_val) if ret_val else Decimal('0.0')
                
        report_status["expenses"] = total_expenses
        report_status["damaged"] = damaged
        report_status["agent_dues"] = agent_dues
        report_status["returned_net"] = returned_net
                
        agent_str = f"مستحقات الوكيل: **{int(abs(agent_dues))} ريال**.\n" if agent_dues > 0 else ""
        reply_text = f"ممتاز. **الخطوة 4 (المصروفات والتوالف):**\n{expenses_breakdown_text}\nإجمالي المصروفات: **{int(abs(total_expenses))} ريال**.\nكروت تالفة: **{int(abs(damaged))} ريال**.\n{agent_str}مرتجع للإدارة: **{int(abs(returned_net))} ريال**.\nنكمل؟"

        await state.set_state(ManagerReportFlow.step_4_net_total)
    else:
        prompt = "المدير يعترض على الدفعات المسددة للإدارة. رد عليه بلهجة يمنية محترمة ومختصرة جداً (سطرين). طمئنه أن كل التحويلات مسجلة وموثقة، واسأله: هل ننتقل للخطوة التالية؟"
        reply_text = await generate_groq_response(prompt, message.from_user.id, user_text)
        
    report_status["meeting_minutes"].append(f"شهاب: {reply_text}")
    
    if is_voice:
        try:
            voice_buffer, file_ext = await text_to_voice(reply_text.replace("**", ""))
            voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
            await message.answer_voice(voice=voice_file, caption="🎙️ (رد شهاب)")
        except:
            await message.answer(reply_text)
    else:
        await message.answer(reply_text)

@router.message(ManagerReportFlow.step_4_net_total, F.from_user.id == int(NETWORK_OWNER_ID))
async def manager_step_4(message: types.Message, state: FSMContext):
    is_voice = False
    if message.voice:
        is_voice = True
        file = await message.bot.get_file(message.voice.file_id)
        downloaded_file = await message.bot.download_file(file.file_path)
        user_text = await transcribe_audio(downloaded_file.read())
    else:
        user_text = message.text or ""
        
    report_status["meeting_minutes"].append(f"المدير: {user_text}")
    
    user_text_clean = user_text.replace(".", "").replace("،", "").strip()
    negative_words = ["لا", "غلط", "خطا", "وقف", "لحظه", "راجع"]
    approval_words = ["صح", "موافق", "نعم", "اقرا", "كمل", "تمام", "مضبوط", "اوكي", "ايوه", "اعتمد", "بعده", "التالي"]
    is_approved = False
    if not any(w in user_text_clean for w in negative_words):
        if any(w in user_text_clean for w in approval_words) or len(user_text_clean.split()) <= 3:
            is_approved = True
    
    if is_approved:
        prev = report_status.get("previous_balance", Decimal('0.0'))
        rec = report_status.get("received_cards_value", Decimal('0.0'))
        paid = report_status.get("paid_to_manager", Decimal('0.0'))
        dmg = report_status.get("damaged", Decimal('0.0'))
        exp = report_status.get("expenses", Decimal('0.0'))
        agent = report_status.get("agent_dues", Decimal('0.0'))
        ret_net = report_status.get("returned_net", Decimal('0.0'))
        
        # 👈 المعادلة الصحيحة للصافي
        net_total = (prev + rec) - (paid + dmg + exp + agent + ret_net)
        
        reply_text = f"ممتاز. **الخطوة 5:** المتبقي حالياً للإدارة (الصافي) هو: **{int(abs(net_total))} ريال**.\nننتقل لتفاصيل هذا الصافي؟"
        await state.set_state(ManagerReportFlow.step_5_details)
    else:
        prompt = "المدير يعترض على المصروفات أو التوالف أو مستحقات الوكيل. رد عليه بلهجة يمنية محترمة ومختصرة جداً (سطرين). دافع عن الوكيل واشرح أن المصروفات ضرورية للعمل، واسأله: هل ننتقل للخطوة التالية؟"
        reply_text = await generate_groq_response(prompt, message.from_user.id, user_text)
        
    report_status["meeting_minutes"].append(f"شهاب: {reply_text}")
    
    if is_voice:
        try:
            voice_buffer, file_ext = await text_to_voice(reply_text.replace("**", ""))
            voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
            await message.answer_voice(voice=voice_file, caption="🎙️ (رد شهاب)")
        except:
            await message.answer(reply_text)
    else:
        await message.answer(reply_text)

@router.message(ManagerReportFlow.step_5_details, F.from_user.id == int(NETWORK_OWNER_ID))
async def manager_step_5(message: types.Message, state: FSMContext):
    is_voice = False
    if message.voice:
        is_voice = True
        file = await message.bot.get_file(message.voice.file_id)
        downloaded_file = await message.bot.download_file(file.file_path)
        user_text = await transcribe_audio(downloaded_file.read())
    else:
        user_text = message.text or ""
        
    report_status["meeting_minutes"].append(f"المدير: {user_text}")
    
    user_text_clean = user_text.replace(".", "").replace("،", "").strip()
    negative_words = ["لا", "غلط", "خطا", "وقف", "لحظه", "راجع"]
    approval_words = ["صح", "موافق", "نعم", "اقرا", "كمل", "تمام", "مضبوط", "اوكي", "ايوه", "اعتمد", "بعده", "التالي"]
    is_approved = False
    if not any(w in user_text_clean for w in negative_words):
        if any(w in user_text_clean for w in approval_words) or len(user_text_clean.split()) <= 3:
            is_approved = True
    
    if is_approved:
        inv_value, total_debt, available_cash = Decimal('0.0'), Decimal('0.0'), Decimal('0.0')
        
        if database.pool:
            async with database.pool.acquire() as conn:
                start_date_obj = report_status.get("start_date")
                end_date_obj = report_status.get("end_date")
                
                if start_date_obj and end_date_obj:
                    # 🌟 الإصلاح المحاسبي: تضمين التحويلات ليتطابق 100% مع المطبخ المركزي
                    cash_in = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE (type IN ('تسديد_من_عميل', 'بيع_مباشر', 'رصيد_افتتاحي_كاش') OR (type = 'تحويل_رصيد' AND details LIKE 'تسوية واردة للصندوق%')) AND is_reverted = FALSE AND date <= $1::date + interval '1 day'", end_date_obj)
                    cash_out = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE (type IN ('تسديد_للشبكة', 'مصروفات', 'مرتجع_بيع_مباشر', 'كروت_تالفة', 'سحب_أرباح') OR (type = 'تحويل_رصيد' AND details LIKE 'تسوية صادرة من الصندوق%')) AND is_reverted = FALSE AND date <= $1::date + interval '1 day'", end_date_obj)
                    realized_profit = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM agent_profits WHERE date <= $1::date + interval '1 day'", end_date_obj)
                    
                    # كاش المدير = (الكاش الداخل للشبكة - الكاش الخارج للشبكة) - أرباح الوكيل
                    available_cash = (Decimal(cash_in or 0) - Decimal(cash_out or 0)) - Decimal(realized_profit or 0)
                    available_cash = max(Decimal('0.0'), available_cash)
                    
                    # 2. حساب الديون التاريخية الصافية (بدون الأرباح المعلقة وبدون ديون التسديدات)
                    debt_added = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسليم_لعميل', 'رصيد_افتتاحي') AND is_reverted = FALSE AND date <= $1::date + interval '1 day'", end_date_obj)
                    debt_removed = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسديد_من_عميل', 'مرتجع_من_عميل') AND is_reverted = FALSE AND date <= $1::date + interval '1 day'", end_date_obj)
                    pending_profit = await conn.fetchval("SELECT COALESCE(SUM(pending_profit), 0) FROM users WHERE role = 'client'")
                    
                    total_debt = (Decimal(debt_added or 0) - Decimal(debt_removed or 0)) - Decimal(pending_profit or 0)
                    
                    # 3. استنتاج قيمة المخزون لتتطابق مع الصافي
                    prev = report_status.get("previous_balance", Decimal('0.0'))
                    rec = report_status.get("received_cards_value", Decimal('0.0'))
                    paid = report_status.get("paid_to_manager", Decimal('0.0'))
                    dmg = report_status.get("damaged", Decimal('0.0'))
                    exp = report_status.get("expenses", Decimal('0.0'))
                    agent = report_status.get("agent_dues", Decimal('0.0'))
                    ret_net = report_status.get("returned_net", Decimal('0.0'))
                    
                    net_total = (prev + rec) - (paid + dmg + exp + agent + ret_net)
                    inv_value = net_total - total_debt - available_cash

                else:
                    from core_accounting import FinancialEngine
                    engine = FinancialEngine(database.pool)
                    fin_stats = await engine.get_summary()
                    inv_value = fin_stats["inv_value"]
                    total_debt = fin_stats["debt_cost"]
                    available_cash = max(Decimal('0.0'), fin_stats["cash"] - fin_stats["realized"])
        
        reply_text = f"ممتاز. **الخطوة 6 (تفاصيل الصافي):**\n▪️ إجمالي الكروت في المخزون: **{int(abs(inv_value))} ريال**\n▪️ إجمالي ديون العملاء بالسوق: **{int(abs(total_debt))} ريال**\n▪️ السيولة النقدية المتوفرة (الكاش): **{int(abs(available_cash))} ريال**.\n\nهل نعتمد التقرير النهائي؟ (نعم / لا)"
        await state.set_state(ManagerReportFlow.final_approval)
    else:
        prompt = "المدير يعترض على الصافي المتبقي للإدارة. رد عليه بلهجة يمنية محترمة ومختصرة جداً (سطرين). طمئنه أن الحسابات دقيقة ومطابقة، واسأله: هل ننتقل لتفاصيل الصافي؟"
        reply_text = await generate_groq_response(prompt, message.from_user.id, user_text)
        
    report_status["meeting_minutes"].append(f"شهاب: {reply_text}")
    
    if is_voice:
        try:
            voice_buffer, file_ext = await text_to_voice(reply_text.replace("**", ""))
            voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
            await message.answer_voice(voice=voice_file, caption="🎙️ (رد شهاب)")
        except:
            await message.answer(reply_text)
    else:
        await message.answer(reply_text)

@router.message(ManagerReportFlow.final_approval, F.from_user.id == int(NETWORK_OWNER_ID))
async def manager_final_approval(message: types.Message, state: FSMContext):
    is_voice = False
    if message.voice:
        is_voice = True
        file = await message.bot.get_file(message.voice.file_id)
        downloaded_file = await message.bot.download_file(file.file_path)
        user_text = await transcribe_audio(downloaded_file.read())
    else:
        user_text = message.text or ""
        
    report_status["meeting_minutes"].append(f"المدير: {user_text}")
    
    reply_text = "تم الاعتماد طال عمرك. الجلسة انتهت، وتقدر تسحب ملف الإكسل في أي وقت من زر (سحب التقرير) في لوحة التحكم. في أمان الله 🌹"
    
    if is_voice:
        try:
            voice_buffer, file_ext = await text_to_voice(reply_text)
            voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
            await message.answer_voice(voice=voice_file, caption="🎙️ (رد شهاب)")
        except:
            await message.answer(reply_text)
    else:
        await message.answer(reply_text)
        
    await state.clear()

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
import calendar
from datetime import datetime

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

# ================= 1. دالة تحويل الصوت إلى نص (مباشر إلى Groq) =================
async def transcribe_audio(audio_bytes: bytes) -> str:
    if not GROQ_API_KEYS:
        raise Exception("لا يوجد مفاتيح Groq في الإعدادات.")
    if not groq_session:
        await init_ai_session()
        
    # التعديل هنا: استخدام الرابط الرسمي لـ Groq
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    last_error = "Audio Error: فشل الاتصال."
    
    keys_to_try = get_smart_groq_keys( )

    for key in keys_to_try:
        headers = {
            "Authorization": f"Bearer {key}"
        }
        form = aiohttp.FormData( )
        form.add_field('file', audio_bytes, filename='voice.ogg', content_type='audio/ogg')
        form.add_field('model', 'whisper-large-v3')
        form.add_field('language', 'ar') 
        form.add_field('temperature', '0.0') # 👈 (إضافة هامة) تمنع النموذج من تأليف كلمات عند وجود ضجيج أو صمت
        
        async with groq_session.post(url, headers=headers, data=form) as response:
            if response.status == 200:
                result = await response.json()
                return result.get('text', '')
            elif response.status == 429:
                mark_key_rate_limited(key) # 🌟 هذا هو السطر السحري الذي كان ناقصاً!
                last_error = "RATE_LIMIT_AUDIO"
                continue
            else:
                err = await response.text()
                last_error = f"Audio Error {response.status}: {err}"
                continue

    raise Exception(last_error)

# ================= 2. دالة تحليل الصور (مباشر إلى Groq) =================
async def analyze_ledger_image(base64_image: str, is_network_owner: bool) -> list:
    if not GROQ_API_KEYS:
        raise Exception("لا يوجد مفاتيح Groq في الإعدادات.")
    if not groq_session:
        await init_ai_session()

    if is_network_owner:
        prompt = """أنت محاسب مالي خبير. هذه صورة لدفتر حسابات مكتوب بخط اليد.
        ملاحظة هامة: الأرقام قد تكون مكتوبة بالصيغة العربية (٠,١,٢,٣,٤,٥,٦,٧,٨,٩). قم بتحويلها إلى أرقام إنجليزية (0-9).
        استخرج العمليات المالية. الأنواع المسموحة: "استلام_من_الشبكة", "تسديد_للشبكة", "مصروفات", "كروت_تالفة". 
        أرجع البيانات بصيغة JSON تحتوي على مفتاح "transactions" بداخله مصفوفة العمليات."""
    else:
        prompt = """أنت محاسب مالي خبير ودقيق جداً. هذه صورة لدفتر حسابات عميل مكتوب بخط اليد.
        الجدول مقسم من اليمين لليسار كالتالي: (التاريخ | الفئة | العدد | السعر | الإجمالي | واصل | الباقي).
        ملاحظة هامة جداً جداً: الأرقام مكتوبة بالصيغة العربية اليدوية: (٠=0, ١=1, ٢=2, ٣=3, ٤=4, ٥=5, ٦=6, ٧=7, ٨=8, ٩=9).
        استخرج العمليات المالية بدقة متناهية وأرجعها بصيغة JSON تحتوي على مفتاح "transactions" بداخله مصفوفة العمليات."""
    
    payload = {
        "model": "llama-3.2-11b-vision-instruct",
        "messages": [
            {
                "role": "user", 
                "content": [
                    {"type": "text", "text": prompt}, 
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }
        ], 
        "temperature": 0.1,
        "response_format": {"type": "json_object"}
    }
    
    # التعديل هنا: استخدام الرابط الرسمي لـ Groq
    url = "https://api.groq.com/openai/v1/chat/completions"
    keys_to_try = GROQ_API_KEYS.copy( )
    import random
    random.shuffle(keys_to_try)
    
    last_error = "Vision Error: فشل الاتصال."
    for key in keys_to_try:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        async with groq_session.post(url, headers=headers, json=payload) as resp:
            if resp.status == 200:
                res_json = await resp.json()
                ai_text = res_json['choices'][0]['message']['content']
                try:
                    # 🌟 منظف الـ Markdown: إزالة أي علامات زائدة يكتبها الذكاء الاصطناعي
                    clean_json = re.sub(r'```(?:json)?', '', ai_text).strip()
                    extracted_data = json.loads(clean_json)
                    return extracted_data.get("transactions", [])
                except Exception:
                    raise Exception(f"الذكاء الاصطناعي أرجع بيانات غير صالحة:\n{ai_text}")
            elif resp.status == 429:
                mark_key_rate_limited(key) # 🌟 إيقاف المفتاح لمدة دقيقة
                last_error = "RATE_LIMIT_VISION"
                continue
            else:
                err = await resp.text()
                last_error = f"Vision Error {resp.status}: {err}"
                continue
    raise Exception(last_error)

# ================= 3. دالة المحادثة والذكاء الاصطناعي (الحل النهائي الذكي) =================

# متغير لحفظ اسم النموذج المتاح حتى لا نسأل السيرفر في كل رسالة
active_groq_model = None

async def get_best_groq_model(api_key: str) -> str:
    """دالة ذكية تجلب النماذج المسموحة لمفتاحك من Groq وتختار أفضلها تلقائياً"""
    global active_groq_model
    if active_groq_model:
        return active_groq_model
        
    url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    
    try:
        async with groq_session.get(url, headers=headers ) as response:
            if response.status == 200:
                data = await response.json()
                available_models = [model["id"] for model in data.get("data", [])]
                
                # قائمة بأفضل النماذج بالترتيب (من الأقوى للأسرع)
                preferred_models = [
                    "llama-3.3-70b-versatile",
                    "llama-3.1-70b-versatile",
                    "llama-3.1-8b-instant",
                    "llama3-8b-8192",
                    "mixtral-8x7b-32768",
                    "gemma2-9b-it"
                ]
                
                for pref in preferred_models:
                    if pref in available_models:
                        active_groq_model = pref
                        return active_groq_model
                        
    except Exception as e:
        pass
        
    # 🌟 الإجبار الصارم: إذا لم يجد النماذج المفضلة، يستخدم هذا النموذج المضمون دائماً
    # ولن يختار أي نموذج عشوائي من القائمة
    return "llama-3.1-8b-instant"

async def generate_groq_response(system_prompt: str, user_id: int, custom_user_text: str = None) -> str:
    if not GROQ_API_KEYS:
        raise Exception("لا يوجد مفاتيح Groq في الإعدادات.")
    if not groq_session:
        await init_ai_session()
        
    url = "https://api.groq.com/openai/v1/chat/completions"
    messages = [{"role": "system", "content": system_prompt}]
    
    if custom_user_text:
        messages.append({"role": "user", "content": custom_user_text}  )
    else:
        if user_id in user_memory:
            messages.extend(user_memory[user_id])
    
    keys_to_try = get_smart_groq_keys()
    
    # 🌟 التعديل الذكي: تحديد سقف الكلمات (150 للعميل، 1024 للإدارة)
    max_tok = 1024 if user_id in [ADMIN_ID, int(NETWORK_OWNER_ID)] else 150
    
    last_error = ""
    for key in keys_to_try:
        best_model = await get_best_groq_model(key)
        
        data = {
            "model": best_model,
            "messages": messages,
            "temperature": 0.1,    # 👈 (أقوى ضبط) تقليل الإبداع لأقصى حد لضمان الطاعة العمياء وعدم التأليف
            "top_p": 0.85,         # 👈 (أقوى ضبط) منع الذكاء الاصطناعي من استخدام كلمات عشوائية أو الخروج عن الموضوع
            "max_tokens": max_tok  # 👈 إضافة الكمامة هنا لحماية تليجرام والرصيد
        }
        
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        async with groq_session.post(url, headers=headers, json=data) as response:
            raw_text = await response.text()
            
            if response.status == 200:
                try:
                    import json
                    result = json.loads(raw_text)
                    ai_reply = result['choices'][0]['message']['content']
                    if not custom_user_text:
                        update_memory(user_id, "assistant", ai_reply)
                    return ai_reply
                except Exception as e:
                    raise Exception(f"السيرفر رد بنص غير مفهوم.\nالرد: {raw_text[:200]}")         
            elif response.status == 429:
                mark_key_rate_limited(key) # 🌟 إيقاف المفتاح لمدة دقيقة

                last_error = "تم تجاوز الحد المسموح (Rate Limit) لهذا المفتاح."
                continue
            elif response.status in [400, 404]:
                global active_groq_model
                active_groq_model = None # تفريغ النموذج للبحث عن غيره
                last_error = f"النموذج {best_model} غير مدعوم."
                continue
            else:
                last_error = f"خطأ {response.status}: {raw_text[:200]}"
                continue
                
    raise Exception(f"فشلت كل المحاولات. آخر خطأ: {last_error}")

# ================= دالة التوجيه الخفي (System Prompt) المدمجة =================
async def get_system_prompt(user_id: int) -> tuple[str, str]:
    client_name = "عميل/زائر"
    learned_knowledge = await database.get_all_learned_facts()
    knowledge_section = f"\n🧠 **معلومات إضافية تعلمتها ويجب أن تلتزم بها دائماً:**\n{learned_knowledge}\n" if learned_knowledge else ""
    current_date = datetime.now().strftime('%Y-%m-%d')

    # 👈 القواعد الصارمة المدمجة (من الكودين)
    strict_rule = f"""
    🚨 **قواعد صارمة جداً:** 
    0. **تاريخ اليوم هو:** {current_date}. اعتمد عليه دائماً في أي حسابات أو تواريخ.
    1. تحدث باللغة العربية فقط (واللهجة اليمنية). ممنوع منعاً باتاً استخدام أي كلمات صينية، روسية، أو إنجليزية.
    2. إياك أن تقول "أنا ذكاء اصطناعي" أو "لا أستطيع". أنت اسمك (سيستم)، ومهمتك إدارة حسابات شبكة الشهاب نت.
    3. **الدقة الرياضية المطلقة (تحذير شديد):** إياك ثم إياك أن تقوم بحساب الأرقام أو الكاش من رأسك بناءً على المحادثات السابقة! عندما تُسأل عن الكاش أو الديون أو المخزون، اقرأ الرقم الموجود في قسم (بيانات التقرير الشهري والمالية) أسفل هذه الرسالة كما هو بالضبط حرفياً دون أي زيادة أو نقصان.
    4. **التشكيل الصوتي (هام جداً):** يجب عليك تشكيل الكلمات العربية في ردودك النصية لكي ينطقها المولد الصوتي بشكل صحيح. خصوصاً لفظ الجلالة (اللَّه).
    5. **الفرق بين العدد والمبلغ:** إذا سألك عن (عدد الكروت) أعطه الكمية. وإذا سألك عن (المبلغ) أعطه القيمة بالريال.
    6. **التأكيد اللفظي:** إذا طلب منك تنفيذ عملية مالية قل "تم تنفيذ العملية بنجاح". لا تقل "تم الحفظ" أبداً إلا إذا قال لك المستخدم صراحة "احفظ هذه المعلومة".
    7. **فهم التواريخ بذكاء مطلق (هام جداً):** عندما يطلب منك الوكيل تقريراً، جرداً، أو كشف حساب ويحدد لك فترة (مثال: "من 1-6-2026 الى 11-6-2026" أو "من بداية شهر 6 الى اليوم")، يجب عليك فهم التاريخين وتحويلهما فوراً إلى صيغة (YYYY-MM-DD) ووضعها داخل الأقواس المخصصة للأوامر. إذا قال "إلى اليوم"، استخدم تاريخ اليوم ({current_date}).
    8. **الصدق المطلق ومنع التأليف (تحذير أمني):** إياك أن تدعي أنك قمت بتنفيذ عملية (مثل إضافة عميل، حذف عميل، تعديل سعر، أو أي شيء آخر) إذا لم تكن تملك الكود السري (التاج) الخاص بها في قائمة ميزاتك بالأسفل. أنت لا تملك صلاحية إضافة أو حذف العملاء. لأي طلب خارج صلاحياتك المكتوبة، اعتذر بلباقة وقل: "عذراً يا دكتور، لا أملك صلاحية تنفيذ هذا الأمر آلياً، يرجى تنفيذه يدوياً من أزرار لوحة التحكم".
    """

    stats = ""
    telecom_section = "" # 🌟 متغير جديد خاص بك فقط

    if user_id == ADMIN_ID or user_id == int(NETWORK_OWNER_ID):
        if database.pool:
            async with database.pool.acquire() as conn:
                # الاستعلام الشامل للأرقام (معزول تماماً عن التسديدات لحماية الوكيل)
                query = """
                SELECT 
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND date < date_trunc('month', CURRENT_DATE)) AS received_before,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('تسديد_للشبكة', 'مصروفات', 'كروت_تالفة', 'نسبة_الوكيل', 'مرتجع_للشبكة') AND date < date_trunc('month', CURRENT_DATE)) AS paid_before,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'استلام_من_الشبكة' AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)) AS received_cards_this_month,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)) AS opening_balances_this_month,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'تسديد_للشبكة' AND date >= date_trunc('month', CURRENT_DATE)) AS payments_this_month,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'مصروفات' AND date >= date_trunc('month', CURRENT_DATE)) AS expenses_this_month,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'كروت_تالفة' AND date >= date_trunc('month', CURRENT_DATE)) AS damaged_cards,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'نسبة_الوكيل' AND date >= date_trunc('month', CURRENT_DATE)) AS agent_commission,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'مرتجع_للشبكة' AND date >= date_trunc('month', CURRENT_DATE)) AS returned_to_network,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'تسديد_من_عميل' AND is_reverted = FALSE) AS total_collected,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'بيع_مباشر' AND is_reverted = FALSE) AS total_direct,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('تسديد_للشبكة', 'مصروفات', 'مرتجع_بيع_مباشر', 'سحب_أرباح', 'كروت_تالفة') AND is_reverted = FALSE) AS total_paid_cash,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'قيد_عكسي') AS total_reverted,
                    (SELECT COALESCE(SUM(amount), 0) FROM agent_profits) AS total_realized_profit,
                    (SELECT COALESCE(SUM(debt), 0) FROM users WHERE role = 'client') AS total_clients_debt,
                    (SELECT COALESCE(SUM(quantity * cost_price), 0) FROM inventory) AS agent_inventory_value,
                    (SELECT COALESCE(SUM(ci.quantity * i.cost_price), 0) FROM client_inventory ci JOIN inventory i ON ci.card_type = i.card_type) AS clients_unsold_value,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'تسليم_لعميل' AND is_reverted = FALSE AND date >= CURRENT_DATE) AS wholesale_today,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'بيع_مباشر' AND is_reverted = FALSE AND date >= CURRENT_DATE) AS retail_today,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'تسديد_من_عميل' AND is_reverted = FALSE AND date >= CURRENT_DATE) AS collected_today,
                    (SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('مصروفات', 'تسديد_للشبكة') AND is_reverted = FALSE AND date >= CURRENT_DATE) AS expenses_today
                """
                data = await conn.fetchrow(query)
                
                inv_items = await conn.fetch("SELECT card_type, quantity FROM inventory WHERE quantity > 0")
                inv_text = "\n".join([f"- {i['card_type']}: {i['quantity']} كرت" for i in inv_items]) if inv_items else "المخزون فارغ حالياً"

                sold_txs = await conn.fetch("SELECT details FROM transactions WHERE type IN ('تسليم_لعميل', 'بيع_مباشر', 'مبيعات_بقالة') AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)")
                sold_cards = {}
                total_cards_sold = 0
                for tx in sold_txs:
                    matches = re.findall(r"(\d+)\s*كرت\s*([^\+]+)", tx['details'])
                    for match in matches:
                        qty = int(match[0])
                        ctype = match[1].replace("كاش", "").replace("طياري", "").replace("جملة", "").strip()
                        sold_cards[ctype] = sold_cards.get(ctype, 0) + qty
                        total_cards_sold += qty
                sold_text = "\n".join([f"- {ctype}: {qty} كرت" for ctype, qty in sold_cards.items()]) if sold_cards else "لم يتم بيع أي كروت هذا الشهر"

                clients_data = await conn.fetch("SELECT name, debt FROM users WHERE role = 'client' ORDER BY debt DESC")
                clients_text = "\n".join([f"- {c['name']}: دينه {int(c['debt'])} ريال" for c in clients_data]) if clients_data else "لا يوجد عملاء"

                if user_id == int(NETWORK_OWNER_ID):
                    recent_txs = await conn.fetch("SELECT type, amount, details, u.name FROM transactions t LEFT JOIN users u ON t.user_id = u.user_id WHERE t.is_reverted = FALSE AND t.wallet_type = 'manager' ORDER BY t.date DESC LIMIT 10")
                else:
                    recent_txs = await conn.fetch("SELECT type, amount, details, u.name FROM transactions t LEFT JOIN users u ON t.user_id = u.user_id WHERE t.is_reverted = FALSE ORDER BY t.date DESC LIMIT 10")
                recent_txs_text = "\n".join([f"- {tx['type'].replace('_', ' ')}: {int(tx['amount'])} ريال | {tx['details']} | لـ: {tx['name'] or 'الإدارة/طياري'}" for tx in recent_txs]) if recent_txs else "لا توجد عمليات حديثة"

                if data:
                    previous_debt = Decimal(data['received_before']) - Decimal(data['paid_before'])
                    total_owed = previous_debt + Decimal(data['received_cards_this_month']) + Decimal(data['opening_balances_this_month'])
                    net_remaining = total_owed - Decimal(data['payments_this_month']) - Decimal(data['expenses_this_month']) - Decimal(data['damaged_cards']) - Decimal(data['agent_commission']) - Decimal(data['returned_to_network'])
                    
                    from core_accounting import FinancialEngine
                    engine = FinancialEngine(database.pool)
                    fin_stats = await engine.get_summary()
                    
                    available_cash = fin_stats["cash"]
                    telecom_cash = fin_stats.get("telecom", Decimal('0.0'))
                    
                    # 🌟 جلب مفتاح التشغيل الرئيسي
                    is_telecom_active = await conn.fetchval("SELECT value FROM settings WHERE key = 'is_telecom_active'")
                    is_telecom_active = is_telecom_active or 'off'

                    # 🌟 تعليم الـ AI عن قسم التسديدات (يتم حفظها في متغير مستقل)
                    telecom_profit = await database.get_telecom_total_profit()
                    agent_telecom_balance = await conn.fetchval("SELECT telecom_balance FROM agent_wallet WHERE id = 1")
                    agent_telecom_balance = Decimal(agent_telecom_balance) if agent_telecom_balance else Decimal('0.0')
                    telecom_clients_debt = await conn.fetchval("SELECT COALESCE(SUM(telecom_debt), 0) FROM users WHERE role = 'client'")
                    telecom_clients_debt = Decimal(telecom_clients_debt or 0)
                    
                    total_drawer_cash = available_cash + telecom_cash 
                    realized_profit = fin_stats["realized"]
                    
                    gm_actual_cash = max(Decimal('0.0'), available_cash - realized_profit)
                    required_to_remit = gm_actual_cash
                    
                    total_clients_debt = fin_stats["debt_market"]
                    pending_profits = fin_stats["pending"]
                    net_market_debt = fin_stats["debt_cost"] 
                    
                    clients_unsold_value = Decimal(data['clients_unsold_value'])
                    actual_dead_debt = total_clients_debt - clients_unsold_value             

                    agent_line = f"6. خصم مستحقات الوكيل: {int(data['agent_commission'])} ريال." if Decimal(data['agent_commission']) > 0 else ""

                    if user_id == ADMIN_ID:
                        if is_telecom_active == 'on':
                            cash_instructions = f"""
                            💰 **الوضع المالي للصندوق الآن (هام جداً):**
                            - إجمالي الكاش الفعلي في الدرج (شبكة + تسديدات): {int(total_drawer_cash)} ريال.
                            - تفصيل الكاش: (كاش الشبكة: {int(available_cash)} ريال | كاش التسديدات: {int(telecom_cash)} ريال).
                            - أرباح الوكيل الجاهزة (حقه الشخصي): {int(realized_profit)} ريال.
                            - الصافي المطلوب توريده للمدير: {int(required_to_remit)} ريال.
                            
                            🚨 **تعليمات صارمة جداً عند الإجابة عن الكاش:**
                            إذا سألك الوكيل (كم الكاش المتوفر؟) أو (كم أورد للمدير؟)، يجب أن تجيبه بالتفصيل الدقيق:
                            (الكاش الإجمالي في درجك هو كذا، منه كذا ريال كاش شبكة وكذا تسديدات. أرباحك الجاهزة هي كذا، والمبلغ الصافي الذي يجب توريده للمدير هو كذا ريال).
                            """
                            
                            telecom_section = f"""
                            📱 **قسم الخدمات الإلكترونية (التسديدات - خاص بك فقط):**
                            - رصيد البوابة الحالي (أم دراهم): {int(agent_telecom_balance)} ريال.
                            - أرباح التسديدات الصافية: {int(telecom_profit)} ريال.
                            - ديون البقالات من التسديدات (سلف): {int(telecom_clients_debt)} ريال.
                            """
                        else:
                            cash_instructions = f"""
                            💰 **الوضع المالي للصندوق الآن (هام جداً):**
                            - الكاش الصافي للشبكة المتوفر مع الوكيل: {int(required_to_remit)} ريال.
                            - أرباح الوكيل الجاهزة (حقه الشخصي): {int(realized_profit)} ريال.
                            
                            🚨 **تعليمات صارمة جداً عند الإجابة عن الكاش:**
                            أجب بالرقم الصافي الخاص بالشبكة وأرباح الوكيل فقط.
                            """
                            telecom_section = "" # إخفاء قسم التسديدات تماماً لأن المفتاح مغلق
                    else:
                        cash_instructions = f"""
                        💰 **الوضع المالي للشبكة (هام جداً):**
                        - الكاش الصافي للشبكة المتوفر مع الوكيل: {int(required_to_remit)} ريال.
                        
                        🚨 **تعليمات صارمة جداً عند الإجابة عن الكاش:**
                        إذا سألك المدير (كم الكاش المتوفر؟)، يجب أن تعطيه الرقم الصافي الخاص بالشبكة فقط وتقول:
                        (يا شيخ رهيب، الكاش الصافي الخاص بالشبكة والمتوفر حالياً مع الوكيل هو {int(required_to_remit)} ريال).
                        يمنع منعاً باتاً أن تذكر أرباح الوكيل للمدير أو تتحدث وكأنك الوكيل.
                        """
                        telecom_section = "" # 🌟 المدير لا يرى هذا القسم أبداً

                    stats = f"""
                    📊 **بيانات التقرير الشهري والمالية:**
                    1. المبلغ المتبقي من الشهر السابق: {int(previous_debt)} ريال.
                    2. إجمالي الأرصدة الافتتاحية (ديون سابقة): {int(data['opening_balances_this_month'])} ريال.
                    3. إجمالي الكروت المستلمة هذا الشهر: {int(data['received_cards_this_month'])} ريال.
                    3. إجمالي الدفعات المسددة للإدارة: {int(data['payments_this_month'])} ريال.
                    4. خصم كروت تالفة: {int(data['damaged_cards'])} ريال.
                    5. مصروفات وحوالات: {int(data['expenses_this_month'])} ريال.
                    {agent_line}
                    7. **المتبقي حالياً للإدارة (الصافي): {int(net_remaining)} ريال.**
                    
                    🔍 **تفاصيل المبلغ المتبقي للإدارة (أين يتوزع الصافي؟):**
                    - إجمالي الكروت الحالية في المخزون: {int(data['agent_inventory_value'])} ريال.
                    - إجمالي ديون العملاء بالسوق (الصافي): {int(net_market_debt)} ريال.
  
                    {cash_instructions}
                    
                    📦 **تفاصيل المخزون الحالي:**\n{inv_text}
                    📈 **مبيعات الكروت (هذا الشهر):** إجمالي {total_cards_sold} كرت.\n{sold_text}
                    👥 **قائمة العملاء وديونهم:**\n{clients_text}
                    🔄 **آخر 10 عمليات:**\n{recent_txs_text}
                    
                    🚨 **ميزة توضيح حركة الديون (دوران رأس المال) - هام جداً:**
                    إذا سألك المدير عن ديون السوق، اشرح له فوراً:
                    "يا شيخ رهيب، الأرقام التي تراها ليست ديوناً متراكمة! الدكتور وليد يقوم بالتحصيل وتسليم كروت جديدة للشهر الجديد في نفس الوقت.
                    للتوضيح بالأرقام الحية الآن:
                    - إجمالي الديون المسجلة: {int(total_clients_debt)} ريال.
                    - منها بضاعة (كروت جديدة) موجودة الآن في أدراج البقالات لم تُبع بعد بقيمة: {int(clients_unsold_value)} ريال.
                    - إذن، الدين الفعلي (كروت تم بيعها ولم يُسدد ثمنها بعد) هو: {int(actual_dead_debt)} ريال فقط!"
                
                    📈 **حركة اليوم (للملخص اليومي السريع):**
                    - مبيعات الجملة اليوم: {int(data['wholesale_today'])} ريال.
                    - مبيعات الكاش (طياري) اليوم: {int(data['retail_today'])} ريال.
                    - التحصيلات (سداد الديون) اليوم: {int(data['collected_today'])} ريال.
                    - المصروفات اليوم: {int(data['expenses_today'])} ريال.
                    """     
    if user_id == ADMIN_ID:
        system_prompt = f"""
        {strict_rule}
        أنت (سيستم)، المساعد الشخصي، المحاسبي، والتقني فائق الذكاء الخاص بـ (الدكتور وليد مهدي - وكيل شبكة الشهاب نت).
        أنت أيضاً مهندس برمجيات محترف. إذا طلب منك كتابة كود برمجي أو حل مشكلة تقنية، قم بكتابة الكود فوراً وباحترافية عالية.
  
        🗣️ **شخصيتك وأسلوبك:**
        - ناده دائماً بـ "دكتور وليد" أو "يا هندسة".
        - 🛑 **قاعدة الاختصار (خط أحمر):** كن ودوداً ولكن باختصار شديد جداً. لا تكتب مقدمات طويلة. أعطه الزبدة والأرقام مباشرة.
        - تحدث بلهجة يمنية بيضاء وعملية.
        - **الملخص الصوتي:** إذا طلب "ملخص اليوم"، اقرأ له (حركة اليوم) والكاش المتوفر بنقاط سريعة ومباشرة.
        
        🚨 **ميزة التذكير الذكي (من الكود الثاني):**
        إذا طلب منك تذكيره بشيء، احسب الوقت المطلوب بالثواني بناءً على الوقت الحالي، واستخرج نص التذكير، ورد بهذه الصيغة السرية فقط: [تذكير|عدد_الثواني|نص_التذكير]

        🚨 **ميزة الزيارة الميدانية:** [زيارة_ميدانية|اسم_العميل]
        🚨 **ميزة المحاسب الصوتي والنصي:** [تنفيذ_تسديد|اسم_العميل|المبلغ]
        🚨 **ميزة الإدخال المجمع (Batch Processing):** [مسودة_مجمعة|[{{ "action": "collect", "client": "اسم العميل", "amount": 5000 }}]]
        🚨 **ميزة التحويل الشامل (كاش أو كروت):** [تحويل_شامل|اسم_المرسل|اسم_المستقبل|نوع_التحويل|الكمية_أو_المبلغ|فئة_الكرت]
        🚨 **ميزة تسديد المدير العام:** [تسديد_للمدير|المبلغ]
        🚨 **ميزة استلام كروت من الإدارة:** [استلام_كروت|الكمية|الفئة]
        🚨 **ميزة تسجيل المصروفات:** [تسجيل_مصروف|المبلغ|التفاصيل]
        🚨 **ميزة إضافة دين على عميل:** [إضافة_دين|اسم_العميل|المبلغ]
        🚨 **ميزة تغيير الصوت:** [تغيير_الصوت|كود_الصوت]
        🧠 **ميزة التعلم الذاتي:** [حفظ_معلومة|المعلومة]
        🚨 **ميزة المراسلة الذكية:** [مراسلة_المدير|نص الرسالة]
        🚨 **ميزة كشف الحساب المفصل:** [كشف_مفصل|اسم_العميل|تاريخ_البداية|تاريخ_النهاية]
        🚨 **ميزة استخراج تقرير مالي (إكسل):** [تقرير_اكسل|تاريخ_البداية|تاريخ_النهاية]
        🚨 **ميزة تقير التسديدات السري (خاص بك):** [تقرير_التسديدات|تاريخ_البداية|تاريخ_النهاية]
        🚨 **ميزة التنبؤ بالاحتياج:** [تنبؤ_احتياج|اسم_الفئة|عدد_الأشهر]
        🚨 **ميزة تحليل مبيعات عميل:** [تحليل_عميل|اسم_العميل]
        🚨 **ميزة المطابقة الصوتية:** [مطابقة_صوتية|اسم_العميل|إجمالي_التسديدات|إجمالي_المسحوبات]
        🚨 **ميزة بدء مطابقة الدفاتر:** [بدء_مطابقة_العملاء]
        🚨 **ميزة تحليل كل العملاء:** [تحليل_كل_العملاء]

        {stats}
        {telecom_section}
        {knowledge_section}
        """
    elif user_id == int(NETWORK_OWNER_ID):
        s_cash = await database.get_setting('share_available_cash')
        s_debt = await database.get_setting('share_market_debt')
        
        # --- الحارس الذكي للإغلاق التلقائي للتقرير ---
        s_report = 'off'
        if database.pool:
            async with database.pool.acquire() as conn:
                s_report_db = await conn.fetchval("SELECT value FROM settings WHERE key = 'share_monthly_report'")
                if s_report_db == 'on':
                    last_report_date = await conn.fetchval("SELECT created_at FROM reports_archive ORDER BY created_at DESC LIMIT 1")
                    if last_report_date:
                        if (datetime.now() - last_report_date).days >= 3:
                            await conn.execute("UPDATE settings SET value = 'off' WHERE key = 'share_monthly_report'")
                            s_report = 'off'
                        else:
                            s_report = 'on'
                    else:
                        await conn.execute("UPDATE settings SET value = 'off' WHERE key = 'share_monthly_report'")
                        s_report = 'off'
        
        if s_cash == 'off': stats = stats.replace(f"- السيولة النقدية المتوفرة (الكاش)", "- السيولة النقدية المتوفرة (الكاش): (الوكيل أقفل الصندوق للمراجعة)")
        if s_debt == 'off': stats = stats.replace(f"- إجمالي ديون العملاء بالسوق", "- إجمالي ديون العملاء بالسوق: (غير مصرح بعرضها حالياً)")

        if s_report == 'off':
            stats = """
            ⚠️ **تنبيه أمني صارم للذكاء الاصطناعي:** 
            التقرير الشهري غير معتمد حالياً (لا يزال في مرحلة الجرد مع الوكيل). 
            يمنع منعاً باتاً إعطاء المدير العام أي أرقام تخص التقرير الشهري، الصافي، أو الحسابات. 
            إذا طلبها، اعتذر له بلباقة وأخبره أن التقرير قيد التجهيز والمراجعة مع الدكتور وليد، وسيتم إشعاره فور اعتماده.
            """

        system_prompt = f"""
        {strict_rule}
        أنت (سيستم)، المستشار المالي، الإداري، والتقني لشبكة الشهاب نت. تتحدث مع المؤسس والمدير العام (الشيخ رهيب / أبو شهاب).
        
        👑 **شخصيتك وأسلوبك مع المدير العام:**
        - ناده بـ (يا شيخ رهيب) أو (يا أبو شهاب) مع الاحترام التام.
        - 🛑 **قاعدة الاختصار (خط أحمر):** وقت المدير ثمين. أجب باختصار شديد وفي صلب الموضوع. تجنب المقدمات الإنشائية الطويلة.
        - تحدث بلهجة يمنية بيضاء، طبيعية، وعملية.
        
        📑 **مناقشة التقرير الشهري:**
        - عندما يطلب منك قراءة التقرير، قم بسرد الأرقام بنفس الترتيب الموجود في (بيانات التقرير الشهري) خطوة بخطوة.
        - بعد الانتهاء، أضف: "بالمناسبة يا شيخ رهيب، تفاصيل هذه الأرقام موجودة بالكامل في التقرير الشهري، وتقدر تسحب ملف الإكسل في أي وقت من زر (سحب التقرير) في لوحة التحكم."
        
        🚨 **ميزة المراسلة الذكية:** [مراسلة_الوكيل|نص الرسالة]
        🚨 **ميزة كشف الحساب المفصل:** [كشف_مفصل|اسم_العميل|تاريخ_البداية|تاريخ_النهاية]
        🚨 **ميزة التنبؤ بالاحتياج:** [تنبؤ_احتياج|اسم_الفئة|عدد_الأشهر]
        🚨 **ميزة تحليل مبيعات عميل:** [تحليل_عميل|اسم_العميل]
        🚨 **ميزة تحليل كل العملاء:** [تحليل_كل_العملاء]

        🚨 ميزة المستشار المالي الصوتي:
        إذا سألك المدير "كيف وضع السوق؟" أو "انصحني"، قم بقراءة الأرقام وأعطه 3 نصائح إدارية سريعة ومباشرة بناءً عليها.
        
        {stats}
        {telecom_section}
        {knowledge_section}
        """        
    else:
        client_debt = Decimal('0.0')
        is_registered = False
        if database.pool:
            async with database.pool.acquire() as conn:
                user_row = await conn.fetchrow("SELECT name, debt FROM users WHERE user_id = $1", user_id)
                if user_row:
                    client_name = user_row['name']
                    client_debt = Decimal(user_row['debt'])
                    is_registered = True

        if is_registered:
            system_prompt = f"""
            {strict_rule}
            أنت (سيستم)، النظام الآلي لخدمة عملاء شبكة الشهاب نت. تتحدث مع العميل: {client_name}. دينه الحالي: {int(client_debt)} ريال.
            
            ⚠️ **قواعد الرد (إجباري وخط أحمر):**
            1. الرد يجب أن يكون قصيراً جداً (سطر واحد فقط). إذا قال "مرحبا" أو "السلام عليكم" قل "حياك الله يا غالي، كيف أقدر أخدمك؟" فقط!
            2. تحدث بلهجة يمنية محببة وطبيعية.
            3. أجب على قدر السؤال فقط. لا تتبرع بأي معلومات إضافية لم يطلبها العميل.
            4. 🚨 حماية: ممنوع إعطاء أي وعود مالية أو تأكيد تسديد ديون من تلقاء نفسك.
            
            🚨 **ميزة الجرد الآلي:** [تحديث_مخزون|اسم_الفئة:الكمية]
            🚨 **ميزة المراسلة الذكية:** [مراسلة_الوكيل|نص الرسالة]
            
            معلومات سرية لك (ممنوع ذكرها أبداً إلا إذا سألك العميل عنها مباشرة):
            - الكروت ورقية وتُسلم يداً بيد.
            - إذا أبلغ عن كرت تالف، قل له سيتم فحصه وتعويضه، واكتب في نهاية ردك: [كرت_تالف]
            {knowledge_section}
            """
        else:
            system_prompt = f"""
            {strict_rule}
            أنت (سيستم)، المساعد الذكي لخدمة عملاء شبكة الشهاب نت. تتحدث مع زبون جديد.
            
            ⚠️ **قواعد الرد (إجباري وخط أحمر):**
            1. الرد يجب أن يكون قصيراً جداً (سطر واحد فقط). إذا قال "مرحبا" أو "السلام عليكم" قل "حياك الله، كيف أقدر أخدمك؟" فقط!
            2. تحدث بلهجة يمنية لطيفة.
            3. 🚨 إياك أن تذكر أرقام التواصل، أو الموقع، أو طريقة البيع في ردك الترحيبي!
            4. أجب على قدر السؤال فقط. لا تتبرع بمعلومات لم يطلبها الزبون.
            5. 🚨 حماية: ممنوع إعطاء أي وعود مالية.
            
            معلومات سرية لك (ممنوع منعاً باتاً ذكرها إلا إذا سأل عنها الزبون حرفياً):
            - طريقة البيع: نبيع الكروت الورقية يداً بيد فقط.
            - الموقع: قرية البلس.
            - أرقام التواصل: 711843112 - 777914318.
            {knowledge_section}
            """
            
    return system_prompt, client_name
          
# ================= دالة معالجة الأوامر الذكية للعملاء والزوار =================
async def process_client_tags(message: types.Message, bot: Bot, user_id: int, client_name: str, reply_text: str) -> str:
    clean_reply = reply_text
    
    if "[طلب_ترقية]" in reply_text:
        upgrade_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌟 ترقية إلى عميل جملة (بقالة)", callback_data=f"upgrade_client_{user_id}")]
        ])
        try:
            await bot.send_message(
                ADMIN_ID, 
                f"🔔 **طلب ترقية من زائر!**\n👤 الاسم: {message.from_user.first_name}\n💬 يقول: {message.text}\n\n*(اضغط الزر أدناه لفتح حساب موزع له)*",
                reply_markup=upgrade_kb
            )
        except: pass

    if "[طلب_كروت|" in reply_text:
        try:
            order_details = reply_text[reply_text.find("[طلب_كروت|")+10 : reply_text.find("]", reply_text.find("[طلب_كروت|"))]
            await bot.send_message(ADMIN_ID, f"🛒 **طلب كروت (عبر الذكاء الاصطناعي):**\n👤 العميل: {client_name}\n📦 الطلب: {order_details}")
        except: pass

    if "[تجهيز_كاش|" in reply_text:
        try:
            amount_str = reply_text[reply_text.find("[تجهيز_كاش|")+11 : reply_text.find("]", reply_text.find("[تجهيز_كاش|"))]
            amount = Decimal(re.sub(r'[^\d.]', '', amount_str))
            if database.pool:
                async with database.pool.acquire() as conn:
                    await conn.execute("UPDATE users SET promised_payment = $1 WHERE user_id = $2", amount, user_id)
            await bot.send_message(ADMIN_ID, f"💸 **إشعار كاش جاهز (عبر الذكاء الاصطناعي):**\n👤 العميل: {client_name}\n💵 المبلغ: {int(amount)} ريال")
        except: pass

    if "[كرت_تالف]" in reply_text:
        try:
            await bot.send_message(ADMIN_ID, f"⚠️💳 **بلاغ كرت تالف!**\n👤 العميل: {client_name}\n💬 يقول: {message.text}")
        except: pass

    if "[تحديث_مخزون|" in reply_text:
        try:
            content = reply_text[reply_text.find("[")+1 : reply_text.find("]")]
            parts = content.split("|")[1:]
            
            total_sold_value = Decimal('0.0')
            sold_details = []
            
            from core_accounting import FinancialEngine
            engine = FinancialEngine(database.pool)
            
            if database.pool:
                async with database.pool.acquire() as conn:
                    for part in parts:
                        if ":" not in part: continue
                        ctype, rem_qty_str = part.split(":", 1)
                        ctype = ctype.strip()
                        try: rem_qty = int(rem_qty_str.strip())
                        except: continue
                            
                        row = await conn.fetchrow("SELECT quantity FROM client_inventory WHERE user_id = $1 AND card_type = $2", user_id, ctype)
                        
                        if row:
                            current_qty = row['quantity']
                            try:
                                res = await engine.record_client_sale(user_id, ctype, current_qty, rem_qty)
                                if res["status"] == "success":
                                    total_sold_value += Decimal(str(res.get("item_due", 0)))
                                    sold_qty = res.get("sold_qty", 0)
                                    if sold_qty > 0:
                                        sold_details.append(f"{sold_qty} كرت {ctype}")
                                    
                                    if res.get("adjustment_msg"):
                                        clean_reply += f"\n\n⚠️ ملاحظة سيستم: {res['adjustment_msg']}"
                                        try:
                                            await bot.send_message(ADMIN_ID, f"🚨 **تدخل آلي (جرد العميل):**\nالعميل ({client_name}) أبلغ عن كروت زائدة.\n{res['adjustment_msg']}")
                                        except: pass
                            except Exception as e:
                                print(f"AI Inventory Update Error: {e}")
                                    
            if total_sold_value > 0 or sold_details:
                details_str = " + ".join(sold_details) if sold_details else "تسوية آلية"
                clean_reply += f"\n\n✅ تم تحديث مخزونك. مبيعاتك: ({details_str}). المطلوب تجهيز **{int(total_sold_value)} ريال** كاش."
                try:
                    await bot.send_message(ADMIN_ID, f"🔔 **جرد آلي من العميل:**\nالعميل: {client_name}\nباع: {details_str}\nالمبلغ الجاهز عنده الآن: **{int(total_sold_value)} ريال**")
                except: pass
        except Exception as e:
            print(f"Inventory Update Error: {e}")

    # 🌟 التنظيف النهائي الشامل لجميع الأكواد السرية
    clean_reply = re.sub(r'\[.*?\]', '', clean_reply).strip()
    
    # 🌟 إذا أصبح الرد فارغاً بعد التنظيف، نضع رداً افتراضياً لكي لا ينهار البوت
    if not clean_reply:
        clean_reply = "تم استلام طلبك وإرساله للإدارة بنجاح 🌹"
        
    return clean_reply

# ================= دالة تنفيذ الأوامر الذكية (للمدير والوكيل) =================
async def execute_smart_action(message: types.Message, client_id: int, client_name: str, action: str, amount: Decimal, user_id: int):
    if database.pool:
        async with database.pool.acquire() as conn:
            if action == "query_debt":
                u = await conn.fetchrow("SELECT debt, pending_profit FROM users WHERE user_id = $1", client_id)
                gross_debt = Decimal(u['debt'])
                pending = Decimal(u['pending_profit'])
                net_debt = gross_debt - pending
                
                if user_id == ADMIN_ID:
                    await message.answer(f"💰 حساب العميل (**{client_name}**) الإجمالي هو: **{int(gross_debt)} ريال**.\n*(منها {int(pending)} ريال أرباح معلقة لك، الصافي للإدارة: {int(net_debt)} ريال)*.")
                else:
                    await message.answer(f"💰 حساب العميل (**{client_name}**) الصافي للإدارة هو: **{int(net_debt)} ريال**.")
            
            elif action == "query_inv":
                inv = await conn.fetch("SELECT card_type, quantity FROM client_inventory WHERE user_id = $1 AND quantity > 0", client_id)
                if inv:
                    text = f"📦 **مخزون ({client_name}):**\n"
                    for item in inv: text += f"▪️ {item['card_type']}: {item['quantity']} كرت\n"
                    await message.answer(text)
                else:
                    await message.answer(f"📦 العميل (**{client_name}**) ليس لديه أي كروت حالياً.")

            elif action == "add_debt" and user_id == ADMIN_ID:
                # 🌟 التعديل: استخدام المحرك المالي (مركز التحويلات) لإضافة الدين بأمان
                engine = FinancialEngine(database.pool)
                try:
                    await engine.transfer_assets('agent', 0, 'client', client_id, 'cash', amount)
                    new_debt = await conn.fetchval("SELECT debt FROM users WHERE user_id = $1", client_id)
                    await message.answer(f"✅ تم إضافة **{int(amount)} ريال** على حساب (**{client_name}**) بنجاح.\nالرصيد الجديد: {int(new_debt)} ريال.")
                except FinancialError as e:
                    await message.answer(f"❌ فشل إضافة الدين: {e.message}")

            elif action == "visit" and user_id == ADMIN_ID:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🚶‍♂️ بدء الجرد والتحصيل", callback_data=f"visit_inventory_{client_id}")],
                    [InlineKeyboardButton(text="📦 تسليم كروت مباشرة", callback_data=f"gclient_{client_id}")]
                ])
                await message.answer(f"?? ماذا تريد أن تفعل مع العميل (**{client_name}**)؟", reply_markup=kb)
                
            elif action == "pdf_statement":
                from pdf_generator import generate_client_statement
                
                user = await conn.fetchrow("SELECT debt FROM users WHERE user_id = $1", client_id)

                if not user: return await message.answer("❌ عذراً، الحساب غير مسجل.")
                
                debt = Decimal(user['debt'])
                inv_items = await conn.fetch("SELECT card_type, quantity FROM client_inventory WHERE user_id = $1 AND quantity > 0", client_id)
                last_trans = await conn.fetchrow("SELECT amount, date FROM transactions WHERE user_id = $1 AND type = 'تسديد_من_عميل' ORDER BY date DESC LIMIT 1", client_id)
                last_payment = f"{int(Decimal(last_trans['amount']))} ريال (بتاريخ {str(last_trans['date'])[:10]})" if last_trans else "لا توجد دفعات سابقة"
                
                pdf_buffer = await asyncio.to_thread(generate_client_statement, client_name, debt, inv_items, last_payment)
                pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Statement_{client_name}.pdf")
                
                await message.answer_document(document=pdf_file, caption=f"📑 **كشف حساب (PDF) للعميل: {client_name}**")

@router.message(F.text.in_(["جرد", "نبدأ الجرد", "جرد الشهر"]) & (F.from_user.id == ADMIN_ID))
async def start_interactive_audit(message: types.Message, state: FSMContext):
    msg = "أبشر يا دكتور. هل تريد جرد هذا الشهر؟ (اكتب التواريخ)" # 👈 أضف هذا السطر كحماية
    if database.pool:
        async with database.pool.acquire() as conn:

            last_report = await conn.fetchrow("SELECT created_at FROM reports_archive ORDER BY created_at DESC LIMIT 1")
            now = datetime.now()
            
            if last_report:
                last_date = last_report['created_at']
                days_passed = (now - last_date).days
                last_date_str = last_date.strftime('%Y-%m-%d')
                today_str = now.strftime('%Y-%m-%d')
                
                if 25 <= days_passed <= 35:
                    msg = f"يا دكتور، مر **{days_passed} يوم** على آخر تقرير (كان بتاريخ {last_date_str}).\nهل تريد جرد هذه الفترة (إلى اليوم)؟ أم تريد تحديد تواريخ معينة؟\n\n*(اكتب: 'نعم' أو اكتب التواريخ مثل: من 2024-04-01 إلى 2024-04-30)*"
                elif days_passed < 25:
                    msg = f"يا دكتور، مر **{days_passed} يوم فقط** على آخر تقرير (كان بتاريخ {last_date_str}).\nهل تريد جرد هذه الفترة القصيرة؟ أم تريد تحديد تواريخ معينة؟\n\n*(اكتب: 'نعم' أو اكتب التواريخ)*"
                else:
                    msg = f"يا دكتور، تأخرنا! مر **{days_passed} يوم** على آخر تقرير (كان بتاريخ {last_date_str}).\nهل تريد جرد الفترة كاملة إلى اليوم؟ أم تريد تحديد تواريخ معينة؟\n\n*(اكتب: 'نعم' أو اكتب التواريخ)*"
                
                # حفظ التواريخ المقترحة في الذاكرة المؤقتة
                await state.update_data(suggested_start=last_date_str, suggested_end=today_str)
            else:
                first_day = now.replace(day=1).strftime('%Y-%m-%d')
                today_str = now.strftime('%Y-%m-%d')
                msg = f"أبشر يا دكتور. هذا أول جرد في النظام!\nهل تريد جرد هذا الشهر (من {first_day} إلى {today_str})؟ أم تريد تحديد تواريخ معينة؟\n\n*(اكتب: 'نعم' أو اكتب التواريخ)*"
                await state.update_data(suggested_start=first_day, suggested_end=today_str)
                
    await message.answer(msg)
    await state.set_state(AgentInteractiveAudit.waiting_for_dates)

@router.message(AgentInteractiveAudit.waiting_for_dates, F.from_user.id == ADMIN_ID)
async def process_audit_dates(message: types.Message, state: FSMContext):
    text = message.text or ""  
    data = await state.get_data()
    
    if any(word in text for word in ["نعم", "موافق", "توكل", "السابق", "هذا الشهر", "ايوه"]):
        start_date_str = data.get('suggested_start')
        end_date_str = data.get('suggested_end')
    else:
        start_date_str, end_date_str = get_smart_dates(text)
        
    await state.update_data(start_date=start_date_str, end_date=end_date_str)
    
    from datetime import datetime
    start_date_obj = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    
    prev_balance = Decimal('0.0')
    if database.pool:
        async with database.pool.acquire() as conn:
            # 🌟 الإصلاح المحاسبي: حساب الرصيد السابق بدقة متضمنة الأرصدة الافتتاحية وتجاهل التراجعات
            prev_received = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE AND date < $1::date", start_date_obj)
            prev_paid = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسديد_للشبكة', 'مصروفات', 'كروت_تالفة', 'نسبة_الوكيل', 'مرتجع_للشبكة') AND is_reverted = FALSE AND date < $1::date", start_date_obj)
            prev_balance = Decimal(prev_received or 0) - Decimal(prev_paid or 0)
            
    await state.update_data(prev_balance=str(prev_balance))
    
    await message.answer(f"بسم الله نبدأ الجرد للفترة من {start_date_str} إلى {end_date_str}.\n\n**الخطوة 1:** المبلغ المتبقي من الفترة السابقة هو: **{int(abs(prev_balance))} ريال**.\nهل الرقم صحيح؟ (نعم / لا، ضيف كذا)")
    await state.set_state(AgentInteractiveAudit.step_1_prev)

# ================= الجرد التفاعلي للوكيل (Agent Interactive Audit) =================

async def handle_audit_correction(message: types.Message, state: FSMContext):
    """دالة الذكاء الاصطناعي لتصحيح وإضافة العمليات المنسية أثناء الجرد"""
    text = message.text or ""
    if not text:
        return await message.answer("يرجى كتابة التعديل كنص.")

    wait_msg = await message.answer("⏳ جاري تحليل طلبك وتحديث الحسابات في قاعدة البيانات...")
    prompt = """
    أنت نظام استخراج بيانات آلي.
    استخرج العملية المالية من النص التالي وأرجعها بصيغة JSON فقط.
    🛑 تحذير صارم: إياك أن تكتب أي كلمة أو حرف خارج أقواس الـ JSON. لا تكتب مقدمات ولا شروحات.
    الصيغة المطلوبة حصراً:
    {"type": "نوع_العملية", "amount": 1000, "details": "التفاصيل"}
    
    الأنواع المسموحة فقط هي:
    - مصروفات (إذا قال صرفت، بترول، غداء، الخ)
    - تسديد_للشبكة (إذا قال سددت للإدارة، حولت للمدير، الخ)
    - استلام_من_الشبكة (إذا قال استلمت كروت)
    - كروت_تالفة (إذا قال كرت تالف)
    
    إذا لم تفهم العملية أو كانت غير واضحة، أرجع: {"error": "غير واضح"}
    """
    try:
        response = await generate_groq_response(prompt, message.from_user.id, text)
        
        # 🌟 منظف الـ Markdown: إزالة أي علامات زائدة يكتبها الذكاء الاصطناعي
        clean_json = re.sub(r'```(?:json)?', '', response).strip()
        data = json.loads(clean_json)
        
        if "error" in data:
            await wait_msg.edit_text("🤔 لم أفهم العملية بوضوح. يرجى كتابتها هكذا: (ضيف 5000 مصروفات بترول) ثم أجب بـ (نعم) للمتابعة.")
            return
            
        # 🌟 التعديل: استخدام المحرك المالي الجديد
        engine = FinancialEngine(database.pool)
        
        t_type = data['type']
        amount = Decimal(str(data['amount']))
        details = data['details']
        
        if t_type == 'مصروفات':
            await engine.finance_action(TxType.EXPENSE, amount, details, source="(إصلاح جرد)")
        elif t_type == 'تسديد_للشبكة':
            await engine.finance_action(TxType.PAY_MANAGER, amount, details, source="(إصلاح جرد)")
        elif t_type == 'كروت_تالفة':
            # الكروت التالفة تعتبر مصروفات في المحرك الجديد إذا لم تكن مرتبطة بكرت معين
            await engine.finance_action(TxType.DAMAGED_CARDS, amount, details, source="(إصلاح جرد)")
        elif t_type == 'استلام_من_الشبكة':
            # 🌟 الحماية المحاسبية: منع إدخال الكروت كنص مبهم لحماية المخزون
            await wait_msg.edit_text("⚠️ **تنبيه محاسبي:** لا يمكن إضافة (استلام كروت) كنص مبهم لأنها تؤثر على المخزون وسعر التكلفة.\nالرجاء إضافتها من واجهة الويب، أو استخدام الأمر الدقيق:\n`[استلام_كروت|الكمية|الفئة]`")
            return
            
        await wait_msg.edit_text(f"✅ تم التعديل وإضافة: **{int(amount)} ريال** ({t_type.replace('_', ' ')} - {details}).\n\nالرجاء كتابة **(نعم)** للمتابعة للخطوة التالية (الأرقام ستتحدث تلقائياً).")

    except Exception as e:
        await wait_msg.edit_text("❌ حدث خطأ في الفهم. يرجى إضافتها يدوياً من الإعدادات ثم كتابة (نعم) للمتابعة.")

@router.message(AgentInteractiveAudit.step_1_prev, F.from_user.id == ADMIN_ID)
async def audit_step_1_answer(message: types.Message, state: FSMContext):
    text = message.text or ""  
    if not any(w in text for w in ["نعم", "صح", "مضبوط", "تمام", "كمل"]):
        return await handle_audit_correction(message, state)
        
    data = await state.get_data()
    start_date_str, end_date_str = data['start_date'], data['end_date']
    
    from datetime import datetime
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    received_breakdown_text = ""
    opening_balances_text = ""
    total_received = Decimal('0.0')
    total_opening = Decimal('0.0')
    
    if database.pool:
        async with database.pool.acquire() as conn:
            received_txs = await conn.fetch("SELECT type, amount, details FROM transactions WHERE type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date, end_date)
            
            received_breakdown = {}
            for tx in received_txs:
                if tx['type'] in ('رصيد_افتتاحي', 'رصيد_افتتاحي_كاش'):
                    total_opening += Decimal(tx['amount'])
                    opening_balances_text += f"▪️ {tx['details']} ({int(abs(Decimal(tx['amount'])))} ريال)\n"
                else:
                    total_received += Decimal(tx['amount'])
                    match = re.search(r"استلام (\d+) كرت (.+)", tx['details'])
                    if match:
                        qty = int(match.group(1))
                        ctype = match.group(2).strip()
                        if ctype not in received_breakdown:
                            received_breakdown[ctype] = {'qty': 0, 'amount': Decimal(0)}
                        received_breakdown[ctype]['qty'] += qty
                        received_breakdown[ctype]['amount'] += Decimal(tx['amount'])
                    else:
                        ctype = tx['details']
                        if ctype not in received_breakdown:
                            received_breakdown[ctype] = {'qty': 0, 'amount': Decimal(0)}
                        received_breakdown[ctype]['amount'] += Decimal(tx['amount'])
            
            if received_breakdown:
                for ctype, d in received_breakdown.items():
                    if d['qty'] > 0:
                        received_breakdown_text += f"▪️ {ctype} | العدد: {d['qty']} ({int(abs(d['amount']))} ريال)\n"
                    else:
                        received_breakdown_text += f"▪️ {ctype} ({int(abs(d['amount']))} ريال)\n"
            else:
                received_breakdown_text = "▪️ لا توجد كروت مستلمة\n"
                
            if not opening_balances_text:
                opening_balances_text = "▪️ لا توجد أرصدة افتتاحية\n"
    
    await state.update_data(total_received=str(total_received + total_opening))
    await state.set_state(AgentInteractiveAudit.step_2_received)
    await message.answer(f"**الخطوة 2 (الأرصدة والكروت المستلمة):**\n\n📦 **الأرصدة الافتتاحية (ديون سابقة):**\n{opening_balances_text}الإجمالي: {int(abs(total_opening))} ريال\n\n📦 **الكروت المستلمة:**\n{received_breakdown_text}الإجمالي: {int(abs(total_received))} ريال\n\nإجمالي المضاف لحساب الإدارة: **{int(abs(total_received + total_opening))} ريال**.\n\nهل الأرقام صحيحة؟ (نعم / لا، ضيف كذا)")

@router.message(AgentInteractiveAudit.step_2_received, F.from_user.id == ADMIN_ID)
async def audit_step_2_answer(message: types.Message, state: FSMContext):
    text = message.text or ""  
    if not any(w in text for w in ["نعم", "صح", "مضبوط", "تمام", "كمل"]):
        return await handle_audit_correction(message, state)
        
    data = await state.get_data()
    start_date_str, end_date_str = data['start_date'], data['end_date']
    
    from datetime import datetime
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    paid_breakdown_text = ""
    total_paid = Decimal('0.0')
    if database.pool:
        async with database.pool.acquire() as conn:
            paid_txs = await conn.fetch("SELECT amount, details FROM transactions WHERE type = 'تسديد_للشبكة' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date, end_date)
            if paid_txs:
                for tx in paid_txs:
                    total_paid += Decimal(tx['amount'])
                    paid_breakdown_text += f"▪️ {tx['details']} ({int(abs(Decimal(tx['amount'])))} ريال)\n"
            else:
                paid_breakdown_text = "▪️ لا توجد دفعات مسجلة\n"
            
    await state.update_data(total_paid=str(total_paid))
    await state.set_state(AgentInteractiveAudit.step_3_paid)
    await message.answer(f"**الخطوة 3 (الدفعات المسددة للإدارة):**\n{paid_breakdown_text}\nإجمالي الدفعات المسددة: **{int(abs(total_paid))} ريال**.\n\nهل الأرقام صحيحة؟ (نعم / لا، ضيف كذا)")

@router.message(AgentInteractiveAudit.step_3_paid, F.from_user.id == ADMIN_ID)
async def audit_step_3_answer(message: types.Message, state: FSMContext):
    text = message.text or ""  
    if not any(w in text for w in ["نعم", "صح", "مضبوط", "تمام", "كمل"]):
        return await handle_audit_correction(message, state)
        
    data = await state.get_data()
    start_date_str, end_date_str = data['start_date'], data['end_date']
    
    from datetime import datetime
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    expenses_breakdown_text = ""
    total_expenses = Decimal('0.0')
    damaged = Decimal('0.0')
    agent_dues = Decimal('0.0')
    returned_net = Decimal('0.0')
    
    if database.pool:
        async with database.pool.acquire() as conn:
            exp_txs = await conn.fetch("SELECT amount, details FROM transactions WHERE type = 'مصروفات' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date, end_date)
            if exp_txs:
                for tx in exp_txs:
                    total_expenses += Decimal(tx['amount'])
                    expenses_breakdown_text += f"▪️ {tx['details']} ({int(abs(Decimal(tx['amount'])))} ريال)\n"
            else:
                expenses_breakdown_text = "▪️ لا توجد مصروفات مسجلة\n"
            
            dmg_val = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'كروت_تالفة' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date, end_date)
            agent_val = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'نسبة_الوكيل' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date, end_date)
            ret_val = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'مرتجع_للشبكة' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date, end_date)

            damaged = Decimal(dmg_val) if dmg_val else Decimal('0.0')
            agent_dues = Decimal(agent_val) if agent_val else Decimal('0.0')
            returned_net = Decimal(ret_val) if ret_val else Decimal('0.0')
            
    await state.update_data(total_expenses=str(total_expenses), damaged=str(damaged), agent_dues=str(agent_dues), returned_net=str(returned_net))
    await state.set_state(AgentInteractiveAudit.step_4_expenses)
    await message.answer(f"**الخطوة 4 (المصروفات والتوالف):**\n{expenses_breakdown_text}\nإجمالي المصروفات: **{int(abs(total_expenses))} ريال**.\nكروت تالفة: **{int(abs(damaged))} ريال**.\nمستحقات الوكيل: **{int(abs(agent_dues))} ريال**.\nمرتجع للإدارة: **{int(abs(returned_net))} ريال**.\n\nهل الأرقام صحيحة؟ (نعم / لا، ضيف كذا)")

@router.message(AgentInteractiveAudit.step_4_expenses, F.from_user.id == ADMIN_ID)
async def audit_step_4_answer(message: types.Message, state: FSMContext):
    text = message.text or ""
    if not any(w in text for w in ["نعم", "صح", "مضبوط", "تمام", "كمل"]):
        return await handle_audit_correction(message, state)
        
    data = await state.get_data()
    
    prev = Decimal(str(data.get("prev_balance", "0.0")))
    rec = Decimal(str(data.get("total_received", "0.0")))
    paid = Decimal(str(data.get("total_paid", "0.0")))
    exp = Decimal(str(data.get("total_expenses", "0.0")))
    dmg = Decimal(str(data.get("damaged", "0.0")))
    agent = Decimal(str(data.get("agent_dues", "0.0")))
    ret_net = Decimal(str(data.get("returned_net", "0.0")))
    
    # 👈 المعادلة الصحيحة للصافي
    net_total = (prev + rec) - (paid + dmg + exp + agent + ret_net)
    
    await state.update_data(net_total=str(net_total))
    await state.set_state(AgentInteractiveAudit.step_5_net)
    await message.answer(f"**الخطوة 5 (الصافي):**\nالمتبقي حالياً للإدارة (الصافي) هو: **{int(abs(net_total))} ريال**.\n\nهل ننتقل لتفاصيل هذا الصافي؟ (نعم)")

@router.message(AgentInteractiveAudit.step_5_net, F.from_user.id == ADMIN_ID)
async def audit_step_5_answer(message: types.Message, state: FSMContext):
    text = message.text or ""
    if not any(w in text for w in ["نعم", "صح", "مضبوط", "تمام", "كمل"]):
        return await message.answer("الرجاء كتابة (نعم) للمتابعة.")
        
    data = await state.get_data()
    end_date_str = data['end_date']
    from datetime import datetime
    end_date_obj = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    
    inv_value, total_debt, available_cash = Decimal('0.0'), Decimal('0.0'), Decimal('0.0')
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # 1. حساب الكاش التاريخي الصافي (معزول للكروت فقط)
            cash_in = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND (type IN ('تسديد_من_عميل', 'بيع_مباشر', 'رصيد_افتتاحي_كاش') OR (type = 'تحويل_رصيد' AND details LIKE 'تسوية واردة للصندوق%')) AND is_reverted = FALSE AND date <= $1::date + interval '1 day'", end_date_obj)
            cash_out = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND (type IN ('تسديد_للشبكة', 'مصروفات', 'مرتجع_بيع_مباشر', 'كروت_تالفة', 'سحب_أرباح') OR (type = 'تحويل_رصيد' AND details LIKE 'تسوية صادرة من الصندوق%')) AND is_reverted = FALSE AND date <= $1::date + interval '1 day'", end_date_obj)
            realized_profit = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM agent_profits WHERE date <= $1::date + interval '1 day'", end_date_obj)
            
            available_cash = (Decimal(cash_in or 0) - Decimal(cash_out or 0)) - Decimal(realized_profit or 0)
            
            # 2. حساب الديون التاريخية الصافية (معزول للكروت فقط)
            debt_added = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('تسليم_لعميل', 'رصيد_افتتاحي') AND is_reverted = FALSE AND date <= $1::date + interval '1 day'", end_date_obj)
            debt_removed = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('تسديد_من_عميل', 'مرتجع_من_عميل') AND is_reverted = FALSE AND date <= $1::date + interval '1 day'", end_date_obj)
            pending_profit = await conn.fetchval("SELECT COALESCE(SUM(pending_profit), 0) FROM users WHERE role = 'client'")
            
            total_debt = (Decimal(debt_added or 0) - Decimal(debt_removed or 0)) - Decimal(pending_profit or 0)
            
            # 3. استنتاج قيمة المخزون لتتطابق مع الصافي
            net_total = Decimal(str(data.get("net_total", "0.0")))
            inv_value = net_total - total_debt - available_cash
        
    await state.set_state(AgentInteractiveAudit.step_6_details)
    await message.answer(f"**الخطوة 6 والأخيرة (تفاصيل الصافي):**\n▪️ إجمالي الكروت في المخزون: **{int(inv_value)} ريال**\n▪️ إجمالي ديون العملاء بالسوق: **{int(total_debt)} ريال**\n▪️ السيولة النقدية المتوفرة (الكاش): **{int(available_cash)} ريال**.\n\nهل نعتمد التقرير النهائي ونرسله للمدير؟ (نعم / لا)")

@router.message(AgentInteractiveAudit.step_6_details, F.from_user.id == ADMIN_ID)
async def audit_step_6_answer(message: types.Message, state: FSMContext, bot: Bot):
    text = message.text or ""
    if not any(w in text for w in ["نعم", "صح", "مضبوط", "تمام", "اعتمد"]):
        await state.clear()
        return await message.answer("❌ تم إلغاء الاعتماد. يمكنك مراجعة حساباتك والبدء من جديد متى شئت.")
        
    data = await state.get_data()
    start_date_str, end_date_str = data['start_date'], data['end_date']
    
    from datetime import datetime
    start_date_obj = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date_obj = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    
    wait_msg = await message.answer("⏳ جاري تجهيز **مسودة التقرير النهائي** للمراجعة الأخيرة...")
    
    try:
        # 1. توليد مسودة الإكسل
        from core_accounting import generate_detailed_excel_report
        excel_stream = await generate_detailed_excel_report(start_date_str, end_date_str)
        excel_doc = BufferedInputFile(excel_stream.read(), filename=f"DRAFT_Report_{start_date_str}_to_{end_date_str}.xlsx")
        
        # 2. جلب البيانات الحقيقية لمسودة الـ PDF
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
                query = """
                    SELECT type, details, COALESCE(SUM(amount), 0) as total 
                    FROM transactions 
                    WHERE wallet_type = 'manager' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'
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
                                if "جملة كاش" in t_details:
                                    summary_data['wholesale_cash'] += t_total
                                else:
                                    summary_data['retail_cash'] += t_total
                
                # حساب صافي الكاش (مع إضافة التوالف وسحب الأرباح ليكون مطابقاً للتقرير النهائي)
                cash_in = summary_data['retail_cash'] + summary_data['wholesale_cash'] + summary_data['collected']
                cash_out = summary_data['expenses'] + summary_data['paid_to_gm'] + summary_data['returns_retail'] + summary_data['damaged'] + summary_data.get('profit_withdrawal', Decimal('0.0'))
                summary_data['net_cash'] = cash_in - cash_out

        from pdf_generator import generate_custom_report_pdf
        pdf_buffer = await asyncio.to_thread(generate_custom_report_pdf, start_date_str, end_date_str, summary_data)
        pdf_doc = BufferedInputFile(pdf_buffer.read(), filename=f"DRAFT_Report_{start_date_str}_to_{end_date_str}.pdf")
        
        await wait_msg.delete()
        
        # إرسال المسودة للوكيل فقط
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ اعتماد التقرير وإرساله للأرشيف", callback_data="approve_final_draft")],
            [InlineKeyboardButton(text="❌ إلغاء وتعديل", callback_data="cancel_action")]
        ])
        
        await message.answer_document(document=excel_doc)
        await message.answer_document(document=pdf_doc, caption="📝 **مسودة التقرير النهائي جاهزة!**\n\nيا دكتور، راجع الملفات أعلاه. إذا كانت الأرقام صحيحة وشكل التقرير يبيض الوجه، اضغط (اعتماد).", reply_markup=kb)
        
        await state.set_state(AgentInteractiveAudit.waiting_for_draft_approval)
        
    except Exception as e:
        await wait_msg.edit_text(f"❌ حدث خطأ أثناء تجهيز المسودة: {e}")

# دالة الاعتماد النهائي (بعد مراجعة المسودة)
@router.callback_query(F.data == "approve_final_draft")
async def approve_final_draft_action(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    start_date_str, end_date_str = data['start_date'], data['end_date']
    
    from datetime import datetime
    start_date_obj = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date_obj = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    
    await callback.message.edit_caption(caption="⏳ جاري الاعتماد والحفظ في الأرشيف السري...")
    
    try:
        # 1. توليد الإكسل النهائي
        from core_accounting import generate_detailed_excel_report
        excel_stream = await generate_detailed_excel_report(start_date_str, end_date_str)
        file_msg = await bot.send_document(ADMIN_ID, BufferedInputFile(excel_stream.read(), filename=f"Final_Report_{start_date_str}.xlsx"))
        file_id = file_msg.document.file_id

        # الحفظ في الأرشيف
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
                        query = """
                            SELECT type, details, COALESCE(SUM(amount), 0) as total 
                            FROM transactions 
                            WHERE wallet_type = 'manager' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'
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
                            # 🌟 الإصلاح المحاسبي: إضافة الأرصدة الافتتاحية والتحويلات الواردة
                            elif t_type == 'رصيد_افتتاحي_كاش' or (t_type == 'تحويل_رصيد' and 'تسوية واردة' in t_details):
                                summary_data['retail_cash'] = summary_data.get('retail_cash', Decimal('0.0')) + t_total
                        
                        # 🚨 التعديل: إضافة التوالف وسحب الأرباح للكاش الخارج ليكون الـ PDF دقيقاً 100%
                        cash_in = summary_data['retail_cash'] + summary_data['wholesale_cash'] + summary_data['collected']
                        cash_out = summary_data['expenses'] + summary_data['paid_to_gm'] + summary_data['returns_retail'] + summary_data['damaged'] + summary_data.get('profit_withdrawal', Decimal('0.0'))
                        summary_data['net_cash'] = cash_in - cash_out

                from pdf_generator import generate_custom_report_pdf
                pdf_buffer = await asyncio.to_thread(generate_custom_report_pdf, start_date_str, end_date_str, summary_data)
                pdf_document = BufferedInputFile(pdf_buffer.read(), filename=f"Archive_Report_{start_date_str}_to_{end_date_str}.pdf")
                await bot.send_document(archive_channel, pdf_document, caption=f"📂 **أرشيف التقارير (PDF):** تقرير الفترة من {start_date_str} إلى {end_date_str}")
                await bot.send_document(archive_channel, file_id, caption=f"📂 **أرشيف التقارير (Excel):** تقرير الفترة من {start_date_str} إلى {end_date_str}")
            except Exception as e:
                print(f"Archive Error: {e}")
        
        # إشعار المدير العام
        try:
            await bot.send_message(NETWORK_OWNER_ID, f"🔔 **يا شيخ رهيب، د. وليد اعتمد التقرير الشهري (من {start_date_str} إلى {end_date_str}).**\nالتقرير موجود ومحفوظ تسطيع الحصول علية من خلال  ارسال رسالة صوتية هات التقرير  او رساله نصية اعطني التقرير او تسحب ملف الإكسل متى ما تحب من زر (سحب التقرير).")
        except: pass
        
        # إرسال واتساب للمدير
        try:
            from unified_main import send_whatsapp_message, GM_WA_NUMBER
            wa_text = f"📊 *تنبيه إداري:*\nيا شيخ رهيب، تم اعتماد وتجهيز التقرير الشهري (من {start_date_str} إلى {end_date_str}). يمكنك الدخول إلى البوت في التليجرام وسحب التقرير في أي وقت 🌹"
            await send_whatsapp_message(GM_WA_NUMBER, wa_text)
        except Exception as e:
            print(f"WhatsApp Error: {e}")
        
        await callback.message.edit_caption(caption="✅ **تم الاعتماد بنجاح!**\nتم حفظ التقرير في الأرشيف وإشعار المدير العام.")
        await state.clear()
        
    except Exception as e:
        print(f"Error in approve_final_draft: {e}")
        await callback.message.edit_caption(caption=f"❌ حدث خطأ أثناء الاعتماد: {e}")

async def process_admin_ai_tags(reply_text: str, message: types.Message, bot: Bot, user_id: int, state: FSMContext, is_voice: bool = False) -> bool:
    """دالة موحدة لمعالجة أوامر الذكاء الاصطناعي الخاصة بالإدارة"""
    
    async def send_reply(text_msg):
        if is_voice:
            try:
                voice_buffer, file_ext = await text_to_voice(text_msg)
                voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
                await message.answer_voice(voice=voice_file, caption="🎙️")
            except: await message.answer(text_msg)
        else:
            await message.answer(text_msg)

    if "[تذكير|" in reply_text:
        try:
            parts = reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")
            delay_seconds = int(parts[1].strip())
            reminder_text = parts[2].strip()
            asyncio.create_task(send_delayed_reminder(bot, user_id, delay_seconds, reminder_text))
            await send_reply("✅ أبشر يا دكتور، سجلت التذكير وبذكرك في الوقت المحدد إن شاء الله.")
        except Exception as e:
            await send_reply(f"🤖 عذراً، حدث خطأ في جدولة التذكير: {e}")
        return True

    if "[تغيير_الصوت|" in reply_text:
        try:
            voice_code = reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")[1].strip()
            if database.pool:
                async with database.pool.acquire() as conn:
                    exists = await conn.fetchval("SELECT 1 FROM settings WHERE key = 'bot_voice'")
                    if exists: await conn.execute("UPDATE settings SET value = $1 WHERE key = 'bot_voice'", voice_code)
                    else: await conn.execute("INSERT INTO settings (key, value) VALUES ('bot_voice', $1)", voice_code)
            await send_reply("✅ تم تغيير صوتي بنجاح يا دكتور! كيف تسمعني الآن؟")
        except Exception as e: await send_reply(f"🤖 خطأ: {e}")
        return True

    if "[مراسلة_المدير|" in reply_text:
        msg_content = reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")[1].strip()
        await bot.send_message(NETWORK_OWNER_ID, f"📩 **رسالة من الوكيل:**\n{msg_content}")
        await send_reply("✅ تم إرسال رسالتك للمدير العام.")
        return True

    if "[تسديد_للمدير|" in reply_text:
        try:
            amount = Decimal(reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")[1].strip())
            
            # 🌟 التعديل: استخدام المحرك المالي الجديد
            engine = FinancialEngine(database.pool)
            try:
                await engine.finance_action(TxType.PAY_MANAGER, amount, "تحويل للمدير العام", source="(عبر الذكاء الاصطناعي)")
                await send_reply(f"✅ تم تنفيذ العملية: تسجيل تسديد مبلغ {int(amount)} ريال للمدير العام بنجاح.")
            except FinancialError as e:
                await send_reply(f"❌ **رفض أمني:** {e.message}")
        except Exception as e: await send_reply(f"🤖 خطأ: {e}")
        return True

    if "[استلام_كروت|" in reply_text:
        try:
            parts = reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")
            qty = int(parts[1].strip())
            card_type = parts[2].strip()
            if database.pool:
                async with database.pool.acquire() as conn:
                    row = await conn.fetchrow("SELECT card_type, cost_price FROM inventory WHERE card_type LIKE $1", f"%{card_type}%")
                    
                    if row:
                        exact_card_type = row['card_type']
                        cost_price = Decimal(row['cost_price'] or 0)
                        
                        # 🌟 التعديل: استخدام المحرك المالي الجديد لضمان تسجيل البيانات المهيكلة
                        from core_accounting import FinancialEngine
                        engine = FinancialEngine(database.pool)
                        try:
                            await engine.receive_from_network(exact_card_type, qty, cost_price)
                            await send_reply(f"✅ تم تنفيذ العملية: إضافة {qty} كرت من فئة {exact_card_type} للمخزون.")
                        except Exception as e:
                            # في حال حدوث خطأ مالي (مثل كمية سالبة)
                            await send_reply(f"❌ فشل استلام الكروت: {str(e)}")
                    else:
                        # 🚨 التعديل: إخبار المستخدم إذا لم يتم العثور على الكرت بدلاً من الصمت
                        await send_reply(f"🤖 عذراً، لم أجد فئة كرت تطابق '{card_type}' في المخزون.")
        except Exception as e: 
            await send_reply(f"🤖 خطأ: {e}")
        return True

    if "[تسجيل_مصروف|" in reply_text:
        try:
            parts = reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")
            amount = Decimal(parts[1].strip())
            details = parts[2].strip()
            
            # 🌟 التعديل: استخدام المحرك المالي الجديد
            engine = FinancialEngine(database.pool)
            try:
                await engine.finance_action(TxType.EXPENSE, amount, details, source="(عبر الذكاء الاصطناعي)")
                await send_reply(f"✅ تم تنفيذ العملية: تسجيل مصروفات بقيمة {int(amount)} ريال ({details}).")
            except FinancialError as e:
                await send_reply(f"❌ **رفض أمني:** {e.message}")
        except Exception as e:
            await send_reply(f"?? خطأ في التنفيذ: {e}")
        return True

    if "[إضافة_دين|" in reply_text:
        try:
            parts = reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")
            client_name_ai = normalize_arabic_name(parts[1].strip())
            amount = Decimal(parts[2].strip())
            
            client_id = None
            if database.pool:
                async with database.pool.acquire() as conn:
                    clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client'")
                    client_row = next((c for c in clients if normalize_arabic_name(c['name']) == client_name_ai or client_name_ai in normalize_arabic_name(c['name'])), None)
                    if not client_row:
                        await send_reply(f"🤖 لم أجد عميلاً باسم '{parts[1].strip()}'.")
                        return True
                    client_id = client_row['user_id']
                    client_name = client_row['name']
                    
            # 🌟 التعديل: استخدام المحرك المالي الجديد
            engine = FinancialEngine(database.pool)
            try:
                await engine.transfer_assets('agent', 0, 'client', client_id, 'cash', amount)
                await send_reply(f"✅ تم تنفيذ العملية: إضافة {int(amount)} ريال على حساب {client_name}.")
            except FinancialError as e:
                await send_reply(f"❌ فشل إضافة الدين: {e.message}")
                
        except Exception as e:
            await send_reply(f"🤖 خطأ في التنفيذ: {e}")
        return True

    if "[تحويل_شامل|" in reply_text:
        try:
            parts = reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")
            sender_str = parts[1].strip()
            receiver_str = parts[2].strip()
            t_type = parts[3].strip()
            val = Decimal(parts[4].strip())
            card_type = parts[5].strip() if len(parts) > 5 else ""
            
            if database.pool:
                async with database.pool.acquire() as conn:
                    async def resolve_entity(name_str):
                        name_norm = normalize_arabic_name(name_str)
                        if any(w in name_norm for w in ["مدير", "اداره", "إدارة", "شبكه", "شبكة", "رهيب"]): return "manager"
                        if any(w in name_norm for w in ["وكيل", "صندوق", "مخزون", "حسابي", "وليد"]): return "agent"
                        
                        clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client'")
                        for c in clients:
                            if name_norm in normalize_arabic_name(c['name']) or normalize_arabic_name(c['name']) in name_norm:
                                return f"client_{c['user_id']}"
                        return None

                    sender_id = await resolve_entity(sender_str)
                    receiver_id = await resolve_entity(receiver_str)
                    
                    if not sender_id or not receiver_id:
                        await send_reply(f"🤖 عذراً، لم أتمكن من التعرف على الأطراف بدقة.")
                        return True
                        
                    # 🌟 التعديل: استخدام المحرك المالي الجديد
                    engine = FinancialEngine(database.pool)
                    t_type_en = "cash" if t_type == "كاش" else "cards"
                    
                    s_type = "client" if sender_id.startswith("client_") else sender_id
                    s_id = int(sender_id.split("_")[1]) if sender_id.startswith("client_") else 0
                    
                    r_type = "client" if receiver_id.startswith("client_") else receiver_id
                    r_id = int(receiver_id.split("_")[1]) if receiver_id.startswith("client_") else 0
                    
                    try:
                        result = await engine.transfer_assets(s_type, s_id, r_type, r_id, t_type_en, val, card_type)
                        await send_reply(result["message"])
                    except FinancialError as e:
                        await send_reply(e.message)
                        
        except Exception as e:
            await send_reply(f"🤖 عذراً، حدث خطأ أثناء تنفيذ التحويل: {e}")
        return True

    if "[تنبؤ_احتياج|" in reply_text:
        try:
            parts = reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")
            card_type_ai = parts[1].strip()
            months = int(parts[2].strip())
            
            if database.pool:
                async with database.pool.acquire() as conn:
                    current_stock = await conn.fetchval("SELECT quantity FROM inventory WHERE card_type LIKE $1", f"%{card_type_ai}%")
                    current_stock = current_stock if current_stock else 0
                    
                    transactions = await conn.fetch("""
                        SELECT details FROM transactions 
                        WHERE type IN ('مبيعات_بقالة', 'بيع_مباشر') 
                        AND is_reverted = FALSE
                        AND date >= CURRENT_DATE - INTERVAL '30 days'
                        AND details LIKE $1
                    """, f"%{card_type_ai}%")
                    
                    total_sold_30_days = sum(int(re.search(r"(\d+)\s*كرت", t['details']).group(1)) for t in transactions if re.search(r"(\d+)\s*كرت", t['details']))
                    
                    if total_sold_30_days == 0:
                        await send_reply(f"🤖 **تحليل system:**\nلم أجد أي مبيعات مسجلة لفئة ({card_type_ai}) خلال الـ 30 يوماً الماضية.")
                        return True
                        
                    daily_avg = total_sold_30_days / 30.0
                    required_for_period = int(daily_avg * 30 * months)
                    net_to_order = required_for_period - current_stock
                    
                    report = f"📈 **تحليل وتنبؤ الذكاء الاصطناعي:**\n\n▪️ **الفئة:** {card_type_ai}\n▪️ **معدل سحب السوق:** {total_sold_30_days} كرت شهرياً.\n▪️ **الاحتياج الفعلي لمدة {months} أشهر:** {required_for_period} كرت.\n▪️ **المخزون المتوفر حالياً:** {current_stock} كرت.\n\n"
                    report += f"💡 **القرار المقترح:**\nيجب طلب **{net_to_order} كرت**." if net_to_order > 0 else "💡 **القرار المقترح:**\nالمخزون الحالي يكفي وزيادة."
                    await send_reply(report)
        except Exception as e:
            await send_reply(f"🤖 عذراً، حدث خطأ في تحليل البيانات: {e}")
        return True

    if "[تحليل_عميل|" in reply_text:
        try:
            client_name_ai = normalize_arabic_name(reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")[1].strip())
            if database.pool:
                async with database.pool.acquire() as conn:
                    clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client'")
                    client_row = next((c for c in clients if normalize_arabic_name(c['name']) == client_name_ai or client_name_ai in normalize_arabic_name(c['name'])), None)
                    
                    if client_row:
                        transactions = await conn.fetch("SELECT details FROM transactions WHERE user_id = $1 AND type = 'مبيعات_بقالة' AND is_reverted = FALSE AND date >= CURRENT_DATE - INTERVAL '30 days'", client_row['user_id'])
                        if not transactions:
                            await send_reply(f"🤖 **تحليل system:**\nالعميل ({client_row['name']}) ليس لديه مبيعات آخر 30 يوماً.")
                            return True
                        
                        sales_data = {}
                        for t in transactions:
                            match = re.search(r"(\d+)\s*كرت\s*(.+)", t['details'])
                            if match:
                                sales_data[match.group(2).strip()] = sales_data.get(match.group(2).strip(), 0) + int(match.group(1))
                        
                        report = f"📊 **تحليل مبيعات العميل ({client_row['name']}) لآخر 30 يوماً:**\n\n"
                        best_selling_card = max(sales_data, key=sales_data.get)
                        for ctype, qty in sales_data.items():
                            report += f"▪️ **{ctype}:** باع {qty} كرت.\n"
                        report += f"\n🏆 **الفئة الأسرع مبيعاً لديه:** {best_selling_card}\n"
                        await send_reply(report)
                    else:
                        await send_reply(f"🤖 لم أجد عميلاً باسم '{client_name_ai}'.")
        except Exception as e:
            await send_reply(f"🤖 عذراً، حدث خطأ في تحليل العميل: {e}")
        return True

    if "[تحليل_كل_العملاء]" in reply_text:
        try:
            if database.pool:
                async with database.pool.acquire() as conn:
                    transactions = await conn.fetch("SELECT user_id, details FROM transactions WHERE type = 'مبيعات_بقالة' AND is_reverted = FALSE AND date >= CURRENT_DATE - INTERVAL '30 days'")
                    if not transactions:
                        await send_reply("?? **تحليل system:**\nلا توجد أي مبيعات مسجلة للبقالات خلال الـ 30 يوماً الماضية.")
                        return True
                        
                    client_sales = {}
                    for t in transactions:
                        match = re.search(r"(\d+)\s*كرت", t['details'])
                        if match:
                            client_sales[t['user_id']] = client_sales.get(t['user_id'], 0) + int(match.group(1))
                            
                    sorted_clients = sorted(client_sales.items(), key=lambda x: x[1], reverse=True)
                    report = "🏆 **ترتيب أفضل العملاء سحباً للكروت (خلال 30 يوماً):**\n\n"
                    for i, (uid, total_qty) in enumerate(sorted_clients[:10], 1):
                        c_name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", uid)
                        medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else "▪️"))
                        report += f"{medal} **{c_name}:** باع {total_qty} كرت.\n"
                    await send_reply(report)
        except Exception as e:
            await send_reply(f"🤖 عذراً، حدث خطأ في تحليل السوق: {e}")
        return True

    if "[كشف_مفصل|" in reply_text:
        try:
            parts = reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")
            client_name_ai = normalize_arabic_name(parts[1].strip())
            start_date_str, end_date_str = parts[2].strip(), parts[3].strip()
            
            from datetime import datetime
            start_date_obj = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date_obj = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            
            if database.pool:
                async with database.pool.acquire() as conn:
                    clients = await conn.fetch("SELECT user_id, name, debt FROM users WHERE role = 'client'")
                    client_row = next((c for c in clients if normalize_arabic_name(c['name']) == client_name_ai or client_name_ai in normalize_arabic_name(c['name'])), None)
                    
                    if client_row:
                        # 🌟 حماية خصوصية الوكيل: إخفاء عمليات التسديدات من كشف الحساب المفصل للشبكة وتجاهل العمليات الملغاة
                        transactions = await conn.fetch('''SELECT date, type, amount, details FROM transactions WHERE user_id = $1 AND is_reverted = FALSE AND wallet_type = 'manager' AND date >= $2::date AND date <= $3::date + interval '1 day' ORDER BY date DESC''', client_row['user_id'], start_date_obj, end_date_obj)
                        from pdf_generator import generate_detailed_statement
                        pdf_buffer = await asyncio.to_thread(generate_detailed_statement, client_row['name'], Decimal(client_row['debt']), transactions, start_date_str, end_date_str)
                        pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Detailed_Statement_{client_row['name']}.pdf")
                        await message.answer_document(document=pdf_file, caption=f"📑 **كشف حساب مفصل (A4)**\nالعميل: {client_row['name']}\nمن: {start_date_str}\nإلى: {end_date_str}")
                    else:
                        await send_reply(f"🤖 لم أجد عميلاً باسم '{parts[1].strip()}'.")
        except Exception as e:
            await send_reply(f"🤖 عذراً، حدث خطأ في استخراج الكشف المفصل: {e}")
        return True

    if "[تقرير_اكسل|" in reply_text:
        try:
            parts = reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")
            start_date, end_date = parts[1].strip(), parts[2].strip()
            wait_msg = await message.answer(f"⏳ جاري استخراج التقرير المالي الشامل (Excel) من {start_date} إلى {end_date}...")
            
            # 🌟 التعديل: استدعاء الدالة مباشرة بدون استيراد لأنها في نفس الملف
            excel_stream = await generate_detailed_excel_report(start_date, end_date)
            excel_document = BufferedInputFile(excel_stream.read(), filename=f"Report_{start_date}_to_{end_date}.xlsx")
            
            await wait_msg.delete()
            await message.answer_document(document=excel_document, caption=f"📊 **التقرير المالي الشامل (Excel)**\nالفترة: من {start_date} إلى {end_date}")
        except Exception as e:
            await send_reply(f"🤖 عذراً، حدث خطأ في استخراج التقرير: {e}")
        return True

    if "[تقرير_التسديدات|" in reply_text:
        if user_id != ADMIN_ID:
            await send_reply("⛔ عذراً، هذا التقرير خاص بالوكيل فقط ولا يمكن للمدير العام الاطلاع عليه.")
            return True
        try:
            parts = reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")
            start_date, end_date = parts[1].strip(), parts[2].strip()
            wait_msg = await message.answer(f"⏳ جاري استخراج تقرير التسديدات السري من {start_date} إلى {end_date}...")
            
            # 1. استخراج الإكسل
            from core_accounting import generate_telecom_excel_report
            excel_stream = await generate_telecom_excel_report(start_date, end_date)
            excel_document = BufferedInputFile(excel_stream.read(), filename=f"Telecom_Secret_Report_{start_date}_to_{end_date}.xlsx")
            
            # 2. استخراج الـ PDF (الجديد)
            from pdf_generator import generate_telecom_secret_pdf
            from decimal import Decimal
            from datetime import datetime
            
            start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
            end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date()
            
            if database.pool:
                async with database.pool.acquire() as conn:
                    # 🌟 الإصلاح المحاسبي: خصم الاستردادات من إجمالي مبيعات التسديدات
                    sales_gross = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسديد_باقة', 'تسديد_باقة_وكيل') AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
                    refunds = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'استرداد_تسديد' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
                    total_sales = Decimal(sales_gross or 0) - Decimal(refunds or 0)
                    raw_profit = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM telecom_profits WHERE amount > 0 AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
                    rev_profit = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM telecom_profits WHERE amount < 0 AND details LIKE '%إلغاء%' AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
                    total_profit = Decimal(raw_profit or 0) - abs(Decimal(rev_profit or 0))
                    total_collected = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'تحصيل_رصيد' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
                    total_recharged = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'تغذية_رصيد_بوابة' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
                    # 🌟 الإصلاح المحاسبي: جلب الرصيد من المحفظة الجديدة لكي يظهر الرقم الصحيح في التقرير
                    current_balance = await conn.fetchval("SELECT telecom_balance FROM agent_wallet WHERE id = 1")
            
            pdf_stream = await asyncio.to_thread(generate_telecom_secret_pdf, start_date, end_date, total_sales or 0, total_profit, total_collected or 0, total_recharged or 0, current_balance or 0)
            pdf_document = BufferedInputFile(pdf_stream.read(), filename=f"Telecom_Secret_Report_{start_date}_to_{end_date}.pdf")

            await wait_msg.delete()
            await message.answer_document(document=excel_document)
            await message.answer_document(document=pdf_document, caption=f"🤫 **تقرير التسديدات السري (خاص بالوكيل)**\nالفترة: من {start_date} إلى {end_date}\n*(هذا التقرير لا يراه المدير العام)*")
        except Exception as e:
            await send_reply(f"🤖 عذراً، حدث خطأ في استخراج التقرير السري: {e}")
        return True

        
    if "[زيارة_ميدانية|" in reply_text:
        try:
            client_name_ai = normalize_arabic_name(reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")[1].strip())
            if database.pool:
                async with database.pool.acquire() as conn:
                    clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client'")
                    client_row = next((c for c in clients if normalize_arabic_name(c['name']) == client_name_ai or client_name_ai in normalize_arabic_name(c['name'])), None)
                    
                    if client_row:
                        inv_items = await conn.fetch("SELECT card_type, quantity FROM client_inventory WHERE user_id = $1 AND quantity > 0", client_row['user_id'])
                        inv_text = "".join([f"▪️ {item['quantity']} كرت ({item['card_type']})\n" for item in inv_items]) or "لا يوجد كروت مسجلة في مخزونه حالياً.\n"
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="✅ العدد مطابق (لم يبع شيء)", callback_data=f"visit_match_{client_row['user_id']}")],
                            [InlineKeyboardButton(text="✏️ إدخال الجرد الفعلي", callback_data=f"visit_inventory_{client_row['user_id']}")],
                            [InlineKeyboardButton(text="❌ إلغاء الزيارة", callback_data="cancel_action")]
                        ])
                        await message.answer(f"📍 **بدء زيارة ميدانية: {client_row['name']}**\n\n📦 **مخزون العميل المسجل في النظام:**\n{inv_text}\nهل قمت بمطابقة الدرج والعدد صحيح؟ أم تريد إدخال الجرد الجديد؟", reply_markup=kb)
                    else:
                        await send_reply(f"??️ بحثت عن عميل باسم '{client_name_ai}' ولم أجده.")
        except Exception:
            await send_reply("🎙️ عذراً، حدث خطأ في بدء الزيارة.")
        return True

    if "[حفظ_معلومة|" in reply_text:
        try:
            fact = reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")[1].strip()
            await database.add_learned_fact(fact, user_id)
            title = "يا دكتور وليد" if user_id == ADMIN_ID else "يا شيخ رهيب"
            await send_reply(f"🧠 **تم الحفظ في الذاكرة الدائمة!**\nالمعلومة: {fact}\nلن أنسى ذلك أبداً {title}.")
        except Exception:
            await send_reply("🎙️ عذراً، لم أتمكن من حفظ المعلومة بشكل صحيح.")
        return True

    if "[مسودة_مجمعة|" in reply_text:
        try:
            json_str = reply_text[reply_text.find("[مسودة_مجمعة|")+14 : reply_text.rfind("]")]
            batch_data = json.loads(json_str)
            msg = "📝 **مسودة عمليات اليوم (إدخال مجمع):**\n\n"
            for i, item in enumerate(batch_data, 1):
                if item['action'] == 'collect': msg += f"{i}️⃣ 💵 **تحصيل:** {item.get('client', 'مجهول')} ({int(Decimal(str(item.get('amount', 0))))} ريال)\n"
                elif item['action'] == 'expense': msg += f"{i}️⃣ 📉 **مصروفات:** {item.get('details', 'عام')} ({int(Decimal(str(item.get('amount', 0))))} ريال)\n"
                elif item['action'] == 'give': msg += f"{i}️⃣ 📦 **تسليم:** {item.get('client', 'مجهول')} ({item.get('qty', 0)} كرت {item.get('card_type', '')})\n"
                elif item['action'] == 'damaged': msg += f"{i}️⃣ 💔 **توالف:** ({int(Decimal(str(item.get('amount', 0))))} ريال)\n"
            msg += "\n❓ **هل أعتمد جميع هذه العمليات دفعة واحدة؟**"
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ اعتماد الكل", callback_data="approve_batch_tx")], [InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_action")]])
            await state.update_data(batch_tx_data=json_str)
            await message.answer(msg, reply_markup=kb)
            await state.set_state(BatchProcessingFlow.waiting_for_approval)
        except Exception as e:
            await send_reply(f"🎙️ عذراً، حدث خطأ في تحليل المسودة المجمعة: {e}")
        return True

    if "[تنفيذ_تسديد|" in reply_text:
        try:
            parts = reply_text.replace("[", "").replace("]", "").split("|")
            client_name_ai = normalize_arabic_name(parts[1].strip())
            amount_ai = Decimal(parts[2].strip())
            if database.pool:
                async with database.pool.acquire() as conn:
                    clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client'")
                    client_row = next((c for c in clients if normalize_arabic_name(c['name']) == client_name_ai or client_name_ai in normalize_arabic_name(c['name'])), None)
                    if client_row:
                        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ نعم، استلمت الكاش", callback_data=f"ai_confirm_collect_{client_row['user_id']}_{amount_ai}")], [InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_action")]])
                        await message.answer(f"🎙️ **system:**\nسمعتك تقول أنك استلمت **{int(amount_ai)} ريال** من **{client_row['name']}**.\nهل أؤكد العملية؟", reply_markup=kb)
                    else:
                        await send_reply(f"🎙️ سمعت اسم '{parts[1].strip()}' لكنني لم أجده في النظام.")
        except Exception:
            await send_reply("🎙️ عذراً، لم أفهم المبالغ والأسماء بوضوح.")
        return True

    if "[مطابقة_صوتية|" in reply_text:
        try:
            parts = reply_text[reply_text.find("[")+1 : reply_text.find("]")].split("|")
            client_name_ai = normalize_arabic_name(parts[1].strip())
            spoken_paid, spoken_taken = Decimal(parts[2].strip()), Decimal(parts[3].strip())
            
            if database.pool:
                async with database.pool.acquire() as conn:
                    if client_name_ai in ["الاداره", "الادارة", "الشبكة", "المدير"]:
                        db_paid = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'تسديد_للشبكة' AND date >= date_trunc('month', CURRENT_DATE)")
                        db_taken = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'استلام_من_الشبكة' AND date >= date_trunc('month', CURRENT_DATE)")
                        client_id, real_name = 0, "الإدارة العامة"
                    else:
                        clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client'")
                        client_row = next((c for c in clients if normalize_arabic_name(c['name']) == client_name_ai or client_name_ai in normalize_arabic_name(c['name'])), None)
                        if not client_row: return await send_reply(f"🤖 لم أجد عميلاً باسم '{client_name_ai}'.")
                        client_id, real_name = client_row['user_id'], client_row['name']
                        # 🌟 الإصلاح المحاسبي: تضمين المرتجعات والمبيعات الآجلة (الكروت الإلكترونية) وتجاهل التراجعات
                        db_paid = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND wallet_type = 'manager' AND type IN ('تسديد_من_عميل', 'مرتجع_من_عميل') AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)", client_id)
                        db_taken = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND wallet_type = 'manager' AND type IN ('تسليم_لعميل', 'مبيعات_آجلة') AND is_reverted = FALSE AND date >= date_trunc('month', CURRENT_DATE)", client_id)
                        
                    db_paid, db_taken = Decimal(db_paid or 0), Decimal(db_taken or 0)
                    diff_paid, diff_taken = spoken_paid - db_paid, spoken_taken - db_taken
                    
                    report = f"🕵️‍♂️ **نتيجة المطابقة الصوتية لـ ({real_name}):**\n\n"
                    if diff_paid == 0 and diff_taken == 0:
                        report += "✅ **الحساب مطابق 100% مع البوت!**\nلا يوجد أي نقص أو زيادة."
                        await send_reply(report)
                    else:
                        report += "⚠️ **يوجد اختلاف في الحساب!**\n\n"
                        if diff_paid != 0: report += f"▪️ **التسديدات:** مسجل في البوت ({int(db_paid)}) وأنت قلت ({int(spoken_paid)}). الفارق: {int(abs(diff_paid))} ريال.\n"
                        if diff_taken != 0: report += f"▪️ **المسحوبات:** مسجل في البوت ({int(db_taken)}) وأنت قلت ({int(spoken_taken)}). الفارق: {int(abs(diff_taken))} ريال.\n"
                        report += "\nهل تريد أن يقوم البوت بتسوية الفارق تلقائياً؟"
                        await state.update_data(fix_client_id=client_id, fix_diff_paid=str(diff_paid), fix_diff_taken=str(diff_taken))
                        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛠️ نعم، تسوية الفارق تلقائياً", callback_data="voice_audit_auto_fix")], [InlineKeyboardButton(text="❌ لا، سأراجع الدفتر", callback_data="cancel_action")]])
                        await message.answer(report, reply_markup=kb)
        except Exception as e:
            await send_reply(f"🤖 عذراً، حدث خطأ في المطابقة الصوتية: {e}")
        return True

    if "[بدء_مطابقة_العملاء]" in reply_text:
        await send_reply("🕵️‍♂️ **معالج المطابقة الشهرية:**\nأدخل الفترة المراد مطابقتها (مثال: هذا الشهر، أو من 2024-05-01 إلى 2024-05-30):")
        await state.set_state(AIMonthlyAuditFlow.waiting_for_dates)
        return True
        
    return False
    
# ================= 1. معالجة الرسائل النصية =================
@router.message(F.text)
async def smart_ai_text_reply(message: types.Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    user_text = message.text
    normalized_user_text = normalize_arabic_name(user_text)
    
    update_memory(user_id, "user", user_text)

    current_state = await state.get_state()

    if current_state == SmartVoiceFlow.waiting_for_confirmation.state:
        if any(word in user_text for word in ["نعم", "ايوه", "اي", "صحيح", "اكيد", "هو"]):
            data = await state.get_data()
            await state.clear()
            return await execute_smart_action(message, data['smart_client_id'], data['smart_client_name'], data['smart_action'], Decimal(data['smart_amount']), user_id)
        elif any(word in user_text for word in ["لا", "غلط", "مش", "ليس"]):
            await state.clear()
            if len(user_text.split()) <= 2:
                return await message.answer("❌ حسناً، تم الإلغاء. من فضلك أرسل الاسم الصحيح.")
        else:
            return await message.answer("🤔 لم أفهم تأكيدك. هل تقصد هذا العميل؟ (أجب بنعم أو لا)")
            
    if await state.get_state() is not None: return

    if user_id == int(NETWORK_OWNER_ID) and any(word in user_text for word in ["تقرير", "التقرير", "حسابات", "الحسابات", "الصافي", "ملخص"]):
        is_approved = False
        previous_balance = Decimal('0.0')
        start_date_obj = None
        end_date_obj = None
        month_str = ""

        if database.pool:
            async with database.pool.acquire() as conn:
                val = await conn.fetchval("SELECT value FROM settings WHERE key = 'share_monthly_report'")
                if val == "on":
                    # 👈 جلب أحدث تقرير معتمد من الأرشيف
                    latest_report = await conn.fetchrow("SELECT month_year, created_at FROM reports_archive ORDER BY created_at DESC LIMIT 1")
                    if latest_report:
                        is_approved = True
                        month_str = latest_report['month_year'] # مثال: 2026-05
                        
                        # حساب بداية ونهاية الشهر المعتمد
                        from datetime import datetime
                        import calendar
                        start_date_obj = datetime.strptime(f"{month_str}-01", '%Y-%m-%d').date()
                        last_day = calendar.monthrange(start_date_obj.year, start_date_obj.month)[1]
                        end_date_obj = datetime.strptime(f"{month_str}-{last_day}", '%Y-%m-%d').date()
                        
                            # حساب الرصيد السابق بناءً على بداية الشهر المعتمد
                        prev_received = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE AND date < $1::date", start_date_obj)
                        prev_paid = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسديد_للشبكة', 'مصروفات', 'كروت_تالفة', 'نسبة_الوكيل', 'مرتجع_للشبكة') AND is_reverted = FALSE AND date < $1::date", start_date_obj)
                        previous_balance = Decimal(prev_received or 0) - Decimal(prev_paid or 0)

        if not is_approved:
            msg = "عفواً يا شيخ رهيب، التقرير حالياً في مرحلة الجرد مع الدكتور وليد. سأشعركم فور اعتماده."
            if message.voice:
                try:
                    voice_buffer, file_ext = await text_to_voice(msg)
                    voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
                    return await message.answer_voice(voice=voice_file, caption="⚠️")
                except: return await message.answer(msg)
            else:
                return await message.answer(msg)

        try:
            await bot.send_message(ADMIN_ID, f"🔔 تنبيه صامت: المدير العام بدأ الآن في استعراض تقرير شهر ({month_str}) معي.")
        except: pass
        
        # 👈 حفظ التواريخ في الذاكرة لتستخدمها دوال المناقشة
        report_status["meeting_minutes"] = []
        report_status["previous_balance"] = previous_balance
        report_status["start_date"] = start_date_obj
        report_status["end_date"] = end_date_obj
        report_status["month_str"] = month_str
        
        await state.set_state(ManagerReportFlow.step_1_previous)

        reply_text = f"حياك الله يا شيخ رهيب. نبدأ بسم الله نراجع تقرير شهر ({month_str}).\n\n**الخطوة 1:** المبلغ المتبقي من الشهر السابق هو: **{int(abs(previous_balance))} ريال**.\nصح أو لا؟"
        
        if message.voice:
            try:
                voice_buffer, file_ext = await text_to_voice(reply_text.replace("**", ""))
                voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
                return await message.answer_voice(voice=voice_file, caption=f"🎙️ **الخطوة 1:** المتبقي من الشهر السابق: {int(abs(previous_balance))} ريال")
            except: return await message.answer(reply_text)
        else:
            return await message.answer(reply_text)

    if user_id in [ADMIN_ID, int(NETWORK_OWNER_ID)]:
        if database.pool:
            async with database.pool.acquire() as conn:
                if any(word in user_text for word in ["حسابات العملاء", "ديون السوق", "ديون العملاء", "كشف حسابات"]):
                    clients = await conn.fetch("SELECT name, debt FROM users WHERE role = 'client' ORDER BY debt DESC")
                    if clients:
                        text = "📊 **ديون السوق الحالية:**\n\n"
                        for c in clients: text += f"👤 {c['name']}: {c['debt']} ريال\n"
                        return await message.answer(text)
                    return await message.answer("لا يوجد عملاء مسجلين حالياً.")

                clients = await conn.fetch("SELECT user_id, name, debt FROM users WHERE role = 'client'")
                
                found_client = None
                is_guess = False
                
                for c in clients:
                        if normalize_arabic_name(c['name']) in normalized_user_text:
                            found_client = c
                            break

                if not found_client:
                    client_names_normalized = [normalize_arabic_name(c['name']) for c in clients]
                    words = normalized_user_text.split()
                    for word in words:
                        if len(word) > 3:
                            matches = difflib.get_close_matches(word, client_names_normalized, n=1, cutoff=0.6)
                            if matches:
                                guessed_name_norm = matches[0]
                                found_client = next(c for c in clients if normalize_arabic_name(c['name']) == guessed_name_norm)
                                is_guess = True
                                break

                if found_client:
                    action = "visit"
                    amount = Decimal('0.0')
                    
                    if any(word in user_text for word in ["كشف حساب", "تقرير", "بي دي اف", "pdf"]):
                        action = "pdf_statement"
                    elif any(word in user_text for word in ["كم حساب", "كم حسابه", "كم دين", "دينه"]):
                        action = "query_debt"
                    elif any(word in user_text for word in ["كم كروت", "كم متبقي", "مخزون", "كروته"]):
                        action = "query_inv"
                    
                    if is_guess:
                        await state.update_data(smart_client_id=found_client['user_id'], smart_client_name=found_client['name'], smart_action=action, smart_amount=str(amount))
                        return await message.answer(f"🤔 لم أسمع الاسم بوضوح...\nهل تقصد العميل (**{found_client['name']}**)؟\n*(أجب بنعم أو لا)*")
                    else:
                        return await execute_smart_action(message, found_client['user_id'], found_client['name'], action, amount, user_id)

    if not GROQ_API_KEYS:
        return await message.answer("⚠️ مفتاح GROQ_API_KEY غير موجود في الإعدادات.")

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    system_prompt, client_name = await get_system_prompt(user_id)

    try:
        reply_text = await generate_groq_response(system_prompt, user_id)

        # معالجة أوامر الإدارة باستخدام الدالة الموحدة الجديدة
        if user_id in [ADMIN_ID, int(NETWORK_OWNER_ID)]:
            is_processed = await process_admin_ai_tags(reply_text, message, bot, user_id, state)
            if is_processed:
                return
            
            # تنظيف الرد من أي أكواد متبقية
            clean_reply = re.sub(r'\[.*?\]', '', reply_text).strip()
            if clean_reply:
                await message.answer(clean_reply)
            return

        # معالجة أوامر العملاء والزوار (الذكاء الاستقلالي)
        clean_reply = await process_client_tags(message, bot, user_id, client_name, reply_text)
        
        if clean_reply:
            clean_reply = clean_reply[:3900] # 🌟 حماية تليجرام: قص الرسالة إذا تجاوزت الحد المسموح
            await message.answer(clean_reply)
            
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"🔥 خطأ مفصل:\n{error_details}")
        await message.answer(f"⚠️ **رسالة للمبرمج (كشف الخطأ):**\n{str(e)}")

@router.message(F.voice)
async def smart_ai_voice_reply(message: types.Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    
    if not GROQ_API_KEYS:
        return await message.answer("⚠️ مفتاح GROQ_API_KEY غير موجود في الإعدادات.")

    await bot.send_chat_action(chat_id=message.chat.id, action="record_voice")

    try:
        file = await bot.get_file(message.voice.file_id)
        downloaded_file = await bot.download_file(file.file_path)
        audio_bytes = downloaded_file.read()

        user_text = await transcribe_audio(audio_bytes)
        
        if not user_text.strip():
            return await message.answer("🎙️ عذراً، لم أتمكن من سماع الصوت بوضوح.")

        update_memory(user_id, "user", user_text)
        normalized_user_text = normalize_arabic_name(user_text)

        current_state = await state.get_state()
        if current_state == SmartVoiceFlow.waiting_for_confirmation.state:
            if any(word in user_text for word in ["نعم", "ايوه", "اي", "صحيح", "اكيد", "هو"]):
                data = await state.get_data()
                await state.clear()
                return await execute_smart_action(
                    message, 
                    data['smart_client_id'], 
                    data['smart_client_name'], 
                    data['smart_action'], 
                    Decimal(data['smart_amount']), 
                    user_id
                )
            elif any(word in user_text for word in ["لا", "غلط", "مش", "ليس"]):
                await state.clear()
                if len(user_text.split()) <= 2:
                    return await message.answer("❌ حسناً، تم الإلغاء. من فضلك أرسل الاسم الصحيح.")
            else:
                return await message.answer("🤔 لم أفهم تأكيدك. هل تقصد هذا العميل؟ (أجب بنعم أو لا)")

        # 👈 إضافة ذكية لاكتشاف طلب الجرد من الوكيل (صوتياً)
        if user_id == ADMIN_ID and any(word in user_text for word in ["جرد", "نبدأ الجرد", "جرد الشهر"]):
            msg = "أبشر يا دكتور. هل تريد جرد هذا الشهر؟" 
            if database.pool:
                async with database.pool.acquire() as conn:
                    last_report = await conn.fetchrow("SELECT created_at FROM reports_archive ORDER BY created_at DESC LIMIT 1")
                    now = datetime.now()
                    
                    if last_report:
                        last_date = last_report['created_at']
                        days_passed = (now - last_date).days
                        last_date_str = last_date.strftime('%Y-%m-%d')
                        today_str = now.strftime('%Y-%m-%d')
                        
                        if 25 <= days_passed <= 35:
                            msg = f"يا دكتور، مر **{days_passed} يوم** على آخر تقرير (كان بتاريخ {last_date_str}).\nهل تريد جرد هذه الفترة (إلى اليوم)؟ أم تريد تحديد تواريخ معينة؟\n\n*(اكتب: 'نعم' أو اكتب التواريخ مثل: من 2024-04-01 إلى 2024-04-30)*"
                        elif days_passed < 25:
                            msg = f"يا دكتور، مر **{days_passed} يوم فقط** على آخر تقرير (كان بتاريخ {last_date_str}).\nهل تريد جرد هذه الفترة القصيرة؟ أم تريد تحديد تواريخ معينة؟\n\n*(اكتب: 'نعم' أو اكتب التواريخ)*"
                        else:
                            msg = f"يا دكتور، تأخرنا! مر **{days_passed} يوم** على آخر تقرير (كان بتاريخ {last_date_str}).\nهل تريد جرد الفترة كاملة إلى اليوم؟ أم تريد تحديد تواريخ معينة؟\n\n*(اكتب: 'نعم' أو اكتب التواريخ)*"
                        
                        await state.update_data(suggested_start=last_date_str, suggested_end=today_str)
                    else:
                        first_day = now.replace(day=1).strftime('%Y-%m-%d')
                        today_str = now.strftime('%Y-%m-%d')
                        msg = f"أبشر يا دكتور. هذا أول جرد في النظام!\nهل تريد جرد هذا الشهر (من {first_day} إلى {today_str})؟ أم تريد تحديد تواريخ معينة؟\n\n*(اكتب: 'نعم' أو اكتب التواريخ)*"
                        await state.update_data(suggested_start=first_day, suggested_end=today_str)
                        
            try:
                voice_buffer, file_ext = await text_to_voice(msg)
                voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
                await message.answer_voice(voice=voice_file, caption="🎙️ (رد شهاب)")
            except:
                await message.answer(msg)
                
            return await state.set_state(AgentInteractiveAudit.waiting_for_dates)
            
        if await state.get_state() is not None:
            return

        if user_id == int(NETWORK_OWNER_ID) and any(word in user_text for word in ["تقرير", "التقرير", "حسابات", "الحسابات", "الصافي", "ملخص"]):
            is_approved = False
            previous_balance = Decimal('0.0')
            start_date_obj = None
            end_date_obj = None
            month_str = ""

            if database.pool:
                async with database.pool.acquire() as conn:
                    val = await conn.fetchval("SELECT value FROM settings WHERE key = 'share_monthly_report'")
                    if val == "on":
                        # 👈 جلب أحدث تقرير معتمد من الأرشيف
                        latest_report = await conn.fetchrow("SELECT month_year, created_at FROM reports_archive ORDER BY created_at DESC LIMIT 1")
                        if latest_report:
                            is_approved = True
                            month_str = latest_report['month_year'] # مثال: 2026-05
                            
                            # حساب بداية ونهاية الشهر المعتمد
                            from datetime import datetime
                            import calendar
                            start_date_obj = datetime.strptime(f"{month_str}-01", '%Y-%m-%d').date()
                            last_day = calendar.monthrange(start_date_obj.year, start_date_obj.month)[1]
                            end_date_obj = datetime.strptime(f"{month_str}-{last_day}", '%Y-%m-%d').date()
                            
                            # حساب الرصيد السابق بناءً على بداية الشهر المعتمد
                            prev_received = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE AND date < $1::date", start_date_obj)
                            prev_paid = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسديد_للشبكة', 'مصروفات', 'كروت_تالفة', 'نسبة_الوكيل', 'مرتجع_للشبكة') AND is_reverted = FALSE AND date < $1::date", start_date_obj)
                            previous_balance = Decimal(prev_received or 0) - Decimal(prev_paid or 0)

            if not is_approved:
                msg = "عفواً يا شيخ رهيب، التقرير حالياً في مرحلة الجرد مع الدكتور وليد. سأشعركم فور اعتماده."
                if message.voice:
                    try:
                        voice_buffer, file_ext = await text_to_voice(msg)
                        voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
                        return await message.answer_voice(voice=voice_file, caption="⚠️")
                    except:
                        return await message.answer(msg)
                else:
                    return await message.answer(msg)

            try:
                await bot.send_message(ADMIN_ID, f"🔔 تنبيه صامت: المدير العام بدأ الآن في استعراض تقرير شهر ({month_str}) معي.")
            except:
                pass
            
            # 👈 حفظ التواريخ في الذاكرة لتستخدمها دوال المناقشة
            report_status["meeting_minutes"] = []
            report_status["previous_balance"] = previous_balance
            report_status["start_date"] = start_date_obj
            report_status["end_date"] = end_date_obj
            report_status["month_str"] = month_str
            
            await state.set_state(ManagerReportFlow.step_1_previous)

            reply_text = f"حياك الله يا شيخ رهيب. نبدأ بسم الله نراجع تقرير شهر ({month_str}).\n\n**الخطوة 1:** المبلغ المتبقي من الشهر السابق هو: **{int(abs(previous_balance))} ريال**.\nصح أو لا؟"
            
            if message.voice:
                try:
                    voice_buffer, file_ext = await text_to_voice(reply_text.replace("**", ""))
                    voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
                    return await message.answer_voice(voice=voice_file, caption=f"🎙️ **الخطوة 1:** المتبقي من الشهر السابق: {int(abs(previous_balance))} ريال")
                except:
                    return await message.answer(reply_text)
            else:
                return await message.answer(reply_text)

        if user_id in [ADMIN_ID, int(NETWORK_OWNER_ID)]:
            if database.pool:
                async with database.pool.acquire() as conn:
                    if any(word in user_text for word in ["حسابات العملاء", "ديون السوق", "ديون العملاء", "كشف حسابات"]):
                        clients = await conn.fetch("SELECT name, debt FROM users WHERE role = 'client' ORDER BY debt DESC")
                        if clients:
                            text = "📊 **ديون السوق الحالية:**\n\n"
                            for c in clients:
                                text += f"👤 {c['name']}: {int(c['debt'])} ريال\n"
                            return await message.answer(text)
                        return await message.answer("لا يوجد عملاء مسجلين حالياً.")

                    clients = await conn.fetch("SELECT user_id, name, debt FROM users WHERE role = 'client'")
                    
                    found_client = None
                    is_guess = False
                    
                    for c in clients:
                        if normalize_arabic_name(c['name']) in normalized_user_text:
                            found_client = c
                            break
                    
                    if not found_client:
                        client_names_normalized = [normalize_arabic_name(c['name']) for c in clients]
                        words = normalized_user_text.split()
                        for word in words:
                            if len(word) > 3:
                                matches = difflib.get_close_matches(word, client_names_normalized, n=1, cutoff=0.6)
                                if matches:
                                    guessed_name_norm = matches[0]
                                    found_client = next(c for c in clients if normalize_arabic_name(c['name']) == guessed_name_norm)
                                    is_guess = True
                                    break

                    if found_client:
                        action = "visit"
                        amount = Decimal('0.0')
                        
                        if any(word in user_text for word in ["كشف حساب", "تقرير", "بي دي اف", "pdf"]):
                            action = "pdf_statement"
                        elif any(word in user_text for word in ["كم حساب", "كم حسابه", "كم دين", "دينه"]):
                            action = "query_debt"
                        elif any(word in user_text for word in ["كم كروت", "كم متبقي", "مخزون", "كروته"]):
                            action = "query_inv"
                        
                        if is_guess:
                            await state.update_data(
                                smart_client_id=found_client['user_id'], 
                                smart_client_name=found_client['name'], 
                                smart_action=action, 
                                smart_amount=str(amount)
                            )
                            voice_buffer, file_ext = await text_to_voice(f"لم أسمع الاسم بوضوح، هل تقصد العميل {found_client['name']}؟")
                            voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
                            
                            if file_ext == "mp3":
                                await message.answer_audio(audio=voice_file, caption=f"🤔 هل تقصد العميل (**{found_client['name']}**)؟\n*(أجب بنعم أو لا)*")
                            else:
                                await message.answer_voice(voice=voice_file, caption=f"🤔 هل تقصد العميل (**{found_client['name']}**)؟\n*(أجب بنعم أو لا)*")
                            
                            return await state.set_state(SmartVoiceFlow.waiting_for_confirmation)
                        else:
                            return await execute_smart_action(message, found_client['user_id'], found_client['name'], action, amount, user_id)

        system_prompt, client_name = await get_system_prompt(user_id)
        reply_text = await generate_groq_response(system_prompt, user_id)

        # معالجة أوامر الإدارة باستخدام الدالة الموحدة الجديدة
        if user_id in [ADMIN_ID, int(NETWORK_OWNER_ID)]:
            is_processed = await process_admin_ai_tags(reply_text, message, bot, user_id, state)
            if is_processed:
                return
            
            # تنظيف الرد من أي أكواد متبقية
            clean_reply = re.sub(r'\[.*?\]', '', reply_text).strip()
            if clean_reply:
                try:
                    voice_buffer, file_ext = await text_to_voice(clean_reply)
                    voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
                    await message.answer_voice(voice=voice_file, caption="🎙️ (رد صوتي من system)")
                except Exception as tts_error:
                    await message.answer(clean_reply)
            return

        # معالجة أوامر العملاء والزوار
        clean_reply = await process_client_tags(message, bot, user_id, client_name, reply_text)
        
        if clean_reply:
            try:
                voice_buffer, file_ext = await text_to_voice(clean_reply)
                voice_file = BufferedInputFile(voice_buffer.read(), filename=f"reply.{file_ext}")
                await message.answer_voice(voice=voice_file, caption="🎙️ (رد صوتي من system)")
            except Exception as tts_error:
                await message.answer(clean_reply)

    except Exception as e:
        print(f"AI Voice Error: {e}")
        await message.answer("عذراً، لم أتمكن من معالجة الصوت حالياً.")

# ================= 3. تنفيذ الأوامر الذكية (Callback) =================
@router.callback_query(F.data.startswith("ai_confirm_collect_"))
async def execute_ai_collect(callback: types.CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    client_id = int(parts[3])
    amount = Decimal(parts[4])
    
    # 🌟 الاستدعاء المحلي هنا لتجنب مشكلة (Circular Import)
    from core_accounting import core_collect_debt
    result = await core_collect_debt(client_id, amount)
    
    if result["status"] == "error":
        return await callback.answer(result["message"], show_alert=True)
        
    await callback.message.edit_text(f"✅ **تم التنفيذ بنجاح!**\nتم خصم {int(amount)} ريال من دين العميل ودخولها في صندوق الكاش.")
    
    try:
        await bot.send_message(client_id, f"🌟 **إشعار استلام نقدي (VIP):**\n\nحياك الله يا غالي، تم استلام دفعة نقدية منك يداً بيد بقيمة **{int(amount)} ريال**.\nشكراً لتعاملك الراقي والمستمر مع شبكة الشهاب نت!")
    except: pass

    try:
        from web_api import send_web_push
        await send_web_push(client_id, "💰 سند قبض (استلام كاش)", f"تم استلام دفعة نقدية بقيمة {int(amount)} ريال. شكراً لتعاملك معنا!")
    except Exception as e: pass

# ================= 4. تنفيذ المسودة المجمعة (Callback) =================
@router.callback_query(F.data == "approve_batch_tx")
async def execute_batch_tx(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    batch_data_str = data.get("batch_tx_data")
    if not batch_data_str:
        return await callback.answer("انتهت صلاحية هذه المسودة.", show_alert=True)
        
    batch_data = json.loads(batch_data_str)
    success_count = 0
    error_messages = []
    
    await callback.message.edit_text("⏳ جاري تنفيذ العمليات المجمعة...")
    
    # 🌟 التعديل: استدعاء المحرك المالي الجديد
    from core_accounting import FinancialEngine, TxType, FinancialError, core_process_return
    engine = FinancialEngine(database.pool)
    
    clients = []
    if database.pool:
        async with database.pool.acquire() as conn:
            clients = await conn.fetch("SELECT user_id, name FROM users WHERE role = 'client'")
            
    for item in batch_data:
        try:
            action = item['action']
            
            if action == 'collect':
                client_name_ai = normalize_arabic_name(item.get('client', ''))
                amount = Decimal(item.get('amount', 0))
                client_row = next((c for c in clients if normalize_arabic_name(c['name']) == client_name_ai or client_name_ai in normalize_arabic_name(c['name'])), None)
                if client_row:
                    try:
                        await engine.collect_debt(client_row['user_id'], amount)
                        success_count += 1
                    except FinancialError as e:
                        error_messages.append(f"خطأ في تحصيل {client_name_ai}: {e.message}")
                else:
                    error_messages.append(f"العميل {client_name_ai} غير موجود.")
                    
            elif action == 'expense':
                amount = Decimal(item.get('amount', 0))
                details = item.get('details', 'مصروفات عامة')
                try:
                    await engine.finance_action(TxType.EXPENSE, amount, details, source="(إدخال مجمع)")
                    success_count += 1
                except FinancialError as e:
                    error_messages.append(f"خطأ في المصروفات: {e.message}")
                
            elif action == 'give':
                client_name_ai = normalize_arabic_name(item.get('client', ''))
                qty = int(item.get('qty', 0))
                card_type = item.get('card_type', '')
                client_row = next((c for c in clients if normalize_arabic_name(c['name']) == client_name_ai or client_name_ai in normalize_arabic_name(c['name'])), None)
                if client_row:
                    try:
                        await engine.give_cards(client_row['user_id'], card_type, qty)
                        success_count += 1
                    except FinancialError as e:
                        error_messages.append(f"خطأ في تسليم {client_name_ai}: {e.message}")
                else:
                    error_messages.append(f"العميل {client_name_ai} غير موجود.")
                    
            elif action == 'damaged':
                amount = Decimal(item.get('amount', 0))
                # التوالف ما زالت تستخدم الدالة المستقلة
                res = await core_process_return('damaged', 0, amount, 0, "", source="(إدخال مجمع)")
                if res["status"] == "success": success_count += 1
                else: error_messages.append(f"خطأ في التوالف: {res['message']}")
                
        except Exception as e:
            print(f"Batch item error: {e}")
            error_messages.append(f"خطأ غير متوقع: {str(e)}")
            
    await state.clear()
    
    final_msg = f"✅ **تم التنفيذ!**\nتم اعتماد وتسجيل ({success_count}) عمليات بنجاح."
    if error_messages:
        final_msg += "\n\n⚠️ **ملاحظات (لم يتم تنفيذها):**\n" + "\n".join(error_messages)
        
    await callback.message.edit_text(final_msg)

@router.callback_query(F.data == "voice_audit_auto_fix")
async def voice_audit_auto_fix(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    client_id = data.get('fix_client_id')
    diff_paid = Decimal(data.get('fix_diff_paid', '0'))
    diff_taken = Decimal(data.get('fix_diff_taken', '0'))
    
    await callback.message.edit_text("⏳ جاري تسوية الحساب...")
    
    missing_txs = []
    if client_id == 0:
        if diff_paid > 0: missing_txs.append({"type": "تسديد_للشبكة", "amount": float(diff_paid), "details": "تسوية مطابقة صوتية"})
        elif diff_paid < 0: missing_txs.append({"type": "تسديد_للشبكة", "amount": float(abs(diff_paid)), "details": "تسوية مطابقة صوتية (خصم تسديد زائد)"})
        
        if diff_taken > 0: missing_txs.append({"type": "استلام_من_الشبكة", "amount": float(diff_taken), "details": "تسوية مطابقة صوتية"})
        elif diff_taken < 0: missing_txs.append({"type": "استلام_من_الشبكة", "amount": float(abs(diff_taken)), "details": "تسوية مطابقة صوتية (خصم استلام زائد)"})
    else:
        if diff_paid > 0: missing_txs.append({"type": "تسديد_من_عميل", "amount": float(diff_paid), "details": "تسوية مطابقة صوتية"})
        elif diff_paid < 0: missing_txs.append({"type": "قيد_عكسي", "amount": float(abs(diff_paid)), "details": "تسوية مطابقة صوتية (إلغاء تسديد زائد)"})
        
        if diff_taken > 0: missing_txs.append({"type": "تسليم_لعميل", "amount": float(diff_taken), "details": "تسوية مطابقة صوتية"})
        elif diff_taken < 0: missing_txs.append({"type": "قيد_عكسي", "amount": float(abs(diff_taken)), "details": "تسوية مطابقة صوتية (إلغاء تسليم زائد)"})
        
    # 🌟 التعديل: استخدام المحرك المالي الجديد
    engine = FinancialEngine(database.pool)
    await engine.audit_auto_fix(client_id, missing_txs)
                        
    # 🌟 التعديل: إضافة ملاحظة تنبيهية للوكيل
    await callback.message.edit_text("✅ **تمت تسوية الحساب بنجاح!**\nالآن الحساب مطابق لدفترك 100%.\n*(ملاحظة: إذا كان الفارق بسبب كروت نسيت تسليمها في البوت، يجب إدخالها يدوياً لخصمها من المخزون)*")
    await state.clear()
