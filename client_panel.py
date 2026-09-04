from aiogram import Router, F, types, Bot
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from config import ADMIN_ID, NETWORK_OWNER_ID
import database
from pdf_generator import generate_client_statement
from decimal import Decimal, InvalidOperation
from datetime import datetime
import asyncio

router = Router()

# ================= الحالات (FSM) =================
class ClientOrderFlow(StatesGroup):
    waiting_for_remaining = State()
    waiting_for_new_order = State()

class DamagedCardClientFlow(StatesGroup):
    waiting_for_card_details = State()

# ================= دوال مساعدة للوحات المفاتيح =================
def get_client_main_keyboard():
    """لوحة مفاتيح عميل الجملة (البقالة)"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 حسابي ومخزوني", callback_data="client_account_inv")],
        [InlineKeyboardButton(text="📄 استخراج كشف حسابي", callback_data="client_pdf_statement")],
        [InlineKeyboardButton(text="🛒 طلب كروت شبكة", callback_data="client_order_cards")],
        [InlineKeyboardButton(text="💔 الإبلاغ عن كرت تالف", callback_data="report_damaged_card")],
        [InlineKeyboardButton(text="💡 طريقة الاستخدام", callback_data="client_help")]
    ])

def get_visitor_main_keyboard():
    """لوحة مفاتيح الزائر العادي (الطياري)"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 أسعار كروت الشبكة", callback_data="visitor_prices")],
        [InlineKeyboardButton(text="🏪 أقرب نقطة بيع (بقالة)", callback_data="visitor_nearest_pos")],
        [InlineKeyboardButton(text="🛠️ الدعم الفني وحلول المشاكل", callback_data="visitor_support")],
        [InlineKeyboardButton(text="ℹ️ معلومات عن الشبكة", callback_data="visitor_info"),
         InlineKeyboardButton(text="📍 موقعنا", callback_data="visitor_location")],
        [InlineKeyboardButton(text="📞 التواصل مع الإدارة", callback_data="contact_agent")]
    ])

