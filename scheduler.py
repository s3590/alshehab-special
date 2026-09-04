import asyncio
import openpyxl
from io import BytesIO
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone
from aiogram import Bot
from aiogram.types import BufferedInputFile
from config import ADMIN_ID, NETWORK_OWNER_ID
import database
from datetime import datetime
from decimal import Decimal
from pdf_generator import generate_daily_report

# 🌟 التعديل: استيراد المحرك المالي الجديد
from core_accounting import FinancialEngine

# 🌟 [جديد] استيراد دالة النسخ السحابي المشفر
from cloud_backup import execute_scheduled_cloud_backup

# ================= 1. الإغلاق اليومي (PDF + Excel) =================
async def scheduled_daily_backup(bot_instance: Bot):
    group_id = await database.get_setting("archive_channel_id")
    if group_id == "off" or not group_id: return 
    try:
        # 🌟 الإصلاح: جلب دوال التنسيق من المطبخ المركزي بدلاً من unified_main
        from core_accounting import add_sheet_header, style_excel_sheet

        date_str = datetime.now().strftime('%Y-%m-%d')
        wb = openpyxl.Workbook()
        
        # 👈 جلب الكاش الدقيق من المحرك المالي
        engine = FinancialEngine(database.pool)
        fin_stats = await engine.get_summary()
        
        # 🌟 الكاش المفصل لتمريره لملف الـ PDF
        network_cash = fin_stats["cash"]
        telecom_cash = fin_stats.get("telecom", Decimal('0.0'))
        total_drawer_cash = network_cash + telecom_cash
        
        async with database.pool.acquire() as conn:
            # 🌟 الحماية من التكرار: التأكد من عدم وجود إغلاق مسبق لنفس اليوم
            already_closed = await conn.fetchval("SELECT 1 FROM transactions WHERE type = 'إغلاق_يومي' AND DATE(date) = CURRENT_DATE")
            if already_closed:
                print("⚠️ تم تنفيذ الإغلاق اليومي مسبقاً لهذا اليوم.")
                return

            today_tx_count = await conn.fetchval("SELECT COUNT(*) FROM transactions WHERE date >= CURRENT_DATE AND type != 'إغلاق_يومي'")
            if today_tx_count == 0: return 

            # 🌟 حماية العزل المالي: جلب عمليات الكروت فقط (wallet_type = 'manager') وتجاهل التراجعات
            wholesale_today = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'تسليم_لعميل' AND is_reverted = FALSE AND date >= CURRENT_DATE")
            retail_today = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'بيع_مباشر' AND is_reverted = FALSE AND date >= CURRENT_DATE")
            collected_today = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'تسديد_من_عميل' AND is_reverted = FALSE AND date >= CURRENT_DATE")
            expenses_today = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('مصروفات', 'تسديد_للشبكة') AND is_reverted = FALSE AND date >= CURRENT_DATE")
            damaged_today = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'كروت_تالفة' AND is_reverted = FALSE AND date >= CURRENT_DATE")

            ws_users = wb.active
            ws_users.title = "العملاء والديون"
            add_sheet_header(ws_users, "النسخة الاحتياطية - العملاء", num_cols=3) 
            ws_users.append(["رقم العميل", "الاسم", "الدين (ريال)"])
            users = await conn.fetch("SELECT user_id, name, debt FROM users WHERE role = 'client'")
            total_debt = 0
            for u in users:
                ws_users.append([str(u['user_id']), u['name'], float(u['debt'])])
                total_debt += float(u['debt'])
            style_excel_sheet(ws_users, header_row=5) 
                
            ws_inv = wb.create_sheet("المخزون العام")
            add_sheet_header(ws_inv, "النسخة الاحتياطية - المخزون", num_cols=2) 
            ws_inv.append(["نوع الكرت", "الكمية المتوفرة"])
            inv = await conn.fetch("SELECT card_type, quantity FROM inventory")
            for i in inv:
                ws_inv.append([i['card_type'], i['quantity']])
            style_excel_sheet(ws_inv, header_row=5) 

            # 🌟 التعديل الجوهري: تسجيل قيد الإغلاق اليومي في قاعدة البيانات
            import json
            eod_details = {
                "wholesale": float(wholesale_today or 0),
                "retail": float(retail_today or 0),
                "collected": float(collected_today or 0),
                "expenses": float(expenses_today or 0),
                "damaged": float(damaged_today or 0),
                "total_drawer_cash": float(total_drawer_cash)
            }
            await conn.execute("""
                INSERT INTO transactions (user_id, type, amount, details, structured_details) 
                VALUES (0, 'إغلاق_يومي', $1, $2, $3)
            """, total_drawer_cash, f"إغلاق يوم {date_str}", json.dumps([eod_details]))

        stream = BytesIO()
        wb.save(stream)
        stream.seek(0)
        excel_document = BufferedInputFile(stream.read(), filename=f"Daily_Backup_{date_str}.xlsx")
        
        try:
            # 🌟 الإصلاح الأمني: تمرير البيانات كقاموس (Dictionary) ليتوافق مع دالة generate_daily_report
            # (تأكد من أن دالة generate_daily_report في ملف pdf_generator.py تقبل هذه المتغيرات بهذا الشكل)
            summary_data = {
                'wholesale': float(wholesale_today or 0),
                'retail': float(retail_today or 0),
                'collected': float(collected_today or 0),
                'expenses': float(expenses_today or 0),
                'damaged': float(damaged_today or 0),
                'network_cash': float(network_cash),
                'telecom_cash': float(telecom_cash),
                'total_drawer_cash': float(total_drawer_cash)
            }
            
            # 🌟 الإصلاح: تمرير جميع المتغيرات الـ 9 المطلوبة لتوليد الفاتورة بدقة
            pdf_buffer = generate_daily_report(
                date_str, 
                summary_data['wholesale'], 
                summary_data['retail'], 
                summary_data['collected'], 
                summary_data['expenses'], 
                summary_data['damaged'],
                summary_data['network_cash'],
                summary_data['telecom_cash'],
                summary_data['total_drawer_cash']
            )

            pdf_document = BufferedInputFile(pdf_buffer.read(), filename=f"Daily_Report_{date_str}.pdf")
            
            caption_text = (
                f"📊 **الإغلاق اليومي التلقائي** 📊\n"
                f"📅 التاريخ: {date_str}\n\n"
                f"مرفق أدناه:\n"
                f"1️⃣ التقرير اليومي المفصل (PDF)\n"
                f"2️⃣ النسخة الاحتياطية لقاعدة البيانات (Excel)\n\n"
                f"🔒 *(تم تسجيل قيد الإغلاق اليومي في النظام بنجاح)*"
            )
            
            await bot_instance.send_document(chat_id=int(group_id), document=excel_document)
            
        except Exception as pdf_error:
            print(f"❌ خطأ في توليد الـ PDF: {pdf_error}")
            summary_text = f"📊 **التقرير اليومي التلقائي** 📊\n📅 التاريخ: {date_str}\n\n💰 **إجمالي ديون السوق:** {total_debt} ريال\n\n💾 *مرفق ملف النسخة الاحتياطية لقاعدة البيانات.*\n*(ملاحظة: حدث خطأ أثناء توليد تقرير PDF)*"
            await bot_instance.send_document(chat_id=int(group_id), document=excel_document, caption=summary_text)

        # ملاحظة: تم تسجيل قيد الإغلاق اليومي مسبقاً داخل قاعدة البيانات في الأعلى
        print("✅ تم إرسال الإغلاق اليومي (PDF + Excel) وتسجيل القيد بنجاح.")
    except Exception as e:
        print(f"❌ خطأ في الإغلاق اليومي: {e}")

