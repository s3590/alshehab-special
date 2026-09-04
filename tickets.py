from aiogram import Router, F, types, Bot
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import time
from decimal import Decimal
import asyncio

from config import ADMIN_ID, NETWORK_OWNER_ID
import database

# 🌟 التعديل: استيراد المحرك المالي لضمان توحيد القيود المحاسبية
from core_accounting import FinancialEngine, TxType, FinancialError

router = Router()

# ================= الحالات (FSM) =================
class DamagedCardFlow(StatesGroup):
    waiting_for_number = State()
    waiting_for_image = State()

class AdminCompensateFlow(StatesGroup):
    waiting_for_new_card = State()
    waiting_for_new_card_number = State() 

class NetworkCompensateFlow(StatesGroup):
    waiting_for_new_card = State()

# ================= 1. العميل يبلغ عن كرت تالف =================
@router.callback_query(F.data == "damaged_card")
async def start_ticket(callback: types.CallbackQuery, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_ticket")]]
    )
    await callback.message.edit_text(
        "⚠️ **الإبلاغ عن كرت تالف**\n\nالرجاء إرسال **رقم الكرت** التالف:", 
        reply_markup=cancel_kb
    )
    await state.set_state(DamagedCardFlow.waiting_for_number)

@router.message(DamagedCardFlow.waiting_for_number)
async def get_card_number(message: types.Message, state: FSMContext):
    await state.update_data(card_number=message.text)
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_ticket")]]
    )
    await message.answer("ممتاز. الآن أرسل **صورة واضحة** للكرت التالف:", reply_markup=cancel_kb)
    await state.set_state(DamagedCardFlow.waiting_for_image)

@router.message(DamagedCardFlow.waiting_for_image, F.photo)
async def get_card_image(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user_id = message.from_user.id
    name = message.from_user.first_name
    
    ticket_id = 0
    if database.pool:
        try:
            async with database.pool.acquire() as conn:
                ticket_text = f"🚨 بلاغ كرت تالف (من تليجرام):\nرقم الكرت: {data['card_number']}"
                try:
                    ticket_id = await conn.fetchval(
                        "INSERT INTO chat_messages (client_id, sender_type, message_type, message_text) VALUES ($1, 'client', 'complaint', $2) RETURNING id", 
                        user_id, ticket_text
                    )
                except Exception as db_err:
                    # 🌟 الإصلاح الأمني: التحقق من نوع الخطأ (إذا كان العمود مفقوداً) بدلاً من التجاهل الأعمى
                    if 'message_type' in str(db_err):
                        ticket_id = await conn.fetchval(
                            "INSERT INTO chat_messages (client_id, sender_type, message_text) VALUES ($1, 'client', $2) RETURNING id", 
                            user_id, ticket_text
                        )
                    else:
                        raise db_err # تمرير الخطأ ليتم التقاطه في الـ try الخارجي
        except Exception as e:
            print(f"Database error while creating ticket: {e}")
            # سيتم توليد رقم عشوائي في الأسفل إذا فشلت قاعدة البيانات

    if not ticket_id:
        import random
        ticket_id = random.randint(1000, 9999)

    await message.answer(f"✅ تم رفع طلبك للادارة.\n🎫 رقم التذكرة: #{ticket_id}\nسيتم الرد عليك قريباً.")
    
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 تحويل للمدير العام (رهيب)", callback_data=f"ticket_forward_{ticket_id}_{user_id}")],
        [InlineKeyboardButton(text="✅ تعويض العميل مباشرة", callback_data=f"ticket_admin_comp_{ticket_id}_{user_id}")],
        [InlineKeyboardButton(text="❌ رفض الطلب", callback_data=f"ticket_reject_{ticket_id}_{user_id}")]
    ])
    
    try:
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=message.photo[-1].file_id,
            caption=f"🚨 **تذكرة كرت تالف #{ticket_id}**\n👤 العميل: {name}\n🔢 الرقم: {data['card_number']}",
            reply_markup=admin_kb
        )
    except Exception as e:
        print(f"Error sending ticket to admin: {e}")
        
    await state.clear()

