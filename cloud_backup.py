import asyncio
import io
import pyzipper
from datetime import datetime
import logging
import traceback
import json
from aiogram.types import BufferedInputFile

# استدعاء الإعدادات
from config import SYSTEM_VAULT_PIN, ADMIN_ID
from core_accounting import generate_full_backup_excel, get_financial_summary
import database

async def create_encrypted_zip(excel_bytes: io.BytesIO, filename: str) -> io.BytesIO:
    """تقوم بضغط ملف الإكسل وتشفيره بكلمة مرور عسكرية"""
    zip_buffer = io.BytesIO()
    
    # 🌟 الإصلاح الأمني: استخدام ZIP_DEFLATED بدلاً من ZIP_LZMA لمنع انهيار السيرفر (NotImplementedError)
    with pyzipper.AESZipFile(zip_buffer, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
        zf.setpassword(SYSTEM_VAULT_PIN.encode('utf-8'))
        zf.writestr(filename, excel_bytes.getvalue())
        
    zip_buffer.seek(0)
    return zip_buffer

async def execute_scheduled_cloud_backup(bot=None):
    """الدالة الرئيسية التي ستعمل تلقائياً كل 12 ساعة (ترسل لمجموعة الأرشيف مع المنظف التلقائي)"""
    if not bot:
        logging.error("⚠️ لم يتم تمرير كائن البوت لدالة النسخ الاحتياطي.")
        return

    try:
        logging.info("⏳ بدء عملية النسخ الاحتياطي السحابي المشفر...")
        
        if not database.pool:
            raise Exception("قاعدة البيانات غير متصلة حالياً.")

        # ================= الحساس الذكي =================
        async with database.pool.acquire() as conn:
            recent_tx_count = await conn.fetchval(
                "SELECT COUNT(*) FROM transactions WHERE date >= NOW() - INTERVAL '12 hours'"
            )
            
            if recent_tx_count == 0:
                logging.info("🛑 لم يحدث أي عمل أو تغيير في آخر 12 ساعة. تم إلغاء إرسال النسخة.")
                try: await bot.send_message(ADMIN_ID, "ℹ️ **نظام الخزنة السحابية:**\nلم يتم إرسال نسخة احتياطية لأنه لم يتم تسجيل أي عمليات جديدة في النظام خلال الـ 12 ساعة الماضية.", disable_notification=True)
                except: pass
                return 
                
            # جلب آيدي مجموعة الأرشيف
            group_id_str = await conn.fetchval("SELECT value FROM settings WHERE key = 'archive_channel_id'")
        # ================================================

        # تحديد مكان الإرسال (الأرشيف إذا كان مفعلاً، وإلا محادثة البوت)
        try:
            target_chat_id = int(group_id_str) if group_id_str and group_id_str != "off" else ADMIN_ID
        except ValueError:
            target_chat_id = ADMIN_ID # العودة للوكيل إذا كان الآيدي المحفوظ تالفاً

        fin_stats = await get_financial_summary()
        available_cash = max(0, fin_stats.get("cash", 0) - fin_stats.get("realized", 0))
        total_debt = fin_stats.get("debt_cost", 0)
        stats_text = f"💰 الكاش الصافي: {int(available_cash)} ريال\n👥 ديون السوق: {int(total_debt)} ريال"

        excel_stream = await generate_full_backup_excel()
        if not excel_stream or not excel_stream.getvalue():
            raise Exception("فشل توليد ملف الإكسل (الملف فارغ).")

        excel_filename = f"Database_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        zip_filename = f"SecureBackup_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
        encrypted_zip = await create_encrypted_zip(excel_stream, excel_filename)

        # 🌟 إرسال الملف المشفر
        caption = (
            f"🛡️ **الخزنة السحابية المشفرة** 🛡️\n\n"
            f"مرحباً بك يا دكتور وليد 👑،\n"
            f"مرفق طيه النسخة الاحتياطية الشاملة والمشفرة لنظام الشهاب Pro.\n"
            f"📅 تاريخ النسخة: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"📊 **ملخص مالي سريع:**\n{stats_text}\n\n"
            f"⚠️ **تنبيه أمني:**\n"
            f"الملف المرفق مشفر (AES-256). لفك الضغط، يرجى استخدام (الرمز السري للنظام / رمز الفورمات)."
        )
        
        zip_file = BufferedInputFile(encrypted_zip.read(), filename=zip_filename)
        sent_msg = await bot.send_document(target_chat_id, document=zip_file, caption=caption)
        
        logging.info("✅ تم إرسال النسخة الاحتياطية المشفرة بنجاح.")

        # ================= 🧹 المنظف التلقائي (يحتفظ بآخر 3 نسخ فقط) =================
        try:
            async with database.pool.acquire() as conn:
                old_msgs_str = await conn.fetchval("SELECT value FROM settings WHERE key = 'backup_msg_ids'")
                old_msgs = json.loads(old_msgs_str) if old_msgs_str else []
                
                # 🌟 الإصلاح الأمني: حفظ الـ chat_id مع الـ message_id لضمان الحذف الصحيح
                old_msgs.append({"chat_id": target_chat_id, "msg_id": sent_msg.message_id})
                
                # إذا زاد العدد عن 3، نحذف الأقدم من التليجرام ومن القائمة
                while len(old_msgs) > 3:
                    msg_to_delete = old_msgs.pop(0)
                    try:
                        # 🌟 التوافق مع السجلات القديمة (التي كانت أرقاماً فقط)
                        if isinstance(msg_to_delete, dict):
                            await bot.delete_message(msg_to_delete["chat_id"], msg_to_delete["msg_id"])
                        else:
                            await bot.delete_message(target_chat_id, msg_to_delete)
                    except:
                        pass # نتجاهل الخطأ إذا كانت الرسالة محذوفة يدوياً
                        
                # حفظ القائمة الجديدة في قاعدة البيانات
                await conn.execute("""
                    INSERT INTO settings (key, value) VALUES ('backup_msg_ids', $1)
                    ON CONFLICT (key) DO UPDATE SET value = $1
                """, json.dumps(old_msgs))
        except Exception as clean_err:
            logging.error(f"⚠️ فشل التنظيف التلقائي: {clean_err}")
        # ==============================================================================

    except Exception as e:
        error_details = traceback.format_exc()
        error_msg = f"❌ **فشل إرسال النسخة الاحتياطية المشفرة:**\n`{str(e)}`\n\n🔍 **التفاصيل الفنية:**\n`{error_details[-600:]}`"
        logging.error(error_msg)
        try: await bot.send_message(ADMIN_ID, error_msg)
        except: pass