# ================= القائمة الرئيسية (مع نظام الزوار والرد الذكي) =================
@router.message(CommandStart())
async def client_start(message: types.Message, bot: Bot):
    user_id = message.from_user.id
    
    # 🚨 نقطة التفتيش الإجبارية للوكيل والمدير 🚨
    if user_id == int(NETWORK_OWNER_ID):
        from unified_main import get_owner_main_keyboard
        kb = await get_owner_main_keyboard()
        return await message.answer("👑 **لوحة تحكم الإدارة العامة**\nأهلاً بك يا شيخ رهيب:", reply_markup=kb)
        
    if user_id == ADMIN_ID:
        from unified_main import get_main_admin_keyboard
        kb = await get_main_admin_keyboard()
        return await message.answer("👨‍💻 **لوحة تحكم وكيل فرع بني علي**\nحياك الله يا دكتور وليد:", reply_markup=kb)

    # --- تكملة كود العملاء والزوار ---
    name = message.from_user.first_name
    username = message.from_user.username
    username_str = f"(@{username})" if username else ""
    
    user_role = "visitor" # الافتراضي زائر
    is_new_user = False
    
    if database.pool:
        async with database.pool.acquire() as conn:
            existing_user = await conn.fetchrow("SELECT role FROM users WHERE user_id = $1", user_id)
            
            if not existing_user:
                is_new_user = True
                # تسجيله كزائر جديد
                await conn.execute('''
                    INSERT INTO users (user_id, name, role, debt)
                    VALUES ($1, $2, 'visitor', 0.0)
                ''', user_id, name)
            else:
                user_role = existing_user['role']
            
    # إشعار الوكيل بالزائر الجديد مع زر الترقية
    if is_new_user:
        upgrade_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌟 ترقية إلى عميل جملة (بقالة)", callback_data=f"upgrade_client_{user_id}")]
        ])
        try:
            await bot.send_message(
                ADMIN_ID, 
                f"🔔 **تنبيه: زائر جديد دخل النظام!**\n👤 الاسم: {name} {username_str}\n🆔 الايدي: `{user_id}`\n\n*(إذا كان هذا الشخص صاحب بقالة وتريد فتح النظام له، اضغط على زر الترقية أدناه)*",
                reply_markup=upgrade_kb
            )
        except: pass
    
    # توجيه المستخدم حسب رتبته (الرد الذكي)
    if user_role == "client":
        net_debt = Decimal('0.0')
        inv_count = 0
        if database.pool:
            async with database.pool.acquire() as conn:
                # 🌟 الإصلاح المحاسبي: جلب الدين والأرباح المعلقة لحساب الصافي
                user_data = await conn.fetchrow("SELECT debt, pending_profit FROM users WHERE user_id = $1", user_id)
                if user_data:
                    debt = Decimal(user_data['debt']) if user_data['debt'] else Decimal('0.0')
                    pending = Decimal(user_data['pending_profit']) if user_data['pending_profit'] else Decimal('0.0')
                    net_debt = debt # 🌟 إصلاح: العميل يرى دينه الإجمالي كاملاً
                    
                inv_val = await conn.fetchval("SELECT SUM(quantity) FROM client_inventory WHERE user_id = $1", user_id)
                inv_count = int(inv_val) if inv_val else 0
                
        await message.answer(
            f"يا هلا وغلا بصاحب بقالة ({name}) 🌹.\n"
            f"نورت نظام الشهاب نت. للتذكير يا غالي:\n\n"
            f"💵 حسابك الحالي (الدين الصافي): **{int(net_debt)} ريال**.\n"
            f"📦 كروتك المتبقية في الدرج: **{inv_count} كرت**.\n\n"
            f"كيف أقدر أخدمك اليوم؟ 👇",
            reply_markup=get_client_main_keyboard()
        )
    elif user_role == "visitor":
        await message.answer(
            f"أهلاً بك يا {name} في شبكة الشهاب نت 🌐!\n"
            f"إنترنت يسبق الخيال، وبنج يريح البال 🎮.\n\n"
            f"كيف أقدر أخدمك اليوم؟ 👇",
            reply_markup=get_visitor_main_keyboard()
        )

# ================= زر ترقية الزائر إلى عميل (خاص بالوكيل) =================
@router.callback_query(F.data.startswith("upgrade_client_"))
async def upgrade_visitor_to_client(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_ID: return
    
    client_id = int(callback.data.split("_")[2])
    
    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute("UPDATE users SET role = 'client' WHERE user_id = $1", client_id)
            client_name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
            
    await callback.message.edit_text(callback.message.text + f"\n\n✅ **(تمت ترقيته إلى عميل جملة بنجاح)**", reply_markup=None)
    
    try:
        await bot.send_message(
            client_id, 
            "🎉 **مبروك!**\nتمت ترقية حسابك إلى **(عميل جملة)** من قبل إدارة الشبكة.\n\nأرسل /start الآن لفتح لوحة التحكم الخاصة بالبقالات (طلب كروت، كشف حساب، إلخ).",
            reply_markup=types.ReplyKeyboardRemove()
        )
    except: pass

# ================= أزرار الزوار (الطياري) =================
@router.callback_query(F.data == "visitor_prices")
async def visitor_prices(callback: types.CallbackQuery):
    text = "🛒 **أسعار كروت شبكة الشهاب نت:**\n\n"
    if database.pool:
        async with database.pool.acquire() as conn:
            cards = await conn.fetch("SELECT card_type, retail_price FROM inventory WHERE retail_price > 0 ORDER BY retail_price ASC")
            if cards:
                for c in cards:
                    text += f"▪️ فئة ({c['card_type']}) ➔ السعر: **{int(c['retail_price'])} ريال**\n"
            else:
                text += "الأسعار غير متوفرة حالياً، يرجى التواصل مع الإدارة."
                
    text += "\n*(هذه الأسعار الرسمية المعتمدة في جميع نقاط البيع)*"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="visitor_cancel_action")]]))