# ================= 2. قرارات وكيل فرع بني علي =================
@router.callback_query(F.data.startswith("ticket_reject_"))
async def admin_reject_ticket(callback: types.CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    ticket_id = parts[2]
    user_id = int(parts[3])
    
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n❌ **(تم رفض الطلب من الوكيل)**", 
        reply_markup=None
    )

    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute(
                "UPDATE chat_messages SET message_text = message_text || '\n(❌ تم الرفض من الوكيل)' WHERE id = $1", 
                int(ticket_id)
            )

    msg_text = f"❌ **بخصوص التذكرة #{ticket_id}:**\nنعتذر، تم رفض طلب التعويض من قبل وكيل فرع بني علي بعد فحص الكرت."
    
    try: await bot.send_message(user_id, msg_text)
    except: pass
    
    # 🌟 إرسال للواتساب والويب
    from unified_main import safe_send_whatsapp
    asyncio.create_task(safe_send_whatsapp(user_id, msg_text.replace('**', '*'), bot=bot))
    try:
        from web_api import send_web_push
        await send_web_push(user_id, "❌ رفض تعويض", f"تم رفض طلب التعويض للتذكرة #{ticket_id}.")
    except: pass

@router.callback_query(F.data.startswith("ticket_forward_"))
async def admin_forward_ticket(callback: types.CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    ticket_id = parts[2]
    user_id = int(parts[3])
    
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n🔄 **(تم التحويل للمدير العام)**", 
        reply_markup=None
    )

    msg_text = f"🔄 **بخصوص التذكرة #{ticket_id}:**\nتم تحويل طلبك إلى الإدارة العامة لشبكة الشهاب نت للفحص، يرجى الانتظار."
    try: await bot.send_message(user_id, msg_text)
    except: pass
    
    # 🌟 إرسال للواتساب والويب
    from unified_main import safe_send_whatsapp
    asyncio.create_task(safe_send_whatsapp(user_id, msg_text.replace('**', '*'), bot=bot))
    try:
        from web_api import send_web_push
        await send_web_push(user_id, "🔄 تحديث التذكرة", f"تم تحويل التذكرة #{ticket_id} للإدارة العامة.")
    except: pass
    
    network_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ إرسال كرت بديل للعميل", callback_data=f"net_comp_{ticket_id}_{user_id}")],
        [InlineKeyboardButton(text="🔄 توكيل وكيل الفرع بالتعويض", callback_data=f"net_delegate_{ticket_id}_{user_id}")],
        [InlineKeyboardButton(text="❌ رفض الطلب", callback_data=f"net_reject_{ticket_id}_{user_id}")]
    ])
    
    try:
        await bot.send_photo(
            chat_id=NETWORK_OWNER_ID,
            photo=callback.message.photo[-1].file_id,
            caption=callback.message.caption + "\n\n(محول من وكيل فرع بني علي للفحص)",
            reply_markup=network_kb
        )
    except: pass

# ================= 3. قرارات المدير العام (رهيب) =================
@router.callback_query(F.data.startswith("net_reject_"))
async def network_reject_ticket(callback: types.CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    ticket_id = parts[2]
    user_id = int(parts[3])
    
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n❌ **(تم الرفض من الإدارة)**", 
        reply_markup=None
    )

    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute(
                "UPDATE chat_messages SET message_text = message_text || '\n(❌ تم الرفض من الإدارة)' WHERE id = $1", 
                int(ticket_id)
            )

    msg_text = f"❌ **بخصوص التذكرة #{ticket_id}:**\nتم رفض طلب التعويض من قبل الإدارة العامة لشبكة الشهاب نت."
    try: await bot.send_message(user_id, msg_text)
    except: pass
    
    # 🌟 إرسال للواتساب والويب
    from unified_main import safe_send_whatsapp
    asyncio.create_task(safe_send_whatsapp(user_id, msg_text.replace('**', '*'), bot=bot))
    try:
        from web_api import send_web_push
        await send_web_push(user_id, "❌ رفض تعويض", f"تم رفض طلب التعويض للتذكرة #{ticket_id} من قبل الإدارة.")
    except: pass
    
    try: await bot.send_message(ADMIN_ID, f"ℹ️ المدير العام (رهيب) **رفض** تعويض التذكرة #{ticket_id}.")
    except: pass

@router.callback_query(F.data.startswith("net_delegate_"))
async def network_delegate_ticket(callback: types.CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    ticket_id = parts[2]
    user_id = int(parts[3])
    
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n🔄 **(تم توكيل وكيل فرع بني علي بالتعويض)**", 
        reply_markup=None
    )

    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute(
                "UPDATE chat_messages SET message_text = message_text || '\n(👨‍💻 تم التحويل للوكيل للتعويض)' WHERE id = $1", 
                int(ticket_id)
            )

    comp_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تعويض العميل الآن", callback_data=f"ticket_admin_comp_{ticket_id}_{user_id}")]
    ])
    
    try:
        await bot.send_message(
            ADMIN_ID, 
            f"🔄 **توجيه من المدير العام (رهيب):**\nالرجاء تعويض العميل صاحب التذكرة #{ticket_id} من مخزونك (سيتم خصمها كـ تالف ولن تحسب كدين).", 
            reply_markup=comp_kb
        )
    except: pass