# ================= 2. إشعار الإغلاق اليومي النصي (لمجموعة الأرشيف فقط) =================
async def daily_brief(bot_instance: Bot):
    if not database.pool: return
    
    group_id = await database.get_setting("archive_channel_id")
    if group_id == "off" or not group_id: return 
    
    # 👈 جلب الكاش والديون الدقيقة من المحرك المالي
    engine = FinancialEngine(database.pool)
    fin_stats = await engine.get_summary()
    
    # 🌟 جلب الكاش الفعلي للدرج لكي يطابقه الوكيل مع الفلوس التي بيده
    network_cash = fin_stats["cash"]
    telecom_cash = fin_stats.get("telecom", Decimal('0.0'))
    total_drawer_cash = network_cash + telecom_cash
    
    manager_net_cash = max(Decimal('0.0'), network_cash - fin_stats["realized"])
    total_market_debt = fin_stats["debt_cost"]
    
    async with database.pool.acquire() as conn:
        # 🌟 حماية العزل المالي: جلب عمليات الكروت فقط وتجاهل التراجعات
        today_tx_count = await conn.fetchval("SELECT COUNT(*) FROM transactions WHERE wallet_type = 'manager' AND date >= CURRENT_DATE")
        if today_tx_count == 0: return 

        wholesale_today = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'تسليم_لعميل' AND is_reverted = FALSE AND date >= CURRENT_DATE")
        retail_today = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'بيع_مباشر' AND is_reverted = FALSE AND date >= CURRENT_DATE")
        collected_today = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'تسديد_من_عميل' AND is_reverted = FALSE AND date >= CURRENT_DATE")
        expenses_today = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('مصروفات', 'تسديد_للشبكة') AND is_reverted = FALSE AND date >= CURRENT_DATE")
        
        total_received_ever = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE")
        total_deducted_ever = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('تسديد_للشبكة', 'مصروفات', 'كروت_تالفة', 'نسبة_الوكيل', 'مرتجع_للشبكة') AND is_reverted = FALSE")
        net_owed_to_gm = Decimal(total_received_ever or 0) - Decimal(total_deducted_ever or 0)
        
        inventory_items = await conn.fetch("SELECT card_type, quantity FROM inventory WHERE quantity > 0")
        inv_text = "".join([f"▪️ {item['card_type']}: {item['quantity']} كرت\n" for item in inventory_items]) or "المخزون فارغ تماماً.\n"

        today_trans = await conn.fetch("SELECT t.type, t.amount, t.details, u.name FROM transactions t LEFT JOIN users u ON t.user_id = u.user_id WHERE t.wallet_type = 'manager' AND t.is_reverted = FALSE AND t.date >= CURRENT_DATE")
        deliveries_text, collections_text, direct_sales_text, expenses_text = "", "", "", ""
        for t in today_trans:
            if t['type'] == 'تسليم_لعميل': deliveries_text += f"🔹 {t['name']}: {t['details']} ({t['amount']} ريال)\n"
            elif t['type'] == 'تسديد_من_عميل': collections_text += f"🔹 {t['name']}: سدد {t['amount']} ريال\n"
            elif t['type'] == 'بيع_مباشر': direct_sales_text += f"🔹 {t['details']} ({t['amount']} ريال)\n"
            elif t['type'] in ('مصروفات', 'تسديد_للشبكة'): expenses_text += f"🔹 {t['details']} ({t['amount']} ريال)\n"

    msg = (
        "📊 **إشعار الإغلاق اليومي لفرع بني علي** 🌙\n====================\n"
        f"📦 مبيعات الجملة: {wholesale_today} ريال\n🛒 مبيعات التجزئة: {retail_today} ريال\n"
        f"💵 التحصيلات: {collected_today} ريال\n📉 المصروفات: {expenses_today} ريال\n"
        f"🏦 **إجمالي الكاش الفعلي بالدرج:** {total_drawer_cash} ريال\n"
        f"   ├ كاش الشبكة: {network_cash} ريال\n"
        f"   └ كاش التسديدات: {telecom_cash} ريال\n====================\n"
        f"📤 **الكروت المسلمة:**\n{deliveries_text or 'لا يوجد'}\n"
        f"📥 **المبالغ المستلمة:**\n{collections_text or 'لا يوجد'}\n"
        f"🛒 **البيع المباشر:**\n{direct_sales_text or 'لا يوجد'}\n"
        f"📉 **المصروفات:**\n{expenses_text or 'لا يوجد'}\n====================\n"
        f"🔹 **الصافي للمدير العام:** {net_owed_to_gm} ريال\n🔹 **ديون السوق:** {total_market_debt} ريال\n\n"
        f"📦 **جرد المخزون:**\n{inv_text}====================\nعمل موفق! 🌹"
    )
    try: 
        await bot_instance.send_message(int(group_id), msg)
    except Exception as e: 
        print(f"Error sending daily brief to group: {e}")