@router.callback_query(F.data == "visitor_nearest_pos")
async def visitor_nearest_pos(callback: types.CallbackQuery):
    text = "🏪 **نقاط البيع المعتمدة (البقالات):**\n\nيمكنك شراء كروت شبكة الشهاب نت من الموزعين التاليين:\n\n"
    if database.pool:
        async with database.pool.acquire() as conn:
            clients = await conn.fetch("SELECT name FROM users WHERE role = 'client'")
            if clients:
                for c in clients:
                    text += f"📍 {c['name']}\n"
            else:
                text += "جاري تحديث قائمة نقاط البيع..."
                
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="visitor_cancel_action")]]))

@router.callback_query(F.data == "visitor_support")
async def visitor_support(callback: types.CallbackQuery):
    text = (
        "🛠️ **الدعم الفني وحلول المشاكل الشائعة:**\n\n"
        "1️⃣ **الشبكة لا تظهر في الجوال؟**\n"
        "➔ تأكد من إطفاء الواي فاي وتشغيله مرة أخرى، أو اقترب من جهاز البث (الأنتينا).\n\n"
        "2️⃣ **الكرت يرفض تسجيل الدخول؟**\n"
        "➔ تأكد من كتابة الحروف والأرقام بشكل صحيح (الحروف الإنجليزية تكون صغيرة Small عادة)، وتأكد أن الكرت لم ينتهِ وقته.\n\n"
        "3️⃣ **الإنترنت بطيء فجأة؟**\n"
        "➔ قم بإيقاف تشغيل الواي فاي في هاتفك لمدة 10 ثوانٍ ثم أعد تشغيله لتحديث الاتصال بالشبكة.\n\n"
        "👨‍🔧 *إذا استمرت المشكلة، لا تتردد في التواصل معنا عبر زر (التواصل مع الإدارة).* 👇"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="visitor_cancel_action")]]))

@router.callback_query(F.data == "visitor_info")
async def visitor_info(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "ℹ️ **معلومات عن شبكة الشهاب نت:**\n\n"
        "نحن نقدم أفضل وأسرع خدمة إنترنت في المنطقة، مع استقرار تام وبنج ممتاز للألعاب.\n"
        "نبيع الكروت الورقية يداً بيد لضمان الأمان والموثوقية.\n\n"
        "*(إذا كنت صاحب بقالة وتريد أن تصبح موزعاً معتمداً، يرجى التواصل مع الوكيل)*",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="visitor_cancel_action")]])
    )

@router.callback_query(F.data == "visitor_location")
async def visitor_location(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "📍 **موقعنا:**\n\n"
        "نتواجد في **قرية البلس**.\n"
        "نسعد بزيارتكم لخدمتكم بشكل مباشر!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="visitor_cancel_action")]])
    )

@router.callback_query(F.data == "visitor_cancel_action")
async def visitor_cancel_action(callback: types.CallbackQuery):
    await callback.message.edit_text(
        f"أهلاً بك يا {callback.from_user.first_name} في شبكة الشهاب نت 🌐!\n"
        f"إنترنت يسبق الخيال، وبنج يريح البال 🎮.\n\n"
        f"كيف أقدر أخدمك اليوم؟ 👇",
        reply_markup=get_visitor_main_keyboard()
    )

