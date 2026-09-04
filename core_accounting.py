import json
import re
import os
import asyncio
import logging
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import List, Dict, Any, Optional, Union
from io import BytesIO
import database # أو اسم الملف الذي يحتوي على اتصال قاعدة البيانات في مشروعك

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.drawing.image import Image

# =====================================================================
# 1. نظام التسجيل والرقابة (Logging & Monitoring)
# =====================================================================
import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("FinancialEngine")
logger.setLevel(logging.INFO)

# 🌟 [جديد] إعداد الـ Logger ليكتب في ملف نصي سري مع حماية حجم الملف (5 ميجا كحد أقصى)
if not logger.handlers:
    file_handler = RotatingFileHandler("financial_security.log", maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # للطباعة في شاشة السيرفر أيضاً
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

# =====================================================================
# 2. الاستثناءات المخصصة (Enterprise Exceptions)
# =====================================================================
class FinancialError(Exception):
    def __init__(self, message: str, details: Optional[Dict] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

class InsufficientFundsError(FinancialError): pass
class InsufficientStockError(FinancialError): pass
class CreditLimitExceededError(FinancialError): pass
class TransactionNotFoundError(FinancialError): pass
class InvalidOperationError(FinancialError): pass
class SecurityViolationError(FinancialError): pass

# =====================================================================
# 3. التعريفات والثوابت (Enums & Constants)
# =====================================================================
class TxType(Enum):
    GIVE_CARDS = "تسليم_لعميل"
    COLLECT_DEBT = "تسديد_من_عميل" 
    DIRECT_SALE = "بيع_مباشر"
    RETAIL_SALE = "مبيعات_آجلة"
    RETURN_FROM_CLIENT = "مرتجع_من_عميل"
    RETURN_TO_NETWORK = "مرتجع_للشبكة"
    RECEIVE_FROM_NETWORK = "استلام_من_الشبكة"
    EXPENSE = "مصروفات"
    AGENT_COMMISSION = "نسبة_الوكيل"
    WITHDRAW_PROFIT = "سحب_أرباح"
    WITHDRAW_TELECOM_PROFIT = "سحب_أرباح_تسديدات"
    TRANSFER_BALANCE = "تحويل_رصيد" 
    REVERSE_ENTRY = "قيد_عكسي"
    OPENING_BALANCE = "رصيد_افتتاحي"
    TELECOM_PAYMENT = "تسديد_باقة"
    TELECOM_TOPUP = "تغذية_رصيد_بوابة" # 🌟 تم الإصلاح: ليتطابق مع دوال الحساب والتراجع
    CLIENT_SALE = "مبيعات_بقالة" 
    DAMAGED_CARDS = "كروت_تالفة"
    PAY_MANAGER = "تسديد_للشبكة"
    EOD_CLOSING = "إغلاق_يومي" 
    ADD_TELECOM_CAPITAL = "رأس_مال_تسديدات"
    AGENT_TELECOM_SALE = "تسديد_باقة_وكيل"

class UserRole(Enum):
    CLIENT = "client"
    AGENT = "agent"
    MANAGER = "manager"
    SYSTEM = "system"

# =====================================================================
# 4. أدوات المساعدة ومدير الأقفال (Helpers & LockManager)
# =====================================================================
class FinancialEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal): return str(obj)
        if isinstance(obj, datetime): return obj.isoformat()
        return super().default(obj)

def to_decimal(value: Any, default: str = '0.0', raise_errors: bool = True) -> Decimal:
    if value is None: 
        return Decimal(default)
    try:
        clean_val = str(value).replace(',', '').strip()
        if not clean_val: 
            return Decimal(default)
        return Decimal(clean_val).quantize(Decimal('0.00'), rounding=ROUND_HALF_UP)
    except Exception as e:
        logger.error(f"🚨 [CRITICAL] فشل تحويل القيمة إلى Decimal: '{value}' | الخطأ: {e}")
        if raise_errors:
            # إيقاف العملية فوراً لمنع تسجيل مبالغ خاطئة
            raise InvalidOperationError(f"قيمة مالية غير صالحة تم إرسالها للنظام: {value}")
        return Decimal(default)

class LockManager:
    @staticmethod
    async def lock_users(conn, user_ids: List[int]):
        uids = sorted(list(set(user_ids)))
        if uids: await conn.execute("SELECT 1 FROM users WHERE user_id = ANY($1) ORDER BY user_id FOR UPDATE", uids)

    @staticmethod
    async def lock_inventory(conn, card_types: List[str]):
        types = sorted(list(set(card_types)))
        if types: await conn.execute("SELECT 1 FROM inventory WHERE card_type = ANY($1) ORDER BY card_type FOR UPDATE", types)

    @staticmethod
    async def lock_client_inventory(conn, user_id: int, card_types: List[str]):
        types = sorted(list(set(card_types)))
        if types: await conn.execute("SELECT 1 FROM client_inventory WHERE user_id = $1 AND card_type = ANY($2) ORDER BY card_type FOR UPDATE", user_id, types)
        
# =====================================================================
# 5. المحرك المالي الاحترافي (The Financial Engine)
# =====================================================================
class FinancialEngine:
    def __init__(self, pool):
        self.pool = pool

    # --- المساعدات الداخلية (Internal Helpers) ---
    # 🌟 إضافة wallet_type للدالة لكي تقبل تحديد نوع المحفظة (مدير أو وكيل)
    async def _log_tx(self, conn, user_id: int, tx_type: TxType, amount: Decimal, 
                     details: str, sd: Optional[List[Dict]] = None, key: Optional[str] = None, wallet_type: str = 'manager'):
        sd_json = json.dumps(sd, cls=FinancialEncoder) if sd else None
        
        # 🌟 الحماية الفولاذية 1: الإدخال المباشر في الجدول السريع (أسرع من البحث بـ 100 مرة)
        if key:
            try:
                await conn.execute("INSERT INTO idempotency_keys (key) VALUES ($1)", key)
            except Exception as e:
                if 'unique constraint' in str(e).lower() or 'duplicate key' in str(e).lower():
                    raise InvalidOperationError("🚨 رفض أمني: تم تنفيذ هذه العملية مسبقاً (تم صد محاولة تكرار أو نقر مزدوج).")
                raise e

        try:
            # 🌟 إدخال العملية في الدفتر (مع الاحتفاظ بالمفتاح للتدقيق)
            tx_id = await conn.fetchval(
                "INSERT INTO transactions (user_id, type, amount, details, structured_details, idempotency_key, wallet_type, date) VALUES ($1, $2, $3, $4, $5, $6, $7, CURRENT_TIMESTAMP) RETURNING id",
                user_id, tx_type.value, amount, details, sd_json, key, wallet_type
            )
            
            # ==========================================
            # 📸 [جديد] التقاط لقطة الصندوق الأسود (Black Box Snapshot)
            # ==========================================
            try:
                state_dict = {}
                
                # 1. لقطة محفظة الوكيل
                wallet = await conn.fetchrow("SELECT manager_cash, telecom_cash, telecom_balance, realized_profit FROM agent_wallet WHERE id = 1")
                if wallet:
                    state_dict['agent'] = {
                        'manager_cash': str(wallet['manager_cash']),
                        'telecom_cash': str(wallet['telecom_cash']),
                        'telecom_balance': str(wallet['telecom_balance']),
                        'realized_profit': str(wallet['realized_profit'])
                    }
                    
                # 2. لقطة حساب العميل (إن وجد)
                if user_id != 0:
                    user = await conn.fetchrow("SELECT debt, old_debt, pending_profit, pos_cash_collected, telecom_debt, telecom_balance FROM users WHERE user_id = $1", user_id)
                    if user:
                        state_dict['client'] = {
                            'debt': str(user['debt']),
                            'old_debt': str(user['old_debt']),
                            'pending_profit': str(user['pending_profit']),
                            'pos_cash_collected': str(user['pos_cash_collected']),
                            'telecom_debt': str(user['telecom_debt']),
                            'telecom_balance': str(user['telecom_balance'])
                        }
                
                # 3. جلب اللقطة السابقة (State Before) من آخر عملية لنفس العميل
                last_bb = await conn.fetchrow("SELECT state_after FROM system_blackbox WHERE client_id = $1 ORDER BY id DESC LIMIT 1", user_id)
                state_before = last_bb['state_after'] if last_bb else None
                
                # 4. حفظ اللقطة في الصندوق الأسود
                await conn.execute("""
                    INSERT INTO system_blackbox (tx_id, action_type, client_id, amount, state_before, state_after)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """, tx_id, tx_type.value, user_id, amount, state_before, json.dumps(state_dict))
                
            except Exception as bb_err:
                # لا نوقف العملية الأساسية إذا فشل الصندوق الأسود، بل نسجل الخطأ فقط
                logger.error(f"BlackBox Error: {bb_err}")
            # ==========================================

            return tx_id

        except Exception as e:
            if 'unique constraint' in str(e).lower() or 'duplicate key' in str(e).lower():
                raise InvalidOperationError("🚨 رفض أمني: تم صد محاولة تكرار العملية (تم منع النقر المزدوج).")
            raise e

    # 🌟 الحماية الفولاذية 2: تسجيل العمليات المتأخرة في "غرفة النزاعات" (Reverse Reconciliation)
    async def log_dispute(self, client_id: int, phone: str, amount: Decimal, details: str):
        """تسجل العمليات التي فشلت في التطبيق ولكنها نجحت لاحقاً في سيرفر المزود"""
        async with self.pool.acquire() as conn:
            # إنشاء الجدول إذا لم يكن موجوداً
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS telecom_disputes (
                    id SERIAL PRIMARY KEY,
                    client_id BIGINT,
                    phone VARCHAR(20),
                    amount NUMERIC,
                    details TEXT,
                    status VARCHAR(20) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute(
                "INSERT INTO telecom_disputes (client_id, phone, amount, details) VALUES ($1, $2, $3, $4)",
                client_id, phone, amount, details
            )

    async def _update_balances(self, conn, user_id: int, debt: Decimal, pending: Decimal = 0, old: Decimal = 0):                            
        await conn.execute("""
            UPDATE users 
            SET debt = debt + $1, 
                pending_profit = pending_profit + $2, 
                old_debt = GREATEST(0, old_debt + $3),
                pos_cash_collected = LEAST(COALESCE(pos_cash_collected, 0), GREATEST(0, COALESCE(debt, 0) + $1))
            WHERE user_id = $4
        """, debt, pending, old, user_id)

    async def _record_profit(self, conn, amount: Decimal, details: str):
        if amount == 0: return
        await conn.execute("INSERT INTO agent_profits (amount, details, date) VALUES ($1, $2, CURRENT_TIMESTAMP)", amount, details)

    # 🌟 التحديث الخارق: قراءة الأرصدة التراكمية الجاهزة لتسريع النظام ومنع اختناق المعال
    async def get_summary(self, conn=None):
        async def fetch(c):
            # 🌟 الإصلاح الأمني: تنفيذ الاستعلامات بشكل تسلسلي لمنع خطأ (another operation is in progress)
            wallet = await c.fetchrow("SELECT manager_cash, realized_profit, telecom_balance, telecom_cash FROM agent_wallet WHERE id = 1")
            res_users = await c.fetchrow("SELECT COALESCE(SUM(debt), 0) as debt, COALESCE(SUM(pending_profit), 0) as pending FROM users WHERE role = 'client'")
            res_inv = await c.fetchval("SELECT COALESCE(SUM(quantity * cost_price), 0) FROM inventory")

            # 🛡️ حماية فولاذية ضد أخطاء (NoneType) في حال كانت الجداول فارغة تماماً
            debt_market = to_decimal(res_users['debt'] if res_users else 0)
            pending = to_decimal(res_users['pending'] if res_users else 0)
            inv_value = to_decimal(res_inv if res_inv else 0)

            res = {
                "debt_market": debt_market,
                "pending": pending,
                "inv_value": inv_value,
                "realized": to_decimal(wallet['realized_profit'] if wallet else 0),
                "cash": to_decimal(wallet['manager_cash'] if wallet else 0),
                "telecom": to_decimal(wallet['telecom_cash'] if wallet else 0), # 🌟 الكاش الفعلي (الورقي)
                "telecom_portal_balance": to_decimal(wallet['telecom_balance'] if wallet else 0) # 🌟 الرصيد الرقمي (في البوابة)
            }
            res["debt_cost"] = res["debt_market"] - res["pending"]
            return res

        if conn: return await fetch(conn)
        async with self.pool.acquire() as c: return await fetch(c)

        # 🌟 دالة المصروفات وتسديد الإدارة (معدلة لتعمل بتناغم تام مع الـ Triggers)
    async def finance_action(self, action_type: TxType, amount: Decimal, details: str, source: str = "", key: Optional[str] = None):
        if amount <= 0: raise InvalidOperationError("المبلغ يجب أن يكون موجباً")
        
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                fin_stats = await self.get_summary(conn)
                available_cash = fin_stats["cash"]
                
                if action_type == TxType.EXPENSE:
                    if amount > available_cash: 
                        # 🌟 تسجيل محاولة صرف تتجاوز الكاش
                        logger.warning(f"⚠️ محاولة صرف مصروفات تتجاوز الكاش! المطلوب: {amount}، المتوفر: {available_cash}")
                        raise InsufficientFundsError(f"كاش الصندوق لا يكفي! المتوفر: {available_cash}")
                    # الـ Trigger سيخصم الكاش تلقائياً
                    tx_id = await self._log_tx(conn, 0, action_type, amount, f"{details} {source}", None, key)
                    
                elif action_type == TxType.PAY_MANAGER:
                    required_to_remit = max(Decimal('0.0'), available_cash - fin_stats["realized"])
                    if amount > required_to_remit: raise InvalidOperationError(f"المبلغ يتجاوز المطلوب توريده! أقصى مبلغ هو {required_to_remit}")
                    # الـ Trigger سيخصم الكاش تلقائياً
                    tx_id = await self._log_tx(conn, 0, action_type, amount, f"{details} {source}", None, key)
                    
                elif action_type == TxType.AGENT_COMMISSION:
                    # هذه العملية تزيد الأرباح فقط ولا تخصم كاش (سحب الكاش يتم عبر سحب_أرباح)
                    tx_id = await self._log_tx(conn, 0, action_type, amount, f"{details} {source}", None, key)
                    await self._record_profit(conn, amount, f"نسبة/راتب من الإدارة {source}")
                    
                elif action_type == TxType.WITHDRAW_PROFIT:
                    if amount > fin_stats["realized"]: raise InsufficientFundsError(f"رصيد الأرباح لا يكفي! أقصى مبلغ هو {fin_stats['realized']}")
                    if amount > available_cash: raise InsufficientFundsError(f"كاش الصندوق لا يكفي! المتوفر: {available_cash}")
                    # الـ Trigger سيخصم الكاش ويخصم الأرباح تلقائياً
                    await self._record_profit(conn, -amount, f"{details} {source}")
                    tx_id = await self._log_tx(conn, 0, action_type, amount, f"{details} {source}", None, key)
                    
                elif action_type == TxType.ADD_TELECOM_CAPITAL:
                    # 🌟 إيداع رأس مال لبدء العمل في التسديدات
                    # الـ Trigger سيضيف المبلغ تلقائياً إلى كاش التسديدات (telecom_cash)
                    tx_id = await self._log_tx(conn, 0, action_type, amount, f"{details} {source}", None, key, wallet_type='telecom')

                elif action_type == TxType.WITHDRAW_TELECOM_PROFIT:
                    # 🌟 سحب أرباح التسديدات
                    telecom_profits = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM telecom_profits")
                    telecom_cash = fin_stats.get("telecom", Decimal('0.0'))
                    
                    if amount > telecom_profits: raise InsufficientFundsError(f"رصيد أرباح التسديدات لا يكفي! أقصى مبلغ هو {telecom_profits}")
                    if amount > telecom_cash: raise InsufficientFundsError(f"كاش التسديدات المتوفر لا يكفي للسحب! المتوفر: {telecom_cash}")
                    
                    await conn.execute("INSERT INTO telecom_profits (amount, details, date) VALUES ($1, $2, CURRENT_TIMESTAMP)", -amount, f"{details} {source}")
                    # الـ Trigger سيخصم الكاش تلقائياً من درج التسديدات
                    tx_id = await self._log_tx(conn, 0, action_type, amount, f"{details} {source}", None, key, wallet_type='telecom')

                elif action_type == TxType.TELECOM_TOPUP:
                    # 🌟 تمويل البوابة من كاش التسديدات الخاص بك فقط (لحماية أموال المدير)
                    telecom_cash = fin_stats.get("telecom", Decimal('0.0'))
                    if amount > telecom_cash: raise InsufficientFundsError(f"كاش التسديدات الخاص بك لا يكفي لتغذية البوابة! المتوفر: {telecom_cash}")
                    
                    # 🌟 هنا الاستثناء الوحيد: يجب تحديث الرصيد الرقمي (telecom_balance) يدوياً لأن الـ Trigger يخصم الكاش الفعلي (telecom_cash) فقط.
                    await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", amount)
                    tx_id = await self._log_tx(conn, 0, action_type, amount, f"{details} {source}", None, key, wallet_type='telecom')
                    
                else:
                    raise InvalidOperationError("نوع العملية غير مدعوم")
                    
                return {"status": "success", "trans_id": tx_id, "message": "تم تسجيل العملية بنجاح."}

    # 🌟 دالة معالجة الذكاء الاصطناعي (معدلة من الملف الأول لتناسب الكلاس وتستخدم Savepoints)
    async def process_ai_daily_entries(self, client_id: int, new_txs: list):
        success = 0
        error_msg = ""
        async with self.pool.acquire() as conn:
            async with conn.transaction(): # المعاملة الرئيسية
                for tx in new_txs:
                    try:
                        async with conn.transaction(): # Savepoint لكل عملية (إذا فشلت واحدة لا تفشل البقية)
                            amount = to_decimal(tx.get('amount', 0))
                            if amount <= 0:
                                error_msg += f"\n⚠️ تم تجاهل عملية بقيمة {amount} (قيمة غير صالحة)."
                                continue
                                
                            tx_type_str = tx.get('type')
                            try: tx_enum = TxType(tx_type_str)
                            except ValueError: tx_enum = TxType.REVERSE_ENTRY # افتراضي للعمليات غير المعروفة
                            
                            if client_id != 0 and tx_enum == TxType.COLLECT_DEBT:
                                await self.collect_debt(client_id, amount) 
                                success += 1
                            elif tx_enum == TxType.GIVE_CARDS:
                                error_msg += f"\n⚠️ تم تجاهل (تسليم كروت بـ {amount}) لأن الذكاء الاصطناعي لا يعرف الفئات."
                            # 🌟 منع الذكاء الاصطناعي من إدخال أي عمليات تخص التسديدات لتجنب تسريبها للمدير
                            elif tx_enum in [TxType.TELECOM_PAYMENT, TxType.TELECOM_TOPUP, TxType.ADD_TELECOM_CAPITAL, TxType.AGENT_TELECOM_SALE, TxType.WITHDRAW_TELECOM_PROFIT]:
                                error_msg += f"\n⚠️ تم تجاهل عملية ({tx_enum.value}) لأن عمليات التسديدات لا تُسجل عبر الذكاء الاصطناعي."
                            else:
                                await self._log_tx(conn, client_id, tx_enum, amount, tx.get('details', 'إدخال بالصورة'), wallet_type='manager')
                                success += 1

                    except Exception as e:
                        error_msg += f"\n❌ فشل إدخال عملية: {str(e)}"
                        
        return {"status": "success", "success_count": success, "error_msg": error_msg}

    # 🌟 دالة الإصلاح الآلي للمطابقة (معدلة من الملف الأول)
    async def audit_auto_fix(self, client_id: int, missing_txs: list):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for tx in missing_txs:
                    amount = to_decimal(tx.get('amount', 0))
                    if amount <= 0: continue 
                    
                    details = tx.get('details', 'إصلاح آلي للمطابقة')
                    tx_type_str = tx.get('type')
                    
                    if client_id != 0:
                        if tx_type_str == 'تسديد_من_عميل': 
                            await self.collect_debt(client_id, amount)
                        elif tx_type_str == 'قيد_عكسي':
                            if 'تسديد' in details: await self._update_balances(conn, client_id, amount, 0, amount)
                            elif 'تسليم' in details: await self._update_balances(conn, client_id, -amount, 0, -amount)
                            await self._log_tx(conn, client_id, TxType.REVERSE_ENTRY, amount, details)
                    else:
                        # معالجة عمليات الإدارة
                        await self._log_tx(conn, 0, TxType.REVERSE_ENTRY, amount, details)
        return {"status": "success"}

    # =====================================================================
    # ⬇️ انسخ باقي دوال الكلاس من الملف الثاني هنا ⬇️
    # =====================================================================
    # --- 2. البيع المباشر (Direct Sale) ---
    async def direct_sale(self, card_type: str, qty: int, collected_cash: Decimal, discount: Decimal = Decimal("0.0"), sale_mode: str = "wholesale", key: Optional[str] = None):
        if qty <= 0 or collected_cash < 0 or discount < 0:
            raise InvalidOperationError("الكمية أو المبالغ المدخلة غير صالحة (يجب أن تكون موجبة)." if qty <= 0 else "المبالغ المدخلة غير صالحة (يجب ألا تكون سالبة).")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await LockManager.lock_inventory(conn, [card_type])
                
                # 🌟 الإصلاح الأمني: التأكد من أن الفئة نشطة ولم يتم حذفها وهمياً
                inv = await conn.fetchrow("SELECT quantity, cost_price, price, retail_price FROM inventory WHERE card_type = $1 AND is_active = TRUE FOR UPDATE", card_type)
                if not inv:
                    raise TransactionNotFoundError(f"نوع الكرت {card_type} غير موجود أو تم إيقافه من قبل الإدارة.")
                if inv["quantity"] < qty:
                    raise InsufficientStockError(f"المخزون غير كافٍ من {card_type}. المتوفر: {inv['quantity']}")

                cost_price = to_decimal(inv["cost_price"])
                wholesale_price = to_decimal(inv["price"])
                retail_price = to_decimal(inv["retail_price"])

                target_sell_price = retail_price if sale_mode == "retail" else wholesale_price
                total_price = target_sell_price * qty
                agent_profit = (target_sell_price - cost_price) * qty

                if collected_cash < total_price and discount == Decimal("0.0"):
                    raise SecurityViolationError(f"المبلغ المستلم ({collected_cash}) أقل من السعر المطلوب ({total_price}). يمنع النظام بيع الكروت بأسعار مخفضة بدون خصم صريح.")
                
                final_agent_profit = agent_profit - discount

                await conn.execute("UPDATE inventory SET quantity = quantity - $1 WHERE card_type = $2", qty, card_type)
                
                # 🌟 إغلاق ثغرة التهرب من الخسارة
                if final_agent_profit != 0:
                    profit_desc = "ربح" if final_agent_profit > 0 else "خسارة"
                    await self._record_profit(conn, final_agent_profit, f"{profit_desc} بيع مباشر {qty} كرت {card_type} (خصم {discount})")
                
                sd = [{
                    "card_type": card_type,
                    "quantity": qty,
                    "cost_price": str(cost_price),
                    "sell_price": str(target_sell_price),
                    "discount": str(discount),
                    "collected_cash": str(collected_cash)
                }]
                details = f"بيع {qty} كرت {card_type} كاش ({sale_mode})" + (f" بخصم {discount} ريال" if discount > 0 else "")
                tx_id = await self._log_tx(conn, 0, TxType.DIRECT_SALE, collected_cash, details, sd, key)
                
                return {
                    "status": "success", 
                    "trans_id": tx_id, 
                    "total_price": total_price, 
                    "collected_cash": collected_cash,
                    "agent_profit": final_agent_profit,
                    "network_amount": collected_cash - final_agent_profit,
                    "current_stock": inv["quantity"],
                    "new_stock": inv["quantity"] - qty
                }

    # --- 3. تسليم الكروت (Giving Cards) ---
    async def give_cards(self, client_id: int, card_type: str, qty: int, override: bool = False, key: Optional[str] = None):
        if qty <= 0: raise InvalidOperationError("الكمية يجب أن تكون موجبة")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await LockManager.lock_users(conn, [client_id])
                await LockManager.lock_inventory(conn, [card_type])
                # 🌟 الإصلاح الأمني: التأكد من أن الفئة نشطة
                inv = await conn.fetchrow("SELECT quantity, cost_price, price FROM inventory WHERE card_type = $1 AND is_active = TRUE", card_type)
                if not inv or inv['quantity'] < qty: raise InsufficientStockError(f"مخزون {card_type} غير كافٍ أو الفئة موقوفة")
                user = await conn.fetchrow("SELECT name, debt, credit_limit, phone FROM users WHERE user_id = $1", client_id)
                
                cost, sell = to_decimal(inv['cost_price']), to_decimal(inv['price'])
                total, profit = sell * qty, (sell - cost) * qty
                
                if not override:
                    # 1. فحص سقف المديونية العادي
                    if (to_decimal(user['debt']) + total > to_decimal(user['credit_limit'] or 50000)):
                        # 🌟 [جديد] تسجيل محاولة التجاوز في ملف المراقبة السري
                        logger.warning(f"🚨 محاولة تجاوز سقف المديونية! العميل: {user['name']} (ID: {client_id}). الدين الحالي: {user['debt']}، المطلوب: {total}، السقف: {user['credit_limit']}")
                        raise CreditLimitExceededError(f"تجاوز سقف المديونية للعميل {user['name']}")
                        
                    # 2. 🌟 منع التدوير المالي (Kiting Protection)
                    today_payments = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND type = 'تسديد_من_عميل' AND date >= CURRENT_DATE", client_id)
                    if today_payments and today_payments > 0:
                        max_allowed_today = to_decimal(today_payments) * Decimal('0.5') # يسمح بسحب 50% فقط مما سدده اليوم
                        today_taken = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND type IN ('تسليم_لعميل', 'مبيعات_آجلة') AND date >= CURRENT_DATE", client_id)
                        
                        if (to_decimal(today_taken) + total) > max_allowed_today:
                            # 🌟 [جديد] تسجيل محاولة التدوير المالي في ملف المراقبة السري
                            logger.warning(f"🚨 اشتباه تدوير مالي (Kiting)! العميل: {user['name']} (ID: {client_id}). سدد اليوم: {today_payments}، يحاول سحب: {total}، المسموح: {max_allowed_today}")
                            raise SecurityViolationError(f"🚨 اشتباه تدوير مالي: العميل سدد اليوم ({today_payments} ريال). لضمان الجدية، يُسمح له بسحب بضاعة بحد أقصى ({max_allowed_today} ريال) اليوم.\n(يُمنع تجاوز هذا الحظر أمنياً - كود: ERR#)")

                realized, added_pending = Decimal('0.0'), profit
                curr_debt = to_decimal(user['debt'])
                if curr_debt < 0:
                    adv = abs(curr_debt)
                    if adv >= total: realized, added_pending = profit, Decimal('0.0')
                    else:
                        ratio = (adv / total) if total > 0 else Decimal('0.0')
                        realized = (profit * ratio).quantize(Decimal('0.00'))
                        added_pending = profit - realized
                
                await conn.execute("UPDATE inventory SET quantity = quantity - $1 WHERE card_type = $2", qty, card_type)
                await self._update_balances(conn, client_id, total, added_pending)
                if realized > 0: await self._record_profit(conn, realized, f"ربح محصل مسبقاً (رصيد دائن): {user['name']}")
                await conn.execute("INSERT INTO client_inventory (user_id, card_type, quantity) VALUES ($1, $2, $3) ON CONFLICT (user_id, card_type) DO UPDATE SET quantity = client_inventory.quantity + $3", client_id, card_type, qty)
                
                sd = [{"card_type": card_type, "quantity": qty, "cost_price": str(cost), "sell_price": str(sell)}]
                tx_id = await self._log_tx(conn, client_id, TxType.GIVE_CARDS, total, f"استلم {qty} كرت {card_type}", sd, key)
                return {
                    "status": "success", 
                    "trans_id": tx_id, 
                    "total_price": total,
                    "client_name": user['name'],
                    "client_phone": user.get('phone'),
                    "current_stock": inv['quantity'],
                    "new_stock": inv['quantity'] - qty
                }

    # --- 3. تسليم الكروت بالجملة (Bulk Giving Cards) ---
    async def bulk_give_cards(self, client_id: int, suggested_order: Dict[str, int], override: bool = False, key: Optional[str] = None):
        if not suggested_order or any(qty <= 0 for qty in suggested_order.values()):
            raise InvalidOperationError("يجب أن تكون جميع الكميات المطلوبة أكبر من صفر.")

        total_price = Decimal("0.0")
        total_agent_profit = Decimal("0.0")
        details_list = []
        structured_items = []
        out_of_stock_items = []

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await LockManager.lock_users(conn, [client_id])
                
                sorted_card_types = sorted(suggested_order.keys())
                await LockManager.lock_inventory(conn, sorted_card_types)

                for ctype in sorted_card_types:
                    qty = suggested_order[ctype]
                    # 🌟 الإصلاح الأمني: تجاهل الفئات الموقوفة (المحذوفة وهمياً)
                    inv = await conn.fetchrow("SELECT quantity, cost_price, price FROM inventory WHERE card_type = $1 AND is_active = TRUE FOR UPDATE", ctype)
                    if not inv or inv["quantity"] < qty:
                        out_of_stock_items.append(ctype)
                        continue
                    
                    cost_price = to_decimal(inv["cost_price"])
                    unit_price = to_decimal(inv["price"])
                    
                    total_price += unit_price * qty
                    total_agent_profit += (unit_price - cost_price) * qty
                    details_list.append(f"{qty} كرت {ctype}")
                    structured_items.append({
                        "card_type": ctype, "quantity": qty, 
                        "cost_price": str(cost_price), "sell_price": str(unit_price)
                    })
                
                if out_of_stock_items:
                    raise InsufficientStockError(f"مخزون غير كافٍ للفئات: {', '.join(out_of_stock_items)}")

                user = await conn.fetchrow("SELECT name, phone, debt, credit_limit FROM users WHERE user_id = $1 FOR UPDATE", client_id)
                if not user: raise TransactionNotFoundError("العميل غير موجود")

                current_debt = to_decimal(user["debt"])
                credit_limit = to_decimal(user["credit_limit"] or 50000)

                # 🌟 منع التدوير المالي (Kiting Protection)
                if not override:
                    if (current_debt + total_price > credit_limit):
                        raise CreditLimitExceededError(f"تجاوز سقف المديونية! الحالي: {current_debt}, السقف: {credit_limit}")
                        
                    today_payments = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND type = 'تسديد_من_عميل' AND date >= CURRENT_DATE", client_id)
                    if today_payments and today_payments > 0:
                        max_allowed_today = to_decimal(today_payments) * Decimal('0.5')
                        today_taken = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND type IN ('تسليم_لعميل', 'مبيعات_آجلة') AND date >= CURRENT_DATE", client_id)
                        if (to_decimal(today_taken) + total_price) > max_allowed_today:
                            raise SecurityViolationError(f"🚨 اشتباه تدوير مالي: العميل سدد اليوم ({today_payments} ريال). لضمان الجدية، يُسمح له بسحب بضاعة بحد أقصى ({max_allowed_today} ريال) اليوم.\n(يُمنع تجاوز هذا الحظر أمنياً - كود: ERR#)")

                realized_profit = Decimal("0.0")
                added_pending = total_agent_profit

                if current_debt < 0:
                    advance_payment = abs(current_debt)
                    if advance_payment >= total_price:
                        realized_profit = total_agent_profit
                        added_pending = Decimal("0.0")
                    else:
                        ratio = (advance_payment / total_price) if total_price > 0 else Decimal("0.0")
                        realized_profit = (total_agent_profit * ratio).quantize(Decimal("0.00"))
                        added_pending = total_agent_profit - realized_profit
                
                for ctype in sorted_card_types:
                    qty = suggested_order[ctype]
                    await conn.execute("UPDATE inventory SET quantity = quantity - $1 WHERE card_type = $2", qty, ctype)
                    await conn.execute("INSERT INTO client_inventory (user_id, card_type, quantity) VALUES ($1, $2, $3) ON CONFLICT (user_id, card_type) DO UPDATE SET quantity = client_inventory.quantity + $3", client_id, ctype, qty)
                
                await self._update_balances(conn, client_id, total_price, added_pending)
                if realized_profit > 0:
                    await self._record_profit(conn, realized_profit, f"ربح محصل مسبقاً (رصيد دائن طلب مجمع): {user['name']}")

                # 6. تسجيل العملية
                details_str = " + ".join(details_list)
                tx_id = await self._log_tx(conn, client_id, TxType.GIVE_CARDS, total_price, f"استلم (تلقائي): {details_str}", structured_items, key)
                
                return {
                    "status": "success", 
                    "tx_id": tx_id, 
                    "total_price": total_price, 
                    "client_name": user["name"], 
                    "client_phone": user["phone"],
                    "details_str": details_str
                }

    # --- الفحص الأمني ومستشار الذكاء الاصطناعي (Pre-Give Check) ---
    async def pre_give_cards_check(self, client_id: int):
        async with self.pool.acquire() as conn:
            user = await conn.fetchrow("SELECT name, debt, credit_limit, account_status, suspend_reason FROM users WHERE user_id = $1", client_id)
            
            # 🌟 الحارس الأمني: تنبيه الوكيل أن الحساب مجمد
            if user.get('account_status') == 'suspended':
                return {
                    "status": "blocked",
                    "message": f"🚨 **حساب مجمد من قبل الإدارة!** 🚨\n\nالسبب: {user.get('suspend_reason', 'غير محدد')}\n\nيمنع تسليم كروت لهذا العميل. (يمكنك التجاوز على مسؤوليتك باستخدام الرمز السري)."
                }
            if not user: raise TransactionNotFoundError("العميل غير موجود")
            
            client_inv = await conn.fetchval("SELECT COALESCE(SUM(quantity), 0) FROM client_inventory WHERE user_id = $1", client_id)
            debt = to_decimal(user['debt'])
            
            # =========================================================
            # 🕵️‍♂️ مؤشر الثقة وكشف التهرب (Trust Score & Fraud Detection)
            # =========================================================
            trust_score = 100
            warnings = []
            
            # 1. فحص الديون الميتة (تأخر السداد)
            last_payment = await conn.fetchval("SELECT MAX(date) FROM transactions WHERE user_id = $1 AND type = 'تسديد_من_عميل'", client_id)
            if last_payment and debt > 0:
                days_late = (datetime.now() - last_payment).days
                if days_late > 30:
                    trust_score -= 40
                    warnings.append(f"⚠️ لم يقم بأي سداد منذ {days_late} يوم (دين ميت).")
                elif days_late > 15:
                    trust_score -= 20
                    warnings.append(f"⚠️ تأخر في السداد لمدة {days_late} يوم.")
                    
            # 2. فحص التلاعب في الجرد (مخالفات سابقة)
            adjustments = await conn.fetchval("SELECT COUNT(*) FROM transactions WHERE user_id = $1 AND details LIKE '%تسوية جرد آلي%'", client_id)
            if adjustments and adjustments > 0:
                trust_score -= (adjustments * 15)
                warnings.append(f"🚩 لديه سوابق في التلاعب بالجرد أو إخفاء المبيعات ({adjustments} مخالفات مسجلة).")
                
            # 3. كاشف المبيعات الوهمية (Burn-Rate Anomaly)
            # حساب معدل السحب اليومي في آخر 30 يوم
            sales_30d = await conn.fetchval("SELECT COALESCE(SUM(quantity), 0) FROM client_sales WHERE client_id = $1 AND sale_date >= CURRENT_DATE - INTERVAL '30 days'", client_id)
            if sales_30d and sales_30d > 0:
                avg_daily_sales = sales_30d / 30
                last_sale_date = await conn.fetchval("SELECT MAX(sale_date) FROM client_sales WHERE client_id = $1", client_id)
                if last_sale_date:
                    days_since_last_sale = (datetime.now() - last_sale_date).days
                    expected_sales = avg_daily_sales * days_since_last_sale
                    
                    # إذا كان من المفترض أن يبيع كروت ولكنه يدعي عدم البيع
                    if days_since_last_sale > 3 and expected_sales > 5 and client_inv > 0:
                        trust_score -= 15
                        warnings.append(f"🚨 **اشتباه تهرب:** معدل سحبه المعتاد ({avg_daily_sales:.1f} كرت/يوم). مر {days_since_last_sale} أيام ولم يبلغ عن مبيعات! يُحتمل أنه باع الكروت ويخفي الكاش.")

            # 4. فحص سقف المديونية
            limit = to_decimal(user['credit_limit'] or 50000)
            if debt >= limit * Decimal('0.8'):
                trust_score -= 10
                warnings.append(f"⚠️ ديونه ({debt}) اقتربت جداً من السقف المسموح ({limit}).")

            # تحديد مستوى الخطورة
            trust_score = max(0, trust_score)
            if trust_score >= 80: risk_level = "🟢 موثوق"
            elif trust_score >= 50: risk_level = "🟡 متوسط الخطورة"
            else: risk_level = "🔴 خطير (متهرب)"

            # =========================================================
            # الحارس الأمني: قرار المنع
            # =========================================================
            if debt > 0 or (client_inv and client_inv > 0):
                block_msg = (
                    f"🚨 **رفض أمني من النظام!** 🚨\n\n"
                    f"👤 **العميل:** {user['name']}\n"
                    f"📊 **مؤشر الثقة:** {trust_score}% ({risk_level})\n"
                )
                if warnings:
                    block_msg += "\n📌 **ملاحظات الرقابة الآلية:**\n" + "\n".join(warnings) + "\n"
                    
                block_msg += (
                    f"\n⛔ **القرار:** العميل لديه كروت سابقة ({client_inv}) أو عليه دين ({debt}).\n"
                    f"يُمنع تسليمه كروت جديدة عن بعد. يجب النزول الميداني لإجراء (جرد وتحصيل) أولاً."
                )
                return {
                    "status": "blocked",
                    "message": block_msg
                }
            
            # 2. مستشار الذكاء الاصطناعي (الميزانية والنواقص)
            available_budget = max(Decimal('0.0'), to_decimal(user['credit_limit'] or 50000) - debt)
            
            suggested_order = {}
            if available_budget > 0:
                # جلب مخزون العميل الحالي لمعرفة النواقص
                client_inv_records = await conn.fetch("SELECT card_type, quantity FROM client_inventory WHERE user_id = $1", client_id)
                client_stock = {r['card_type']: r['quantity'] for r in client_inv_records}
                
                # جلب أسعار الكروت ومخزون الوكيل (الورقية فقط)
                agent_inv_records = await conn.fetch("SELECT card_type, quantity, price FROM inventory WHERE is_active = TRUE AND quantity > 0 AND card_type NOT LIKE '%إلكتروني%' AND card_type NOT LIKE '%الكتروني%' ORDER BY price ASC")
                
                # حساب معدل السحب لآخر 30 يوم لمعرفة الفئات المهمة
                txs = await conn.fetch("SELECT details FROM transactions WHERE user_id = $1 AND type = 'مبيعات_بقالة' AND date >= CURRENT_DATE - INTERVAL '30 days'", client_id)
                sales_data = {}
                for tx in txs:
                    match = re.search(r"(\d+)\s*كرت\s*(.+)", tx['details'])
                    if match:
                        qty = int(match.group(1))
                        ctype = match.group(2).strip()
                        sales_data[ctype] = sales_data.get(ctype, 0) + qty
                
                # ترتيب الفئات: الأولوية لما يباع كثيراً ورصيده صفر، ثم الباقي
                priority_list = []
                for r in agent_inv_records:
                    ctype = r['card_type']
                    price = to_decimal(r['price'])
                    c_qty = client_stock.get(ctype, 0)
                    s_qty = sales_data.get(ctype, 0)
                    
                    # نعطي وزناً أعلى للفئة التي تباع كثيراً ومخزونها قليل
                    weight = (s_qty + 1) / (c_qty + 1)
                    priority_list.append({'type': ctype, 'price': price, 'weight': weight, 'agent_qty': r['quantity']})
                    
                priority_list.sort(key=lambda x: x['weight'], reverse=True)
                
                # تعبئة السلة بناءً على الميزانية المتاحة (Knapsack Algorithm)
                current_budget = available_budget
                for item in priority_list:
                    if current_budget < item['price']: continue
                    
                    # كم كرت نحتاج؟ (نحاول تغطية مبيعات 30 يوم + 20% احتياطي)
                    needed_qty = int(sales_data.get(item['type'], 5) * 1.2)
                    if needed_qty == 0: needed_qty = 5 # افتراضي إذا لم يبع سابقاً
                    
                    # نخصم ما لديه أصلاً
                    needed_qty -= client_stock.get(item['type'], 0)
                    if needed_qty <= 0: continue
                    
                    # لا نتجاوز مخزون الوكيل
                    needed_qty = min(needed_qty, item['agent_qty'])
                    
                    # لا نتجاوز الميزانية
                    max_affordable = int(current_budget // item['price'])
                    final_qty = min(needed_qty, max_affordable)
                    
                    if final_qty > 0:
                        suggested_order[item['type']] = final_qty
                        current_budget -= (final_qty * item['price'])
                        
            return {
                "status": "clear",
                "suggested_order": suggested_order,
                "available_budget": str(available_budget)
            }

    # --- 4. مرتجع من عميل (Return From Client) ---
    async def return_from_client(self, client_id: int, card_type: str, qty: int, key: Optional[str] = None):
        if qty <= 0: raise InvalidOperationError("الكمية يجب أن تكون موجبة")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. القفل الموحد
                await LockManager.lock_users(conn, [client_id])
                await LockManager.lock_inventory(conn, [card_type])
                await LockManager.lock_client_inventory(conn, client_id, [card_type])

                # 2. جلب البيانات والتحقق
                client_inv = await conn.fetchrow("SELECT quantity FROM client_inventory WHERE user_id = $1 AND card_type = $2 FOR UPDATE", client_id, card_type)
                if not client_inv or client_inv["quantity"] < qty:
                    raise InsufficientStockError(f"العميل لا يملك {qty} كرت من نوع {card_type} في عهدته.")
                
                inv = await conn.fetchrow("SELECT cost_price, price FROM inventory WHERE card_type = $1 FOR UPDATE", card_type)
                if not inv: raise TransactionNotFoundError(f"نوع الكرت {card_type} غير موجود.")

                user = await conn.fetchrow("SELECT name, debt, pending_profit FROM users WHERE user_id = $1", client_id)
                if not user: raise TransactionNotFoundError("العميل غير موجود")

                cost_price = to_decimal(inv["cost_price"])
                sell_price = to_decimal(inv["price"])
                total_value = sell_price * qty
                total_profit = (sell_price - cost_price) * qty

                # 3. تحديث المخزون وأرصدة العميل
                await conn.execute("UPDATE inventory SET quantity = quantity + $1 WHERE card_type = $2", qty, card_type)
                await conn.execute("UPDATE client_inventory SET quantity = quantity - $1 WHERE user_id = $2 AND card_type = $3", qty, client_id, card_type)
                
                # 🌟 إغلاق ثغرة سرقة الأرباح المحصلة
                curr_debt = to_decimal(user['debt'])
                if curr_debt < 0:
                    advance_payment = abs(curr_debt)
                    if advance_payment >= total_value:
                        realized_profit_to_reverse = total_profit
                        pending_to_reverse = Decimal('0.0')
                    else:
                        ratio = (advance_payment / total_value) if total_value > 0 else Decimal('0.0')
                        realized_profit_to_reverse = (total_profit * ratio).quantize(Decimal('0.00'))
                        pending_to_reverse = total_profit - realized_profit_to_reverse
                else:
                    realized_profit_to_reverse = Decimal('0.0')
                    pending_to_reverse = total_profit

                await self._update_balances(conn, client_id, -total_value, -pending_to_reverse, -total_value)
                if realized_profit_to_reverse > 0:
                    await self._record_profit(conn, -realized_profit_to_reverse, f"خصم ربح محصل مسبقاً (مرتجع من عميل): {user['name']}")

                # 4. تسجيل العملية
                sd = [{
                    "card_type": card_type,
                    "quantity": qty,
                    "cost_price": str(cost_price),
                    "sell_price": str(sell_price)
                }]
                tx_id = await self._log_tx(conn, client_id, TxType.RETURN_FROM_CLIENT, -total_value, f"مرتجع من العميل: {qty} كرت {card_type}", sd, key)
                
                return {"status": "success", "tx_id": tx_id, "total_value": total_value}

    # --- 5. استلام من الشبكة (Receive From Network) ---
    async def receive_from_network(self, card_type: str, qty: int, cost_price: Decimal, key: Optional[str] = None):
        if qty <= 0 or cost_price <= 0: raise InvalidOperationError("الكمية أو سعر التكلفة يجب أن يكونا موجبين")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await LockManager.lock_inventory(conn, [card_type])
                
                # 🌟 معالجة تغير الأسعار (متوسط التكلفة المرجح - WAC)
                inv = await conn.fetchrow("SELECT quantity, cost_price FROM inventory WHERE card_type = $1", card_type)
                
                if inv and inv['quantity'] > 0:
                    current_qty = inv['quantity']
                    current_cost = to_decimal(inv['cost_price'])
                    
                    # معادلة WAC: (الكمية القديمة * السعر القديم + الكمية الجديدة * السعر الجديد) / إجمالي الكمية
                    total_value_old = current_qty * current_cost
                    total_value_new = qty * cost_price
                    new_avg_cost = (total_value_old + total_value_new) / (current_qty + qty)
                    new_avg_cost = new_avg_cost.quantize(Decimal('0.00'))
                else:
                    new_avg_cost = cost_price
                
                # تحديث المخزون بمتوسط التكلفة الجديد
                await conn.execute("""
                    INSERT INTO inventory (card_type, quantity, cost_price, price, retail_price) 
                    VALUES ($1, $2, $3, $4, $5) 
                    ON CONFLICT (card_type) DO UPDATE 
                    SET quantity = inventory.quantity + $2, 
                        cost_price = $3
                """, card_type, qty, new_avg_cost, new_avg_cost * Decimal("1.05"), new_avg_cost * Decimal("1.10"))
                
                # تسجيل العملية
                sd = [{
                    "card_type": card_type,
                    "quantity": qty,
                    "cost_price": str(new_avg_cost)
                }]
                tx_id = await self._log_tx(conn, 0, TxType.RECEIVE_FROM_NETWORK, cost_price * qty, f"استلام {qty} كرت {card_type} من الشبكة", sd, key)
                
                return {"status": "success", "tx_id": tx_id}
                
    # --- 6. مرتجع للشبكة (Return To Network) ---
    async def return_to_network(self, card_type: str, qty: int, key: Optional[str] = None):
        if qty <= 0: raise InvalidOperationError("الكمية يجب أن تكون موجبة")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await LockManager.lock_inventory(conn, [card_type])
                
                inv = await conn.fetchrow("SELECT quantity, cost_price FROM inventory WHERE card_type = $1 FOR UPDATE", card_type)
                if not inv or inv["quantity"] < qty:
                    raise InsufficientStockError(f"المخزون غير كافٍ لمرتجع {card_type}. المتوفر: {inv['quantity'] if inv else 0}")

                cost_price = to_decimal(inv["cost_price"])
                total_value = cost_price * qty

                # تحديث المخزون
                await conn.execute("UPDATE inventory SET quantity = quantity - $1 WHERE card_type = $2", qty, card_type)
                
                # تسجيل العملية (قيد عكسي للمخزون)
                sd = [{
                    "card_type": card_type,
                    "quantity": qty,
                    "cost_price": str(cost_price)
                }]
                tx_id = await self._log_tx(conn, 0, TxType.RETURN_TO_NETWORK, -total_value, f"مرتجع {qty} كرت {card_type} للشبكة", sd, key)
                
                return {"status": "success", "tx_id": tx_id}

    # --- 7. تحويل الأصول (Transfer Assets) ---
    async def transfer_assets(self, sender_type: str, sender_id: int, receiver_type: str, receiver_id: int,
                             asset_type: str, value: Union[Decimal, int], card_type: Optional[str] = None, key: Optional[str] = None):
        if value <= 0: raise InvalidOperationError("قيمة التحويل يجب أن تكون موجبة")
        if asset_type not in ["cash", "cards"]: raise InvalidOperationError("نوع الأصل غير مدعوم")
        if asset_type == "cards" and not card_type: raise InvalidOperationError("يجب تحديد نوع الكرت عند تحويل الكروت")

        # 🌟 الإصلاح الأسطوري: تقسيم مفتاح الحماية إلى مفتاحين (صادر ووارد) لمنع الحذف الشبحي
        key_out = f"{key}_out" if key else None
        key_in = f"{key}_in" if key else None

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. القفل الموحد (Locking)
                user_ids_to_lock = []
                if sender_type == "client": user_ids_to_lock.append(sender_id)
                if receiver_type == "client": user_ids_to_lock.append(receiver_id)
                await LockManager.lock_users(conn, user_ids_to_lock)
                
                if asset_type == "cards":
                    await LockManager.lock_inventory(conn, [card_type])
                    if sender_type == "client": await LockManager.lock_client_inventory(conn, sender_id, [card_type])
                    if receiver_type == "client": await LockManager.lock_client_inventory(conn, receiver_id, [card_type])

                details = f"تحويل {value} {asset_type} من {sender_type}:{sender_id} إلى {receiver_type}:{receiver_id}"
                
                if asset_type == "cash":
                    # ================= تحويل الكاش (نقل مديونية) =================
                    extracted_pending = Decimal("0.0")
                    extracted_old = Decimal("0.0")
                    
                    if sender_type == "client":
                        u_sender = await conn.fetchrow("SELECT debt, old_debt, pending_profit FROM users WHERE user_id = $1", sender_id)
                        debt, old, pending = to_decimal(u_sender["debt"]), to_decimal(u_sender["old_debt"]), to_decimal(u_sender["pending_profit"])
                        
                        if value <= old:
                            extracted_old = value
                        else:
                            extracted_old = old
                            rem = value - old
                            new_part = debt - old
                            if new_part > 0:
                                ratio = min(Decimal("1.0"), rem / new_part)
                                extracted_pending = (pending * ratio).quantize(Decimal("0.00"))
                            else:
                                extracted_pending = pending
                                
                        await self._update_balances(conn, sender_id, -value, -extracted_pending, -extracted_old)
                        await self._log_tx(conn, sender_id, TxType.TRANSFER_BALANCE, -value, f"تحويل صادر (نقل دين): {details}", None, key_out)
                    elif sender_type == "agent":
                        # 🌟 التصحيح: "تسوية صادرة" لكي يخصم الـ Trigger الكاش من الصندوق
                        await self._log_tx(conn, 0, TxType.TRANSFER_BALANCE, -value, f"تسوية صادرة من الصندوق: {details}", None, key_out)

                    if receiver_type == "client":
                        add_pending = extracted_pending if sender_type == "client" else Decimal("0.0")
                        add_old = extracted_old if sender_type == "client" else Decimal("0.0")
                        
                        await self._update_balances(conn, receiver_id, value, add_pending, add_old)
                        await self._log_tx(conn, receiver_id, TxType.TRANSFER_BALANCE, value, f"تحويل وارد (استلام دين): {details}", None, key_in)
                    elif receiver_type == "agent":
                        # 🌟 التصحيح: "تسوية واردة" لكي يضيف الـ Trigger الكاش للصندوق
                        await self._log_tx(conn, 0, TxType.TRANSFER_BALANCE, value, f"تسوية واردة للصندوق: {details}", None, key_in)

                elif asset_type == "cards":
                    # ================= تحويل الكروت (نقل عهدة) =================
                    qty = int(value)
                    inv = await conn.fetchrow("SELECT cost_price, price FROM inventory WHERE card_type = $1", card_type)
                    cost_price = to_decimal(inv['cost_price'])
                    wholesale_price = to_decimal(inv['price'])
                    total_value = wholesale_price * qty
                    
                    actual_profit_to_move = Decimal('0.0')
                    sd = [{"card_type": card_type, "quantity": qty, "cost_price": str(cost_price), "sell_price": str(wholesale_price)}]

                    if sender_type == "client":
                        client_inv = await conn.fetchrow("SELECT quantity FROM client_inventory WHERE user_id = $1 AND card_type = $2", sender_id, card_type)
                        if not client_inv or client_inv["quantity"] < qty: raise InsufficientStockError(f"العميل لا يملك {qty} كرت {card_type}")
                        
                        u_sender = await conn.fetchrow("SELECT pending_profit FROM users WHERE user_id = $1", sender_id)
                        profit_margin = (wholesale_price - cost_price) * qty
                        actual_profit_to_move = min(to_decimal(u_sender['pending_profit']), profit_margin)
                        
                        await conn.execute("UPDATE client_inventory SET quantity = quantity - $1 WHERE user_id = $2 AND card_type = $3", qty, sender_id, card_type)
                        await self._update_balances(conn, sender_id, -total_value, -actual_profit_to_move, Decimal('0.0'))
                        await self._log_tx(conn, sender_id, TxType.TRANSFER_BALANCE, -total_value, f"نقل كروت صادر: {details}", sd, key_out)
                        
                    elif sender_type == "agent":
                        agent_inv = await conn.fetchrow("SELECT quantity FROM inventory WHERE card_type = $1", card_type)
                        if not agent_inv or agent_inv["quantity"] < qty: raise InsufficientStockError(f"الوكيل لا يملك {qty} كرت {card_type}")
                        await conn.execute("UPDATE inventory SET quantity = quantity - $1 WHERE card_type = $2", qty, card_type)
                        actual_profit_to_move = (wholesale_price - cost_price) * qty

                    if receiver_type == "client":
                        # 🌟 إصلاح الأرباح المعلقة: المستلم يجب أن يحصل على هامش الربح كاملاً ليتطابق مع مخزونه
                        full_profit_margin = (wholesale_price - cost_price) * qty
                        
                        # إذا كان المرسل عميلاً وسحب جزءاً من أرباحه مسبقاً، نسجل الفارق كدين عليه لضبط المعادلة
                        if sender_type == "client" and actual_profit_to_move < full_profit_margin:
                            profit_difference = full_profit_margin - actual_profit_to_move
                            await self._update_balances(conn, sender_id, profit_difference, Decimal('0.0'), Decimal('0.0'))
                            await self._record_profit(conn, -profit_difference, f"تسوية أرباح مسحوبة مسبقاً (تحويل صادر): {details}")
                            
                        await conn.execute("INSERT INTO client_inventory (user_id, card_type, quantity) VALUES ($1, $2, $3) ON CONFLICT (user_id, card_type) DO UPDATE SET quantity = client_inventory.quantity + $3", receiver_id, card_type, qty)
                        await self._update_balances(conn, receiver_id, total_value, full_profit_margin, Decimal('0.0'))
                        await self._log_tx(conn, receiver_id, TxType.TRANSFER_BALANCE, total_value, f"نقل كروت وارد: {details}", sd, key_in)
                        
                    elif receiver_type == "agent":
                        await conn.execute("UPDATE inventory SET quantity = quantity + $1 WHERE card_type = $2", qty, card_type)
                        
                    elif receiver_type == "manager":
                        # 🌟 سد الثقب الأسود: إذا تم التحويل للمدير، يتم إرجاعها كمرتجع للشبكة
                        await self._log_tx(conn, 0, TxType.RETURN_TO_NETWORK, -total_value, f"تحويل كروت للمدير: {details}", sd, key_in)

                return {"status": "success", "message": "تم التحويل بنجاح"}

    # --- 8. تحصيل الديون ونظام العجز (Debt Collection & Shortage System) ---
    async def collect_debt(self, client_id: int, amount: Decimal, bypass_shortage: bool = False, key: Optional[str] = None):
        if amount <= 0: raise InvalidOperationError("المبلغ يجب أن يكون موجباً")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await LockManager.lock_users(conn, [client_id])
                # 🌟 التعديل: جلب السقف ونسبة الجدولة
                u = await conn.fetchrow("SELECT name, phone, debt, old_debt, pending_profit, pos_cash_collected, credit_limit, debt_schedule_pct FROM users WHERE user_id = $1", client_id)
                if not u: raise TransactionNotFoundError("العميل غير موجود")
                
                debt, old, pending = to_decimal(u["debt"]), to_decimal(u["old_debt"]), to_decimal(u["pending_profit"])
                expected_cash = to_decimal(u["pos_cash_collected"])
                
                # 🚨 نظام عجز العهدة: رفض المبلغ إذا كان أقل من المبيعات ولم يتم استخدام التجاوز
                if amount < expected_cash and not bypass_shortage:
                    return {
                        "status": "shortage_error", 
                        "expected": expected_cash, 
                        "paid": amount,
                        "shortage": expected_cash - amount,
                        "message": f"⚠️ عجز في العهدة! المطلوب {expected_cash}، المدفوع {amount}. (يُمنع التجاوز - كود: ERR#)"
                    }
                
                # 💸 استخراج الأرباح من الدين القديم والجديد
                extracted, old_delta = Decimal("0.0"), Decimal("0.0")
                if amount <= old: old_delta = -amount
                else:
                    old_delta = -old
                    rem = amount - old
                    new_part = debt - old
                    if new_part > 0:
                        ratio = min(Decimal("1.0"), rem / new_part)
                        extracted = (pending * ratio).quantize(Decimal("0.00"))
                    else: extracted = pending
                
                await self._update_balances(conn, client_id, -amount, -extracted, old_delta)
                
                # 🌟 الخوارزمية الذكية: تقليص السقف آلياً بناءً على نسبة الجدولة
                schedule_pct = to_decimal(u.get("debt_schedule_pct", 0))
                limit_reduction = Decimal('0.0')
                if schedule_pct > 0:
                    limit_reduction = (amount * (schedule_pct / Decimal('100'))).quantize(Decimal('0.00'))
                
                # تصفير أو تقليل الكاش الجاهز والوعود بالسداد + تقليص السقف
                await conn.execute("""
                    UPDATE users 
                    SET promised_payment = GREATEST(0, COALESCE(promised_payment, 0) - $1), 
                        pos_cash_collected = GREATEST(0, COALESCE(pos_cash_collected, 0) - $1),
                        credit_limit = GREATEST(0, COALESCE(credit_limit, 50000) - $2)
                    WHERE user_id = $3
                """, amount, limit_reduction, client_id)
                
                if extracted > 0: await self._record_profit(conn, extracted, f"ربح محصل من سداد: {u['name']}")
                
                sd = [{"extracted_profit": str(extracted), "old_debt_paid": str(abs(old_delta)), "bypassed_shortage": bypass_shortage}]
                tx_id = await self._log_tx(conn, client_id, TxType.COLLECT_DEBT, amount, f"تحصيل مبلغ {amount} ريال", sd, key)
                
                return {
                    "status": "success", 
                    "trans_id": tx_id,
                    "client_name": u['name'],
                    "client_phone": u['phone'],
                    "new_debt": debt - amount
                }

    # --- 9. تسجيل مبيعات العميل (Record Client Sale) ---
    async def record_client_sale(self, client_id: int, card_type: str, expected_qty: int, actual_remaining_qty: int, key: Optional[str] = None):
        if actual_remaining_qty < 0: raise InvalidOperationError("الكمية المتبقية لا يمكن أن تكون سالبة.")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await LockManager.lock_users(conn, [client_id])
                await LockManager.lock_inventory(conn, [card_type]) 
                await LockManager.lock_client_inventory(conn, client_id, [card_type])

                prices = await conn.fetchrow("SELECT cost_price, price, retail_price FROM inventory WHERE card_type = $1", card_type)
                if not prices: raise TransactionNotFoundError(f"نوع الكرت {card_type} غير موجود.")

                cost_price = to_decimal(prices["cost_price"])
                wholesale_price = to_decimal(prices["price"])
                retail_price = to_decimal(prices["retail_price"])

                adjustment_msg = ""
                adj_tx_id = None
                sold_qty = 0

                if actual_remaining_qty > expected_qty:
                    excess_qty = actual_remaining_qty - expected_qty
                    
                    if excess_qty > 5:
                        raise SecurityViolationError(f"🚨 زيادة غير منطقية ({excess_qty} كرت). يرجى مراجعة العميل يدوياً وإجراء تسوية شاملة.")
                        
                    agent_inv = await conn.fetchval("SELECT quantity FROM inventory WHERE card_type = $1", card_type)
                    if agent_inv is None or agent_inv < excess_qty:
                        raise InsufficientStockError(f"لا يوجد مخزون كافٍ في صندوقك لتسوية الزيادة ({excess_qty} كرت {card_type}).")
                        
                    await conn.execute("UPDATE inventory SET quantity = quantity - $1 WHERE card_type = $2", excess_qty, card_type)
                    
                    excess_value = excess_qty * wholesale_price
                    excess_profit = (wholesale_price - cost_price) * excess_qty
                    await self._update_balances(conn, client_id, excess_value, excess_profit)
                    
                    sd_adj = [{"card_type": card_type, "quantity": excess_qty, "cost_price": str(cost_price), "sell_price": str(wholesale_price)}]
                    # 🌟 التعديل: التقاط رقم العملية (ID) لكي نستخدمه في زر التراجع
                    adj_tx_id = await self._log_tx(conn, client_id, TxType.GIVE_CARDS, excess_value, f"تسوية جرد آلي: إضافة {excess_qty} كرت {card_type} (إرسالية منسية)", sd_adj, key)
                    
                    adjustment_msg = f"⚠️ تم اكتشاف {excess_qty} كرت زائدة من فئة ({card_type}). تم تسويتها آلياً وخصمها من صندوقك وإضافتها لمديونية العميل."
                else:
                    sold_qty = expected_qty - actual_remaining_qty

                item_due = sold_qty * wholesale_price
                client_profit = (retail_price - wholesale_price) * sold_qty

                if actual_remaining_qty == 0:
                    await conn.execute("DELETE FROM client_inventory WHERE user_id = $1 AND card_type = $2", client_id, card_type)
                else:
                    await conn.execute("UPDATE client_inventory SET quantity = $1 WHERE user_id = $2 AND card_type = $3", actual_remaining_qty, client_id, card_type)

                if sold_qty > 0:
                    sd = [{"card_type": card_type, "sold_quantity": sold_qty, "remaining_quantity": actual_remaining_qty, "wholesale_price": str(wholesale_price), "retail_price": str(retail_price), "client_profit": str(client_profit)}]
                    await self._log_tx(conn, client_id, TxType.CLIENT_SALE, item_due, f"مبيعات بقالة: {sold_qty} كرت {card_type}", sd, key)
                    
                    # 🌟 التعديل المحاسبي المحكم: حماية الأرصدة الدائنة والمقاصة الذكية
                    await conn.execute("""
                        UPDATE users 
                        SET pos_cash_collected = GREATEST(0, LEAST(GREATEST(0, debt), COALESCE(pos_cash_collected, 0) + $1))
                        WHERE user_id = $2
                    """, item_due, client_id)

                    try:
                        await conn.execute("INSERT INTO client_sales (client_id, card_type, quantity, total_price, profit, sale_date) VALUES ($1, $2, $3, $4, $5, CURRENT_TIMESTAMP)", client_id, card_type, sold_qty, item_due, client_profit)
                    except Exception as e:
                        logger.warning(f"فشل تسجيل مبيعات العميل: {e}")

                return {
                    "status": "success", 
                    "item_due": item_due,
                    "sold_qty": sold_qty,
                    "adjustment_msg": adjustment_msg,
                    "adj_tx_id": adj_tx_id # 🌟 إرجاع رقم العملية
                }

    # --- محرك التراجع الشامل (Revert Transaction) ---
    async def revert_transaction(self, tx_id: int, key: Optional[str] = None):
        async with self.pool.acquire() as conn:
            async with conn.transaction(): # 🌟 فتح المعاملة أولاً لحماية العملية بالكامل
                
                # 🌟 جلب العملية مع قفل FOR UPDATE لمنع التراجع المزدوج (Race Condition)
                tx = await conn.fetchrow("SELECT * FROM transactions WHERE id = $1 FOR UPDATE", tx_id)
                
                if not tx or tx['is_reverted']: 
                    raise InvalidOperationError("العملية غير موجودة أو تم التراجع عنها مسبقاً")
                
                # 🌟 جلب نوع المحفظة لكي لا يتسرب التراجع لتقرير المدير
                t_type, t_user, t_amount, t_details, t_date, t_wallet = tx['type'], tx['user_id'], to_decimal(tx['amount']), tx['details'], tx['date'], tx.get('wallet_type', 'manager')
                
                # منع التراجع عن العمليات الحساسة أو العكسية أو عمليات تصفية الحساب المركبة
                if t_type in ['تسديد_باقة', 'تسديد_باقة_وكيل', 'استرداد_تسديد', 'شحن_رصيد_بوابة', 'مبيعات_آجلة', 'تحويل_رصيد', 'مبيعات_بقالة', 'مبيعات_كاش'] or 'إلكتروني' in t_details or 'الكتروني' in t_details or 'تراجع عن' in t_details or 'تصفية حساب' in t_details or 'تسوية جرد آلي' in t_details or t_amount < 0:
                    raise SecurityViolationError("⛔ رفض أمني: لا يمكن التراجع عن هذه العمليات، أو تسويات الجرد الآلي، أو العمليات العكسية.")

                # الحماية الفولاذية لمعالجة البيانات المهيكلة (تمنع انهيار الـ JSON تماماً)
                raw_sd = tx.get('structured_details')
                if not raw_sd:
                    t_sd = []
                elif isinstance(raw_sd, str):
                    try: t_sd = json.loads(raw_sd)
                    except: t_sd = []
                elif isinstance(raw_sd, list):
                    t_sd = raw_sd
                else:
                    t_sd = []
                
                # جلب ملخص الكاش والأرباح للتحقق
                fin_stats = await self.get_summary(conn)
                available_cash = fin_stats['cash']
                realized_profit = fin_stats['realized']

                # القفل الموحد
                await LockManager.lock_users(conn, [t_user] if t_user != 0 else [])
                if t_sd:
                    await LockManager.lock_inventory(conn, [i.get('card_type') for i in t_sd if 'card_type' in i])

                # ================= 1. تسديد من عميل =================
                if t_type == TxType.COLLECT_DEBT.value:
                    if t_amount > available_cash:
                        raise InsufficientFundsError(f"⛔ رفض قاطع: الكاش المتوفر في الصندوق ({available_cash} ريال) لا يكفي لإلغاء هذا التحصيل. يبدو أنك قمت بصرف المبلغ.")
                    
                    # 🌟 الإصلاح المحاسبي: استخراج الأرباح والدين القديم من الـ JSON بدقة
                    extracted = to_decimal(t_sd[0].get('extracted_profit', 0)) if t_sd else Decimal('0.0')
                    old_restored = to_decimal(t_sd[0].get('old_debt_paid', t_amount)) if t_sd else t_amount
                    
                    # إرجاع الدين، الأرباح المعلقة، والدين القديم للعميل
                    await self._update_balances(conn, t_user, t_amount, extracted, old_restored)
                    
                    # 🌟 الإصلاح الأمني: إزالة try/except لضمان التراجع الكامل (Rollback) عند حدوث أي خطأ
                    await conn.execute("""
                        UPDATE users 
                        SET pos_cash_collected = GREATEST(0, LEAST(GREATEST(0, debt), COALESCE(pos_cash_collected, 0) + $1))
                        WHERE user_id = $2
                    """, t_amount, t_user)
                    
                    # خصم الربح الذي دخل محفظتك بالخطأ
                    if extracted > 0: 
                        await self._record_profit(conn, -extracted, f"خصم ربح (تراجع تحصيل #{tx_id})")

                # ================= 2. تسليم لعميل =================
                elif t_type == TxType.GIVE_CARDS.value:
                    items_to_revert = t_sd
                    if not items_to_revert: # دعم رجعي للعمليات القديمة
                        matches = re.findall(r"(\d+)\s*كرت\s*([^\+\(]+)", t_details)
                        for match in matches:
                            qty, ctype = int(match[0]), match[1].replace("كاش", "").strip()
                            prices = await conn.fetchrow("SELECT cost_price, price FROM inventory WHERE card_type = $1", ctype)
                            # 🌟 الإصلاح المحاسبي: استخدام str() بدلاً من float()
                            if prices: items_to_revert.append({"card_type": ctype, "quantity": qty, "cost_price": str(prices['cost_price'] or 0), "sell_price": str(prices['price'] or 0)})

                    for item in items_to_revert:
                        client_qty = await conn.fetchval("SELECT quantity FROM client_inventory WHERE user_id = $1 AND card_type = $2", t_user, item['card_type'])
                        if not client_qty or client_qty < item['quantity']:
                            raise InsufficientStockError(f"⛔ رفض قاطع: العميل قام ببيع جزء من كروت ({item['card_type']}). لا يمكن التراجع عن الإرسالية بالكامل. يرجى استخدام قسم (المرتجعات) لتسوية العدد المتبقي.")
                            
                    total_profit_to_reverse = Decimal('0.0')
                    for item in items_to_revert:
                        qty, ctype = item['quantity'], item['card_type']
                        await conn.execute("UPDATE inventory SET quantity = quantity + $1 WHERE card_type = $2", qty, ctype)
                        await conn.execute("UPDATE client_inventory SET quantity = quantity - $1 WHERE user_id = $2 AND card_type = $3", qty, t_user, ctype)
                        profit = (to_decimal(item['sell_price']) - to_decimal(item['cost_price'])) * qty
                        total_profit_to_reverse += profit
                            
                    u_data = await conn.fetchrow("SELECT pending_profit FROM users WHERE user_id = $1 FOR UPDATE", t_user)
                    current_pending = to_decimal(u_data['pending_profit']) if u_data else Decimal('0.0')
                    deduct_from_pending = min(current_pending, total_profit_to_reverse)
                    deduct_from_real = total_profit_to_reverse - deduct_from_pending
                    
                    # 🌟 الإصلاح المحاسبي: خصم الدين، والأرباح المعلقة، والدين القديم معاً
                    await self._update_balances(conn, t_user, -t_amount, -deduct_from_pending, -t_amount)
                    
                    if deduct_from_real > 0:
                        if deduct_from_real > realized_profit: 
                            raise InsufficientFundsError("⛔ رفض قاطع: لقد قمت بسحب أرباح هذه العملية مسبقاً من الصندوق. لا يمكن التراجع لكي لا يصبح رصيد أرباحك بالسالب.")
                        await self._record_profit(conn, -deduct_from_real, f"إلغاء ربح (تراجع عن عملية #{tx_id})")

                # ================= 3. بيع مباشر =================
                elif t_type == 'بيع_مباشر':
                    if t_amount > available_cash: 
                        raise InsufficientFundsError(f"⛔ رفض قاطع: الكاش المتوفر ({available_cash} ريال) لا يكفي لإلغاء هذه البيعة.")
                        
                    items_to_revert = t_sd
                    if not items_to_revert:
                        matches = re.findall(r"(\d+)\s*كرت\s*([^\+\(]+)", t_details)
                        for match in matches:
                            qty, ctype = int(match[0]), match[1].replace("كاش", "").strip()
                            prices = await conn.fetchrow("SELECT cost_price, price, retail_price FROM inventory WHERE card_type = $1", ctype)
                            if prices:
                                sell_p = prices['price'] if "جملة" in t_details else prices['retail_price']
                                # 🌟 الإصلاح المحاسبي: استخدام str() بدلاً من float()
                                items_to_revert.append({"card_type": ctype, "quantity": qty, "cost_price": str(prices['cost_price'] or 0), "sell_price": str(sell_p)})
                                
                    total_profit_to_reverse = Decimal('0.0')
                    for item in items_to_revert:
                        qty, ctype = item['quantity'], item['card_type']
                        await conn.execute("UPDATE inventory SET quantity = quantity + $1 WHERE card_type = $2", qty, ctype)
                        profit = (to_decimal(item['sell_price']) - to_decimal(item['cost_price'])) * qty
                        total_profit_to_reverse += profit
                        
                    # 🌟 الإصلاح: استخراج الخصم من البيانات المهيكلة بدقة 100% بدلاً من الاعتماد على النصوص
                    discount = Decimal('0.0')
                    if t_sd and 'discount' in t_sd[0]:
                        discount = to_decimal(t_sd[0]['discount'])
                    else:
                        # دعم رجعي للعمليات القديمة جداً
                        discount_match = re.search(r"بخصم (\d+(?:\.\d+)?) ريال", t_details)
                        if discount_match: discount = Decimal(discount_match.group(1))
                        
                    total_profit_to_reverse -= discount
                        
                    if total_profit_to_reverse != 0:
                        if total_profit_to_reverse > realized_profit: 
                            raise InsufficientFundsError("⛔ رفض قاطع: لقد قمت بسحب أرباح هذه البيعة مسبقاً. لا يمكن التراجع لكي لا يصبح رصيد أرباحك بالسالب.")
                        profit_desc = "إلغاء ربح" if total_profit_to_reverse > 0 else "استرداد خسارة"
                        await self._record_profit(conn, -total_profit_to_reverse, f"{profit_desc} (تراجع عن عملية #{tx_id})")

                # ================= 4. مرتجع من عميل =================
                elif t_type == 'مرتجع_من_عميل':
                    items_to_restore = t_sd
                    if not items_to_restore:
                        matches = re.findall(r"(\d+)\s*كرت\s*([^\+\(]+)", t_details)
                        for match in matches:
                            qty, ctype = int(match[0]), match[1].replace("كاش", "").strip()
                            prices = await conn.fetchrow("SELECT cost_price, price FROM inventory WHERE card_type = $1", ctype)
                            # 🌟 الإصلاح المحاسبي: استخدام str() بدلاً من float()
                            if prices: items_to_restore.append({"card_type": ctype, "quantity": qty, "cost_price": str(prices['cost_price'] or 0), "sell_price": str(prices['price'] or 0)})
                                
                    for item in items_to_restore:
                        agent_qty = await conn.fetchval("SELECT quantity FROM inventory WHERE card_type = $1", item['card_type'])
                        if not agent_qty or agent_qty < item['quantity']: 
                            raise InsufficientStockError(f"⛔ رفض قاطع: لقد قمت ببيع الكروت المرتجعة ({item['card_type']}) لعميل آخر. لا يمكن التراجع عن هذا المرتجع.")
                            
                    u_data = await conn.fetchrow("SELECT debt FROM users WHERE user_id = $1 FOR UPDATE", t_user)
                    current_debt = to_decimal(u_data['debt'])
                    
                    total_profit_to_restore = Decimal('0.0')
                    for item in items_to_restore:
                        qty, ctype = item['quantity'], item['card_type']
                        await conn.execute("UPDATE inventory SET quantity = quantity - $1 WHERE card_type = $2", qty, ctype)
                        await conn.execute("UPDATE client_inventory SET quantity = quantity + $1 WHERE user_id = $2 AND card_type = $3", qty, t_user, ctype)
                        total_profit_to_restore += (to_decimal(item['sell_price']) - to_decimal(item['cost_price'])) * qty
                            
                    if current_debt < 0:
                        advance_payment = abs(current_debt)
                        if advance_payment >= t_amount:
                            realized_profit_restored = total_profit_to_restore
                            added_pending = Decimal('0.0')
                        else:
                            ratio = (advance_payment / t_amount) if t_amount > 0 else Decimal('0.0')
                            realized_profit_restored = total_profit_to_restore * ratio
                            added_pending = total_profit_to_restore - realized_profit_restored
                    else:
                        realized_profit_restored = Decimal('0.0')
                        added_pending = total_profit_to_restore

                    # 🌟 إصلاح الدين القديم عند التراجع عن المرتجع
                    await conn.execute("UPDATE users SET debt = debt + $1, old_debt = old_debt + $1, pending_profit = pending_profit + $2 WHERE user_id = $3", t_amount, added_pending, t_user)
                    if realized_profit_restored > 0: await self._record_profit(conn, realized_profit_restored, f"إعادة ربح محصل (تراجع عن مرتجع #{tx_id})")

                # ================= 5. استلام من الشبكة =================
                elif t_type == 'استلام_من_الشبكة':
                    items_to_revert = t_sd
                    if not items_to_revert:
                        matches = re.findall(r"(\d+)\s*كرت\s*([^\+\(]+)", t_details)
                        for match in matches:
                            qty, ctype = int(match[0]), match[1].replace("كاش", "").strip()
                            items_to_revert.append({"card_type": ctype, "quantity": qty})
                            
                    for item in items_to_revert:
                        agent_qty = await conn.fetchval("SELECT quantity FROM inventory WHERE card_type = $1", item['card_type'])
                        if not agent_qty or agent_qty < item['quantity']: 
                            raise InsufficientStockError(f"⛔ رفض قاطع: لقد قمت ببيع جزء من إرسالية ({item['card_type']}). لا يمكن التراجع عن استلامها.")
                    
                    for item in items_to_revert:
                        await conn.execute("UPDATE inventory SET quantity = quantity - $1 WHERE card_type = $2", item['quantity'], item['card_type'])

                # ================= 6. نسبة الوكيل =================
                elif t_type == 'نسبة_الوكيل':
                    if t_amount > realized_profit: 
                        raise InsufficientFundsError("⛔ رفض قاطع: أرباحك الحالية لا تكفي لخصم هذه النسبة. لقد قمت بسحبها مسبقاً.")
                    await self._record_profit(conn, -t_amount, f"إلغاء نسبة (عملية #{tx_id})")

                # ================= 7. تحصيل رصيد (تسديدات) =================
                elif t_type == 'تحصيل_رصيد':
                    paid_debt, added_bal = Decimal('0.0'), Decimal('0.0')
                    match_debt = re.search(r"سداد دين:\s*([\d\.]+)", t_details)
                    if match_debt: paid_debt = Decimal(match_debt.group(1))
                    match_bal = re.search(r"رصيد مضاف:\s*([\d\.]+)", t_details)
                    if match_bal: added_bal = Decimal(match_bal.group(1))
                    
                    if paid_debt == 0 and added_bal == 0:
                        paid_debt = t_amount # إذا لم يجد التفاصيل، نعتبر المبلغ كله سداد دين
                        
                    await conn.execute("""
                        UPDATE users 
                        SET telecom_debt = telecom_debt + $1, 
                            telecom_balance = GREATEST(0, telecom_balance - $2),
                            telecom_pos_cash = GREATEST(0, LEAST(GREATEST(0, telecom_debt + $1), COALESCE(telecom_pos_cash, 0) + $1))
                        WHERE user_id = $3
                    """, paid_debt, added_bal, t_user)

                # ================= 8. تغذية رصيد بوابة =================
                elif t_type == 'تغذية_رصيد_بوابة':
                    # 🌟 توجيه التراجع إلى المحفظة الجديدة (agent_wallet) بدلاً من الإعدادات
                    await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance - $1 WHERE id = 1", t_amount)

                # ================= 9. سحب أرباح تسديدات =================
                elif t_type == 'سحب_أرباح_تسديدات':
                    await conn.execute("INSERT INTO telecom_profits (amount, details, date) VALUES ($1, $2, CURRENT_TIMESTAMP)", t_amount, f"إلغاء سحب أرباح (تراجع عن عملية #{tx_id})")

                # ================= 10. مرتجع للشبكة =================
                elif t_type == 'مرتجع_للشبكة':
                    items_to_revert = t_sd
                    if not items_to_revert:
                        matches = re.findall(r"(\d+)\s*كرت\s*([^\+\(]+)", t_details)
                        for match in matches:
                            qty, ctype = int(match[0]), match[1].replace("كاش", "").replace("للإدارة", "").strip()
                            items_to_revert.append({"card_type": ctype, "quantity": qty})
                            
                    for item in items_to_revert:
                        await conn.execute("UPDATE inventory SET quantity = quantity + $1 WHERE card_type = $2", item['quantity'], item['card_type'])

                # ================= 11. مرتجع بيع مباشر =================
                elif t_type == 'مرتجع_بيع_مباشر':
                    items_to_revert = t_sd
                    if not items_to_revert:
                        matches = re.findall(r"(\d+)\s*كرت\s*([^\+\(]+)", t_details)
                        for match in matches:
                            qty, ctype = int(match[0]), match[1].replace("كاش", "").replace("طياري", "").replace("جملة", "").strip()
                            prices = await conn.fetchrow("SELECT cost_price, price, retail_price FROM inventory WHERE card_type = $1", ctype)
                            if prices:
                                sell_p = prices['price'] if "جملة" in t_details else prices['retail_price']
                                # 🌟 الإصلاح المحاسبي: استخدام str() بدلاً من float()
                                items_to_revert.append({"card_type": ctype, "quantity": qty, "cost_price": str(prices['cost_price'] or 0), "sell_price": str(sell_p)})
                                
                    for item in items_to_revert:
                        agent_qty = await conn.fetchval("SELECT quantity FROM inventory WHERE card_type = $1", item['card_type'])
                        if not agent_qty or agent_qty < item['quantity']: 
                            raise InsufficientStockError(f"⛔ رفض قاطع: مخزونك لا يكفي للتراجع عن مرتجع ({item['card_type']}). لقد قمت ببيعها مجدداً.")
                            
                    for item in items_to_revert:
                        qty, ctype = item['quantity'], item['card_type']
                        await conn.execute("UPDATE inventory SET quantity = quantity - $1 WHERE card_type = $2", qty, ctype)
                        profit = (to_decimal(item['sell_price']) - to_decimal(item['cost_price'])) * qty
                        if profit != 0:
                            profit_desc = "إعادة ربح" if profit > 0 else "إعادة خصم خسارة"
                            await self._record_profit(conn, profit, f"{profit_desc} (تراجع عن مرتجع #{tx_id})")

                # ================= 12. سحب أرباح =================
                elif t_type == 'سحب_أرباح':
                    await self._record_profit(conn, t_amount, f"إعادة ربح (تراجع عن سحب #{tx_id})")

                # 🌟 الإصلاح المحاسبي: السماح بالتراجع عن المصروفات، تسديد الإدارة، والأرصدة الافتتاحية
                elif t_type in [TxType.EXPENSE.value, TxType.PAY_MANAGER.value, 'رصيد_افتتاحي_كاش', 'رصيد_افتتاحي', 'دين_سابق_عميل']:
                    if t_type in ['رصيد_افتتاحي', 'دين_سابق_عميل'] and t_user != 0:
                        await conn.execute("UPDATE users SET debt = debt - $1, old_debt = GREATEST(0, COALESCE(old_debt, 0) - $1) WHERE user_id = $2", t_amount, t_user)
                    pass

                # 🌟 السماح بالتراجع عن إيداع رأس مال التسديدات
                elif t_type == 'رأس_مال_تسديدات':
                    pass # الكاش سيتم خصمه تلقائياً عبر القيد العكسي في نهاية الدالة

                else: 
                    raise InvalidOperationError(f"نوع العملية '{t_type}' لا يدعم التراجع التلقائي حالياً")
                
                # تحديث حالة العملية الأصلية وتسجيل القيد العكسي
                await conn.execute("UPDATE transactions SET is_reverted = TRUE WHERE id = $1", tx_id)
                # 🌟 تسجيل القيد العكسي في نفس المحفظة الأصلية (لكي يختفي من تقرير المدير إذا كان يخص التسديدات)
                await self._log_tx(conn, t_user, TxType.REVERSE_ENTRY, -t_amount, f"تراجع عن {t_type.replace('_', ' ')} (إلغاء عملية #{tx_id})", None, key, wallet_type=t_wallet)
                
        return {"status": "success", "message": "تم تسجيل القيد العكسي وإلغاء العملية بنجاح."}

    # --- 5. الخدمات الإلكترونية (Telecom) ---
    async def process_telecom(self, client_id: int, phone: str, network: str, pkg: str, cost: Decimal, cash_profit: Decimal, credit_profit: Decimal, is_agent: bool, loan_amount: Decimal = Decimal('0.0'), key: Optional[str] = None):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 🌟 ترتيب الأقفال لمنع الـ Deadlock
                await LockManager.lock_users(conn, [client_id] if not is_agent else [])
                
                if key:
                    exists = await conn.fetchval("SELECT id FROM telecom_transactions WHERE idempotency_key = $1", key)
                    if exists: raise InvalidOperationError("تم استلام طلبك مسبقاً وهو قيد التنفيذ أو مكتمل.")
                
                # 🌟 الإصلاح 1: إجمالي الخصم من الوكيل = تكلفة الباقة + السلفة
                total_agent_deduction = cost + loan_amount
                
                # 🌟 خصم التكلفة والسلفة من محفظة الوكيل (agent_wallet)
                updated = await conn.fetchval("UPDATE agent_wallet SET telecom_balance = telecom_balance - $1 WHERE id = 1 AND telecom_balance >= $1 RETURNING telecom_balance", total_agent_deduction)
                if updated is None: raise InsufficientFundsError(f"رصيد البوابة غير كافٍ لتغطية التكلفة والسلفة. المطلوب: {total_agent_deduction}")
                
                recent_tx = await conn.fetchval("SELECT id FROM telecom_transactions WHERE phone_number = $1 AND package_name = $2 AND created_at > NOW() - INTERVAL '2 minutes'", phone, pkg)
                if recent_tx:
                    await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", total_agent_deduction)
                    raise InvalidOperationError("تم استلام طلبك مسبقاً وهو قيد التنفيذ! يرجى الانتظار.")

                final_price = total_agent_deduction
                agent_profit = Decimal('0.0')
                paid_from_balance = Decimal('0.0')
                added_to_debt = Decimal('0.0')
                db_id = 0 if is_agent else client_id
                
                if is_agent:
                    final_price += cash_profit
                    agent_profit = cash_profit
                else:
                    u = await conn.fetchrow("SELECT telecom_balance, telecom_debt, telecom_credit_limit, telecom_status, is_vip, special_discount, debt, credit_limit FROM users WHERE user_id = $1", client_id)
                    if not u:
                        await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", total_agent_deduction)
                        raise TransactionNotFoundError("العميل غير موجود")
                        
                    if u['telecom_status'] == 'off': 
                        await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", total_agent_deduction)
                        raise SecurityViolationError("خدمة التسديدات مقفلة لحسابك")
                        
                    # 🌟 الحماية المتقاطعة (Cross-Default)
                    if to_decimal(u['debt']) > to_decimal(u['credit_limit'] or 50000):
                        await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", total_agent_deduction)
                        raise CreditLimitExceededError("أنت متجاوز لسقف ديون الكروت! يرجى سداد ديونك أولاً لتتمكن من استخدام التسديدات.")

                    bal = to_decimal(u['telecom_balance'])
                    debt = to_decimal(u['telecom_debt'])
                    lim = to_decimal(u['telecom_credit_limit'] or 20000)
                    special_discount = to_decimal(u.get('special_discount', 0))
                    
                    base_cash_price = max(cost, cost + cash_profit - special_discount)
                    base_credit_price = max(cost, cost + credit_profit - special_discount)
                    if u.get('is_vip', False): base_credit_price = base_cash_price
                        
                    total_cash_price = base_cash_price + loan_amount
                    total_credit_price = base_credit_price + loan_amount
                        
                    if bal >= total_cash_price:
                        final_price = total_cash_price
                        paid_from_balance = final_price
                        await conn.execute("UPDATE users SET telecom_balance = telecom_balance - $1 WHERE user_id = $2", final_price, client_id)
                    else:
                        final_price = total_credit_price
                        paid_from_balance = bal
                        added_to_debt = final_price - bal
                        new_debt = debt + added_to_debt
                        
                        if new_debt > lim: 
                            await conn.execute("UPDATE agent_wallet SET telecom_balance = telecom_balance + $1 WHERE id = 1", total_agent_deduction)
                            raise CreditLimitExceededError(f"رصيدك لا يكفي والعملية ستتجاوز سقف المديونية! السقف: {lim}")
                            
                    # 🌟 التعديل المحاسبي المحكم: زيادة كاش التسديدات الجاهز مع حماية الأرصدة الدائنة
                    await conn.execute("""
                        UPDATE users 
                        SET telecom_balance = 0, 
                            telecom_debt = $1,
                            telecom_pos_cash = GREATEST(0, LEAST(GREATEST(0, $1), COALESCE(telecom_pos_cash, 0) + $2))
                        WHERE user_id = $3
                    """, new_debt, added_to_debt, client_id)

                    agent_profit = final_price - total_agent_deduction
                
                # 🌟 توثيق الدفع المختلط
                tx_id = await conn.fetchval("INSERT INTO telecom_transactions (client_id, network, phone_number, package_name, amount, cost_price, selling_price, profit, paid_from_balance, added_to_debt, status, idempotency_key) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'pending', $11) RETURNING id", db_id, network, phone, pkg, final_price, cost, final_price, agent_profit, paid_from_balance, added_to_debt, key)
                
                # 🌟 الإصلاح 2: إدخال العملية في الدفتر الشامل للوكيل وللعميل
                if is_agent:
                    await self._log_tx(conn, 0, TxType.AGENT_TELECOM_SALE, final_price, f"مبيعات طياري (كاش): {phone} - {pkg}", None, f"{key}_cash" if key else None, wallet_type='telecom')
                else:
                    await self._log_tx(conn, client_id, TxType.TELECOM_PAYMENT, final_price, f"تسديد إلكتروني: {phone} - {pkg}", None, f"{key}_ledger" if key else None, wallet_type='telecom')
                
                # 🌟 إضافة الأرباح لمحفظة التسديدات
                if agent_profit > 0:
                    await conn.execute("INSERT INTO telecom_profits (amount, details) VALUES ($1, $2)", agent_profit, f"ربح شحن/باقة: {phone}")
                
                sale_type_name = "تسديد وكيل" if is_agent else ("دفع مقدم" if paid_from_balance == final_price else "دفع آجل")
                return {
                    "status": "success", "trans_id": tx_id, "final_price": final_price,
                    "agent_profit": agent_profit, "sale_type_name": sale_type_name,
                    "loan_amount": loan_amount, "base_cost": cost
                }

    # --- 6. الأرشفة الذكية (Smart Archive) ---
    async def archive_data(self, months: int):
        if months < 1: raise InvalidOperationError("يجب الاحتفاظ بشهر واحد على الأقل")
        
        deleted_stats = {}
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. حذف المحادثات القديمة
                res = await conn.execute("DELETE FROM chat_messages WHERE created_at < NOW() - (INTERVAL '1 month' * $1)", months)
                deleted_stats['chats'] = int(res.split()[-1]) if res else 0
                
                # 2. حذف الإشعارات القديمة
                res = await conn.execute("DELETE FROM web_notifications WHERE created_at < NOW() - (INTERVAL '1 month' * $1)", months)
                deleted_stats['notifications'] = int(res.split()[-1]) if res else 0
                
                # 3. حذف سجلات مبيعات البقالات القديمة (POS)
                res = await conn.execute("DELETE FROM client_sales WHERE sale_date < NOW() - (INTERVAL '1 month' * $1)", months)
                deleted_stats['client_sales'] = int(res.split()[-1]) if res else 0
                
        logger.info(f"تمت أرشفة البيانات الأقدم من {months} أشهر بنجاح: {deleted_stats}")
        return {"status": "success", "deleted_stats": deleted_stats, "message": f"تم تنظيف البيانات الأقدم من {months} أشهر بنجاح."}

    # --- 7. تصفية حساب عميل (Account Closure) ---
    async def close_account(self, client_id: int, key: Optional[str] = None):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if key:
                    exists = await conn.fetchval("SELECT id FROM transactions WHERE idempotency_key = $1", key)
                    if exists: return exists

                # 🌟 الحماية الفولاذية: قفل المستخدم، ثم المخزون العام، ثم مخزون العميل (بالترتيب لمنع الـ Deadlock)
                await LockManager.lock_users(conn, [client_id])
                
                # جلب أنواع الكروت التي يمتلكها العميل لقفلها مسبقاً
                inv_items_check = await conn.fetch("SELECT card_type FROM client_inventory WHERE user_id = $1 AND quantity > 0", client_id)
                card_types_to_lock = [r['card_type'] for r in inv_items_check]
                
                if card_types_to_lock:
                    await LockManager.lock_inventory(conn, card_types_to_lock)
                    await LockManager.lock_client_inventory(conn, client_id, card_types_to_lock)

                # 🌟 جلب جميع الأرصدة بما فيها التسديدات
                u = await conn.fetchrow("SELECT name, debt, pending_profit, pos_cash_collected, telecom_pos_cash, telecom_debt, telecom_balance FROM users WHERE user_id = $1", client_id)
                
                if not u: raise TransactionNotFoundError("العميل غير موجود")
                
                if to_decimal(u['pos_cash_collected']) > 0 or to_decimal(u.get('telecom_pos_cash', 0)) > 0:
                    raise InvalidOperationError(f"العميل لديه كاش غير محصل في الدرج. يرجى تحصيل الكاش أولاً قبل التصفية.")
                    
                # 🌟 حماية العزل المالي (Air-Gap Protection)
                if to_decimal(u.get('telecom_debt', 0)) > 0 or to_decimal(u.get('telecom_balance', 0)) > 0:
                    raise InvalidOperationError("العميل لديه حسابات تسديدات نشطة (ديون أو رصيد). يرجى تصفية قسم التسديدات يدوياً من تطبيق الويب قبل الإغلاق النهائي لحماية العزل المالي.")

                if to_decimal(u.get('telecom_pos_cash', 0)) > 0:
                    raise InvalidOperationError(f"العميل لديه كاش تسديدات غير محصل ({u['telecom_pos_cash']} ريال). يرجى تحصيل الكاش أولاً قبل التصفية.")
                    
                # 🌟 الحماية الفولاذية: منع التصفية إذا كان هناك ديون أو أرصدة تسديدات
                if to_decimal(u.get('telecom_debt', 0)) > 0 or to_decimal(u.get('telecom_balance', 0)) > 0:
                    raise InvalidOperationError("العميل لديه ديون أو أرصدة في قسم التسديدات. يرجى تصفيتها أولاً قبل إغلاق الحساب.")
                
                client_name = u['name']
                current_debt = to_decimal(u['debt'])
                current_pending = to_decimal(u['pending_profit'])
                
                # جلب كروت العميل لإرجاعها
                inv_items = await conn.fetch("SELECT card_type, quantity FROM client_inventory WHERE user_id = $1 AND quantity > 0", client_id)
                
                returned_cards_value = Decimal('0.0')
                total_profit_to_reverse = Decimal('0.0')
                returned_details = []
                structured_items = []
                
                for item in inv_items:
                    ctype, cqty = item['card_type'], item['quantity']
                    prices = await conn.fetchrow("SELECT cost_price, price FROM inventory WHERE card_type = $1", ctype)
                    if prices:
                        cost_price = to_decimal(prices['cost_price'])
                        wholesale_price = to_decimal(prices['price'])
                        
                        val = wholesale_price * cqty
                        profit_rev = (wholesale_price - cost_price) * cqty
                        
                        returned_cards_value += val
                        total_profit_to_reverse += profit_rev
                        returned_details.append(f"{cqty} {ctype}")
                        
                        structured_items.append({
                            "card_type": ctype, "quantity": cqty,
                            "cost_price": str(cost_price), "sell_price": str(wholesale_price)
                        })
                        
                        await conn.execute("UPDATE inventory SET quantity = quantity + $1 WHERE card_type = $2", cqty, ctype)
                        await conn.execute("DELETE FROM client_inventory WHERE user_id = $1 AND card_type = $2", client_id, ctype)
                
                final_debt = current_debt - returned_cards_value     
                refund_cash = Decimal('0.0')
                details_str = ""
       
                if returned_cards_value > 0:
                    details_str = " + ".join(returned_details)
                    await self._log_tx(conn, client_id, TxType.RETURN_FROM_CLIENT, returned_cards_value, f"تصفية حساب: إرجاع {details_str}", structured_items, key)
                    
                if total_profit_to_reverse > 0:
                    deduct_from_pending = min(current_pending, total_profit_to_reverse)
                    deduct_from_real = total_profit_to_reverse - deduct_from_pending
                    current_pending -= deduct_from_pending
                    
                    if deduct_from_real > 0:
                        await self._record_profit(conn, -deduct_from_real, f"خصم ربح (تصفية حساب): {details_str}")

                if final_debt < 0:
                    refund_cash = abs(final_debt)
                    fin_stats = await self.get_summary(conn)
                    if refund_cash > fin_stats["cash"]:
                        raise InsufficientFundsError(f"لا يوجد كاش كافٍ في الصندوق لإرجاع الفائض للعميل! المطلوب: {refund_cash}، المتوفر: {fin_stats['cash']}")
                        
                    await self._log_tx(conn, 0, TxType.EXPENSE, refund_cash, f"تسوية نقدية (إرجاع كاش) لتصفية حساب: {client_name}")
                    final_debt = Decimal('0.0')
                
                if current_pending < 0:
                    await self._record_profit(conn, current_pending, f"تسوية أرباح معلقة سالبة لتصفية حساب: {client_name}")
                    current_pending = Decimal('0.0')

                await conn.execute("UPDATE users SET debt = $1, old_debt = GREATEST(0, LEAST(COALESCE(old_debt, 0), $1)), pending_profit = $2, pos_cash_collected = 0, promised_payment = 0 WHERE user_id = $3", final_debt, current_pending, client_id)

        return {
            "status": "success",
            "client_name": client_name,
            "returned_cards_value": returned_cards_value,
            "details_str": details_str,
            "total_profit_to_reverse": total_profit_to_reverse,
            "refund_cash": refund_cash,
            "final_debt": final_debt
        }

        # =====================================================================
# الدوال الإدارية والخدمات الإلكترونية (Standalone Functions)
# =====================================================================
import database

async def core_process_return(return_type: str, qty: int, amount: Decimal, client_id: int, card_type: str, source: str = ""):
    """المنطق المركزي الشامل لجميع أنواع المرتجعات والتوالف"""
    if not database.pool: return {"status": "error", "message": "قاعدة البيانات غير متصلة"}
    
    # 🌟 الإصلاح: منع المرتجعات العادية للكروت الإلكترونية، ولكن السماح بتسجيلها كـ "تالف"
    if card_type and ('إلكتروني' in card_type or 'الكتروني' in card_type):
        if return_type != 'damaged':
            return {"status": "error", "message": "⛔ عذراً، لا يمكن إرجاع الكروت الإلكترونية لحساسية الأرقام السرية."}
        
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            # 🌟 الإصلاح الأسطوري: توحيد الأقفال باستخدام LockManager لمنع الـ Deadlock
            
            # 1. قفل المستخدمين (الإدارة + العميل إن وجد)
            users_to_lock = [0]
            if client_id and client_id != 0:
                users_to_lock.append(client_id)
            await LockManager.lock_users(conn, users_to_lock)
            
            # 2. قفل المخزون العام
            if card_type:
                await LockManager.lock_inventory(conn, [card_type])
                
            # 3. قفل مخزون العميل
            if client_id and client_id != 0 and card_type:
                await LockManager.lock_client_inventory(conn, client_id, [card_type])
            
            # 🌟 التعديل: استخدام المحرك الجديد لجلب الكاش والأرباح
            engine = FinancialEngine(database.pool)
            fin_stats = await engine.get_summary(conn)
            available_cash = fin_stats["cash"]
            realized_profit = fin_stats["realized"]

            if return_type == 'damaged':
                if amount > available_cash:
                    return {"status": "error", "message": f"الكاش المتوفر لا يكفي! المتوفر: {available_cash} ريال."}
                
                # 🌟 خصم الكروت التالفة من المخزون
                if card_type and qty > 0:
                    await conn.execute("UPDATE inventory SET quantity = quantity - $1 WHERE card_type = $2", qty, card_type)
                    
                trans_id = await conn.fetchval("INSERT INTO transactions (user_id, type, amount, details) VALUES (0, 'كروت_تالفة', $1, $2) RETURNING id", amount, f"إرجاع {qty} كروت تالفة ({card_type}) للإدارة {source}")
                return {"status": "success", "trans_id": trans_id, "total_value": amount, "message": "تم تسجيل الكروت التالفة وخصمها من المخزون بنجاح"}
                
            if not card_type or qty <= 0:
                return {"status": "error", "message": "يجب تحديد نوع الكرت وكمية صحيحة."}
                
            if return_type == 'from_client':
                client_qty = await conn.fetchval("SELECT quantity FROM client_inventory WHERE user_id = $1 AND card_type = $2 FOR UPDATE", client_id, card_type)
                if not client_qty or client_qty < qty:
                    return {"status": "error", "message": f"العميل لا يملك {qty} كرت من فئة ({card_type}) في عهدته لكي يرجعها!"}
                
                prices = await conn.fetchrow("SELECT cost_price, price FROM inventory WHERE card_type = $1", card_type)
                if not prices: return {"status": "error", "message": "نوع الكرت غير موجود في المخزون."}
                    
                # استخدام دالة to_decimal الآمنة بدلاً من Decimal المباشرة
                total_value = to_decimal(prices['price']) * qty
                profit_to_reverse = (to_decimal(prices['price']) - to_decimal(prices['cost_price'])) * qty
                
                user_data = await conn.fetchrow("SELECT pending_profit, name FROM users WHERE user_id = $1 FOR UPDATE", client_id)
                if not user_data: return {"status": "error", "message": "العميل غير موجود في النظام."}
                    
                current_pending = to_decimal(user_data['pending_profit'])
                client_name = user_data['name']
                
                deduct_from_pending = min(current_pending, profit_to_reverse)
                deduct_from_real = profit_to_reverse - deduct_from_pending
                
                await conn.execute("UPDATE client_inventory SET quantity = quantity - $1 WHERE user_id = $2 AND card_type = $3", qty, client_id, card_type)
                await conn.execute("UPDATE inventory SET quantity = quantity + $1 WHERE card_type = $2", qty, card_type)
                
                await conn.execute("""
                    UPDATE users 
                    SET debt = debt - $1, 
                        old_debt = GREATEST(0, LEAST(COALESCE(old_debt, 0), debt - $1)), 
                        pending_profit = pending_profit - $2,
                        promised_payment = GREATEST(0, COALESCE(promised_payment, 0) - $1)
                    WHERE user_id = $3
                """, total_value, deduct_from_pending, client_id)
                
                import json
                # 🌟 الإصلاح المحاسبي: استخدام str() بدلاً من float() لمنع ضياع الهللات
                structured_data = json.dumps([{"card_type": card_type, "quantity": qty, "cost_price": str(prices['cost_price'] or 0), "sell_price": str(prices['price'] or 0)}])
                trans_id = await conn.fetchval("INSERT INTO transactions (user_id, type, amount, details, structured_details) VALUES ($1, 'مرتجع_من_عميل', $2, $3, $4) RETURNING id", client_id, total_value, f"إرجاع {qty} كرت {card_type} {source}", structured_data)
                
                if deduct_from_real > 0:
                    await conn.execute("INSERT INTO agent_profits (amount, details) VALUES ($1, $2)", -deduct_from_real, f"خصم ربح مسحوب مسبقاً (مرتجع من عميل {source}): {qty} كرت {card_type}")
                    
                return {"status": "success", "trans_id": trans_id, "total_value": total_value, "client_name": client_name, "message": "تم قبول المرتجع من العميل بنجاح وتحديث الأرباح"}

            elif return_type == 'to_network':
                agent_qty = await conn.fetchval("SELECT quantity FROM inventory WHERE card_type = $1 FOR UPDATE", card_type)
                if not agent_qty or agent_qty < qty:
                    return {"status": "error", "message": f"مخزونك لا يسمح! لا تملك {qty} كرت من فئة ({card_type})."}
                    
                cost_price = await conn.fetchval("SELECT cost_price FROM inventory WHERE card_type = $1", card_type)
                if cost_price is None: return {"status": "error", "message": "سعر التكلفة غير محدد لهذا الكرت."}
                    
                total_value = Decimal(cost_price) * qty
                await conn.execute("UPDATE inventory SET quantity = quantity - $1 WHERE card_type = $2", qty, card_type)
                import json
                structured_data = json.dumps([{"card_type": card_type, "quantity": qty, "cost_price": str(cost_price), "sell_price": str(cost_price)}])
                trans_id = await conn.fetchval("INSERT INTO transactions (user_id, type, amount, details, structured_details) VALUES (0, 'مرتجع_للشبكة', $1, $2, $3) RETURNING id", total_value, f"إرجاع {qty} كرت {card_type} للإدارة {source}", structured_data)
                
                return {"status": "success", "trans_id": trans_id, "total_value": total_value, "message": "تم تسجيل المرتجع للإدارة بنجاح"}

                        # الكود الجديد (حماية حسابية فولاذية)
            elif return_type.startswith('direct_sale'):
                prices = await conn.fetchrow("SELECT cost_price, price, retail_price FROM inventory WHERE card_type = $1", card_type)
                if not prices: return {"status": "error", "message": "نوع الكرت غير موجود في المخزون."}
                    
                cost_price = to_decimal(prices['cost_price'])
                
                if 'wholesale' in return_type:
                    sale_price = to_decimal(prices['price'])
                    sale_type_name = "جملة"
                else:
                    sale_price = to_decimal(prices['retail_price'])
                    sale_type_name = "طياري"
                
                total_value = sale_price * qty
                agent_profit_loss = (sale_price - cost_price) * qty
                
                if total_value > available_cash:
                    return {"status": "error", "message": f"الكاش المتوفر لا يكفي لإرجاع المبلغ للعميل! المتوفر: {available_cash} ريال."}
                if agent_profit_loss > realized_profit:
                    return {"status": "error", "message": f"رصيد أرباحك لا يكفي لخصم ربح هذه العملية! أرباحك: {realized_profit} ريال."}
                
                await conn.execute("UPDATE inventory SET quantity = quantity + $1 WHERE card_type = $2", qty, card_type)
                import json
                structured_data = json.dumps([{"card_type": card_type, "quantity": qty, "cost_price": str(cost_price), "sell_price": str(sale_price)}])
                trans_id = await conn.fetchval("INSERT INTO transactions (user_id, type, amount, details, structured_details) VALUES (0, 'مرتجع_بيع_مباشر', $1, $2, $3) RETURNING id", total_value, f"إرجاع {qty} كرت {card_type} {sale_type_name} {source}", structured_data)
                
                if agent_profit_loss != 0:
                    profit_desc = "خصم ربح" if agent_profit_loss > 0 else "استرداد خسارة"
                    await conn.execute("INSERT INTO agent_profits (amount, details) VALUES ($1, $2)", -agent_profit_loss, f"{profit_desc} مرتجع مباشر {source}: {qty} كرت {card_type}")
                
                return {"status": "success", "trans_id": trans_id, "total_value": total_value, "message": f"تم تسجيل مرتجع البيع المباشر ({sale_type_name}) بنجاح"}
                
    return {"status": "error", "message": "نوع المرتجع غير معروف"}

async def core_receive_shipment(shipment_id: int, agent_items: dict, match_type: str):
    if not database.pool: return {"status": "error", "message": "قاعدة البيانات غير متصلة"}
    if any(qty < 0 for qty in agent_items.values()): return {"status": "error", "message": "عذراً، لا يمكن استلام كميات سالبة."}
        
    total_price = Decimal('0.0')
    structured_items = []
    import json
    
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            for ctype, qty in agent_items.items():
                
                # 🌟 الإصلاح الأسطوري (Atomic Insert): إدخال الفئة بأمان تام بدون ثغرة السباق
                await conn.execute("""
                    INSERT INTO inventory (card_type, quantity, cost_price, price, retail_price) 
                    VALUES ($1, 0, 0, 0, 0) 
                    ON CONFLICT (card_type) DO NOTHING
                """, ctype)
                
                # جلب سعر التكلفة (سواء كانت الفئة قديمة أو تم إنشاؤها للتو بصفر)
                cost_price = await conn.fetchval("SELECT cost_price FROM inventory WHERE card_type = $1", ctype)
                cost_price_dec = Decimal(str(cost_price)) if cost_price else Decimal('0.0')
                total_price += cost_price_dec * qty
                
                structured_items.append({
                    "card_type": ctype, "quantity": qty, 
                    "cost_price": str(cost_price_dec), "sell_price": 0.0
                })
                
                # تحديث الكمية
                await conn.execute('UPDATE inventory SET quantity = quantity + $1 WHERE card_type = $2', qty, ctype)
            
            details = "استلام إرسالية مطابقة 100%" if match_type == 'exact' else "استلام إرسالية (باعتماد العدد الفعلي للوكيل)"
            new_status = 'matched' if match_type == 'exact' else 'resolved'
            
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, details, structured_details, wallet_type) VALUES (0, 'استلام_من_الشبكة', $1, $2, $3, 'manager')", 
                total_price, details, json.dumps(structured_items)
            )
            await conn.execute("UPDATE pending_shipments SET status = $1, agent_items = $2 WHERE id = $3", new_status, json.dumps(agent_items), shipment_id)
            
    return {"status": "success", "total_price": total_price}

async def core_cancel_shipment(shipment_id: int):
    if not database.pool: return {"status": "error", "message": "قاعدة البيانات غير متصلة"}
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            shipment = await conn.fetchrow("SELECT status FROM pending_shipments WHERE id = $1 FOR UPDATE", shipment_id)
            if not shipment or shipment['status'] != 'pending':
                return {"status": "error", "message": "تمت معالجة هذه الإرسالية مسبقاً."}
            await conn.execute("UPDATE pending_shipments SET status = 'cancelled' WHERE id = $1", shipment_id)
    return {"status": "success"}

async def core_update_order_status(order_id: int, new_status: str):
    if not database.pool: return {"status": "error", "message": "قاعدة البيانات غير متصلة"}
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            order = await conn.fetchrow("SELECT user_id, status, new_order_text FROM pending_orders WHERE id = $1 FOR UPDATE", order_id)
            if not order or order['status'] != 'pending':
                return {"status": "error", "message": "هذا الطلب تمت معالجته مسبقاً!"}
            await conn.execute("UPDATE pending_orders SET status = $1 WHERE id = $2", new_status, order_id)
    return {"status": "success", "client_id": order['user_id'], "new_order_text": order['new_order_text']}

async def core_delete_card_type(card_type: str):
    if not database.pool: return {"status": "error", "message": "قاعدة البيانات غير متصلة"}
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            in_market = await conn.fetchval("SELECT COALESCE(SUM(quantity), 0) FROM client_inventory WHERE card_type = $1", card_type)
            in_agent = await conn.fetchval("SELECT quantity FROM inventory WHERE card_type = $1", card_type)
            in_ecards = await conn.fetchval("SELECT COUNT(*) FROM electronic_cards WHERE card_type = $1 AND status = 'available'", card_type)
            
            if in_market > 0 or (in_agent and in_agent > 0) or (in_ecards and in_ecards > 0):
                return {
                    "status": "error", 
                    "message": f"🚨 **رفض أمني:** لا يمكن حذف فئة ({card_type})!\nيوجد ({in_market}) كرت في البقالات، و ({in_agent or 0}) كرت في صندوقك، و ({in_ecards or 0}) كرت إلكتروني في الخزنة.\nيجب تصفير الكميات أولاً لتجنب خسارة قيمتها."
                }
            # 🌟 الحذف الآمن (Soft Delete) لحماية سجلات التراجع القديمة
            await conn.execute("UPDATE inventory SET is_active = FALSE WHERE card_type = $1", card_type)
    return {"status": "success"}

async def core_add_offline_client(client_name: str, phone: str):
    if not database.pool: return {"status": "error", "message": "قاعدة البيانات غير متصلة"}
    async with database.pool.acquire() as conn:
        try:
            # 🌟 سحب الرقم التالي من العداد بسرعة البرق وبدون قفل الجدول
            fake_user_id = await conn.fetchval("SELECT nextval('offline_users_seq')")
            
            await conn.execute(
                "INSERT INTO users (user_id, name, role, debt, phone) VALUES ($1, $2, 'client', 0.0, $3)", 
                fake_user_id, client_name, phone
            )
            return {"status": "success", "user_id": fake_user_id}
        except Exception as e:
            return {"status": "error", "message": f"حدث خطأ أثناء إضافة العميل: {str(e)}"}
    
async def core_link_account(offline_id: int, real_id: int):
    if not database.pool: return {"status": "error", "message": "قاعدة البيانات غير متصلة"}
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            ids_to_lock = sorted([offline_id, real_id])
            for uid in ids_to_lock:
                await conn.execute("SELECT 1 FROM users WHERE user_id = $1 FOR UPDATE", uid)
                
            # 🌟 جلب ونقل ديون وأرصدة التسديدات والكاش الجاهز لكي لا تضيع
            offline_user = await conn.fetchrow("SELECT name, debt, old_debt, pending_profit, telecom_debt, telecom_balance, pos_cash_collected, telecom_pos_cash FROM users WHERE user_id = $1", offline_id)
            real_user = await conn.fetchrow("SELECT name FROM users WHERE user_id = $1", real_id)
            
            if not offline_user: return {"status": "error", "message": "حساب الأوفلاين غير موجود!"}
            if not real_user: return {"status": "error", "message": "حساب التليجرام الجديد غير موجود!"}
            
            await conn.execute("""
                UPDATE users 
                SET debt = debt + $1, old_debt = COALESCE(old_debt, 0) + COALESCE($2, 0), pending_profit = COALESCE(pending_profit, 0) + COALESCE($3, 0),
                    telecom_debt = COALESCE(telecom_debt, 0) + COALESCE($4, 0), telecom_balance = COALESCE(telecom_balance, 0) + COALESCE($5, 0),
                    pos_cash_collected = COALESCE(pos_cash_collected, 0) + COALESCE($6, 0), telecom_pos_cash = COALESCE(telecom_pos_cash, 0) + COALESCE($7, 0),
                    name = $8, role = 'client' 
                WHERE user_id = $9
            """, offline_user["debt"], offline_user.get("old_debt", 0), offline_user.get("pending_profit", 0), offline_user.get("telecom_debt", 0), offline_user.get("telecom_balance", 0), offline_user.get("pos_cash_collected", 0), offline_user.get("telecom_pos_cash", 0), offline_user["name"], real_id)
            
            # 🌟 الإصلاح الشامل: نقل جميع السجلات المرتبطة بالعميل لمنع ضياع البيانات
            await conn.execute("UPDATE transactions SET user_id = $1 WHERE user_id = $2", real_id, offline_id)
            await conn.execute("UPDATE client_sales SET client_id = $1 WHERE client_id = $2", real_id, offline_id)
            await conn.execute("UPDATE client_customers SET client_id = $1 WHERE client_id = $2", real_id, offline_id)
            await conn.execute("UPDATE client_customer_ledger SET client_id = $1 WHERE client_id = $2", real_id, offline_id)
            await conn.execute("UPDATE telecom_transactions SET client_id = $1 WHERE client_id = $2", real_id, offline_id)
            await conn.execute("UPDATE chat_messages SET client_id = $1 WHERE client_id = $2", real_id, offline_id)
            
            await conn.execute('''
                INSERT INTO client_inventory (user_id, card_type, quantity)
                SELECT $1, card_type, quantity FROM client_inventory WHERE user_id = $2
                ON CONFLICT (user_id, card_type) DO UPDATE SET quantity = client_inventory.quantity + EXCLUDED.quantity
            ''', real_id, offline_id)
            
            await conn.execute("DELETE FROM client_inventory WHERE user_id = $1", offline_id)
            await conn.execute("UPDATE users SET role = 'archived', debt = 0, old_debt = 0, pending_profit = 0 WHERE user_id = $1", offline_id)
            
    return {"status": "success", "offline_name": offline_user['name'], "real_name": real_user['name']}

async def core_set_debt(client_id: int, amount: Decimal, profit: Decimal = Decimal('0.0')):
    if not database.pool: return {"status": "error", "message": "قاعدة البيانات غير متصلة"}
    async with database.pool.acquire() as conn:
        user = await conn.fetchrow("SELECT name FROM users WHERE user_id = $1", client_id)
        if not user: return {"status": "error", "message": "العميل غير موجود!"}
        
        async with conn.transaction():
            # 🌟 التعديل المحاسبي: إضافة الربح إلى الأرباح المعلقة لكي لا يضيع تعب الوكيل
            await conn.execute("UPDATE users SET debt = debt + $1, old_debt = COALESCE(old_debt, 0) + $1, pending_profit = COALESCE(pending_profit, 0) + $2 WHERE user_id = $3", amount, profit, client_id)
            await conn.execute("INSERT INTO transactions (user_id, type, amount, details) VALUES ($1, 'دين_سابق_عميل', $2, $3)", client_id, amount, f"إدخال دين سابق من الدفتر (يتضمن ربح: {profit})")
            
    return {"status": "success", "client_name": user['name']}

async def core_set_inv(client_id: int, card_type: str, quantity: int):
    if not database.pool: return {"status": "error", "message": "قاعدة البيانات غير متصلة"}
    async with database.pool.acquire() as conn:
        user = await conn.fetchrow("SELECT name FROM users WHERE user_id = $1", client_id)
        if not user: return {"status": "error", "message": "العميل غير موجود!"}
        
        async with conn.transaction():
            # جلب الأسعار فقط لحساب الأرباح المعلقة (بدون فحص كمية الوكيل)
            prices = await conn.fetchrow("SELECT cost_price, price FROM inventory WHERE card_type = $1", card_type)
            
                        # الكود الجديد (حماية حسابية فولاذية)
            if not prices:
                return {"status": "error", "message": f"عذراً، فئة الكرت ({card_type}) غير مسجلة في النظام. أضفها من الإعدادات أولاً."}
                
            # حساب الأرباح المعلقة وإضافتها لك باستخدام to_decimal
            profit = (to_decimal(prices['price']) - to_decimal(prices['cost_price'])) * quantity
            await conn.execute("UPDATE users SET pending_profit = pending_profit + $1 WHERE user_id = $2", profit, client_id)
            
            # 🌟 التعديل 1: جعل الكمية تراكمية (تُجمع مع ما سبق) لكي تتطابق مع الأرباح المعلقة
            await conn.execute('''
                INSERT INTO client_inventory (user_id, card_type, quantity) VALUES ($1, $2, $3) 
                ON CONFLICT (user_id, card_type) DO UPDATE SET quantity = client_inventory.quantity + $3
            ''', client_id, card_type, quantity)
            
            # 🌟 التعديل 2: توثيق العملية في الدفتر لكي لا تظهر كأرباح أشباح!
            details = f"إدخال مخزون سابق من الدفتر: {quantity} كرت {card_type} (يتضمن ربح معلق: {profit})"
            await conn.execute("INSERT INTO transactions (user_id, type, amount, details) VALUES ($1, 'مخزون_سابق_عميل', 0, $2)", client_id, details)
            
    return {"status": "success", "client_name": user['name']}

async def core_discount_gm(amount: Decimal):
    if not database.pool: return {"status": "error", "message": "قاعدة البيانات غير متصلة"}
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("INSERT INTO transactions (user_id, type, amount, details) VALUES (0, 'تسديد_للشبكة', $1, 'خصم/مسامحة من المدير العام')", amount)
            await conn.execute("INSERT INTO transactions (user_id, type, amount, details) VALUES (0, 'قيد_عكسي', $1, 'تسوية كاش (مسامحة المدير)')", amount)
            await conn.execute("INSERT INTO agent_profits (amount, details) VALUES ($1, $2)", amount, 'خصم/مسامحة من المدير العام')
    return {"status": "success"}

async def core_extract_ecard(client_id: int, card_type: str, sale_type: str, customer_name: str):
    """المنطق المركزي لسحب الكروت الإلكترونية"""
    if not database.pool: return {"status": "error", "message": "قاعدة البيانات غير متصلة"}
    
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            user_info = await conn.fetchrow("SELECT name, role, debt, credit_limit, ecard_status, phone, wa_status FROM users WHERE user_id = $1 FOR UPDATE", client_id)
            if not user_info: return {"status": "error", "message": "بيانات العميل غير موجودة."}
            
            if user_info['role'] != 'client':
                return {"status": "error", "message": "⛔ تم سحب صلاحياتك كعميل جملة. لا يمكنك إجراء هذه العملية."}

            if user_info.get('ecard_status') == 'off':
                return {"status": "error", "message": "⛔ خدمة سحب الكروت الإلكترونية مقفلة لحسابك. يرجى التواصل مع الإدارة لتفعيلها."}

                        # الكود الجديد (حماية حسابية فولاذية)
            limit = to_decimal(user_info.get('credit_limit', 50000))
            current_debt = to_decimal(user_info.get('debt', 0))
            client_name_db = user_info.get('name', 'عميل')

            prices = await conn.fetchrow("SELECT cost_price, price, retail_price FROM inventory WHERE card_type = $1", card_type)
            if not prices or prices['price'] is None:
                return {"status": "error", "message": "سعر البيع أو التكلفة غير محدد لهذه الفئة!"}

            # استخدام to_decimal لمنع أي تلاعب أو أخطاء في القيم
            wholesale_price = to_decimal(prices['price'])
            cost_price = to_decimal(prices['cost_price'])
            retail_price = to_decimal(prices['retail_price'])
            
            client_profit = retail_price - wholesale_price 
            agent_profit = wholesale_price - cost_price    
            new_total_debt = current_debt + wholesale_price

            if new_total_debt > limit:
                return {"status": "error", "message": f"تجاوز سقف المديونية!\nدينك: {current_debt}\nالسقف: {limit}\nيرجى تسديد الديون أولاً."}

            # =========================================================
            # 🚨 كاشف الاحتيال المتقدم (Velocity Check) لحماية حساب العميل
            # =========================================================
            # 1. حساب متوسط السحب اليومي في آخر 30 يوم
            avg_daily_sales = await conn.fetchval("""
                SELECT COALESCE(SUM(amount), 0) / 30.0 
                FROM transactions 
                WHERE user_id = $1 AND type IN ('مبيعات_آجلة', 'تسليم_لعميل') 
                AND date >= CURRENT_DATE - INTERVAL '30 days' AND is_reverted = FALSE
            """, client_id)
            
            # 2. حساب ما تم سحبه خلال الساعة الماضية
            last_hour_sales = await conn.fetchval("""
                SELECT COALESCE(SUM(amount), 0) 
                FROM transactions 
                WHERE user_id = $1 AND type IN ('مبيعات_آجلة', 'تسليم_لعميل') 
                AND date >= NOW() - INTERVAL '1 hour' AND is_reverted = FALSE
            """, client_id)
            
            # 3. تحديد الحد الآمن (300% من المتوسط اليومي، أو 10,000 ريال كحد أدنى للعملاء الجدد)
            safe_threshold = max(Decimal('10000.0'), to_decimal(avg_daily_sales) * Decimal('3.0'))
            
            if (to_decimal(last_hour_sales) + wholesale_price) > safe_threshold:
                return {
                    "status": "error", 
                    "message": f"🚨 **نظام الحماية الآلي:**\nعذراً، لقد تجاوزت معدل سحبك الطبيعي خلال ساعة واحدة!\nالحد الآمن لك: {safe_threshold:,.0f} ريال.\nما سحبته مؤخراً: {to_decimal(last_hour_sales):,.0f} ريال.\n\nتم تعليق السحب مؤقتاً لحماية حسابك من الاختراق. يرجى التواصل مع الإدارة."
                }
            # =========================================================

            card = await conn.fetchrow("""
                SELECT id, card_number FROM electronic_cards
                WHERE card_type = $1 AND status = 'available'
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            """, card_type)

            if not card:
                return {"status": "error", "message": f"لا يوجد كروت متاحة من فئة ({card_type}) في الخزنة!"}

            await conn.execute("UPDATE electronic_cards SET status = 'sold', sold_to_client = $1, sold_at = CURRENT_TIMESTAMP WHERE id = $2", client_id, card['id'])

            if current_debt < 0:
                advance_payment = abs(current_debt)
                if advance_payment >= wholesale_price:
                    realized_profit = agent_profit
                    added_pending = Decimal('0.0')
                else:
                    ratio = (advance_payment / wholesale_price) if wholesale_price > 0 else Decimal('0.0')
                    realized_profit = agent_profit * ratio
                    added_pending = agent_profit - realized_profit
            else:
                realized_profit = Decimal('0.0')
                added_pending = agent_profit

            await conn.execute("UPDATE users SET debt = debt + $1, pending_profit = COALESCE(pending_profit, 0) + $2 WHERE user_id = $3", wholesale_price, added_pending, client_id)
            
            if realized_profit > 0:
                await conn.execute("INSERT INTO agent_profits (amount, details) VALUES ($1, $2)", realized_profit, f"ربح محصل مسبقاً (رصيد دائن سحب إلكتروني): {client_name_db}")

            # 🌟 تم إزالة سطر UPDATE inventory لأن الكروت الإلكترونية تُجرد من جدول electronic_cards مباشرة
            await conn.execute("INSERT INTO transactions (user_id, type, amount, details) VALUES ($1, 'مبيعات_آجلة', $2, $3)", client_id, wholesale_price, f"سحب آلي: كرت إلكتروني {card_type}")
            await conn.execute("INSERT INTO client_sales (client_id, card_type, quantity, total_price, profit) VALUES ($1, $2, 1, $3, $4)", client_id, card_type, retail_price, client_profit)
            await conn.execute("""
                UPDATE users 
                SET pos_cash_collected = GREATEST(0, LEAST(GREATEST(0, debt), COALESCE(pos_cash_collected, 0) + $1))
                WHERE user_id = $2
            """, wholesale_price, client_id)

            if sale_type == 'credit':
                await conn.execute("""
                    INSERT INTO client_customers (client_id, customer_name, debt) 
                    VALUES ($1, $2, $3) 
                    ON CONFLICT (client_id, customer_name) 
                    DO UPDATE SET debt = client_customers.debt + $3
                """, client_id, customer_name, retail_price)
                await conn.execute("INSERT INTO client_customer_ledger (client_id, customer_name, type, amount, details) VALUES ($1, $2, 'دين', $3, $4)", client_id, customer_name, retail_price, f"شراء 1 كرت إلكتروني {card_type}")

    return {
        "status": "success", 
        "card_number": card['card_number'], 
        "client_name_db": client_name_db, 
        "wholesale_price": wholesale_price, 
        "new_total_debt": new_total_debt,
        "phone": user_info.get('phone'),
        "wa_status": user_info.get('wa_status')
    }

async def core_collect_telecom_debt(client_id: int, amount: Decimal):
    """المنطق المركزي لتحصيل ديون الرصيد والباقات"""
    if not database.pool: return {"status": "error", "message": "قاعدة البيانات غير متصلة"}
    
    if amount <= 0:
        return {"status": "error", "message": "عذراً، يجب أن يكون المبلغ أكبر من صفر."}
        
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            user_data = await conn.fetchrow("SELECT name, telecom_debt, telecom_balance FROM users WHERE user_id = $1 FOR UPDATE", client_id)
            if not user_data:
                return {"status": "error", "message": "العميل غير موجود!"}
                
            client_name = user_data['name']
            current_telecom_debt = Decimal(user_data['telecom_debt']) if user_data['telecom_debt'] else Decimal('0.0')
            
            paid_for_debt = Decimal('0.0')
            added_to_balance = Decimal('0.0')
            
            if amount <= current_telecom_debt:                           
                paid_for_debt = amount
            else:
                paid_for_debt = current_telecom_debt
                added_to_balance = amount - current_telecom_debt
            
            await conn.execute("""
                UPDATE users 
                SET telecom_debt = telecom_debt - $1,
                    telecom_balance = telecom_balance + $2,
                    telecom_pos_cash = GREATEST(0, COALESCE(telecom_pos_cash, 0) - $1)
                WHERE user_id = $3
            """, paid_for_debt, added_to_balance, client_id)

            details = f"تحصيل دفعة نقدية (شحن فوري) - سداد دين: {paid_for_debt}"
            if added_to_balance > 0:
                details += f" | رصيد مضاف: {added_to_balance}"
                
            # 🌟 توجيه الأموال لمحفظة التسديدات لمنع تسريبها لتقرير المدير
            trans_id = await conn.fetchval("""
                INSERT INTO transactions (user_id, type, amount, details, wallet_type) 
                VALUES ($1, 'تحصيل_رصيد', $2, $3, 'telecom') RETURNING id
            """, client_id, amount, details)
            
    return {
        "status": "success",
        "trans_id": trans_id,
        "client_name": client_name,
        "paid_for_debt": paid_for_debt,
        "added_to_balance": added_to_balance
    }
    
# ================= دالة مراقبة المخزون والإنذار التلقائي (مع التنبؤ) =================
async def check_and_alert_low_stock(bot, card_type: str, old_quantity: int, new_quantity: int):
    """تفحص المخزون وترسل إنذاراً للمدير والوكيل مع تنبؤ الاحتياج"""
    if "1000" in card_type or "3000" in card_type:
        threshold = 10
    else:
        threshold = 60
        
    if old_quantity > threshold and new_quantity <= threshold:
        monthly_sales = 0
        if database.pool:
            async with database.pool.acquire() as conn:
                # 🌟 إصلاح التنبؤ: استخدام البحث الدقيق لمنع تداخل الفئات المتشابهة (مثل يمن موبايل 1000 و 3000)
                txs = await conn.fetch("SELECT structured_details FROM transactions WHERE type IN ('تسليم_لعميل', 'بيع_مباشر') AND date >= CURRENT_DATE - INTERVAL '30 days' AND structured_details IS NOT NULL")
                for tx in txs:
                    try:
                        sd_list = json.loads(tx['structured_details'])
                        for item in sd_list:
                            if item.get('card_type') == card_type:
                                monthly_sales += int(item.get('quantity', 0))
                    except:
                        pass
        
        # 🌟 1. تجهيز رسالة المدير العام 🌟
        manager_suggestion = ""
        if monthly_sales > 0:
            manager_suggestion = (
                f"\n\n💡 **اقتراح الذكاء الاصطناعي للتموين:**\n"
                f"معدل سحب السوق الحالي هو ({monthly_sales}) كرت شهرياً.\n"
                f"▪️ لتموين الوكيل لـ 1 شهر: أرسل **{monthly_sales}** كرت.\n"
                f"▪️ لتموين الوكيل لـ 2 شهرين: أرسل **{monthly_sales * 2}** كرت.\n"
                f"▪️ لتموين الوكيل لـ 3 أشهر: أرسل **{monthly_sales * 3}** كرت."
            )
        else:
            manager_suggestion = "\n\n💡 (لا توجد بيانات سحب كافية لهذا الكرت خلال الشهر الماضي لحساب التنبؤ)."
            
        manager_msg = (
            f"🚨 **إنذار انخفاض مخزون تلقائي!** 🚨\n\n"
            f"📦 **الفئة:** {card_type}\n"
            f"📉 **الكمية المتبقية لدى الوكيل:** **{new_quantity} كرت فقط!**"
            f"{manager_suggestion}"
        )

        # 🌟 2. تجهيز رسالة الوكيل 🌟
        agent_suggestion = ""
        if monthly_sales > 0:
            agent_suggestion = (
                f"\n\n💡 **ملاحظة سيستم:**\n"
                f"معدل سحبك الشهري هو ({monthly_sales}) كرت.\n"
                f"يفضل أن تطلب كمية جديدة من الإدارة قريباً لتجنب توقف المبيعات."
            )
        else:
            agent_suggestion = "\n\n💡 يرجى طلب كمية جديدة من الإدارة قريباً."

        agent_msg = (
            f"🚨 **تنبيه انخفاض مخزون!** 🚨\n\n"
            f"📦 **الفئة:** {card_type}\n"
            f"📉 **الكمية المتبقية في صندوقك:** **{new_quantity} كرت فقط!**"
            f"{agent_suggestion}"
        )

        try:
            from config import NETWORK_OWNER_ID, ADMIN_ID
            # إرسال رسالة المدير
            await bot.send_message(NETWORK_OWNER_ID, manager_msg)
            
            # إرسال رسالة الوكيل
            await bot.send_message(ADMIN_ID, agent_msg)
            
            # إرسال واتساب للمدير
            from unified_main import send_whatsapp_message, GM_WA_NUMBER
            import asyncio
            wa_alert = f"🚨 *تنبيه انخفاض مخزون:*\nيا شيخ رهيب، كروت فئة *{card_type}* قاربت على النفاذ لدى الوكيل (المتبقي *{new_quantity}* كرت فقط).\n{manager_suggestion.replace('**', '*')}"
            asyncio.create_task(send_whatsapp_message(GM_WA_NUMBER, wa_alert))
            
        except Exception as e:
            print(f"Failed to send low stock alert: {e}")
            
            
def add_logo_to_excel(ws, cell_position="C1"):
    """دالة لإضافة شعار الشبكة إلى شيت الإكسل"""
    logo_path = "logo.png"
    if os.path.exists(logo_path):
        try:
            img = Image(logo_path)
            img.width = 80
            img.height = 80
            ws.add_image(img, cell_position)
        except Exception as e:
            print(f"Error adding logo to Excel: {e}")
          
            # ================= دوال التقارير والنسخ الاحتياطي (Excel) =================
def add_sheet_header(ws, title, num_cols=2):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=num_cols)
    ws.cell(row=1, column=1, value=f"الشهاب pro - {title}")
    ws.cell(row=1, column=1).font = Font(size=14, bold=True, color="FFFFFF")
    ws.cell(row=1, column=1).fill = PatternFill(start_color="002060", end_color="002060", fill_type="solid")
    ws.cell(row=1, column=1).alignment = Alignment(horizontal="center", vertical="center")

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=num_cols)
    ws.cell(row=2, column=1, value="| الوكيل: وليد مهدي")
    ws.cell(row=2, column=1).font = Font(size=12, bold=True)
    ws.cell(row=2, column=1).alignment = Alignment(horizontal="center", vertical="center")

    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=num_cols)
    ws.cell(row=3, column=1, value=f"تاريخ الإصدار: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    ws.cell(row=3, column=1).font = Font(size=11, italic=True)
    ws.cell(row=3, column=1).alignment = Alignment(horizontal="center", vertical="center")
    ws.append([]) 

def style_excel_sheet(ws, header_row=5):
    ws.sheet_view.rightToLeft = True
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border = Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))
    
    for cell in ws[header_row]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border
        
    for row in ws.iter_rows(min_row=header_row+1):
        for cell in row:
            if cell.value is not None:
                cell.alignment = center_align
                cell.border = thin_border
                
    for col_idx in range(1, ws.max_column + 1):
        column_letter = get_column_letter(col_idx)
        max_length = 0
        for row_idx in range(header_row, ws.max_row + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            try:
                if cell.value:
                    cell_length = len(str(cell.value))
                    if cell_length > max_length:
                        max_length = cell_length
            except:
                pass
        adjusted_width = (max_length + 4)
        ws.column_dimensions[column_letter].width = adjusted_width

async def generate_detailed_review_ledger(start_date: str, end_date: str) -> BytesIO:
    start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
    end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date()
    
    wb = openpyxl.Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    center_align = Alignment(horizontal="center", vertical="center")
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # --- الصفحة 1: الكروت المستلمة ---
            ws_received = wb.active
            ws_received.title = "الكروت المستلمة"
            ws_received.sheet_view.rightToLeft = True
            ws_received.append(["نوع الكرت", "الكمية المستلمة", "إجمالي القيمة (ريال)"])
            for col in range(1, 4):
                ws_received.cell(row=1, column=col).font = header_font
                ws_received.cell(row=1, column=col).fill = header_fill
                ws_received.cell(row=1, column=col).alignment = center_align
            
            # --- بداية التعديل الجديد (باستخدام Cursor لحماية الذاكرة) ---
            import json
            received_breakdown = {}
            
            async with conn.transaction(): # 🌟 المؤشرات تتطلب Transaction
                async for tx in conn.cursor("SELECT amount, details, structured_details FROM transactions WHERE type = 'استلام_من_الشبكة' AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj):
                    
                    raw_sd = tx.get('structured_details')
                    if isinstance(raw_sd, str): s_details = json.loads(raw_sd or '[]')
                    elif isinstance(raw_sd, list): s_details = raw_sd
                    else: s_details = []
                    
                    if s_details:
                        for item in s_details:
                            ctype = item.get('card_type', 'غير معروف')
                            qty = item.get('quantity', 0)
                            item_cost = Decimal(str(item.get('cost_price', 0))) * qty
                            if ctype not in received_breakdown:
                                received_breakdown[ctype] = {'qty': 0, 'amount': Decimal('0.0')}
                            received_breakdown[ctype]['qty'] += qty
                            received_breakdown[ctype]['amount'] += item_cost
                    else:
                        # دعم رجعي للعمليات القديمة
                        match = re.search(r"استلام (\d+) كرت (.+)", tx['details'])
                        if match:
                            qty = int(match.group(1))
                            ctype = match.group(2).strip()
                            if ctype not in received_breakdown:
                                received_breakdown[ctype] = {'qty': 0, 'amount': Decimal('0.0')}
                            received_breakdown[ctype]['qty'] += qty
                            received_breakdown[ctype]['amount'] += Decimal(tx['amount'])

            # --- نهاية التعديل ---
            
            for ctype, data in received_breakdown.items():
                ws_received.append([ctype, data['qty'], abs(data['amount'])])
            style_excel_sheet(ws_received, header_row=1)

            # --- الصفحة 2: حركة البقالات ---
            ws_clients = wb.create_sheet("حركة البقالات")
            ws_clients.sheet_view.rightToLeft = True
            ws_clients.append(["اسم العميل", "الرصيد السابق", "مسحوبات الفترة (كروت)", "تسديدات الفترة (كاش)", "الرصيد الحالي"])
            for col in range(1, 6):
                ws_clients.cell(row=1, column=col).font = header_font
                ws_clients.cell(row=1, column=col).fill = header_fill
                ws_clients.cell(row=1, column=col).alignment = center_align
                
            clients = await conn.fetch("SELECT user_id, name, debt FROM users WHERE role = 'client'")
            
            # 🌟 الإصلاح المحاسبي: حساب المسحوبات ناقصاً التراجعات
            taken_records = await conn.fetch("""
                SELECT user_id, 
                COALESCE(SUM(CASE WHEN type IN ('تسليم_لعميل', 'مبيعات_آجلة') THEN amount ELSE 0 END), 0) - 
                COALESCE(SUM(CASE WHEN type = 'قيد_عكسي' AND (details LIKE '%تسليم لعميل%' OR details LIKE '%مبيعات آجلة%') THEN amount ELSE 0 END), 0) as total_taken 
                FROM transactions 
                WHERE date >= $1::date AND date <= $2::date + interval '1 day'
                GROUP BY user_id
            """, start_date_obj, end_date_obj)
            taken_dict = {r['user_id']: Decimal(r['total_taken']) for r in taken_records}
            
            # 🌟 الإصلاح المحاسبي: حساب التسديدات ناقصاً التراجعات
            paid_records = await conn.fetch("""
                SELECT user_id, 
                COALESCE(SUM(CASE WHEN type IN ('تسديد_من_عميل', 'مرتجع_من_عميل') THEN amount ELSE 0 END), 0) - 
                COALESCE(SUM(CASE WHEN type = 'قيد_عكسي' AND (details LIKE '%تسديد من عميل%' OR details LIKE '%مرتجع من عميل%') THEN amount ELSE 0 END), 0) as total_paid 
                FROM transactions 
                WHERE date >= $1::date AND date <= $2::date + interval '1 day'
                GROUP BY user_id
            """, start_date_obj, end_date_obj)
            paid_dict = {r['user_id']: Decimal(r['total_paid']) for r in paid_records}
            
            for c in clients:
                uid = c['user_id']
                taken = taken_dict.get(uid, Decimal('0.0'))
                paid = paid_dict.get(uid, Decimal('0.0'))
                current_debt = Decimal(c['debt'])
                prev_debt = current_debt - taken + paid
                ws_clients.append([c['name'], abs(prev_debt), abs(taken), abs(paid), abs(current_debt)])
            style_excel_sheet(ws_clients, header_row=1)

            # --- الصفحة 3: تسديدات الإدارة ---
            ws_admin_paid = wb.create_sheet("تسديدات الإدارة")
            ws_admin_paid.sheet_view.rightToLeft = True
            ws_admin_paid.append(["التاريخ", "المبلغ (ريال)", "التفاصيل (طريقة التحويل/الشخص)"])
            for col in range(1, 4):
                ws_admin_paid.cell(row=1, column=col).font = header_font
                ws_admin_paid.cell(row=1, column=col).fill = header_fill
                ws_admin_paid.cell(row=1, column=col).alignment = center_align
                
            admin_txs = await conn.fetch("SELECT date, amount, details FROM transactions WHERE type = 'تسديد_للشبكة' AND date >= $1::date AND date <= $2::date + interval '1 day' ORDER BY date ASC", start_date_obj, end_date_obj)
            for tx in admin_txs:
                ws_admin_paid.append([str(tx['date'])[:16], abs(Decimal(tx['amount'])), tx['details']])
            style_excel_sheet(ws_admin_paid, header_row=1)

            # --- الصفحة 4: المصروفات والتوالف ---
            ws_exp = wb.create_sheet("المصروفات والتوالف")
            ws_exp.sheet_view.rightToLeft = True
            ws_exp.append(["التاريخ", "النوع", "المبلغ (ريال)", "التفاصيل"])
            for col in range(1, 5):
                ws_exp.cell(row=1, column=col).font = header_font
                ws_exp.cell(row=1, column=col).fill = header_fill
                ws_exp.cell(row=1, column=col).alignment = center_align
                
            exp_txs = await conn.fetch("SELECT date, type, amount, details FROM transactions WHERE type IN ('مصروفات', 'كروت_تالفة') AND date >= $1::date AND date <= $2::date + interval '1 day' ORDER BY date ASC", start_date_obj, end_date_obj)
            for tx in exp_txs:
                ws_exp.append([str(tx['date'])[:16], tx['type'].replace('_', ' '), abs(Decimal(tx['amount'])), tx['details']])
            style_excel_sheet(ws_exp, header_row=1)

            # --- الصفحة 5: الدفتر اليومي الشامل ---
            ws_log = wb.create_sheet("الدفتر اليومي الشامل")
            ws_log.sheet_view.rightToLeft = True
            ws_log.append(["التاريخ والوقت", "العميل/الجهة", "نوع العملية", "المبلغ", "التفاصيل"])
            for col in range(1, 6):
                ws_log.cell(row=1, column=col).font = header_font
                ws_log.cell(row=1, column=col).fill = header_fill
                ws_log.cell(row=1, column=col).alignment = center_align
                
            # 🌟 تطبيق الجدار الناري (wallet_type) مع حماية الذاكرة (Cursor)
            query_all_txs = """
                SELECT t.date, u.name, t.type, t.amount, t.details 
                FROM transactions t 
                LEFT JOIN users u ON t.user_id = u.user_id 
                WHERE t.date >= $1::date AND t.date <= $2::date + interval '1 day' 
                AND t.is_reverted = FALSE
                AND t.wallet_type = 'manager'
                ORDER BY t.date ASC
            """
          
            async with conn.transaction(): # 🌟 فتح المعاملة للمؤشر
                async for tx in conn.cursor(query_all_txs, start_date_obj, end_date_obj):
                    name = tx['name'] if tx['name'] else "الإدارة / النظام"
                    ws_log.append([str(tx['date'])[:16], name, tx['type'].replace('_', ' '), abs(Decimal(tx['amount'])), tx['details']])
                    
            style_excel_sheet(ws_log, header_row=1)

    stream = BytesIO()
    await asyncio.to_thread(wb.save, stream)
    stream.seek(0)
    return stream

async def generate_full_backup_excel() -> BytesIO:
    # 🌟 تفعيل وضع write_only لحماية السيرفر من الانهيار (OOM)
    wb = openpyxl.Workbook(write_only=True)
    if not database.pool: return BytesIO()

    # 🛡️ الدرع الواقي: دالة مساعدة لحماية الأرقام من الـ NULL لمنع انهيار النسخ الاحتياطي
    def safe_dec(val, default=0):
        return Decimal(str(val) if val is not None else str(default))

    async with database.pool.acquire() as conn:
        # 1. العملاء والديون (شامل جميع الأعمدة القديمة والجديدة)
        ws_users = wb.create_sheet("العملاء والديون")
        ws_users.append([
            "الآيدي", "الاسم", "الدور", "الدين الحالي", "الدين القديم", "الأرباح المعلقة", 
            "سقف المديونية", "المنطقة", "الواتساب", "حالة الواتساب", "الوعد بالسداد", 
            "تاريخ الانضمام", "رصيد التسديدات", "دين التسديدات", "حالة التسديدات", 
            "حالة الإلكتروني", "حالة الورقي", "سقف التسديدات", "الكاش الجاهز", "كاش التسديدات الجاهز", "VIP", 
            "خصم خاص", "حالة الحساب", "محدد السقف", "سبب الإيقاف", "نسبة الجدولة", "آخر مبيعات",
            "بصمة الهاتف", "العمال", "كلمة_المرور"
        ])
        try:
            async with conn.transaction():
                async for u in conn.cursor("SELECT * FROM users"):
                    ws_users.append([
                        str(u['user_id']), u['name'], u['role'], safe_dec(u['debt']), safe_dec(u.get('old_debt')), 
                        safe_dec(u.get('pending_profit')), safe_dec(u.get('credit_limit', 50000)), u.get('region') or '', 
                        u.get('phone') or '', u.get('wa_status') or 'off', safe_dec(u.get('promised_payment')), 
                        str(u['joined_at'])[:16] if u['joined_at'] else '', safe_dec(u.get('telecom_balance')), safe_dec(u.get('telecom_debt')),
                        u.get('telecom_status') or 'off', u.get('ecard_status') or 'off', u.get('physical_status') or 'on',
                        safe_dec(u.get('telecom_credit_limit', 20000)), safe_dec(u.get('pos_cash_collected')), safe_dec(u.get('telecom_pos_cash')),
                        str(u.get('is_vip', False)), safe_dec(u.get('special_discount')), u.get('account_status') or 'active',
                        u.get('limit_set_by') or 'agent', u.get('suspend_reason') or '', safe_dec(u.get('debt_schedule_pct')),
                        str(u.get('last_sales_log', '')),
                        u.get('device_id') or '', str(u.get('cashiers', '[]')), u.get('web_password') or '1234'
                    ])

        except Exception as e: 
            ws_users.append(["حدث خطأ:", str(e)])
            
        # 2. المخزون العام
        ws_inv = wb.create_sheet("المخزون العام")
        ws_inv.append(["نوع الكرت", "الكمية المتوفرة", "سعر التكلفة", "سعر الجملة", "سعر التجزئة", "مفعل"])
        try:
            inv = await conn.fetch("SELECT * FROM inventory")
            for i in inv:
                ws_inv.append([i['card_type'], i['quantity'], safe_dec(i.get('cost_price')), safe_dec(i['price']), safe_dec(i.get('retail_price')), str(i.get('is_active', True))])
        except Exception as e: ws_inv.append(["حدث خطأ:", str(e)])

        # 3. مخزون البقالات
        ws_client_inv = wb.create_sheet("مخزون البقالات")
        ws_client_inv.append(["رقم العميل", "نوع الكرت", "الكمية"])
        try:
            client_inv = await conn.fetch("SELECT * FROM client_inventory")
            for ci in client_inv: ws_client_inv.append([str(ci['user_id']), ci['card_type'], ci['quantity']])
        except Exception as e: ws_client_inv.append(["حدث خطأ:", str(e)])

        # 4. سجل العمليات
        ws_trans = wb.create_sheet("سجل العمليات")
        ws_trans.append(["رقم العملية", "رقم الحساب", "نوع العملية", "المبلغ", "التفاصيل", "التاريخ والوقت", "بيانات مهيكلة", "مفتاح التكرار", "ملغاة", "نوع المحفظة"])
        try:
            async with conn.transaction():
                async for t in conn.cursor("SELECT * FROM transactions ORDER BY date ASC"):
                    ws_trans.append([
                        t['id'], str(t['user_id']), t['type'], safe_dec(t['amount']), t['details'], str(t['date'])[:16] if t['date'] else '',
                        str(t.get('structured_details') or ''), t.get('idempotency_key') or '', str(t.get('is_reverted', False)), t.get('wallet_type') or 'manager'
                    ])
        except Exception as e: ws_trans.append(["حدث خطأ:", str(e)])

        # 5. أرباح الوكيل
        ws_profits = wb.create_sheet("أرباح الوكيل")
        ws_profits.append(["المعرف", "المبلغ", "التفاصيل", "التاريخ"])
        try:
            profits = await conn.fetch("SELECT * FROM agent_profits")
            for p in profits: ws_profits.append([p['id'], safe_dec(p['amount']), p['details'], str(p['date'])[:16] if p['date'] else ''])
        except Exception as e: ws_profits.append(["حدث خطأ:", str(e)])

        # 6. ديون ومبيعات الزبائن
        ws_cust = wb.create_sheet("ديون ومبيعات الزبائن")
        ws_cust.append(["--- ديون الزبائن ---", "", "", "", "", ""])
        ws_cust.append(["رقم البقالة", "اسم الزبون", "الدين (ريال)", "", "", ""])
        try:
            custs = await conn.fetch("SELECT * FROM client_customers")
            for c in custs: ws_cust.append([str(c['client_id']), c['customer_name'], safe_dec(c['debt']), "", "", ""])
        except Exception as e: ws_cust.append(["حدث خطأ:", str(e)])
        
        ws_cust.append(["--- مبيعات البقالات (POS) ---", "", "", "", "", ""])
        ws_cust.append(["رقم البقالة", "الكرت", "الكمية", "الإجمالي", "الربح", "التاريخ"])
        try:
            async with conn.transaction():
                async for s in conn.cursor("SELECT * FROM client_sales ORDER BY sale_date ASC"):
                    ws_cust.append([str(s['client_id']), s['card_type'], s['quantity'], safe_dec(s['total_price']), safe_dec(s.get('profit')), str(s['sale_date'])[:16] if s['sale_date'] else ''])
        except Exception as e: ws_cust.append(["حدث خطأ:", str(e)])
            
        ws_cust.append(["--- سجل حركات الزبائن (كشف الحساب) ---", "", "", "", "", ""])
        ws_cust.append(["رقم البقالة", "اسم الزبون", "النوع", "المبلغ", "التفاصيل", "التاريخ"])
        try:
            async with conn.transaction():
                async for l in conn.cursor("SELECT * FROM client_customer_ledger ORDER BY date ASC"):
                    ws_cust.append([str(l['client_id']), l['customer_name'], l['type'], safe_dec(l['amount']), l['details'], str(l['date'])[:16] if l['date'] else ''])
        except Exception as e: ws_cust.append(["حدث خطأ:", str(e)])

        # 7. الطلبات والإرساليات
        ws_pend = wb.create_sheet("الطلبات والإرساليات")
        ws_pend.append(["النوع", "المعرف", "الحالة", "التفاصيل/المدير", "التاريخ", "جرد الوكيل", "السعر", "المتبقي", "رقم_العميل"])
        try:
            shipments = await conn.fetch("SELECT * FROM pending_shipments")
            for s in shipments:
                ws_pend.append(["إرسالية إدارة", s['id'], s['status'], str(s['manager_items']), str(s['created_at'])[:16] if s['created_at'] else '', str(s.get('agent_items') or ''), "", "", ""])
            orders = await conn.fetch("SELECT * FROM pending_orders")
            for o in orders:
                ws_pend.append(["طلب بقالة", o['id'], o['status'], f"{o['client_name']} - {o.get('new_order_text', '')}", str(o['created_at'])[:16] if o['created_at'] else '', str(o.get('order_items', '{}')), safe_dec(o.get('total_price')), o.get('remaining_text', ''), str(o['user_id'])])
        except Exception as e: ws_pend.append(["حدث خطأ:", str(e)])

        # 8. المحادثات والإشعارات
        ws_chat = wb.create_sheet("المحادثات والإشعارات")
        ws_chat.append(["المعرف", "رقم البقالة", "المرسل", "النوع", "النص", "التاريخ", "مقروءة"])
        try:
            async with conn.transaction():
                async for ch in conn.cursor("SELECT * FROM chat_messages ORDER BY created_at ASC"):
                    ws_chat.append([ch['id'], str(ch['client_id']), ch['sender_type'], ch['message_type'], ch['message_text'], str(ch['created_at'])[:16] if ch['created_at'] else '', str(ch.get('is_read', False))])
        except Exception as e: ws_chat.append(["حدث خطأ:", str(e)])

        ws_chat.append(["--- الإشعارات ---", "", "", "", "", "", "صورة"])
        try:
            notifs = await conn.fetch("SELECT * FROM web_notifications")
            for n in notifs: ws_chat.append([n['id'], str(n['user_id']), "إشعار ويب", n['title'], n['message'], str(n['created_at'])[:16] if n['created_at'] else '', str(n.get('is_read', False)), n.get('image_file_id', '')])
        except Exception as e: ws_chat.append(["حدث خطأ:", str(e)])

        # 9. الإعدادات والذاكرة
        ws_set = wb.create_sheet("الإعدادات والذاكرة")
        ws_set.append(["المفتاح (Key)", "القيمة (Value)", ""])
        try:
            settings = await conn.fetch("SELECT * FROM settings")
            for s in settings: ws_set.append([s['key'], s['value'], ""])
            
            ws_set.append(["--- أرشيف التقارير ---", "", "", ""])
            archives = await conn.fetch("SELECT * FROM reports_archive")
            for a in archives: ws_set.append([a['month_year'], str(a['created_at'])[:16] if a['created_at'] else '', a.get('file_id', ''), ""])
            
            ws_set.append(["--- الذاكرة الدائمة (الذكاء الاصطناعي) ---", "", ""])
            facts = await conn.fetch("SELECT * FROM learned_facts")
            for f in facts: ws_set.append([str(f['added_by']), f['fact_text'], str(f['date_added'])[:16] if f['date_added'] else '', ""])
            
            ws_set.append(["--- اشتراكات الإشعارات (Push) ---", "", ""])
            push_subs = await conn.fetch("SELECT * FROM push_subscriptions")
            for ps in push_subs: ws_set.append([str(ps['user_id']), ps['subscription_json'], ""])
        except Exception as e: ws_set.append(["حدث خطأ:", str(e)])

        # 10. الكروت الإلكترونية
        ws_ecards = wb.create_sheet("الكروت الإلكترونية")
        ws_ecards.append(["المعرف", "الرقم السري", "الفئة", "الحالة", "مباع للعميل", "تاريخ البيع", "تاريخ الإضافة"])
        try:
            async with conn.transaction():
                async for e in conn.cursor("SELECT * FROM electronic_cards"):
                    sold_at_str = str(e['sold_at'])[:16] if e.get('sold_at') else ""
                    added_at_str = str(e.get('added_at', ''))[:16] if e.get('added_at') else ""
                    ws_ecards.append([e['id'], e['card_number'], e['card_type'], e['status'], str(e.get('sold_to_client', '')), sold_at_str, added_at_str])
        except Exception as e: ws_ecards.append(["حدث خطأ:", str(e)])

        # 11. بوابة التسديدات
        ws_telecom = wb.create_sheet("بوابة التسديدات")
        ws_telecom.append(["--- الباقات الثابتة ---", "", "", "", "", "", "", "", "", ""])
        ws_telecom.append(["المعرف", "الشبكة", "اسم الباقة", "التكلفة", "سعر الكاش", "سعر الآجل", "سعر الطياري", "الحالة", "", ""])
        try:
            pkgs = await conn.fetch("SELECT * FROM telecom_packages")
            for p in pkgs: 
                ws_telecom.append([p['id'], p['network'], p['package_name'], safe_dec(p['cost_price']), safe_dec(p['selling_price']), safe_dec(p.get('credit_price')), safe_dec(p.get('retail_price')), str(p.get('is_active', True)), "", ""])
        except Exception as e: ws_telecom.append(["حدث خطأ:", str(e)])

        ws_telecom.append(["--- شرائح الأرباح ---", "", "", "", "", "", "", "", "", ""])
        ws_telecom.append(["المعرف", "من مبلغ", "إلى مبلغ", "ربح الكاش", "ربح الآجل", "ربح الطياري", "", "", "", ""])
        try:
            tiers = await conn.fetch("SELECT * FROM pricing_tiers")
            for t in tiers: 
                ws_telecom.append([t['id'], safe_dec(t['min_amount']), safe_dec(t['max_amount']), safe_dec(t['cash_profit']), safe_dec(t['credit_profit']), safe_dec(t['retail_profit']), "", "", "", ""])
        except Exception as e: ws_telecom.append(["حدث خطأ:", str(e)])

        ws_telecom.append(["--- محفظة أرباح التسديدات ---", "", "", "", "", "", "", "", "", ""])
        ws_telecom.append(["المعرف", "المبلغ", "التفاصيل", "التاريخ", "", "", "", "", "", ""])
        try:
            t_profits = await conn.fetch("SELECT * FROM telecom_profits")
            for tp in t_profits: 
                ws_telecom.append([tp['id'], safe_dec(tp['amount']), tp['details'], str(tp['date'])[:16] if tp['date'] else '', "", "", "", "", "", ""])
        except Exception as e: ws_telecom.append(["حدث خطأ:", str(e)])

        ws_telecom.append(["--- سجل عمليات التسديد ---", "", "", "", "", "", "", "", "", ""])
        ws_telecom.append(["المعرف", "رقم العميل", "الشبكة", "الرقم المسدد", "الباقة", "التكلفة", "البيع", "الربح", "الحالة", "التاريخ", "المبلغ", "المرجع", "مفتاح", "من الرصيد", "للدين"])
        try:
            async with conn.transaction():
                async for tt in conn.cursor("SELECT * FROM telecom_transactions ORDER BY created_at DESC"):
                    ws_telecom.append([
                        tt['id'], str(tt['client_id']), tt['network'], tt['phone_number'], tt['package_name'], 
                        safe_dec(tt['cost_price']), safe_dec(tt['selling_price']), safe_dec(tt['profit']), tt['status'], 
                        str(tt['created_at'])[:16] if tt['created_at'] else '', safe_dec(tt.get('amount')), tt.get('provider_reference_id') or '',
                        tt.get('idempotency_key') or '', safe_dec(tt.get('paid_from_balance')), safe_dec(tt.get('added_to_debt'))
                    ])
        except Exception as e: ws_telecom.append(["حدث خطأ:", str(e)])

        # 12. دفتر الوكيل الشخصي
        ws_personal = wb.create_sheet("دفتر الوكيل الشخصي")
        ws_personal.append(["--- العملاء الشخصيون ---", "", "", "", ""])
        ws_personal.append(["المعرف", "الاسم", "الدين", "", ""])
        try:
            p_custs = await conn.fetch("SELECT * FROM agent_personal_customers")
            for pc in p_custs: ws_personal.append([pc['id'], pc['name'], safe_dec(pc['debt']), "", ""])
        except Exception as e: ws_personal.append(["حدث خطأ:", str(e)])
        
        ws_personal.append(["--- سجل الحركات الشخصية ---", "", "", "", ""])
        ws_personal.append(["المعرف", "رقم العميل", "النوع", "المبلغ", "التفاصيل", "التاريخ"])
        try:
            p_ledger = await conn.fetch("SELECT * FROM agent_personal_ledger ORDER BY date ASC")
            for pl in p_ledger: ws_personal.append([pl['id'], pl['customer_id'], pl['type'], safe_dec(pl['amount']), pl['details'], str(pl['date'])[:16] if pl['date'] else ''])
        except Exception as e: ws_personal.append(["حدث خطأ:", str(e)])

        # 13. بيانات النظام الحساسة
        ws_sys = wb.create_sheet("بيانات النظام الحساسة")
        ws_sys.append(["--- محفظة البوابة (agent_wallet) ---", ""])
        ws_sys.append(["المعرف", "رصيد التسديدات", "كاش المدير", "كاش التسديدات", "الأرباح المحصلة"])
        try:
            wallet = await conn.fetchrow("SELECT * FROM agent_wallet WHERE id = 1")
            if wallet: ws_sys.append([wallet['id'], safe_dec(wallet['telecom_balance']), safe_dec(wallet['manager_cash']), safe_dec(wallet['telecom_cash']), safe_dec(wallet['realized_profit'])])
        except Exception as e: ws_sys.append(["حدث خطأ:", str(e)])

        ws_sys.append(["--- القائمة السوداء (banned_ips) ---", ""])
        ws_sys.append(["الآيبي (IP)", "تاريخ الحظر"])
        try:
            banned = await conn.fetch("SELECT * FROM banned_ips")
            for b in banned: ws_sys.append([b['ip'], str(b['banned_at'])[:16] if b['banned_at'] else ''])
        except Exception as e: ws_sys.append(["حدث خطأ:", str(e)])

        # 14. سجل التدقيق (Audit Logs)
        ws_audit = wb.create_sheet("سجل التدقيق")
        ws_audit.append(["المعرف", "الجدول", "معرف السجل", "الإجراء", "التفاصيل", "التاريخ"])
        try:
            audits = await conn.fetch("SELECT * FROM audit_logs ORDER BY changed_at DESC")
            for a in audits: ws_audit.append([a['id'], a['table_name'], a['record_id'], a['action'], a['details'], str(a['changed_at'])[:16] if a['changed_at'] else ''])
        except Exception as e: ws_audit.append(["حدث خطأ:", str(e)])

    stream = BytesIO()
    await asyncio.to_thread(wb.save, stream)
    stream.seek(0)
    return stream

def fix_date_format(date_str: str) -> str:
    """دالة مساعدة لتصحيح صيغة التاريخ تلقائياً"""
    date_str = str(date_str).strip().replace('/', '-')
    formats = ['%Y-%m-%d', '%d-%m-%Y', '%d-%m-%y', '%Y/%m/%d']
    from datetime import datetime
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return date_str

async def generate_detailed_excel_report(start_date: str, end_date: str) -> BytesIO:
    start_date = fix_date_format(start_date)
    end_date = fix_date_format(end_date)

    from datetime import datetime
    start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
    end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date()

    wb = openpyxl.Workbook()
    
    if database.pool:
        async with database.pool.acquire() as conn:            
            inventory_items = await conn.fetch("SELECT card_type, quantity, cost_price FROM inventory")
            total_inv_value = sum((item['quantity'] * Decimal(item.get('cost_price') or 0)) for item in inventory_items)
            
                        # 🌟 الإصلاح المحاسبي الشامل: استخدام is_reverted = FALSE لضمان الدقة وتجاهل التراجعات تماماً
            received_before = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE AND date < $1::date", start_date_obj)
            paid_before = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسديد_للشبكة', 'مصروفات', 'كروت_تالفة', 'نسبة_الوكيل', 'مرتجع_للشبكة') AND is_reverted = FALSE AND date < $1::date", start_date_obj)
            previous_debt_to_network = Decimal(received_before or 0) - Decimal(paid_before or 0)

            # 🌟 تطبيق الجدار الناري (wallet_type) على كل إجماليات تقرير المدير لمنع تسرب مصروفاتك الخاصة
            received_this_month = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
            paid_this_month = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'تسديد_للشبكة' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
            damaged_cards = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'كروت_تالفة' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
            expenses = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'مصروفات' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
            agent_commission = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'نسبة_الوكيل' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
            returned_to_network = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'manager' AND type = 'مرتجع_للشبكة' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)

            received_this_month = Decimal(received_this_month or 0)
            paid_this_month = Decimal(paid_this_month or 0)
            damaged_cards = Decimal(damaged_cards or 0)
            expenses = Decimal(expenses or 0)
            agent_commission = Decimal(agent_commission or 0)
            returned_to_network = Decimal(returned_to_network or 0)
            
            current_debt_to_network = previous_debt_to_network + received_this_month - paid_this_month - damaged_cards - expenses - agent_commission - returned_to_network

            # --- بداية التعديل الجديد (فصل الأرصدة الافتتاحية عن الكروت) ---
            import json
            # 🌟 إضافة الجدار الناري (wallet_type) لتفاصيل الاستلام
            received_txs = await conn.fetch("SELECT type, amount, details, structured_details FROM transactions WHERE wallet_type = 'manager' AND type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
            
            cards_breakdown = {}
            opening_balances_total = Decimal('0.0')
            opening_balances_details = []
            total_cards_amount = Decimal('0.0')

            for tx in received_txs:
                if tx['type'] in ('رصيد_افتتاحي', 'رصيد_افتتاحي_كاش'):
                    opening_balances_total += Decimal(tx['amount'])
                    opening_balances_details.append([f"   ▪️ {tx['details']}", abs(Decimal(tx['amount']))])
                else:
                    # معالجة الكروت المستلمة فقط
                    total_cards_amount += Decimal(tx['amount'])
                    raw_sd = tx.get('structured_details')
                    if isinstance(raw_sd, str): s_details = json.loads(raw_sd or '[]')
                    elif isinstance(raw_sd, list): s_details = raw_sd
                    else: s_details = []
                    
                    if s_details:
                        for item in s_details:
                            ctype = item.get('card_type', 'غير معروف')
                            qty = item.get('quantity', 0)
                            item_cost = Decimal(str(item.get('cost_price', 0))) * qty
                            if ctype not in cards_breakdown:
                                cards_breakdown[ctype] = {'qty': 0, 'amount': Decimal('0.0')}
                            cards_breakdown[ctype]['qty'] += qty
                            cards_breakdown[ctype]['amount'] += item_cost
                    else:
                        match = re.search(r"استلام (\d+) كرت (.+)", tx['details'])
                        if match:
                            qty = int(match.group(1))
                            ctype = match.group(2).strip()
                            if ctype not in cards_breakdown:
                                cards_breakdown[ctype] = {'qty': 0, 'amount': Decimal('0.0')}
                            cards_breakdown[ctype]['qty'] += qty
                            cards_breakdown[ctype]['amount'] += Decimal(tx['amount'])
                        else:
                            ctype = tx['details']
                            if ctype not in cards_breakdown:
                                cards_breakdown[ctype] = {'qty': 0, 'amount': Decimal('0.0')}
                            cards_breakdown[ctype]['amount'] += Decimal(tx['amount'])
            # --- نهاية التعديل الجديد ---

            # 🌟 إضافة الجدار الناري (wallet_type) لتفاصيل الدفعات والمصروفات
            paid_txs = await conn.fetch("SELECT amount, details FROM transactions WHERE wallet_type = 'manager' AND type = 'تسديد_للشبكة' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
            expense_txs = await conn.fetch("SELECT amount, details FROM transactions WHERE wallet_type = 'manager' AND type = 'مصروفات' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)

            ws_summary = wb.active
            ws_summary.title = "الخلاصة الشهرية"
            
            add_sheet_header(ws_summary, "التقرير المحاسبي الشامل", num_cols=2)
            add_logo_to_excel(ws_summary, "B1")
            ws_summary.append(["البند", "المبلغ (ريال)"])
            
            ws_summary.append(["المبلغ المتبقي من الفترة السابقة", abs(previous_debt_to_network)])
            
            # 🌟 قسم الأرصدة الافتتاحية (الجديد)
            ws_summary.append(["الأرصدة الافتتاحية (ديون سابقة مرحلة):", ""])
            if opening_balances_total > 0:
                for detail_row in opening_balances_details:
                    ws_summary.append(detail_row)
                ws_summary.append(["إجمالي الأرصدة الافتتاحية", abs(opening_balances_total)])
            else:
                ws_summary.append(["   ▪️ لا توجد أرصدة افتتاحية", 0])

            # 🌟 قسم الكروت المستلمة
            ws_summary.append(["الكروت المستلمة هذه الفترة:", ""])
            if cards_breakdown:
                for ctype, data in cards_breakdown.items():
                    if data['qty'] > 0:
                        ws_summary.append([f"   ▪️ {ctype} | العدد: {data['qty']}", abs(data['amount'])])
                    else:
                        ws_summary.append([f"   ▪️ {ctype}", abs(data['amount'])])
                ws_summary.append(["إجمالي الكروت المستلمة", abs(total_cards_amount)])
            else:
                ws_summary.append(["   ▪️ لا توجد كروت مستلمة", 0])
                ws_summary.append(["إجمالي الكروت المستلمة", 0])
            
            # إجمالي المضاف لحساب الإدارة
            ws_summary.append(["إجمالي المضاف لحساب الإدارة (كروت + أرصدة)", abs(Decimal(received_this_month or 0))])

            ws_summary.append(["الدفعات المسددة للإدارة:", ""])
            if paid_txs:
                for tx in paid_txs:
                    ws_summary.append([f"   ▪️ {tx['details']}", abs(Decimal(tx['amount']))])
            else:
                ws_summary.append(["   ▪️ لا توجد دفعات مسجلة", 0])
            ws_summary.append(["إجمالي الدفعات المسددة", abs(Decimal(paid_this_month or 0))])
            
            ws_summary.append(["المصروفات والحوالات:", ""])
            if expense_txs:
                for tx in expense_txs:
                    ws_summary.append([f"   ▪️ {tx['details']}", abs(Decimal(tx['amount']))])
            else:
                ws_summary.append(["   ▪️ لا توجد مصروفات مسجلة", 0])
            ws_summary.append(["إجمالي المصروفات", abs(Decimal(expenses or 0))])
            
            ws_summary.append(["كروت تالفة", abs(Decimal(damaged_cards or 0))])
            if Decimal(agent_commission or 0) > 0:
                ws_summary.append(["مستحقات الوكيل", abs(Decimal(agent_commission or 0))])

            ws_summary.append(["مرتجع للإدارة", abs(Decimal(returned_to_network or 0))])            
            ws_summary.append(["المتبقي حالياً للإدارة (الصافي)", abs(current_debt_to_network)])
            
            ws_summary.append(["", ""]) 
            
            total_clients_debt = await conn.fetchval("SELECT COALESCE(SUM(debt), 0) FROM users WHERE role = 'client'")
            pending_profits = await conn.fetchval("SELECT COALESCE(SUM(pending_profit), 0) FROM users WHERE role = 'client'")
            
            net_market_debt = Decimal(total_clients_debt or 0) - Decimal(pending_profits or 0)
            net_manager_cash = abs(current_debt_to_network) - abs(total_inv_value) - abs(net_market_debt)
            net_manager_cash = max(Decimal('0.0'), net_manager_cash)

            ws_summary.append(["تفاصيل المبلغ المتبقي للإدارة", ""])
            ws_summary.append(["منها: إجمالي الكروت الحالية في المخزون", abs(total_inv_value)])
            ws_summary.append(["منها: إجمالي ديون العملاء بالسوق (الصافي)", abs(net_market_debt)])
            ws_summary.append(["ومنها: السيولة النقدية المتوفرة (الصافي)", abs(net_manager_cash)])
            
            style_excel_sheet(ws_summary, header_row=5)

            # 🎨 تعريف الألوان الاحترافية
            title_fill = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid") # رمادي فاتح لعناوين الأقسام
            subtotal_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid") # أخضر فاتح للإجماليات الفرعية
            grand_total_fill = PatternFill(start_color="A9D08E", end_color="A9D08E", fill_type="solid") # أخضر قوي للصافي النهائي
            
            bold_font = Font(bold=True, color="000000")

            # 🧠 التلوين الذكي للتقرير
            for row_idx in range(6, ws_summary.max_row + 1):
                cell_value = str(ws_summary.cell(row=row_idx, column=1).value or "").strip()
                
                # 1. تلوين الصافي النهائي (أهم رقم)
                if "المتبقي حالياً للإدارة (الصافي)" in cell_value:
                    for col in range(1, 3):
                        ws_summary.cell(row=row_idx, column=col).fill = grand_total_fill
                        ws_summary.cell(row=row_idx, column=col).font = bold_font
                
                # 2. تلوين الإجماليات الفرعية (أي سطر يحتوي على كلمة إجمالي أو المبلغ المتبقي)
                elif ("إجمالي" in cell_value or "المبلغ المتبقي من الفترة السابقة" in cell_value) and "منها:" not in cell_value:
                    for col in range(1, 3):
                        ws_summary.cell(row=row_idx, column=col).fill = subtotal_fill
                        ws_summary.cell(row=row_idx, column=col).font = bold_font
                
                # 3. تلوين عناوين الأقسام (أي سطر ينتهي بنقطتين : )
                elif cell_value.endswith(":") and "منها:" not in cell_value:
                    for col in range(1, 3):
                        ws_summary.cell(row=row_idx, column=col).fill = title_fill
                        ws_summary.cell(row=row_idx, column=col).font = bold_font
                        
            # 🔢 تنسيق الأرقام بفاصلة الآلاف (مثال: 253,300 بدلاً من 253300)
            for row_idx in range(6, ws_summary.max_row + 1):
                cell = ws_summary.cell(row=row_idx, column=2)
                if isinstance(cell.value, (int, float, Decimal)):
                    cell.number_format = '#,##0'

            ws_inv = wb.create_sheet("تفاصيل المخزون")
            add_sheet_header(ws_inv, "جرد المخزون", num_cols=4)
            add_logo_to_excel(ws_inv, "B1")            
            ws_inv.append(["نوع الكرت", "العدد المتوفر", "سعر التكلفة", "إجمالي القيمة"])
            for item in inventory_items:
                cost_p = Decimal(item.get('cost_price') or 0)
                val = item['quantity'] * cost_p
                ws_inv.append([item['card_type'], item['quantity'], abs(cost_p), abs(val)])
                
            for cell in ws_inv[ws_inv.max_row]:
                if isinstance(cell.value, (int, float, Decimal)): cell.number_format = '#,##0'
                
            ws_inv.append(["إجمالي قيمة المخزون:", "", "", abs(total_inv_value)])
            for cell in ws_inv[ws_inv.max_row]:
                if isinstance(cell.value, (int, float, Decimal)): cell.number_format = '#,##0'
                
            style_excel_sheet(ws_inv, header_row=5)

            ws_clients = wb.create_sheet("ديون العملاء")
            add_sheet_header(ws_clients, "ديون السوق", num_cols=3)
            add_logo_to_excel(ws_clients, "C1")
            ws_clients.append(["اسم العميل", "الدين الحالي (الصافي)", "رقم الواتساب"])
            
            clients = await conn.fetch("SELECT name, debt, pending_profit, phone FROM users WHERE role = 'client'")
            for c in clients:
                phone_num = c['phone'] if c['phone'] and str(c['phone']).lower() != 'none' else "لا يوجد"
                net_client_debt = Decimal(c['debt']) - Decimal(c['pending_profit'] or 0)
                ws_clients.append([c['name'], abs(net_client_debt), phone_num])
                
            style_excel_sheet(ws_clients, header_row=5)

    excel_stream = BytesIO()
    import asyncio
    await asyncio.to_thread(wb.save, excel_stream) # 🌟 الإصلاح: حفظ في الخلفية لمنع تجميد البوت
    excel_stream.seek(0)
    return excel_stream

async def generate_telecom_excel_report(start_date: str, end_date: str) -> BytesIO:
    """توليد تقرير إكسل سري خاص بالوكيل لعمليات التسديدات والشحن"""
    start_date = fix_date_format(start_date)
    end_date = fix_date_format(end_date)

    from datetime import datetime
    start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
    end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "تقرير التسديدات السري"
    ws.sheet_view.rightToLeft = True

    add_sheet_header(ws, "تقرير التسديدات والشحن (خاص بالوكيل)", num_cols=4)

    if database.pool:
        async with database.pool.acquire() as conn:
        
            # 🌟 الإصلاح المحاسبي: استخدام is_reverted = FALSE
            total_sales = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسديد_باقة', 'تسديد_باقة_وكيل') AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
            
            raw_profit = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM telecom_profits WHERE amount > 0 AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
            rev_profit = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM telecom_profits WHERE amount < 0 AND details LIKE '%إلغاء%' AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
            total_profit = Decimal(raw_profit or 0) - abs(Decimal(rev_profit or 0))

            # 🌟 جلب البيانات من محفظة التسديدات بدقة
            total_collected = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'telecom' AND type = 'تحصيل_رصيد' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)
            total_recharged = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE wallet_type = 'telecom' AND type = 'تغذية_رصيد_بوابة' AND is_reverted = FALSE AND date >= $1::date AND date <= $2::date + interval '1 day'", start_date_obj, end_date_obj)

            # 🌟 قراءة الرصيد الحالي من المحفظة الجديدة (agent_wallet)
            current_balance = await conn.fetchval("SELECT telecom_balance FROM agent_wallet WHERE id = 1")
            current_balance = Decimal(current_balance) if current_balance else Decimal('0.0')

            ws.append(["--- ملخص الفترة ---", "", "", ""])
            ws.append(["إجمالي المبيعات (شحن وباقات)", abs(Decimal(total_sales or 0)), "", ""])
            ws.append(["إجمالي الأرباح الصافية", abs(Decimal(total_profit or 0)), "", ""])
            ws.append(["إجمالي تحصيلات ديون الرصيد", abs(Decimal(total_collected or 0)), "", ""])
            ws.append(["إجمالي تغذية البوابة (من الصندوق)", abs(Decimal(total_recharged or 0)), "", ""])
            ws.append(["الرصيد الحالي المتبقي في البوابة", abs(current_balance), "", ""])
            ws.append(["", "", "", ""])

            ws.append(["--- تفاصيل مبيعات التسديدات ---", "", "", ""])
            ws.append(["التاريخ", "العميل", "المبلغ (ريال)", "التفاصيل"])
            
            # 🌟 جلب مبيعات التسديدات من المحفظة الخاصة بك
            sales_txs = await conn.fetch("""
                SELECT t.date, u.name, t.amount, t.details 
                FROM transactions t 
                LEFT JOIN users u ON t.user_id = u.user_id 
                WHERE t.wallet_type = 'telecom' AND t.type IN ('تسديد_باقة', 'تسديد_باقة_وكيل', 'استرداد_تسديد') 
                AND t.is_reverted = FALSE 
                AND t.date >= $1::date AND t.date <= $2::date + interval '1 day'
                ORDER BY t.date DESC
            """, start_date_obj, end_date_obj)

            for tx in sales_txs:
                c_name = tx['name'] if tx['name'] else "الوكيل"
                ws.append([str(tx['date'])[:16], c_name, abs(Decimal(tx['amount'])), tx['details']])

    style_excel_sheet(ws, header_row=8)

    excel_stream = BytesIO()
    await asyncio.to_thread(wb.save, excel_stream)
    excel_stream.seek(0)
    return excel_stream
    
async def get_dashboard_stats(is_owner=False):
    """جلب إحصائيات وتنبيهات لوحة التحكم مع مراعاة دور المستخدم"""
    alerts_text = ""
    available_cash = Decimal('0.0')
    today_sales = Decimal('0.0')
    
    import database # التأكد من استدعاء قاعدة البيانات
    
    if database.pool:
        async with database.pool.acquire() as conn:
            # 1. التنبيهات
            promises = await conn.fetch("SELECT name, promised_payment FROM users WHERE promised_payment > 0 LIMIT 3")
            empty_inv = await conn.fetch("SELECT u.name, ci.card_type FROM client_inventory ci JOIN users u ON ci.user_id = u.user_id WHERE ci.quantity = 0 LIMIT 3")
            
            if empty_inv:
                for e in empty_inv: alerts_text += f"🔻 بقالة ({e['name']}) مخزونه صفر من {e['card_type']}.\n"
            if promises:
                for p in promises: alerts_text += f"💸 بقالة ({p['name']}) وعدت بسداد {p['promised_payment']} ريال.\n"
            
            # 2. الإحصائيات
            # 🌟 التعديل: استخدام المحرك المالي الجديد لجلب الإحصائيات
            engine = FinancialEngine(database.pool)
            fin_stats = await engine.get_summary(conn)
            
            if is_owner:
                # كاش المدير = الكاش الكلي - أرباح الوكيل
                available_cash = max(Decimal('0.0'), fin_stats["cash"] - fin_stats["realized"])
                
                costs_data = await conn.fetch("SELECT card_type, cost_price FROM inventory")
                costs_dict = {row['card_type']: Decimal(row['cost_price']) for row in costs_data}
                
                import json
                import re
                # جلب المبيعات لحساب التكلفة
                txs = await conn.fetch("SELECT details, structured_details FROM transactions WHERE type IN ('تسليم_لعميل', 'بيع_مباشر', 'مبيعات_آجلة') AND date >= CURRENT_DATE")
                today_cost_sales = Decimal('0.0')
                
                for tx in txs:
                    raw_sd = tx.get('structured_details')
                    if isinstance(raw_sd, str): s_details = json.loads(raw_sd or '[]')
                    elif isinstance(raw_sd, list): s_details = raw_sd
                    else: s_details = []
                    
                    if s_details:
                        for item in s_details:
                            today_cost_sales += Decimal(str(item.get('cost_price', 0))) * item.get('quantity', 0)
                    else:
                        # دعم رجعي للعمليات القديمة
                        parts = tx['details'].split('+')
                        for part in parts:
                            match = re.search(r"(\d+)\s*كرت\s*([^\(]+)", part)
                            if match:
                                qty = int(match.group(1))
                                ctype = match.group(2).replace("كاش", "").replace("طياري", "").strip()
                                cost = costs_dict.get(ctype)
                                if cost: today_cost_sales += cost * qty
                            else:
                                match_ecard = re.search(r"سحب آلي:\s*كرت إلكتروني\s*(.+)", part)
                                if match_ecard:
                                    ctype = match_ecard.group(1).strip()
                                    cost = costs_dict.get(ctype)
                                    if cost: 
                                        today_cost_sales += cost * 1
                                
                today_sales = today_cost_sales
            else:
                # كاش الوكيل = كاش الشبكة + كاش التسديدات
                available_cash = fin_stats["cash"] + fin_stats.get("telecom", Decimal('0.0'))
                today_sales = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسليم_لعميل', 'بيع_مباشر') AND date >= CURRENT_DATE")
                    
    if not alerts_text: alerts_text = "✅ الأمور طيبة، لا توجد تنبيهات عاجلة حالياً.\n"
    
    return alerts_text, int(available_cash), int(today_sales or 0)
    
    # --- 10. الإغلاق السنوي والترحيل (Annual Closing) ---
    async def execute_annual_closing(self, previous_year: int):
        cutoff_date = f"{previous_year + 1}-01-01"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. حساب صافي حساب المدير قبل الحذف لترحيله
                received = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('استلام_من_الشبكة', 'رصيد_افتتاحي', 'رصيد_افتتاحي_كاش') AND is_reverted = FALSE AND date < $1::date", cutoff_date)
                paid = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type IN ('تسديد_للشبكة', 'مصروفات', 'كروت_تالفة', 'نسبة_الوكيل', 'مرتجع_للشبكة') AND is_reverted = FALSE AND date < $1::date", cutoff_date)
                manager_net_capital = Decimal(received or 0) - Decimal(paid or 0)

                # 2. إيقاف الزنادات (Triggers) لمنع تصفير المحفظة عند الحذف
                await conn.execute("ALTER TABLE agent_profits DISABLE TRIGGER ALL;")
                await conn.execute("ALTER TABLE transactions DISABLE TRIGGER ALL;")

                # 3. الحذف العميق (Deep Archiving)
                await conn.execute("DELETE FROM transactions WHERE date < $1::date", cutoff_date)
                await conn.execute("DELETE FROM telecom_transactions WHERE created_at < $1::date", cutoff_date)
                await conn.execute("DELETE FROM client_sales WHERE sale_date < $1::date", cutoff_date)
                await conn.execute("DELETE FROM client_customer_ledger WHERE date < $1::date", cutoff_date)
                await conn.execute("DELETE FROM agent_profits WHERE date < $1::date", cutoff_date)
                await conn.execute("DELETE FROM telecom_profits WHERE date < $1::date", cutoff_date)

                # 4. إعادة تشغيل الزنادات
                await conn.execute("ALTER TABLE agent_profits ENABLE TRIGGER ALL;")
                await conn.execute("ALTER TABLE transactions ENABLE TRIGGER ALL;")

                # 5. ترحيل رصيد المدير كـ "رصيد افتتاحي" للعام الجديد
                if manager_net_capital > 0:
                    await conn.execute("INSERT INTO transactions (user_id, type, amount, details, date, wallet_type) VALUES (0, 'رصيد_افتتاحي', $1, $2, $3::date, 'manager')", manager_net_capital, f"رصيد افتتاحي مرحل من عام {previous_year}", cutoff_date)
                elif manager_net_capital < 0:
                    await conn.execute("INSERT INTO transactions (user_id, type, amount, details, date, wallet_type) VALUES (0, 'تسديد_للشبكة', $1, $2, $3::date, 'manager')", abs(manager_net_capital), f"عجز مرحل من عام {previous_year}", cutoff_date)

        return {"status": "success", "message": "تم الإغلاق السنوي وترحيل الأرصدة بنجاح."}

async def core_rename_card_type(old_name: str, new_name: str):
    """دالة لتغيير اسم فئة الكرت في جميع الجداول والمخازن"""
    if not database.pool: return {"status": "error", "message": "قاعدة البيانات غير متصلة"}
    
    async with database.pool.acquire() as conn:
        # 1. التحقق من أن الاسم الجديد غير موجود مسبقاً لمنع التداخل
        exists = await conn.fetchval("SELECT 1 FROM inventory WHERE card_type = $1", new_name)
        if exists:
            return {"status": "error", "message": f"الاسم الجديد ({new_name}) موجود مسبقاً في المخزون!"}
        
        async with conn.transaction():
            # 2. تحديث الاسم في المخزون العام
            await conn.execute("UPDATE inventory SET card_type = $1 WHERE card_type = $2", new_name, old_name)
            
            # 3. تحديث الاسم في مخزون البقالات
            await conn.execute("UPDATE client_inventory SET card_type = $1 WHERE card_type = $2", new_name, old_name)
            
            # 4. تحديث الاسم في الخزنة (الكروت الإلكترونية)
            await conn.execute("UPDATE electronic_cards SET card_type = $1 WHERE card_type = $2", new_name, old_name)
            
            # 5. تحديث الاسم في سجلات مبيعات البقالات (POS)
            await conn.execute("UPDATE client_sales SET card_type = $1 WHERE card_type = $2", new_name, old_name)
            
            # ملاحظة محاسبية: لا نغير الاسم في جدول (transactions) لكي تبقى الفواتير والسجلات القديمة مطابقة لما تم إصداره وقتها.
            
    return {"status": "success"}

# =====================================================================
# دوال التغليف (Wrappers) لربط البوت بالمحرك المالي الجديد
# =====================================================================
async def get_financial_summary():
    engine = FinancialEngine(database.pool)
    return await engine.get_summary()

async def core_give_cards(client_id: int, card_type: str, qty: int, override_limit: bool = False):
    engine = FinancialEngine(database.pool)
    try:
        return await engine.give_cards(client_id, card_type, qty, override=override_limit)
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def core_collect_debt(client_id: int, amount: Decimal, bypass_shortage: bool = False):
    engine = FinancialEngine(database.pool)
    try:
        return await engine.collect_debt(client_id, amount, bypass_shortage)
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def core_direct_sale(card_type: str, qty: int, sale_mode: str, collected_cash: Decimal, discount: Decimal = Decimal('0.0')):
    engine = FinancialEngine(database.pool)
    try:
        return await engine.direct_sale(card_type, qty, collected_cash, discount, sale_mode)
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def core_finance_action(action_type_str: str, amount: Decimal, details: str, source: str = ""):
    engine = FinancialEngine(database.pool)
    # تحويل النص إلى Enum
    mapping = {
        'expense': TxType.EXPENSE,
        'pay_manager': TxType.PAY_MANAGER,
        'agent_commission': TxType.AGENT_COMMISSION,
        'withdraw_profit': TxType.WITHDRAW_PROFIT,
        'telecom_topup': TxType.TELECOM_TOPUP,
        'withdraw_telecom_profit': TxType.WITHDRAW_TELECOM_PROFIT, # 🌟 إضافة سحب أرباح التسديدات
        'add_telecom_capital': TxType.ADD_TELECOM_CAPITAL # 🌟 إضافة إيداع رأس مال التسديدات
    }
    tx_enum = mapping.get(action_type_str, TxType.EXPENSE)
    try:
        return await engine.finance_action(tx_enum, amount, details, source)
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def core_bulk_give_cards(client_id: int, suggested_order: dict, override_limit: bool = False):
    engine = FinancialEngine(database.pool)
    try:
        return await engine.bulk_give_cards(client_id, suggested_order, override=override_limit)
    except CreditLimitExceededError as e:
        return {"status": "error", "requires_override": True, "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def core_transfer_center(sender: str, receiver: str, t_type: str, amount: Decimal, card_type: str = None):
    engine = FinancialEngine(database.pool)
    s_type, s_id = ("agent", 0) if sender == "agent" else ("manager", 0) if sender == "manager" else ("client", int(sender.replace("client_", "")))
    r_type, r_id = ("agent", 0) if receiver == "agent" else ("manager", 0) if receiver == "manager" else ("client", int(receiver.replace("client_", "")))
    try:
        return await engine.transfer_assets(s_type, s_id, r_type, r_id, t_type, amount, card_type)
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def core_revert_transaction(tx_id: int):
    engine = FinancialEngine(database.pool)
    try:
        return await engine.revert_transaction(tx_id)
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def core_record_client_sale(client_id: int, card_type: str, expected_qty: int, actual_remaining_qty: int):
    engine = FinancialEngine(database.pool)
    try:
        return await engine.record_client_sale(client_id, card_type, expected_qty, actual_remaining_qty)
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def core_close_client_account(client_id: int):
    engine = FinancialEngine(database.pool)
    try:
        return await engine.close_account(client_id)
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def core_process_ai_daily_entries(client_id: int, new_txs: list):
    engine = FinancialEngine(database.pool)
    try:
        return await engine.process_ai_daily_entries(client_id, new_txs)
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def core_pre_give_cards_check(client_id: int):
    engine = FinancialEngine(database.pool)
    try:
        return await engine.pre_give_cards_check(client_id)
    except Exception as e:
        return {"status": "error", "message": str(e)}

# 🌟 دالة تغليف الإغلاق السنوي
async def core_execute_annual_closing(previous_year: int):
    engine = FinancialEngine(database.pool)
    try:
        return await engine.execute_annual_closing(previous_year)
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =====================================================================
# مهام الخلفية (Background Tasks)
# =====================================================================
async def cleanup_idempotency_keys(pool):
    """مهمة تعمل في الخلفية لتنظيف مفاتيح الحماية القديمة ومنع امتلاء الذاكرة"""
    import asyncio
    while True:
        try:
            async with pool.acquire() as conn:
                # حذف المفاتيح التي مر عليها أكثر من 24 ساعة
                result = await conn.execute("DELETE FROM idempotency_keys WHERE created_at < NOW() - INTERVAL '24 hours'")
                logger.info(f"🧹 تم تنظيف مفاتيح الحماية القديمة: {result}")
        except Exception as e:
            # نتجاهل خطأ عدم وجود العمود مؤقتاً حتى تقوم بإضافته
            if "column \"created_at\" does not exist" not in str(e):
                logger.error(f"خطأ في تنظيف مفاتيح الحماية: {e}")

        # تشغيل التنظيف كل 6 ساعات (6 * 3600 ثانية)
        await asyncio.sleep(21600) 