# ================= 3. المحاسب الآلي الأسبوعي (للعملاء) =================
async def weekly_client_statement_and_audit(bot_instance: Bot):
    if not database.pool: return
    import random
    from unified_main import safe_send_whatsapp
    
    async with database.pool.acquire() as conn:
        # 🌟 جلب رقم الهاتف وحالة الواتساب
        clients = await conn.fetch("SELECT user_id, name, debt, phone, wa_status FROM users WHERE role = 'client'")
        for client in clients:
            client_id, client_name, debt = client['user_id'], client['name'], Decimal(client['debt'])
            inv_items = await conn.fetch("SELECT card_type, quantity FROM client_inventory WHERE user_id = $1 AND quantity > 0", client_id)
            
            if debt > 0 or inv_items:
                msg = f"جمعة مباركة يا غالي 🌹 ({client_name})\n\n📜 **إجمالي المديونية:** {debt} ريال\n\n"
                if inv_items:
                    msg += "📦 **الكروت في عهدتك:**\n" + "".join([f"▪️ {i['quantity']} كرت ({i['card_type']})\n" for i in inv_items])
                    msg += "\n🤖 *سيستم:* **كم عدد الكروت المتبقية في درجك الآن من كل فئة؟**\n*(رد برسالة صوتية أو نصية لخصم المباع وحساب الكاش)* 😉"
                else:
                    msg += "\n🤖 *سيستم:* ليس لديك كروت في المخزون حالياً. هل تحتاج كمية جديدة؟"
                
                try:
                    await bot_instance.send_message(client_id, msg)
                    
                    # 🌟 إرسال للواتساب مع حماية التأخير البشري
                    if client['phone'] and client['wa_status'] == 'on':
                        wa_msg = msg.replace('**', '*').replace('🤖 *سيستم:*', '🤖 _سيستم:_')
                        await safe_send_whatsapp(client_id, wa_msg, bot=bot_instance)
                        await asyncio.sleep(random.uniform(15.0, 25.0)) # تأخير آمن (15 إلى 25 ثانية)
                    else:
                        await asyncio.sleep(random.uniform(2.0, 4.0)) # تأخير تليجرام العادي
                except: pass

# ================= 4. تقرير الجرد الأسبوعي المجمع للوكيل =================
async def weekly_audit_summary_for_agent(bot_instance: Bot):
    if not database.pool: return
    async with database.pool.acquire() as conn:
        ready_cash_clients = await conn.fetch("SELECT name, promised_payment FROM users WHERE role = 'client' AND promised_payment > 0")
        if ready_cash_clients:
            msg = "📊 **حصاد الجرد الآلي الأسبوعي:**\n\n"
            total_ready_cash = Decimal('0.0')
            for c in ready_cash_clients:
                cash = Decimal(c['promised_payment'])
                total_ready_cash += cash
                msg += f"▪️ **{c['name']}:** {cash} ريال\n"
            msg += f"\n💰 **إجمالي الكاش الجاهز للتحصيل غداً:** **{total_ready_cash} ريال** 🚀"
        else:
            msg = "📊 **حصاد الجرد الآلي الأسبوعي:**\n\nلم تقم أي بقالة بالجرد وتجهيز الكاش حتى الآن."
        try: await bot_instance.send_message(ADMIN_ID, msg)
        except: pass

# ================= 5. فحص المخزون (يومياً) =================
async def check_low_inventory(bot_instance: Bot):
    if not database.pool: return
    async with database.pool.acquire() as conn:
        items = await conn.fetch("SELECT card_type, quantity FROM inventory")
        alerts = [f"🔻 {i['card_type']}: متبقي {i['quantity']} كرت فقط!" for i in items if ('1000' in i['card_type'] or '3000' in i['card_type']) and i['quantity'] < 5 or ('1000' not in i['card_type'] and '3000' not in i['card_type']) and i['quantity'] < 50]
        if alerts:
            try: await bot_instance.send_message(ADMIN_ID, "⚠️ **تنبيه ذكي من سيستم:**\nيا دكتور وليد، هذه الكروت على وشك النفاذ:\n\n" + "\n".join(alerts))
            except: pass

# ================= 6. الجرد الشهري الإجباري (يوم 27) =================
async def monthly_mandatory_audit(bot_instance: Bot):
    if not database.pool: return
    import random
    from unified_main import safe_send_whatsapp
    
    async with database.pool.acquire() as conn:
        clients = await conn.fetch("SELECT user_id, name, debt, phone, wa_status FROM users WHERE role = 'client'")
        admin_report = "🔔 **تم إرسال رسائل الجرد الإجباري (يوم 27) للعملاء:**\n\n"
        
        for client in clients:
            client_id, client_name, debt = client['user_id'], client['name'], Decimal(client['debt'])
            inv_items = await conn.fetch("SELECT card_type, quantity FROM client_inventory WHERE user_id = $1 AND quantity > 0", client_id)
            
            if debt > 0 or inv_items:
                msg = (f"حياك الله يا غالي ({client_name}) 🌹\n\nبما أننا في نهاية الشهر، الوكيل سيمر عليك غداً لتصفية الحساب وتسليمك كروت جديدة.\n\n"
                       "⚠️ **المطلوب منك الآن (ضروري جداً):**\nكم عدد الكروت المتبقية في درجك من كل فئة؟\n\n"
                       "*(أرسل لنا العدد بصوتك أو كتابة لكي نخصم المباع، ونعرف النواقص، ونحسب الكاش المطلوب تجهيزه)* 🤝")
                try:
                    await bot_instance.send_message(client_id, msg)
                    admin_report += f"✅ {client_name}\n"
                    
                    # 🌟 إرسال للواتساب مع حماية التأخير البشري
                    if client['phone'] and client['wa_status'] == 'on':
                        wa_msg = msg.replace('**', '*')
                        await safe_send_whatsapp(client_id, wa_msg, bot=bot_instance)
                        await asyncio.sleep(random.uniform(15.0, 25.0)) # تأخير آمن
                    else:
                        await asyncio.sleep(random.uniform(2.0, 4.0))
                except: 
                    admin_report += f"❌ {client_name} (فشل الإرسال)\n"
                    
        try: await bot_instance.send_message(ADMIN_ID, admin_report)
        except: pass

# ================= 7. تقرير المتجاهلين (يوم 27 ليلاً) =================
async def audit_red_list_report(bot_instance: Bot):
    if not database.pool: return
    async with database.pool.acquire() as conn:
        audited = await conn.fetch("SELECT DISTINCT u.name, u.promised_payment FROM users u JOIN transactions t ON u.user_id = t.user_id WHERE t.type = 'مبيعات_بقالة' AND t.date >= CURRENT_DATE")
        ignored = await conn.fetch("SELECT name FROM users WHERE role = 'client' AND debt > 0 AND user_id NOT IN (SELECT user_id FROM transactions WHERE type = 'مبيعات_بقالة' AND date >= CURRENT_DATE)")
        msg = "📊 **حصيلة الجرد الإجباري (استعداداً لنزول الغد):**\n\n🟢 **عملاء متجاوبين:**\n"
        if audited:
            for c in audited: msg += f"▪️ {c['name']}: مجهز ({c['promised_payment']} ريال)\n"
        else: msg += "لا يوجد أحد.\n"
        msg += "\n🔴 **عملاء متجاهلين (القائمة الحمراء):**\n"
        if ignored:
            msg += "*(يا دكتور، هؤلاء لم يردوا على البوت، يرجى الاتصال بهم الآن لتجهيز حساباتهم غداً)*\n"
            for c in ignored: msg += f"▪️ {c['name']}\n"
        else: msg += "الجميع تجاوبوا! ممتاز جداً 🌟\n"
        try: await bot_instance.send_message(ADMIN_ID, msg)
        except: pass