# ================= زر الرجوع الخاص بالعميل (البقالة) =================
@router.callback_query(F.data == "client_cancel_action")
async def client_cancel_action(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except:
        pass
        
    user_id = callback.from_user.id
    name = callback.from_user.first_name
    net_debt = Decimal('0.0')
    inv_count = 0
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # 🌟 الإصلاح المحاسبي: جلب الدين الصافي
            user_data = await conn.fetchrow("SELECT debt, pending_profit FROM users WHERE user_id = $1", user_id)
            if user_data:
                debt = Decimal(user_data['debt']) if user_data['debt'] else Decimal('0.0')
                pending = Decimal(user_data['pending_profit']) if user_data['pending_profit'] else Decimal('0.0')
                net_debt = debt # 🌟 إصلاح: العميل يرى دينه الإجمالي كاملاً
                
            inv_val = await conn.fetchval("SELECT SUM(quantity) FROM client_inventory WHERE user_id = $1", user_id)
            inv_count = int(inv_val) if inv_val else 0
            
    await callback.message.answer(
        f"يا هلا وغلا بصاحب بقالة ({name}) 🌹.\n"
        f"نورت نظام الشهاب نت. للتذكير يا غالي:\n\n"
        f"💵 حسابك الحالي (الدين الصافي): **{int(net_debt)} ريال**.\n"
        f"📦 كروتك المتبقية في الدرج: **{inv_count} كرت**.\n\n"
        f"كيف أقدر أخدمك اليوم؟ 👇",
        reply_markup=get_client_main_keyboard()
    )

# ================= 1. زر: حسابي ومخزوني =================
@router.callback_query(F.data == "client_account_inv")
async def show_account_and_inventory(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    if database.pool:
        async with database.pool.acquire() as conn:
            user = await conn.fetchrow("SELECT name, debt, pending_profit FROM users WHERE user_id = $1", user_id)
            if not user:
                return await callback.answer("عذراً، حسابك غير مسجل.", show_alert=True)
            
            # 🌟 الإصلاح المحاسبي: حساب الدين الصافي
            debt = Decimal(user['debt']) if user['debt'] else Decimal('0.0')
            pending = Decimal(user['pending_profit']) if user['pending_profit'] else Decimal('0.0')
            net_debt = debt # 🌟 إصلاح: العميل يرى دينه الإجمالي كاملاً
            
            client_name = user['name']
            inv_items = await conn.fetch("SELECT card_type, quantity FROM client_inventory WHERE user_id = $1 AND quantity > 0", user_id)
            
            text = f"👤 **أهلاً بك: {client_name}**\n"
            text += f"💸 **الرصيد المتبقي عليك (الصافي):** {int(net_debt)} ريال\n\n"
            text += "📦 **مخزونك المسجل لدينا حالياً:**\n"
            
            if inv_items:
                for item in inv_items:
                    text += f"▪️ {item['quantity']} كرت ({item['card_type']})\n"
            else:
                text += "لا يوجد كروت مسجلة في عهدتك حالياً."
                
            await callback.message.edit_text(text, reply_markup=get_client_main_keyboard())

# ================= 2. زر: استخراج كشف حسابي (PDF) =================
@router.callback_query(F.data == "client_pdf_statement")
async def generate_client_pdf(callback: types.CallbackQuery):
    await callback.answer("⏳ جاري تجهيز كشف الحساب...")
    user_id = callback.from_user.id
    client_name = callback.from_user.first_name
    
    if database.pool:
        async with database.pool.acquire() as conn:
            user = await conn.fetchrow("SELECT name, debt, pending_profit FROM users WHERE user_id = $1", user_id)
            if not user:
                return await callback.message.answer("عذراً، حسابك غير مسجل.")
            
            client_name = user['name']
            debt = Decimal(user['debt']) if user['debt'] else Decimal('0.0')
            net_debt = debt # العميل يرى دينه الإجمالي كاملاً
            
            inv_items = await conn.fetch("SELECT card_type, quantity FROM client_inventory WHERE user_id = $1 AND quantity > 0", user_id)
            
            # 🌟 حماية العزل المالي: جلب آخر دفعة تخص الكروت فقط (wallet_type = 'manager') وتجاهل التراجعات
            last_trans = await conn.fetchrow("SELECT COALESCE(amount, 0) as amount, date FROM transactions WHERE user_id = $1 AND wallet_type = 'manager' AND type = 'تسديد_من_عميل' AND is_reverted = FALSE ORDER BY date DESC LIMIT 1", user_id)
            last_payment = f"{int(Decimal(str(last_trans['amount'])))} ريال (بتاريخ {str(last_trans['date'])[:10]})" if last_trans else "لا توجد دفعات سابقة"

            import asyncio
            pdf_buffer = await asyncio.to_thread(generate_client_statement, client_name, net_debt, inv_items, last_payment)
            pdf_file = BufferedInputFile(pdf_buffer.read(), filename=f"Statement_{client_name}.pdf")
            
            await callback.message.answer_document(document=pdf_file, caption=f"📑 **كشف حسابك الحالي (قسم الكروت) يا غالي 🌹**\n*(ملاحظة: كشف حساب التسديدات يمكنك استخراجه من تطبيق الويب)*")

# ================= 3. زر: طلب كروت شبكة (بداية المسار) =================
@router.callback_query(F.data == "client_order_cards")
async def start_order_flow(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "أبشر يا غالي، سيتم رفع طلبك للإدارة.\n\n"
        "📦 **أولاً: كم متبقي لديك من الكروت السابقة في الدرج؟**\n"
        "*(اكتبها كتابة، مثلاً: باقي 5 أبو شهر)*",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="client_cancel_action")]])
    )
    await state.set_state(ClientOrderFlow.waiting_for_remaining)