@router.callback_query(F.data.startswith("net_comp_"))
async def network_direct_comp(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    ticket_id = parts[2]
    user_id = int(parts[3])
    
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n✅ **(جاري التعويض المباشر...)**", 
        reply_markup=None
    )

    await state.update_data(client_id=user_id, ticket_id=ticket_id)
    await callback.message.answer("📝 الرجاء كتابة **رقم الكرت البديل** لإرساله للعميل مباشرة:")
    await state.set_state(NetworkCompensateFlow.waiting_for_new_card)

@router.message(NetworkCompensateFlow.waiting_for_new_card)
async def process_network_comp(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    client_id = data['client_id']
    ticket_id = data['ticket_id']
    new_card_number = message.text
    
    if database.pool:
        async with database.pool.acquire() as conn:
            await conn.execute(
                "UPDATE chat_messages SET message_text = message_text || $1 WHERE id = $2", 
                f"\n(✅ تم التعويض المباشر. رقم الكرت: {new_card_number})", 
                int(ticket_id)
            )
            # 🌟 إرسال الكرت في شات الويب لسهولة النسخ
            await conn.execute("INSERT INTO chat_messages (client_id, sender_type, message_type, message_text) VALUES ($1, 'agent', 'support', $2)", client_id, f"✅ تم فحص الكرت التالف واعتماد التعويض.\nتفضل رقم الكرت البديل:\n{new_card_number}")

    msg_text = f"🎁 **تعويض من الإدارة العامة لشبكة الشهاب نت (تذكرة #{ticket_id}):**\n\nالكرت البديل:\n`{new_card_number}`"
    try: await bot.send_message(client_id, msg_text)
    except: pass
    
    # 🌟 إرسال للواتساب والويب
    from unified_main import safe_send_whatsapp
    wa_text = f"🎁 *تعويض من الإدارة العامة (تذكرة #{ticket_id}):*\n\nالكرت البديل:\n*{new_card_number}*"
    asyncio.create_task(safe_send_whatsapp(client_id, wa_text, bot=bot))
    try:
        from web_api import send_web_push
        await send_web_push(client_id, "✅ تعويض كرت تالف", "تم اعتماد التعويض وإرسال رقم الكرت البديل في المحادثة.")
    except: pass
    
    try: await bot.send_message(ADMIN_ID, f"ℹ️ قام المدير العام بتعويض العميل مباشرة للتذكرة #{ticket_id}.")
    except: pass
    
    await message.answer("✅ تم إرسال الكرت البديل للعميل بنجاح.")
    await state.clear()

# ================= 4. تعويض من قبل وكيل فرع بني علي =================
@router.callback_query(F.data.startswith("ticket_admin_comp_"))
async def admin_direct_comp(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    ticket_id = parts[3]
    user_id = int(parts[4])
    
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n✅ **(جاري التعويض من الوكيل...)**", 
        reply_markup=None
    )

    await state.update_data(client_id=user_id, ticket_id=ticket_id)
    
    kb = []
    if database.pool:
        async with database.pool.acquire() as conn:
            # 🌟 الدمج الأعظم: إخفاء الكروت المحذوفة + إخفاء الكروت الإلكترونية
            cards = await conn.fetch("SELECT card_type FROM inventory WHERE quantity > 0 AND is_active = TRUE AND card_type NOT LIKE '%إلكتروني%' AND card_type NOT LIKE '%الكتروني%'")
            for c in cards:
                kb.append([InlineKeyboardButton(text=c['card_type'], callback_data=f"acomp_{c['card_type']}")])
                
    if not kb:
        return await callback.message.answer("❌ عذراً، مخزونك فارغ تماماً! لا يمكنك تعويض العميل حالياً.")
        
    kb.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_ticket")])
    
    await callback.message.answer(
        "📦 اختر فئة الكرت الذي ستعوض به العميل (سيتم خصمه من مخزونك كـ تالف):", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )
    await state.set_state(AdminCompensateFlow.waiting_for_new_card)

@router.callback_query(AdminCompensateFlow.waiting_for_new_card, F.data.startswith("acomp_"))
async def process_admin_comp_type(callback: types.CallbackQuery, state: FSMContext):
    card_type = callback.data.split("_")[1]
    await state.update_data(card_type=card_type)
    
    await callback.message.edit_text(f"📝 لقد اخترت تعويض العميل بكرت ({card_type}).\nالرجاء كتابة **رقم الكرت البديل** لإرساله للعميل مباشرة:")
    await state.set_state(AdminCompensateFlow.waiting_for_new_card_number)

@router.message(AdminCompensateFlow.waiting_for_new_card_number)
async def process_admin_comp_number(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    client_id = data['client_id']
    ticket_id = data['ticket_id']
    card_type = data['card_type']
    new_card_number = message.text
    
    try:
        # 1. جلب التكلفة واسم العميل فقط
        async with database.pool.acquire() as conn:
            inv_data = await conn.fetchrow("SELECT quantity, cost_price, price FROM inventory WHERE card_type = $1", card_type)
            if not inv_data or inv_data['quantity'] <= 0:
                return await message.answer(f"❌ عذراً، مخزونك من فئة ({card_type}) نفد! لا يمكنك التعويض بهذه الفئة.")
                
            unit_cost = Decimal(inv_data['cost_price'] if inv_data['cost_price'] else inv_data['price'])
            client_name = await conn.fetchval("SELECT name FROM users WHERE user_id = $1", client_id)
            
        # 2. 🌟 الإصلاح المحاسبي: توجيه العملية بالكامل للمطبخ المركزي ليتولى الخصم والقفل الموحد
        from core_accounting import core_process_return
        result = await core_process_return(
            'damaged', 1, unit_cost, 0, card_type, 
            source=f"(تعويض تذكرة #{ticket_id} بكرت {card_type})"
        )
        
        if result["status"] == "error":
            return await message.answer(f"❌ {result['message']}")
            
    except Exception as e:
        return await message.answer(f"❌ حدث خطأ مالي أثناء التعويض: {e}")

    # 🌟 3. تحديث التذكرة في الشات (خارج الـ try/except المالي لمنع التضليل)
    try:
        if database.pool:
            async with database.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE chat_messages SET message_text = message_text || $1 WHERE id = $2", 
                    f"\n(✅ تم التعويض من الوكيل. رقم الكرت: {new_card_number})", 
                    int(ticket_id)
                )

                await conn.execute("INSERT INTO chat_messages (client_id, sender_type, message_type, message_text) VALUES ($1, 'agent', 'support', $2)", client_id, f"✅ تم فحص الكرت التالف واعتماد التعويض.\nتفضل رقم الكرت البديل:\n{new_card_number}")
    except Exception as chat_err:
        print(f"Failed to update chat messages: {chat_err}")
        # لا نوقف العملية هنا لأن التعويض المالي قد تم بنجاح

    msg_text = f"🎁 **تعويض من الادارة  (تذكرة #{ticket_id}):**\n\nتم تعويضك بكرت جديد من فئة {card_type}.\nالكرت البديل:\n`{new_card_number}`"
    try: await bot.send_message(client_id, msg_text)
    except: pass
    
    # 🌟 إرسال للواتساب والويب
    from unified_main import safe_send_whatsapp
    wa_text = f"🎁 *تعويض من الادارة (تذكرة #{ticket_id}):*\n\nتم تعويضك بكرت جديد من فئة {card_type}.\nالكرت البديل:\n*{new_card_number}*"
    asyncio.create_task(safe_send_whatsapp(client_id, wa_text, bot=bot))
    try:
        from web_api import send_web_push
        await send_web_push(client_id, "✅ تعويض كرت تالف", "تم اعتماد التعويض وإرسال رقم الكرت البديل في المحادثة.")
    except: pass
    
    try:
        await bot.send_message(
            NETWORK_OWNER_ID, 
            f"ℹ️ **إشعار للإدارة:**\nقام الوكيل بتعويض العميل ({client_name}) بكرت بديل من فئة ({card_type}) للتذكرة #{ticket_id}.\n*(تم خصم الكرت من المخزون وتسجيل {int(unit_cost)} ريال كـ تالف)*."
        )
    except: pass
    
    await message.answer(f"✅ تم إرسال الكرت البديل للعميل بنجاح.\nتم خصم الكرت من مخزونك وتسجيله كـ (تالف) في حساب الإدارة.")
    await state.clear()

# ================= زر الإلغاء =================
@router.callback_query(F.data == "cancel_ticket")
async def cancel_flow(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("تم الإلغاء.")
    except: pass