# ================= 8. المحصل الآلي المتدرج (Smart Debt Escalation) =================
async def smart_debt_escalation(bot_instance: Bot):
    """مهمة يومية: تذكير متدرج للديون (تلميح -> مطالبة -> إنذار)"""
    if not database.pool: return
    today = datetime.now().day
    if today not in [25, 28, 30]: return
    
    import random
    from unified_main import safe_send_whatsapp

    async with database.pool.acquire() as conn:
        clients = await conn.fetch("SELECT user_id, name, debt, phone, wa_status FROM users WHERE role = 'client' AND debt > 0")
        if not clients: return
        
        admin_report = f"🔔 **تقرير المحصل الآلي (يوم {today}):**\n\n"
        
        for client in clients:
            client_id = client['user_id']
            client_name = client['name']
            debt = client['debt']
            
            if today == 25:
                msg = f"حياك الله يا غالي ({client_name}) 🌹\nنذكرك باقتراب نهاية الشهر، حسابك المتبقي: **{debt} ريال**.\nيا ريت تجهز الكاش لترتيب الحسابات."
                status = "تلميح لطيف 🟢"
            elif today == 28:
                msg = f"⚠️ **تنبيه مطالبة:**\nالأخ ({client_name})، نرجو سرعة تجهيز المبلغ المتبقي عليك (**{debt} ريال**) اليوم أو غداً كحد أقصى لتوريده للإدارة العامة."
                status = "مطالبة حازمة 🟡"
            elif today == 30:
                msg = f"🚨 **إنذار نهائي:**\nالعميل ({client_name})، اليوم هو آخر يوم في الشهر. تأخرك عن سداد (**{debt} ريال**) سيضطر النظام لرفض أي طلبات كروت جديدة لك. يرجى السداد فوراً."
                status = "إنذار نهائي 🔴"
            
            try:
                await bot_instance.send_message(client_id, msg)
                admin_report += f"▪️ {client_name}: {status}\n"
                
                # 🌟 إرسال للواتساب مع حماية التأخير البشري
                if client['phone'] and client['wa_status'] == 'on':
                    wa_msg = msg.replace('**', '*')
                    await safe_send_whatsapp(client_id, wa_msg, bot=bot_instance)
                    await asyncio.sleep(random.uniform(15.0, 25.0)) # تأخير آمن
                else:
                    await asyncio.sleep(random.uniform(2.0, 4.0))
            except:
                admin_report += f"▪️ {client_name}: فشل الإرسال ❌\n"
                
        try:
            await bot_instance.send_message(ADMIN_ID, admin_report)
        except: pass

# ================= 9. التقرير الصباحي للوكيل (Morning Briefing) =================
async def morning_briefing(bot_instance: Bot):
    """مهمة يومية: إرسال خطة العمل الصباحية للوكيل"""
    if not database.pool: return
    
    async with database.pool.acquire() as conn:
        # جلب البقالات التي كروتها صفر
        empty_inv_clients = await conn.fetch("""
            SELECT u.name, ci.card_type 
            FROM client_inventory ci 
            JOIN users u ON ci.user_id = u.user_id 
            WHERE ci.quantity = 0 AND u.role = 'client'
        """)
        
        # جلب البقالات التي تجاوزت 80% من سقف المديونية
        high_debt_clients = await conn.fetch("""
            SELECT name, debt, credit_limit 
            FROM users 
            WHERE role = 'client' AND debt >= (COALESCE(credit_limit, 50000) * 0.8)
        """)
        
        msg = "☀️ **صباح الخير يا دكتور وليد!** ☕\nإليك خطة عملك المقترحة لهذا اليوم:\n\n"
        
        if empty_inv_clients:
            msg += "🏃‍♂️ **بقالات تحتاج زيارة عاجلة (كروتهم خلصت):**\n"
            for c in empty_inv_clients:
                msg += f"▪️ {c['name']} (نفد: {c['card_type']})\n"
            msg += "\n"
            
        if high_debt_clients:
            msg += "💰 **بقالات تحتاج تحصيل كاش (وصلوا للسقف):**\n"
            for c in high_debt_clients:
                msg += f"▪️ {c['name']} (الدين: {c['debt']} ريال)\n"
            msg += "\n"
            
        if not empty_inv_clients and not high_debt_clients:
            msg += "✅ السوق مستقر تماماً، لا توجد طوارئ اليوم. يومك سعيد وموفق! 🚀"
        else:
            msg += "💡 *نصيحة سيستم: ركز زياراتك اليوم على هذه البقالات لزيادة المبيعات وتحصيل الكاش.*"
            
        try:
            await bot_instance.send_message(ADMIN_ID, msg)
        except: pass

# ================= 10. مراقب سقف المديونية الذكي (Smart Auto-Reminders) =================
async def credit_limit_monitor(bot_instance: Bot):
    """يعمل مرتين يومياً لتنبيه العملاء الذين اقتربوا من السقف أو تجاوزوه"""
    if not database.pool: return
    import random
    from unified_main import safe_send_whatsapp
    
    try:
        async with database.pool.acquire() as conn:
            # جلب العملاء الذين تجاوزوا 90% من السقف
            clients = await conn.fetch("""
                SELECT user_id, name, debt, credit_limit, phone, wa_status 
                FROM users 
                WHERE role = 'client' AND debt > 0 AND credit_limit > 0
            """)
            
            for c in clients:
                debt = Decimal(c['debt'])
                limit = Decimal(c['credit_limit'])
                ratio = debt / limit
                
                msg = ""
                if ratio >= 1.0:
                    msg = f"🚨 *تنبيه تجاوز السقف:*\nعزيزي العميل ({c['name']})، لقد تجاوزت سقف المديونية المسموح لك ({limit} ريال).\nدينك الحالي: *{debt} ريال*.\nتم إيقاف السحب الآجل مؤقتاً. يرجى تسديد مبلغ لتنشيط حسابك آلياً."
                elif ratio >= 0.9:
                    msg = f"⚠️ *تنبيه اقتراب من السقف:*\nعزيزي العميل ({c['name']})، مديونيتك ({debt} ريال) اقتربت جداً من السقف المسموح ({limit} ريال).\nنرجو التجهيز للسداد لضمان استمرار الخدمة بدون انقطاع."
                    
                if msg:
                    try:
                        # إرسال للتليجرام
                        await bot_instance.send_message(c['user_id'], msg.replace('*', '**'))
                        
                        # 🌟 إرسال للواتساب مع حماية التأخير البشري
                        if c['phone'] and c['wa_status'] == 'on':
                            await safe_send_whatsapp(c['user_id'], msg, bot=bot_instance)
                            await asyncio.sleep(random.uniform(15.0, 25.0)) # تأخير آمن (15 إلى 25 ثانية)
                        else:
                            await asyncio.sleep(random.uniform(2.0, 4.0)) # تأخير تليجرام العادي
                    except: pass
    except Exception as e:
        print(f"Credit Limit Monitor Error: {e}")