@router.message(ClientOrderFlow.waiting_for_remaining)
async def process_remaining_cards(message: types.Message, state: FSMContext):
    remaining_text = message.text
    await state.update_data(remaining_cards=remaining_text)
    
    await message.answer(
        "ممتاز، تم تسجيل المتبقي.\n\n"
        "🛒 **ثانياً: كم تحتاج من الكروت الجديدة؟**\n"
        "*(اكتب طلبك، مثلاً: جيب لي 20 أبو شهر)*",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="client_cancel_action")]])
    )
    await state.set_state(ClientOrderFlow.waiting_for_new_order)

@router.message(ClientOrderFlow.waiting_for_new_order)
async def process_new_order(message: types.Message, state: FSMContext, bot: Bot):
    new_order_text = message.text
    user_id = message.from_user.id
    client_name = message.from_user.first_name
    
    data = await state.get_data()
    remaining_text = data.get("remaining_cards", "غير محدد")
    
    order_id = 0 # 🌟 الإصلاح الأمني: تعريف المتغير مسبقاً لمنع انهيار البوت
    if database.pool:
        try:
            async with database.pool.acquire() as conn:
                user = await conn.fetchrow("SELECT name FROM users WHERE user_id = $1", user_id)
                if user:
                    client_name = user['name']
                
                # إدخال الطلب في صندوق الاعتمادات
                order_id = await conn.fetchval('''
                    INSERT INTO pending_orders (user_id, client_name, remaining_text, new_order_text)
                    VALUES ($1, $2, $3, $4) RETURNING id
                ''', user_id, client_name, remaining_text, new_order_text)
        except Exception as e:
            print(f"Database error in process_new_order: {e}")

    if not order_id:
        return await message.answer("❌ عذراً، حدث خطأ في الاتصال بالنظام. يرجى المحاولة بعد قليل.")

    await message.answer(
        "✅ **تم رفع طلبك بنجاح!**\n"
        "ستقوم الإدارة بمراجعة المتبقي وحساب مبيعاتك، وسيتم المرور بك قريباً لتسليم الطلب الجديد واستلام الكاش.\n"
        "شكراً لتعاملك معنا 🌹",
        reply_markup=get_client_main_keyboard()
    )
    
    alert_text = (
        f"🚨 **طلب كروت جديد (صندوق الاعتمادات)** 🚨\n\n"
        f"👤 **العميل:** {client_name}\n"
        f"📦 **المتبقي عنده:** {remaining_text}\n"
        f"🛒 **الطلب الجديد:** {new_order_text}\n\n"
        f"💡 *السكرتير (سيستم): يا دكتور، هل أبدأ بإجراءات تسليم الكروت لهذا العميل؟*"
    )
    
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ مراجعة واعتماد", callback_data=f"review_order_{order_id}")],
        [InlineKeyboardButton(text="❌ رفض الطلب", callback_data=f"reject_order_{order_id}")],
        [InlineKeyboardButton(text="💬 إرسال رسالة للعميل", callback_data=f"msg_client_{user_id}")]
    ])
    
    try:
        await bot.send_message(ADMIN_ID, alert_text, reply_markup=admin_kb)
    except Exception as e:
        print(f"Error sending order alert to admin: {e}")
        
    # 🌟 إرسال إشعار للويب
    try:
        from web_api import notify_clients
        await notify_clients(ADMIN_ID, "new_notification", f"🚨 طلب كروت جديد من: {client_name}")
    except: pass
        
    await state.clear()