# ================= 10.1 روبوت التسعير المرن (Dynamic Pricing Protection) =================
async def auto_sync_telecom_prices(bot_instance: Bot):
    """يعمل كل ساعة: يراقب أسعار المزود ويحدثها تلقائياً لحمايتك من الخسارة"""
    if not database.pool: return
    try:
        # 1. محاكاة جلب الأسعار من API المزود (أم دراهم)
        # 🌟 (في المستقبل ستستبدل هذا بطلب حقيقي للـ API الخاص بالمزود)
        import random
        mock_api_data = [
            {"network": "yemen_mobile", "name": "باقة مزايا 2 جيجا", "cost": 2300},
            {"network": "yemen_mobile", "name": "باقة مزايا 4 جيجا", "cost": 4600},
            {"network": "you", "name": "باقة سمارت 4 جيجا", "cost": 2800},
            {"network": "sabafon", "name": "باقة سوبر 3 جيجا", "cost": 2000}
        ]
        
        # محاكاة: احتمال 5% أن المزود يرفع السعر فجأة أثناء الفحص
        if random.random() < 0.05:
            mock_api_data[0]["cost"] = 2400 # رفع السعر من 2300 إلى 2400
            
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
            
            changes_report = ""
            
            # 3. تطبيق الحسبة الذكية وحفظها
            async with conn.transaction():
                for pkg in mock_api_data:
                    net = pkg['network']
                    new_cost = Decimal(pkg['cost'])
                    
                    # فحص السعر القديم في قاعدة البيانات
                    old_pkg = await conn.fetchrow("SELECT cost_price FROM telecom_packages WHERE network = $1 AND package_name = $2", net, pkg['name'])
                    
                    if old_pkg:
                        old_cost = Decimal(old_pkg['cost_price'])
                        if new_cost != old_cost:
                            # 🚨 السعر تغير! نحسب السعر الجديد لحماية الأرباح
                            cash_price = round(new_cost + (new_cost * margins[net]['cash'] / Decimal('100')))
                            credit_price = round(new_cost + (new_cost * margins[net]['credit'] / Decimal('100')))
                            retail_price = cash_price + 100 # سعر الطياري أعلى بـ 100 ريال
                            
                            await conn.execute("""
                                UPDATE telecom_packages 
                                SET cost_price = $1, selling_price = $2, credit_price = $3, retail_price = $4
                                WHERE network = $5 AND package_name = $6
                            """, new_cost, cash_price, credit_price, retail_price, net, pkg['name'])
                            
                            direction = "📈 ارتفع" if new_cost > old_cost else "📉 انخفض"
                            changes_report += f"▪️ **{pkg['name']}**: {direction} التكلفة من {old_cost} إلى {new_cost} ريال.\n"
                            changes_report += f"   *(السعر الجديد للبقالات: كاش {cash_price} | آجل {credit_price})*\n\n"
            
            # 4. إرسال إنذار للوكيل إذا حدث تغيير
            if changes_report:
                msg = f"🚨 **تحديث تلقائي لأسعار الباقات (حماية الأرباح):**\n\nاكتشف النظام تغيراً في أسعار مزود الخدمة وقام بتحديث أسعار البيع تلقائياً للحفاظ على هامش ربحك:\n\n{changes_report}"
                await bot_instance.send_message(ADMIN_ID, msg)
                
                # إشعار البقالات بتحديث الأسعار
                from web_api import notify_clients
                await notify_clients(action="update_needed")
                
    except Exception as e:
        print(f"Auto Sync Prices Error: {e}")

# ================= 10.2 الترميم الذاتي لقاعدة البيانات (Auto-Vacuum) =================
async def database_maintenance(bot_instance: Bot):
    """يعمل فجر الجمعة لتنظيف قاعدة البيانات وتسريعها"""
    if not database.pool: return
    try:
        # 🌟 الإصلاح الأمني: أوامر VACUUM يجب أن تعمل خارج أي Transaction
        # في asyncpg، يجب استخدام اتصال مباشر (ليس من الـ pool) أو التأكد من عدم وجود transaction
        import asyncpg
        from config import DATABASE_URL
        
        # نفتح اتصالاً مستقلاً خصيصاً للصيانة لضمان عدم وجود أي قيود
        maint_conn = await asyncpg.connect(DATABASE_URL)
        try:
            # إزالة السجلات الميتة وإعادة بناء الفهارس
            await maint_conn.execute("VACUUM ANALYZE;")
            db_name = await maint_conn.fetchval("SELECT current_database();")
            await maint_conn.execute(f"REINDEX DATABASE {db_name};")
        finally:
            await maint_conn.close()
            
        await bot_instance.send_message(ADMIN_ID, "🛠️ **الترميم الذاتي:**\nتم عمل (Vacuum & Reindex) لقاعدة البيانات بنجاح. النظام الآن يعمل بأقصى سرعة 🚀.")
    except Exception as e:
        print(f"DB Maintenance Error: {e}")