# ================= 4. زر: الإبلاغ عن كرت تالف =================
@router.callback_query(F.data == "report_damaged_card")
async def report_damaged_card_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    msg = (
        "💔 **الإبلاغ عن كرت تالف:**\n\n"
        "ولا يهمك يا غالي، حقك محفوظ.\n"
        "الرجاء كتابة **رقم الكرت التالف** أو **إرسال صورة واضحة للكرت** هنا في المحادثة:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="client_cancel_action")]])
    await callback.message.edit_text(msg, reply_markup=kb)
    await state.set_state(DamagedCardClientFlow.waiting_for_card_details)

@router.message(DamagedCardClientFlow.waiting_for_card_details)
async def process_damaged_card_details(message: types.Message, state: FSMContext, bot: Bot):
    client_name = message.from_user.first_name
    client_id = message.from_user.id
    
    ticket_id = 0
    if database.pool:
        try:
            async with database.pool.acquire() as conn:
                db_name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
                if db_name: client_name = db_name
                
                # 🌟 الإصلاح الجذري: إنشاء تذكرة حقيقية في قاعدة البيانات لكي تعمل أزرار التعويض
                details_text = message.text if message.text else "مرفق صورة"
                ticket_text = f"🚨 بلاغ كرت تالف (من تليجرام):\nالتفاصيل: {details_text}"
                
                try:
                    ticket_id = await conn.fetchval(
                        "INSERT INTO chat_messages (client_id, sender_type, message_type, message_text) VALUES ($1, 'client', 'complaint', $2) RETURNING id", 
                        client_id, ticket_text
                    )
                except Exception as db_err:
                    if 'message_type' in str(db_err):
                        ticket_id = await conn.fetchval(
                            "INSERT INTO chat_messages (client_id, sender_type, message_text) VALUES ($1, 'client', $2) RETURNING id", 
                            client_id, ticket_text
                        )
                    else:
                        raise db_err
        except Exception as e:
            print(f"Database error while creating ticket: {e}")

    if not ticket_id:
        import random
        ticket_id = random.randint(1000, 9999)

    admin_msg = f"⚠️💳 **بلاغ كرت تالف من عميل!**\n👤 البقالة: {client_name}\n🎫 رقم التذكرة: #{ticket_id}\n"
    
    # 🌟 إضافة أزرار التعويض للوكيل لكي تكتمل الدورة
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 تحويل للمدير العام (رهيب)", callback_data=f"ticket_forward_{ticket_id}_{client_id}")],
        [InlineKeyboardButton(text="✅ تعويض العميل مباشرة", callback_data=f"ticket_admin_comp_{ticket_id}_{client_id}")],
        [InlineKeyboardButton(text="❌ رفض الطلب", callback_data=f"ticket_reject_{ticket_id}_{client_id}")]
    ])
    
    try:
        if message.photo:
            photo_id = message.photo[-1].file_id
            await bot.send_photo(ADMIN_ID, photo=photo_id, caption=admin_msg + "📸 (مرفق صورة الكرت)", reply_markup=admin_kb)
        else:
            await bot.send_message(ADMIN_ID, admin_msg + f"💬 التفاصيل: {message.text}", reply_markup=admin_kb)
            
        await message.answer(f"✅ **تم استلام بلاغك!**\n🎫 رقم التذكرة: #{ticket_id}\nتم إرسال التفاصيل للوكيل (د. وليد) وسيتم فحص الكرت وتعويضك في أقرب وقت إن شاء الله 🌹.", reply_markup=get_client_main_keyboard())
    except Exception:
        await message.answer("❌ عذراً، حدث خطأ أثناء إرسال البلاغ. يرجى المحاولة لاحقاً.", reply_markup=get_client_main_keyboard())
        
    # 🌟 إرسال إشعار للويب
    try:
        from web_api import notify_clients
        await notify_clients(ADMIN_ID, "new_notification", f"⚠️ بلاغ كرت تالف من: {client_name}")
    except: pass
        
    await state.clear()