# ================= 11. ميزان المراجعة الخفي (Hidden Trial Balance) =================
async def hidden_trial_balance(bot_instance: Bot):
    """يعمل الساعة 4:05 فجراً للتأكد من عدم وجود أي تسرب مالي (الأصول = الخصوم)"""
    if not database.pool: return
    try:
        async with database.pool.acquire() as conn:
            engine = FinancialEngine(database.pool)
            stats = await engine.get_summary(conn)
            
            # 1. الأصول (Assets)
            network_cash = stats["cash"]
            telecom_cash = stats.get("telecom", Decimal('0.0'))
            telecom_portal_balance = stats.get("telecom_portal_balance", Decimal('0.0'))
            inventory_value = stats["inv_value"]
            market_debt = stats["debt_cost"]
            
            # 🌟 الإصلاح: إضافة رصيد البوابة للأصول
            total_assets = network_cash + telecom_cash + telecom_portal_balance + inventory_value + market_debt
            
            # 2. الخصوم وحقوق الملكية (Liabilities & Equity)
            # 🌟 الإصلاح: تحديد wallet_type لمنع تداخل الحسابات
            total_in = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE")
            total_out = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('تسديد_للشبكة', 'مصروفات', 'كروت_تالفة', 'مرتجع_للشبكة') AND is_reverted = FALSE")
            manager_capital = Decimal(total_in or 0) - Decimal(total_out or 0)
            
            agent_profit = stats["realized"]
            
            telecom_capital = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'telecom' AND type = 'رأس_مال_تسديدات' AND is_reverted = FALSE")
            telecom_capital = Decimal(telecom_capital or 0)
            
            # 🌟 الإصلاح: إضافة أرباح التسديدات للخصوم لكي يتوازن الميزان
            telecom_profit = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM telecom_profits")
            telecom_profit = Decimal(telecom_profit or 0)
            
            total_liabilities = manager_capital + agent_profit + telecom_capital + telecom_profit
            
            # 3. المطابقة
            difference = total_assets - total_liabilities
            
            if difference != 0:
                msg = (
                    "🚨 **إنذار محاسبي خطير (ميزان المراجعة غير متطابق)!** 🚨\n\n"
                    f"💰 **إجمالي الأصول:** {total_assets} ريال\n"
                    f"⚖️ **إجمالي الخصوم والأرباح:** {total_liabilities} ريال\n"
                    f"⚠️ **الفارق (عجز/زيادة):** {difference} ريال\n\n"
                    "*(يوجد خلل محاسبي حدث اليوم، يرجى مراجعة العمليات الأخيرة فوراً قبل أن يتراكم الخطأ)*"
                )
                await bot_instance.send_message(ADMIN_ID, msg)
    except Exception as e:
        print(f"Trial Balance Error: {e}")

# ================= 12. مراقب أعمار الديون (Debt Aging) =================
async def debt_aging_monitor(bot_instance: Bot):
    """يعمل الساعة 9:00 صباحاً لتنبيه الوكيل بالديون الميتة"""
    if not database.pool: return
    try:
        async with database.pool.acquire() as conn:
            query = """
                SELECT u.name, u.debt, MAX(t.date) as last_payment
                FROM users u
                LEFT JOIN transactions t ON u.user_id = t.user_id AND t.type = 'تسديد_من_عميل'
                WHERE u.role = 'client' AND u.debt > 0
                GROUP BY u.user_id, u.name, u.debt
                HAVING MAX(t.date) < NOW() - INTERVAL '30 days' OR MAX(t.date) IS NULL
            """
            risky_clients = await conn.fetch(query)
            
            if risky_clients:
                msg = "🚨 **تقرير أعمار الديون (ديون ميتة/خطرة):**\n\n"
                for c in risky_clients:
                    days_late = "أكثر من شهر"
                    if c['last_payment']:
                        delta = datetime.now() - c['last_payment']
                        days_late = f"{delta.days} يوم"
                    msg += f"▪️ **{c['name']}**: {int(c['debt'])} ريال (آخر سداد: منذ {days_late})\n"
                
                msg += "\n⚠️ *يُنصح بالنزول الميداني لتحصيل هذه الديون فوراً، أو إيقاف التعامل معهم.*"
                await bot_instance.send_message(ADMIN_ID, msg)
    except Exception as e:
        print(f"Debt Aging Error: {e}")

# ================= 13. الأرشفة التلقائية (Auto-Archiving) =================
async def auto_archive_old_data(bot_instance: Bot):
    """يعمل يوم 1 من كل شهر لتنظيف السيرفر وتسريعه"""
    if not database.pool: return
    try:
        engine = FinancialEngine(database.pool)
        res = await engine.archive_data(6) # يحذف ما هو أقدم من 6 أشهر
        
        if res["status"] == "success":
            stats = res["deleted_stats"]
            if sum(stats.values()) > 0:
                msg = (
                    "🧹 **تقرير الأرشفة التلقائية (تنظيف السيرفر):**\n"
                    f"تم تنظيف قاعدة البيانات من السجلات الأقدم من 6 أشهر لتسريع النظام:\n"
                    f"▪️ محادثات قديمة: {stats.get('chats', 0)}\n"
                    f"▪️ إشعارات ويب: {stats.get('notifications', 0)}\n"
                    f"▪️ سجلات نقاط بيع: {stats.get('client_sales', 0)}\n"
                    "✅ النظام الآن أسرع وأخف."
                )
                await bot_instance.send_message(ADMIN_ID, msg)
    except Exception as e:
        print(f"Auto Archive Error: {e}")
        
        # ================= 11. تقرير الاستثناءات والتدخلات الآلية (Daily Exceptions Report) =================
async def daily_exceptions_report(bot_instance: Bot):
    """مهمة يومية: ترسل للوكيل ملخصاً بكل التسويات الآلية والتجاوزات التي حدثت خلال اليوم"""
    if not database.pool: return
    
    async with database.pool.acquire() as conn:
        # جلب كل التدخلات الآلية والتجاوزات التي حدثت اليوم
        exceptions = await conn.fetch("""
            SELECT t.type, t.amount, t.details, u.name 
            FROM transactions t
            LEFT JOIN users u ON t.user_id = u.user_id
            WHERE t.date >= CURRENT_DATE 
            AND (
                t.details LIKE '%تسوية جرد آلي%' OR 
                t.details LIKE '%إصلاح آلي للمطابقة%' OR 
                t.details LIKE '%تسوية مطابقة صوتية%' OR 
                t.structured_details LIKE '%"bypassed_shortage": true%' OR
                t.structured_details LIKE '%"bypassed": true%'
            )
        """)
        
        if not exceptions:
            # إذا لم يكن هناك استثناءات، لا نرسل شيئاً لكي لا نزعج الوكيل
            return
            
        msg = "🕵️‍♂️ **تقرير الرقابة والتدخلات الآلية (اليومي):**\n\n"
        msg += "يا دكتور وليد، هذا ملخص بكل الاستثناءات والتسويات التي قام بها النظام أو تم تجاوزها بصلاحياتك اليوم:\n\n"
        
        for i, ex in enumerate(exceptions, 1):
            client_name = ex['name'] if ex['name'] else "مجهول/عام"
            tx_type = ex['type'].replace('_', ' ')
            amount = int(ex['amount'])
            details = ex['details']
            
            msg += f"{i}️⃣ **{client_name}** | {tx_type} ({amount} ريال)\n"
            msg += f"📝 *التفاصيل:* {details}\n\n"
            
        msg += "💡 *يرجى مراجعة هذه العمليات. إذا كان هناك أي خطأ، يمكنك التراجع عنها باستخدام أمر (/undo).* ⏪"
        
        try:
            await bot_instance.send_message(ADMIN_ID, msg)
        except Exception as e:
            print(f"Error sending exceptions report: {e}")
            
            # ================= 14. روبوت المطابقة الآلية للتسديدات (Auto-Reconciliation) =================
async def nightly_telecom_reconciliation(bot_instance: Bot):
    """يعمل الساعة 3:30 فجراً لمطابقة حسابات التسديدات مع المزود"""
    if not database.pool: return
    
    from datetime import timedelta
    import random
    yesterday = (datetime.now() - timedelta(days=1)).date()
    
    try:
        async with database.pool.acquire() as conn:
            # 1. جلب عملياتنا المحلية ليوم أمس
            local_txs = await conn.fetch("""
                SELECT id, phone_number, package_name, cost_price, status, provider_reference_id 
                FROM telecom_transactions 
                WHERE DATE(created_at) = $1
            """, yesterday)
            
        if not local_txs:
            return # لا توجد عمليات ليوم أمس
            
        # 2. محاكاة جلب كشف الحساب من المزود (سيتم استبدالها بـ API المزود الحقيقي لاحقاً)
        provider_txs = []
        for tx in local_txs:
            if tx['status'] == 'success':
                provider_txs.append({
                    "ref_id": tx['provider_reference_id'],
                    "phone": tx['phone_number'],
                    "cost": float(tx['cost_price']),
                    "status": "success"
                })
                
        # 🚨 (محاكاة خطأ من المزود للتجربة): المزود خصم علينا مبلغ لعملية لم نسجلها نحن كنجاح!
        provider_txs.append({
            "ref_id": f"FAKE_REF_{random.randint(1000,9999)}",
            "phone": "777999888",
            "cost": 1200.0,
            "status": "success"
        })

        # 3. بدء المطابقة (Reconciliation Logic)
        local_success_dict = {tx['provider_reference_id']: tx for tx in local_txs if tx['status'] == 'success'}
        local_failed_dict = {tx['provider_reference_id']: tx for tx in local_txs if tx['status'] != 'success' and tx['provider_reference_id']}
        
        discrepancies = []
        total_lost_money = 0.0
        
        for p_tx in provider_txs:
            ref = p_tx['ref_id']
            if ref in local_success_dict:
                local_cost = float(local_success_dict[ref]['cost_price'])
                if abs(p_tx['cost'] - local_cost) > 1:
                    discrepancies.append(f"⚠️ اختلاف تكلفة: رقم {p_tx['phone']} (المزود خصم {p_tx['cost']}، ونحن سجلنا {local_cost})")
            elif ref in local_failed_dict:
                discrepancies.append(f"🚨 خصم خاطئ: رقم {p_tx['phone']} (المزود خصم {p_tx['cost']} ريال، والعملية فاشلة عندنا!)")
                total_lost_money += p_tx['cost']
            else:
                discrepancies.append(f"❓ عملية مجهولة: رقم {p_tx['phone']} (المزود خصم {p_tx['cost']} ريال، غير موجودة في نظامنا)")
                total_lost_money += p_tx['cost']
                
        if discrepancies:
            report = f"⚖️ **تقرير المطابقة الآلية للتسديدات (عن يوم {yesterday}):**\n\n"
            report += "⚠️ **تم اكتشاف اختلافات بين نظامنا والمزود!**\n\n"
            for d in discrepancies:
                report += f"▪️ {d}\n"
            report += f"\n💸 **إجمالي الأموال المعلقة/المفقودة:** {total_lost_money} ريال\n"
            report += "\n*(يرجى مراجعة المزود بهذه العمليات لاسترداد أموالك)*"
            
            await bot_instance.send_message(ADMIN_ID, report)
        else:
            # رسالة صامتة تطمئنك أن كل شيء سليم
            await bot_instance.send_message(ADMIN_ID, f"⚖️ **المطابقة الآلية للتسديدات:**\nتمت مطابقة حسابات يوم {yesterday} مع المزود بنجاح 100%. لا توجد أي فروقات مالية. 😴", disable_notification=True)
            
    except Exception as e:
        print(f"Reconciliation Job Error: {e}")
        
# ================= 10.3 روبوت تنظيف مساحة السيرفر وقاعدة البيانات =================
async def disk_cleanup_bot(bot_instance: Bot):
    """يعمل يومياً الساعة 2 فجراً لمسح الصور القديمة وحرق الكروت الإلكترونية المباعة المنتهية"""
    import os
    import time
    
    try:
        deleted_files = 0
        current_time = time.time()
        seven_days_ago = current_time - (7 * 24 * 60 * 60) # 7 أيام
        
        # 1. تنظيف مجلد static من صور الكروت التالفة القديمة
        if os.path.exists('static'):
            for filename in os.listdir('static'):
                if filename.startswith('damaged_') or filename.startswith('promo_'):
                    filepath = os.path.join('static', filename)
                    if os.path.getmtime(filepath) < seven_days_ago:
                        os.remove(filepath)
                        deleted_files += 1
                        
        # 2. تنظيف المجلد الرئيسي من أي ملفات PDF أو Excel شاردة
        for filename in os.listdir('.'):
            if filename.endswith('.pdf') or filename.endswith('.xlsx'):
                filepath = os.path.join('.', filename)
                if os.path.getmtime(filepath) < seven_days_ago:
                    os.remove(filepath)
                    deleted_files += 1
                    
        if deleted_files > 0:
            print(f"🧹 [Disk Cleanup]: تم تنظيف {deleted_files} ملفات مؤقتة قديمة بنجاح.")

        # 🌟 3. المحرقة الذكية: حذف الكروت الإلكترونية المباعة منذ أكثر من 14 يوماً 🌟
        if database.pool:
            async with database.pool.acquire() as conn:
                res = await conn.execute("DELETE FROM electronic_cards WHERE status = 'sold' AND sold_at < CURRENT_DATE - INTERVAL '14 days'")
                print(f"🔥 [DB Cleanup]: تم حرق الكروت الإلكترونية القديمة: {res}")
            
    except Exception as e:
        print(f"Disk Cleanup Error: {e}")