# ================= 5. زر: طريقة الاستخدام =================
@router.callback_query(F.data == "client_help")
async def show_help_instructions(callback: types.CallbackQuery):
    help_text = (
        "🌟 **أهلاً بك في المساعد الذكي لشبكة الشهاب نت!**\n\n"
        "النظام مصمم لراحتك وتوفير وقتك. إليك كيف تستخدمه:\n\n"
        "🎙️ **1. الميزة السحرية (بصمة الصوت):**\n"
        "أنت لست بحاجة لضغط الأزرار إذا كنت مشغولاً! فقط اضغط زر الميكروفون 🎤 وأرسل رسالة صوتية تقول فيها مثلاً: *(باقي معي 3 كروت أبو شهر، جهز لي 20 كرت جديد)*، والبوت سيفهمك ويرفع طلبك فوراً!\n\n"
        "💰 **2. زر (حسابي ومخزوني):**\n"
        "بضغطة واحدة، يعطيك كشفاً سريعاً لدينك الحالي، وعدد الكروت المسجلة في عهدتك لكي تطابقها مع درجك.\n\n"
        "📄 **3. زر (استخراج كشف حسابي):**\n"
        "يقوم بإصدار فاتورة PDF رسمية تحتوي على ديونك وكروتك وآخر دفعة سددتها.\n\n"
        "🛒 **4. زر (طلب كروت شبكة):**\n"
        "سيرشدك البوت خطوة بخطوة. سيسألك أولاً عن الكروت المتبقية عندك، ثم يسألك عن طلبك الجديد، ويرسل إشعاراً للإدارة لتمر بك.\n\n"
        "💬 **ملاحظة:** أي رسالة نصية أو صوتية ترسلها هنا، سيقرأها الذكاء الاصطناعي، وإذا احتجت الإدارة سيقوم بتحويل رسالتك إلى الإدارة مباشرة."
    )
    await callback.message.edit_text(help_text, reply_markup=get_client_main_keyboard())
    
# ================= زر التواصل مع الإدارة (للزوار) =================
@router.callback_query(F.data == "contact_agent")
async def contact_agent(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "📞 **أرقام التواصل والدعم الفني:**\n\n"
        "👨‍💻 **للتواصل مع (وليد):**\n"
        "*(711843112 - 777914318)*\n\n"
        "👑 **للتواصل مع الإدارة العامة (المدير . رهيب):**\n"
        "*(715649850 - 775972563)*\n\n"
        "نسعد بخدمتكم في أي وقت! 🌹",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 رجوع", callback_data="visitor_cancel_action")]])
    )