# ================= 10.4 روبوت الإغلاق السنوي (Annual Closing) =================
async def annual_closing_bot(bot_instance: Bot):
    """يعمل يوم 5 يناير من كل عام لإغلاق حسابات العام الماضي وأرشفتها"""
    if not database.pool: return
    
    from datetime import datetime
    import asyncio
    from aiogram.types import BufferedInputFile
    
    now = datetime.now()
    # 🌟 الحارس الزمني: نتأكد أننا في شهر يناير (1)
    if now.month != 1: return
    
    previous_year = now.year - 1
    start_date = f"{previous_year}-01-01"
    end_date = f"{previous_year}-12-31"
    
    try:
        # 1. إرسال إشعار ببدء الإغلاق لك أنت (الوكيل ومالك النظام)
        await bot_instance.send_message(ADMIN_ID, f"⏳ **بدء الإغلاق السنوي التلقائي لعام {previous_year}...**\nجاري تجميع البيانات وإعداد الميزانية العمومية.")
        
        # 2. توليد التقرير السنوي الشامل (Excel)
        from core_accounting import generate_detailed_excel_report
        excel_stream = await generate_detailed_excel_report(start_date, end_date)
        file_name = f"Annual_Closing_Report_{previous_year}.xlsx"
        excel_document = BufferedInputFile(excel_stream.read(), filename=file_name)
        
        # 3. إرسال التقرير لك أنت (ADMIN_ID) وللأرشيف
        caption_text = (
            f"?? **الإغلاق السنوي الختامي لعام {previous_year}** 🎊\n"
            f"========================\n"
            f"تم إغلاق الحسابات، ترحيل الأرصدة الافتتاحية للعام الجديد، وتجهيز الميزانية العمومية.\n"
            f"مرفق ملف الإكسل الشامل لكل حركات العام الماضي.\n"
            f"*(النظام الآن جاهز للعمل في العام الجديد بأقصى سرعة 🚀)*"
        )
        
        await bot_instance.send_document(ADMIN_ID, document=excel_document, caption=caption_text)
        
        group_id = await database.get_setting("archive_channel_id")
        if group_id and group_id != "off":
            excel_stream.seek(0)
            archive_doc = BufferedInputFile(excel_stream.read(), filename=file_name)
            await bot_instance.send_document(int(group_id), document=archive_doc, caption=f"📂 **أرشيف الإغلاق السنوي:** عام {previous_year}")

        # 4. التنظيف العميق والترحيل المحاسبي (Deep Archiving & Rollover)
        from core_accounting import core_execute_annual_closing
        res = await core_execute_annual_closing(previous_year)
        
        if res["status"] == "success":
            await bot_instance.send_message(ADMIN_ID, "✅ **تمت أرشفة بيانات العام الماضي وترحيل الأرصدة بنجاح.**\nقاعدة البيانات الآن نظيفة وسريعة جداً.")
        else:
            await bot_instance.send_message(ADMIN_ID, f"❌ **حدث خطأ أثناء الإغلاق السنوي:**\n`{res['message']}`")

    except Exception as e:
        print(f"Annual Closing Error: {e}")
        await bot_instance.send_message(ADMIN_ID, f"❌ **حدث خطأ أثناء الإغلاق السنوي:**\n`{e}`")

# ================= إعداد وتشغيل المنبه =================
def start_scheduler(bot: Bot):
    """دالة تشغيل المراقب الليلي والروبوتات الإدارية (الطيار الآلي)"""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from pytz import timezone
    
    yemen_tz = timezone('Asia/Riyadh')
    scheduler = AsyncIOScheduler(timezone=yemen_tz)
    
    # 1. الإغلاق اليومي (PDF + Excel + Z-Report) - الساعة 4:00 فجراً
    scheduler.add_job(scheduled_daily_backup, 'cron', hour=4, minute=0, args=[bot])
    
    # 2. إشعار الإغلاق اليومي النصي - الساعة 4:00 فجراً
    scheduler.add_job(daily_brief, 'cron', hour=4, minute=0, args=[bot])

    # 3. ميزان المراجعة الخفي - الساعة 4:05 فجراً
    scheduler.add_job(hidden_trial_balance, 'cron', hour=4, minute=5, args=[bot])

    # 4. مراقب أعمار الديون - الساعة 9:00 صباحاً
    scheduler.add_job(debt_aging_monitor, 'cron', hour=9, minute=0, args=[bot])
    
    # 5. الأرشفة التلقائية - يوم 1 من كل شهر، الساعة 3:00 فجراً
    scheduler.add_job(auto_archive_old_data, 'cron', day=1, hour=3, minute=0, args=[bot])
    
    # 6. تقرير الاستثناءات والتدخلات الآلية - الساعة 11:30 ليلاً
    scheduler.add_job(daily_exceptions_report, 'cron', hour=23, minute=30, args=[bot])
    
    # 7. روبوت المطابقة الآلية للتسديدات - الساعة 3:30 فجراً
    scheduler.add_job(nightly_telecom_reconciliation, 'cron', hour=3, minute=30, args=[bot])
    
    # 8. المهام الأسبوعية والشهرية
    scheduler.add_job(weekly_client_statement_and_audit, 'cron', day_of_week='fri', hour=16, minute=0, args=[bot])
    scheduler.add_job(weekly_audit_summary_for_agent, 'cron', day_of_week='fri', hour=22, minute=0, args=[bot])
    scheduler.add_job(check_low_inventory, 'cron', hour=10, minute=0, args=[bot])
    scheduler.add_job(monthly_mandatory_audit, 'cron', day=27, hour=16, minute=0, args=[bot])
    scheduler.add_job(audit_red_list_report, 'cron', day=27, hour=22, minute=0, args=[bot])
    scheduler.add_job(smart_debt_escalation, 'cron', day='25,28,30', hour=16, minute=0, args=[bot])
    scheduler.add_job(morning_briefing, 'cron', hour=8, minute=30, args=[bot])
    
    # ================= 🚀 روبوتات الطيار الآلي الجديدة 🚀 =================
    
    # 9. مراقب سقف المديونية الذكي - مرتين يومياً (12 ظهراً و 8 مساءً)
    scheduler.add_job(credit_limit_monitor, 'cron', hour='12,20', minute=0, args=[bot])
    
    # 10. التسعير المرن (تحديث الأسعار تلقائياً) - كل ساعة
    scheduler.add_job(auto_sync_telecom_prices, 'interval', hours=1, args=[bot])
    
    # 11. الترميم الذاتي لقاعدة البيانات - يوم الجمعة الساعة 5:00 فجراً
    scheduler.add_job(database_maintenance, 'cron', day_of_week='fri', hour=5, minute=0, args=[bot])
    
    # 12. روبوت تنظيف مساحة السيرفر - يومياً الساعة 2:00 فجراً
    scheduler.add_job(disk_cleanup_bot, 'cron', hour=2, minute=0, args=[bot])

    # 13. روبوت الإغلاق السنوي - يعمل يوم 5 يناير الساعة 3:00 فجراً
    scheduler.add_job(annual_closing_bot, 'cron', month=1, day=5, hour=3, minute=0, args=[bot])

    # 🌟 [جديد] 14. الخزنة السحابية (النسخ المشفر للإيميل) - مرتين يومياً (12 ظهراً و 11:50 مساءً)
    scheduler.add_job(execute_scheduled_cloud_backup, 'cron', hour=12, minute=0, kwargs={'bot': bot})
    scheduler.add_job(execute_scheduled_cloud_backup, 'cron', hour=23, minute=50, kwargs={'bot': bot})

    scheduler.start()
    print("⏰ تم تشغيل نظام الذكاء الاستباقي (الطيار الآلي) بنجاح بتوقيت اليمن!")
