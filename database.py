import asyncpg
from decimal import Decimal
from config import DATABASE_URL
import logging

# متغير لحفظ الاتصال بقاعدة البيانات
pool = None

async def init_db():
    global pool
    # 🌟 الإصلاح الأمني (منع تسريب الذاكرة): التأكد من عدم إنشاء أكثر من Pool واحد
    if pool is not None:
        return 
        
    try:
        # 🌟 إعدادات المرونة (Resilience) وإجبار التوقيت المحلي
        pool = await asyncpg.create_pool(
            DATABASE_URL, 
            statement_cache_size=0,
            max_inactive_connection_lifetime=300, # إعادة الاتصال إذا خمل لمدة 5 دقائق
            command_timeout=60, # منع تعليق الأوامر
            min_size=1,
            max_size=10,
            server_settings={'timezone': 'Asia/Aden'} # 🌟 الدرع الزمني
        )
        
        async with pool.acquire() as conn:

                        # ==========================================
            # 1. إنشاء الجداول الأساسية وسجل التدقيق
            # ==========================================
            schema_query = """
            CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, name TEXT, role TEXT, debt NUMERIC DEFAULT 0.0, joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS transactions (id SERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(user_id), type TEXT, amount NUMERIC, details TEXT, structured_details JSONB, idempotency_key VARCHAR(100) UNIQUE, is_reverted BOOLEAN DEFAULT FALSE, date TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS inventory (card_type TEXT PRIMARY KEY, quantity INTEGER DEFAULT 0, price NUMERIC);
            CREATE TABLE IF NOT EXISTS client_inventory (user_id BIGINT REFERENCES users(user_id), card_type TEXT, quantity INTEGER DEFAULT 0, PRIMARY KEY (user_id, card_type));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS learned_facts (id SERIAL PRIMARY KEY, fact_text TEXT, added_by BIGINT, date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS agent_profits (id SERIAL PRIMARY KEY, amount NUMERIC, details TEXT, date TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS reports_archive (id SERIAL PRIMARY KEY, month_year TEXT UNIQUE, file_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS pending_orders (id SERIAL PRIMARY KEY, user_id BIGINT, client_name TEXT, status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS pending_shipments (id SERIAL PRIMARY KEY, status VARCHAR(20) DEFAULT 'pending', manager_items JSONB, agent_items JSONB, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS push_subscriptions (user_id BIGINT PRIMARY KEY, subscription_json TEXT);
            CREATE TABLE IF NOT EXISTS chat_messages (id SERIAL PRIMARY KEY, client_id BIGINT NOT NULL, sender_type VARCHAR(20) NOT NULL, message_type VARCHAR(20) NOT NULL, message_text TEXT NOT NULL, is_read BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS client_customers (id SERIAL PRIMARY KEY, client_id BIGINT NOT NULL, customer_name VARCHAR(100) NOT NULL, debt DECIMAL DEFAULT 0.0, UNIQUE(client_id, customer_name));
            CREATE TABLE IF NOT EXISTS client_customer_ledger (id SERIAL PRIMARY KEY, client_id BIGINT NOT NULL, customer_name VARCHAR(100) NOT NULL, type VARCHAR(50) NOT NULL, amount DECIMAL NOT NULL, details TEXT, date TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS client_sales (id SERIAL PRIMARY KEY, client_id BIGINT NOT NULL, card_type VARCHAR(50) NOT NULL, quantity INT NOT NULL, total_price DECIMAL NOT NULL, sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS web_notifications (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, title VARCHAR(100), message TEXT, image_file_id VARCHAR(255), is_read BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS telecom_packages (id SERIAL PRIMARY KEY, network VARCHAR(50) NOT NULL, package_name VARCHAR(255) NOT NULL, cost_price NUMERIC NOT NULL, selling_price NUMERIC NOT NULL, is_active BOOLEAN DEFAULT TRUE);
            CREATE TABLE IF NOT EXISTS telecom_transactions (id SERIAL PRIMARY KEY, client_id BIGINT REFERENCES users(user_id), network VARCHAR(50), phone_number VARCHAR(20), package_name VARCHAR(255), amount NUMERIC, cost_price NUMERIC, selling_price NUMERIC, profit NUMERIC, status VARCHAR(20) DEFAULT 'pending', provider_reference_id VARCHAR(100), idempotency_key VARCHAR(100) UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS electronic_cards (id SERIAL PRIMARY KEY, card_type VARCHAR(50) NOT NULL, card_number TEXT NOT NULL, status VARCHAR(20) DEFAULT 'available', sold_to_client BIGINT DEFAULT 0, sold_at TIMESTAMP, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS telecom_profits (id SERIAL PRIMARY KEY, amount NUMERIC, details TEXT, date TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS pricing_tiers (id SERIAL PRIMARY KEY, min_amount NUMERIC NOT NULL, max_amount NUMERIC NOT NULL, cash_profit NUMERIC NOT NULL, credit_profit NUMERIC NOT NULL, retail_profit NUMERIC NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_personal_customers (id SERIAL PRIMARY KEY, name VARCHAR(100) UNIQUE, debt DECIMAL DEFAULT 0.0);
            CREATE TABLE IF NOT EXISTS agent_personal_ledger (id SERIAL PRIMARY KEY, customer_id INT REFERENCES agent_personal_customers(id), type VARCHAR(50), amount DECIMAL, details TEXT, date TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            
            -- 🌟 [جديد] جدول محفظة الوكيل (لفصل رصيد البوابة عن حساب المدير والإعدادات)
            CREATE TABLE IF NOT EXISTS agent_wallet (id INT PRIMARY KEY DEFAULT 1, telecom_balance NUMERIC DEFAULT 0.0);
            
            -- 🌟 [جديد] عداد أرقام عملاء الأوفلاين لمنع قفل الجدول
            CREATE SEQUENCE IF NOT EXISTS offline_users_seq START 9990000;
            
            -- 🌟 [جديد] جدول مفاتيح الحماية السريع (لمنع النقر المزدوج بدون إبطاء النظام)
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key VARCHAR(100) PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_idempotency_created ON idempotency_keys(created_at);

            CREATE TABLE IF NOT EXISTS banned_ips (ip VARCHAR(50) PRIMARY KEY, banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);

           
            -- ==========================================
            -- 🌟 [جديد] الصندوق الأسود (Event Sourcing & Snapshots)
            -- ==========================================
            CREATE TABLE IF NOT EXISTS system_blackbox (
                id SERIAL PRIMARY KEY,
                tx_id INT,
                action_type VARCHAR(100),
                client_id BIGINT,
                amount NUMERIC,
                state_before JSONB,
                state_after JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_blackbox_tx ON system_blackbox(tx_id);
            CREATE INDEX IF NOT EXISTS idx_blackbox_client ON system_blackbox(client_id);

            -- ==========================================
            -- 🌟 [جديد] نظام سجل التدقيق (Audit Trail) لمراقبة التعديلات الحساسة
            -- ==========================================
            CREATE TABLE IF NOT EXISTS audit_logs (
                id SERIAL PRIMARY KEY,
                table_name VARCHAR(50),
                record_id VARCHAR(100),
                action VARCHAR(100),
                details TEXT,
                changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 1. مراقبة تغييرات العملاء (سقف المديونية)
            CREATE OR REPLACE FUNCTION audit_users_changes() RETURNS TRIGGER AS $$
            BEGIN
                IF OLD.credit_limit IS DISTINCT FROM NEW.credit_limit THEN
                    INSERT INTO audit_logs (table_name, record_id, action, details)
                    VALUES ('users', NEW.user_id::text, 'تغيير سقف ديون الكروت', 'من ' || COALESCE(OLD.credit_limit::text, '0') || ' إلى ' || COALESCE(NEW.credit_limit::text, '0'));
                END IF;
                IF OLD.telecom_credit_limit IS DISTINCT FROM NEW.telecom_credit_limit THEN
                    INSERT INTO audit_logs (table_name, record_id, action, details)
                    VALUES ('users', NEW.user_id::text, 'تغيير سقف ديون التسديدات', 'من ' || COALESCE(OLD.telecom_credit_limit::text, '0') || ' إلى ' || COALESCE(NEW.telecom_credit_limit::text, '0'));
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trigger_audit_users ON users;
            CREATE TRIGGER trigger_audit_users
            AFTER UPDATE ON users
            FOR EACH ROW EXECUTE FUNCTION audit_users_changes();

            -- 2. مراقبة تغييرات المخزون (الأسعار)
            CREATE OR REPLACE FUNCTION audit_inventory_changes() RETURNS TRIGGER AS $$
            BEGIN
                IF OLD.price IS DISTINCT FROM NEW.price THEN
                    INSERT INTO audit_logs (table_name, record_id, action, details)
                    VALUES ('inventory', NEW.card_type, 'تغيير سعر الجملة', 'من ' || COALESCE(OLD.price::text, '0') || ' إلى ' || COALESCE(NEW.price::text, '0'));
                END IF;
                IF OLD.cost_price IS DISTINCT FROM NEW.cost_price THEN
                    INSERT INTO audit_logs (table_name, record_id, action, details)
                    VALUES ('inventory', NEW.card_type, 'تغيير سعر التكلفة', 'من ' || COALESCE(OLD.cost_price::text, '0') || ' إلى ' || COALESCE(NEW.cost_price::text, '0'));
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trigger_audit_inventory ON inventory;
            CREATE TRIGGER trigger_audit_inventory
            AFTER UPDATE ON inventory
            FOR EACH ROW EXECUTE FUNCTION audit_inventory_changes();
            """
            await conn.execute(schema_query)

            # ==========================================
            # 2. تحديثات الجداول (إضافة الأعمدة الجديدة بأمان فولاذي)
            # ==========================================
            user_columns = [
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_sales_log DATE DEFAULT CURRENT_DATE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS promised_payment NUMERIC DEFAULT 0.0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS credit_limit NUMERIC DEFAULT 50000.0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(20)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS region VARCHAR(100) DEFAULT 'المركز الرئيسي'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS old_debt NUMERIC DEFAULT 0.0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_profit NUMERIC DEFAULT 0.0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS wa_status VARCHAR(10) DEFAULT 'off'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS telecom_status VARCHAR(10) DEFAULT 'off'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS ecard_status VARCHAR(10) DEFAULT 'off'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS physical_status VARCHAR(10) DEFAULT 'on'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS telecom_balance NUMERIC DEFAULT 0.0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS telecom_debt NUMERIC DEFAULT 0.0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS telecom_credit_limit NUMERIC DEFAULT 20000.0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS pos_cash_collected NUMERIC DEFAULT 0.0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_vip BOOLEAN DEFAULT FALSE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS special_discount NUMERIC DEFAULT 0.0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS account_status VARCHAR(20) DEFAULT 'active'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS limit_set_by VARCHAR(20) DEFAULT 'agent'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS suspend_reason TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS debt_schedule_pct NUMERIC DEFAULT 0.0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS telecom_pos_cash NUMERIC DEFAULT 0.0"
            ]

            for query in user_columns:
                try: await conn.execute(query)
                except Exception as e: logging.warning(f"تخطي تحديث عمود: {e}")

            other_columns = [
                "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS retail_price NUMERIC DEFAULT 0.0",
                "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS cost_price NUMERIC DEFAULT 0.0",
                "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
                "ALTER TABLE pending_orders ADD COLUMN IF NOT EXISTS order_items JSONB",
                "ALTER TABLE pending_orders ADD COLUMN IF NOT EXISTS total_price DECIMAL DEFAULT 0.0",
                "ALTER TABLE pending_orders ADD COLUMN IF NOT EXISTS remaining_text TEXT",
                "ALTER TABLE pending_orders ADD COLUMN IF NOT EXISTS new_order_text TEXT",
                "ALTER TABLE client_sales ADD COLUMN IF NOT EXISTS profit DECIMAL DEFAULT 0.0",
                "ALTER TABLE electronic_cards ADD COLUMN IF NOT EXISTS added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS is_reverted BOOLEAN DEFAULT FALSE",
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS structured_details JSONB",
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(100) UNIQUE",
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS wallet_type VARCHAR(20) DEFAULT 'manager'",
                "ALTER TABLE telecom_packages ADD COLUMN IF NOT EXISTS credit_price NUMERIC DEFAULT 0.0",
                "ALTER TABLE telecom_packages ADD COLUMN IF NOT EXISTS retail_price NUMERIC DEFAULT 0.0",
                "ALTER TABLE telecom_transactions ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(100) UNIQUE",
                "ALTER TABLE telecom_transactions ADD COLUMN IF NOT EXISTS cost_price NUMERIC DEFAULT 0.0",
                "ALTER TABLE telecom_transactions ADD COLUMN IF NOT EXISTS profit NUMERIC DEFAULT 0.0",
                "ALTER TABLE telecom_transactions ADD COLUMN IF NOT EXISTS paid_from_balance NUMERIC DEFAULT 0.0",
                "ALTER TABLE telecom_transactions ADD COLUMN IF NOT EXISTS added_to_debt NUMERIC DEFAULT 0.0",
                "ALTER TABLE agent_wallet ADD COLUMN IF NOT EXISTS manager_cash NUMERIC DEFAULT 0.0",
                "ALTER TABLE agent_wallet ADD COLUMN IF NOT EXISTS realized_profit NUMERIC DEFAULT 0.0",
                "ALTER TABLE agent_wallet ADD COLUMN IF NOT EXISTS telecom_cash NUMERIC DEFAULT 0.0"
            ]
            
            for query in other_columns:
                try: await conn.execute(query)
                except Exception as e: logging.warning(f"تخطي تحديث عمود: {e}")

            # ==========================================
            # 3. تسوية البيانات وتعديل أنواع الأعمدة
            # ==========================================
            try: await conn.execute("UPDATE inventory SET cost_price = price WHERE cost_price = 0.0 OR cost_price IS NULL")
            except: pass
            try: await conn.execute("ALTER TABLE transactions ALTER COLUMN structured_details TYPE JSONB USING structured_details::JSONB;")
            except: pass
            try: await conn.execute("UPDATE telecom_packages SET credit_price = selling_price + 50 WHERE credit_price = 0.0")
            except: pass
            try: await conn.execute("ALTER TABLE electronic_cards ALTER COLUMN card_number TYPE TEXT;")
            except: pass
            try: await conn.execute("ALTER TABLE electronic_cards ADD CONSTRAINT unique_card_number UNIQUE (card_number);")
            except: pass
            try: await conn.execute("ALTER TABLE transactions ADD CONSTRAINT unique_idempotency_key UNIQUE (idempotency_key);")
            except: pass
            try: await conn.execute("ALTER TABLE telecom_transactions ADD CONSTRAINT unique_telecom_idempotency_key UNIQUE (idempotency_key);")
            except: pass

            # ==========================================
            # 🌟 [جديد] خط الدفاع الأخير: قيود قاعدة البيانات (CHECK Constraints)
            # ==========================================
            db_constraints = [
                "ALTER TABLE inventory ADD CONSTRAINT check_qty_positive CHECK (quantity >= 0);",
                "ALTER TABLE client_inventory ADD CONSTRAINT check_client_qty_positive CHECK (quantity >= 0);",
                "ALTER TABLE agent_wallet ADD CONSTRAINT check_manager_cash_positive CHECK (manager_cash >= 0);",
                "ALTER TABLE agent_wallet ADD CONSTRAINT check_telecom_cash_positive CHECK (telecom_cash >= 0);",
                "ALTER TABLE agent_wallet ADD CONSTRAINT check_telecom_balance_positive CHECK (telecom_balance >= 0);",
                "ALTER TABLE users ADD CONSTRAINT check_pos_cash_positive CHECK (pos_cash_collected >= 0);",
                "ALTER TABLE users ADD CONSTRAINT check_telecom_pos_cash_positive CHECK (telecom_pos_cash >= 0);"
            ]

            for constraint_query in db_constraints:
                try: await conn.execute(constraint_query)
                except Exception as e: pass

            # ==========================================
            # 🌟 [جديد] الزنادات المحاسبية (Triggers) لتحديث الكاش والأرباح فورياً
            # ==========================================
            triggers_query = """
            -- 1. دالة تحديث الكاش (للمدير والتسديدات)
            CREATE OR REPLACE FUNCTION update_wallet_cash()
            RETURNS TRIGGER AS $$
            DECLARE
                m_change NUMERIC := 0;
                t_change NUMERIC := 0;
            BEGIN
                -- حالة الإدخال الجديد
                IF TG_OP = 'INSERT' AND NEW.is_reverted = FALSE THEN
                    IF NEW.wallet_type = 'manager' THEN
                        IF NEW.type IN ('تسديد_من_عميل', 'رصيد_افتتاحي_كاش', 'بيع_مباشر', 'مبيعات_كاش') OR (NEW.type = 'تحويل_رصيد' AND NEW.details LIKE 'تسوية واردة للصندوق%') THEN
                            m_change := NEW.amount;
                        ELSIF NEW.type IN ('تسديد_للشبكة', 'مصروفات', 'مرتجع_بيع_مباشر', 'سحب_أرباح', 'كروت_تالفة') OR (NEW.type = 'تحويل_رصيد' AND NEW.details LIKE 'تسوية صادرة من الصندوق%') THEN
                            m_change := -NEW.amount;
                        END IF;
                    ELSIF NEW.wallet_type = 'telecom' THEN
                        IF NEW.type IN ('تحصيل_رصيد', 'رأس_مال_تسديدات', 'تسديد_باقة_وكيل') THEN
                            t_change := NEW.amount;
                        ELSIF NEW.type IN ('سحب_أرباح_تسديدات', 'تغذية_رصيد_بوابة') THEN
                            t_change := -NEW.amount;
                        END IF;
                    END IF;
                    
                -- حالة التراجع (القيد العكسي)
                ELSIF TG_OP = 'UPDATE' AND OLD.is_reverted = FALSE AND NEW.is_reverted = TRUE THEN
                    IF NEW.wallet_type = 'manager' THEN
                        IF NEW.type IN ('تسديد_من_عميل', 'رصيد_افتتاحي_كاش', 'بيع_مباشر', 'مبيعات_كاش') OR (NEW.type = 'تحويل_رصيد' AND NEW.details LIKE 'تسوية واردة للصندوق%') THEN
                            m_change := -NEW.amount;
                        ELSIF NEW.type IN ('تسديد_للشبكة', 'مصروفات', 'مرتجع_بيع_مباشر', 'سحب_أرباح', 'كروت_تالفة') OR (NEW.type = 'تحويل_رصيد' AND NEW.details LIKE 'تسوية صادرة من الصندوق%') THEN
                            m_change := NEW.amount;
                        END IF;
                    ELSIF NEW.wallet_type = 'telecom' THEN
                        IF NEW.type IN ('تحصيل_رصيد', 'رأس_مال_تسديدات', 'تسديد_باقة_وكيل') THEN
                            t_change := -NEW.amount;
                        ELSIF NEW.type IN ('سحب_أرباح_تسديدات', 'تغذية_رصيد_بوابة') THEN
                            t_change := NEW.amount;
                        END IF;
                    END IF;
                END IF;

                -- تطبيق التحديث على المحفظة
                IF m_change != 0 OR t_change != 0 THEN
                    UPDATE agent_wallet SET manager_cash = manager_cash + m_change, telecom_cash = telecom_cash + t_change WHERE id = 1;
                END IF;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trigger_update_wallet_cash ON transactions;
            CREATE TRIGGER trigger_update_wallet_cash
            AFTER INSERT OR UPDATE ON transactions
            FOR EACH ROW EXECUTE FUNCTION update_wallet_cash();

            -- 2. دالة تحديث الأرباح المحصلة
            CREATE OR REPLACE FUNCTION update_realized_profit()
            RETURNS TRIGGER AS $$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    UPDATE agent_wallet SET realized_profit = realized_profit + NEW.amount WHERE id = 1;
                ELSIF TG_OP = 'DELETE' THEN
                    UPDATE agent_wallet SET realized_profit = realized_profit - OLD.amount WHERE id = 1;
                END IF;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trigger_update_realized_profit ON agent_profits;
            CREATE TRIGGER trigger_update_realized_profit
            AFTER INSERT OR DELETE ON agent_profits
            FOR EACH ROW EXECUTE FUNCTION update_realized_profit();
            """
            try:
                await conn.execute(triggers_query)
            except Exception as e:
                logging.warning(f"تخطي إنشاء الزنادات (قد تكون موجودة مسبقاً): {e}")

            # ==========================================
            # 4. إدخال البيانات الافتراضية والتجريبية
            # ==========================================
            # 🌟 تهيئة محفظة الوكيل (إذا لم تكن موجودة)
            await conn.execute("INSERT INTO agent_wallet (id, telecom_balance) VALUES (1, 0.0) ON CONFLICT DO NOTHING")

            await conn.execute("""
                INSERT INTO telecom_packages (network, package_name, cost_price, selling_price, credit_price, retail_price)
                SELECT 'yemen_mobile', 'باقة مزايا 1 جيجا (تجريبية)', 1150, 1200, 1250, 1200
                WHERE NOT EXISTS (SELECT 1 FROM telecom_packages WHERE network = 'yemen_mobile');
                
                INSERT INTO telecom_packages (network, package_name, cost_price, selling_price, credit_price, retail_price)
                SELECT 'you', 'باقة سمارت نت 2 جيجا (تجريبية)', 1400, 1500, 1600, 1500
                WHERE NOT EXISTS (SELECT 1 FROM telecom_packages WHERE network = 'you');
                
                INSERT INTO telecom_packages (network, package_name, cost_price, selling_price, credit_price, retail_price)
                SELECT 'sabafon', 'باقة سوبر يمن (تجريبية)', 900, 1000, 1100, 1000
                WHERE NOT EXISTS (SELECT 1 FROM telecom_packages WHERE network = 'sabafon');
            """)

            tier_count = await conn.fetchval("SELECT COUNT(*) FROM pricing_tiers")
            if tier_count == 0:
                await conn.execute("""
                    INSERT INTO pricing_tiers (min_amount, max_amount, cash_profit, credit_profit, retail_profit) VALUES
                    (1, 100, 10, 15, 25),
                    (101, 200, 15, 20, 30),
                    (201, 500, 25, 35, 50),
                    (501, 1000, 40, 60, 100)
                """)
                
            settings_defaults = [
                ('share_inventory', 'off'), ('share_agent_inventory', 'on'), ('share_market_debt', 'on'),
                ('share_available_cash', 'off'), ('share_monthly_report', 'off'), ('maintenance_mode', 'off'),
                ('agent_comp_type', 'none'), ('agent_comp_value', '0'), ('archive_channel_id', 'off'),
                ('over_1000_cash_profit', '50'), ('over_1000_credit_profit', '70'), ('over_1000_retail_profit', '100'),
                ('promo_text', 'off'), ('promo_image', 'off'), ('is_telecom_active', 'off')
            ]

            for k, v in settings_defaults:
                await conn.execute("INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT DO NOTHING", k, v)
                
            await conn.execute("""
                INSERT INTO users (user_id, name, role, debt) 
                VALUES (0, 'النظام / مبيعات مباشرة', 'system', 0.0) 
                ON CONFLICT (user_id) DO NOTHING;
            """)
            
            # 🌟 تفعيل محفظة الوكيل برصيد صفري عند أول تشغيل
            await conn.execute("INSERT INTO agent_wallet (id, telecom_balance) VALUES (1, 0.0) ON CONFLICT (id) DO NOTHING;")

            # ==========================================
            # 5. إنشاء الفهارس (Indexes) لتسريع البحث
            # ==========================================
            indexes_query = """
            -- الفهارس الفردية الأساسية
            CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id);
            CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
            CREATE INDEX IF NOT EXISTS idx_transactions_type ON transactions(type);
            CREATE INDEX IF NOT EXISTS idx_transactions_wallet ON transactions(wallet_type);
            CREATE INDEX IF NOT EXISTS idx_transactions_idempotency ON transactions(idempotency_key);
            
            -- 🌟 [جديد] الفهارس المركبة (Composite Indexes) لتسريع التقارير بـ 100 ضعف
            CREATE INDEX IF NOT EXISTS idx_transactions_date_type ON transactions(date, type);
            CREATE INDEX IF NOT EXISTS idx_transactions_wallet_date ON transactions(wallet_type, date);
            CREATE INDEX IF NOT EXISTS idx_transactions_user_date ON transactions(user_id, date);
            
            -- فهارس الجداول الأخرى
            CREATE INDEX IF NOT EXISTS idx_client_inventory_user_id ON client_inventory(user_id);
            CREATE INDEX IF NOT EXISTS idx_client_customer_ledger ON client_customer_ledger(client_id, customer_name);
            CREATE INDEX IF NOT EXISTS idx_telecom_trans_client ON telecom_transactions(client_id);
            CREATE INDEX IF NOT EXISTS idx_telecom_trans_status ON telecom_transactions(status);
            """
            await conn.execute(indexes_query)
                
            print("✅ تم الاتصال بقاعدة البيانات وتجهيز جميع الجداول والفهارس بنجاح.")
    except Exception as e:
        logging.error(f"❌ [CRITICAL] فشل الاتصال بقاعدة البيانات: {e}")
        import sys
        sys.exit(1) # 🌟 الحماية الفولاذية: إيقاف السيرفر فوراً، لا معنى لتشغيل البوت بدون قاعدة بيانات!

# =====================================================================
# دوال التعامل مع قاعدة البيانات (Helper Functions)
# =====================================================================

async def add_learned_fact(fact_text: str, added_by: int, conn=None):
    try:
        if conn:
            await conn.execute("INSERT INTO learned_facts (fact_text, added_by) VALUES ($1, $2)", fact_text, added_by)
        elif pool:
            async with pool.acquire() as new_conn:
                await new_conn.execute("INSERT INTO learned_facts (fact_text, added_by) VALUES ($1, $2)", fact_text, added_by)
    except Exception as e:
        logging.error(f"DB Error in add_learned_fact: {e}")

async def get_all_learned_facts(conn=None) -> str:
    try:
        async def fetch_data(connection):
            facts = await connection.fetch("SELECT fact_text FROM learned_facts")
            return "\n".join([f"- {f['fact_text']}" for f in facts]) if facts else ""
            
        if conn: return await fetch_data(conn)
        elif pool:
            async with pool.acquire() as new_conn: return await fetch_data(new_conn)
    except Exception as e:
        logging.error(f"DB Error in get_all_learned_facts: {e}")
    return ""

async def get_agent_total_profit(conn=None) -> Decimal:
    try:
        async def fetch_data(connection):
            total = await connection.fetchval("SELECT COALESCE(SUM(amount), 0) FROM agent_profits")
            # 🌟 حماية حسابية: استخدام str() لمنع أخطاء الـ Float
            return Decimal(str(total)) if total else Decimal('0.0')
            
        if conn: return await fetch_data(conn)
        elif pool:
            async with pool.acquire() as new_conn: return await fetch_data(new_conn)
    except Exception as e:
        logging.error(f"DB Error in get_agent_total_profit: {e}")
    return Decimal('0.0')

async def save_report_archive(month_year: str, file_id: str, conn=None):
    try:
        query = '''
            INSERT INTO reports_archive (month_year, file_id) 
            VALUES ($1, $2) 
            ON CONFLICT (month_year) DO UPDATE SET file_id = $2, created_at = CURRENT_TIMESTAMP
        '''
        if conn: await conn.execute(query, month_year, file_id)
        elif pool:
            async with pool.acquire() as new_conn: await new_conn.execute(query, month_year, file_id)
    except Exception as e:
        logging.error(f"DB Error in save_report_archive: {e}")

async def add_or_update_user(user_id: int, name: str, role: str = "client", conn=None):
    try:
        query = '''
            INSERT INTO users (user_id, name, role, debt)
            VALUES ($1, $2, $3, 0.0)
            ON CONFLICT (user_id) DO UPDATE SET name = $2
        '''
        if conn: await conn.execute(query, user_id, name, role)
        elif pool:
            async with pool.acquire() as new_conn: await new_conn.execute(query, user_id, name, role)
    except Exception as e:
        logging.error(f"DB Error in add_or_update_user: {e}")

async def get_user_debt(user_id: int, conn=None) -> Decimal:
    """جلب الدين الصافي للعميل (الدين الإجمالي - الأرباح المعلقة)"""
    try:
        async def fetch_data(connection):
            row = await connection.fetchrow('SELECT debt, pending_profit FROM users WHERE user_id = $1', user_id)
            if row:
                # 🌟 حماية حسابية فولاذية
                return Decimal(str(row['debt'] or '0.0')) - Decimal(str(row['pending_profit'] or '0.0'))
            return Decimal('0.0')
            
        if conn: return await fetch_data(conn)
        elif pool:
            async with pool.acquire() as new_conn: return await fetch_data(new_conn)
    except Exception as e:
        logging.error(f"DB Error in get_user_debt: {e}")
    return Decimal('0.0')

async def get_setting(key: str, conn=None) -> str:
    try:
        async def fetch_data(connection):
            val = await connection.fetchval("SELECT value FROM settings WHERE key = $1", key)
            return val if val else "off"
            
        if conn: return await fetch_data(conn)
        elif pool:
            async with pool.acquire() as new_conn: return await fetch_data(new_conn)
    except Exception as e:
        logging.error(f"DB Error in get_setting: {e}")
    return "off"

async def set_setting(key: str, value: str, conn=None):
    try:
        query = """
            INSERT INTO settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = $2
        """
        if conn: await conn.execute(query, key, value)
        elif pool:
            async with pool.acquire() as new_conn: await new_conn.execute(query, key, value)
    except Exception as e:
        logging.error(f"DB Error in set_setting: {e}")

# =====================================================================
# دوال محفظة أرباح التسديدات (مستقلة تماماً)
# =====================================================================
async def get_telecom_total_profit(conn=None) -> Decimal:
    try:
        async def fetch_data(connection):
            total = await connection.fetchval("SELECT COALESCE(SUM(amount), 0) FROM telecom_profits")
            # 🌟 حماية حسابية
            return Decimal(str(total)) if total else Decimal('0.0')
            
        if conn: return await fetch_data(conn)
        elif pool:
            async with pool.acquire() as new_conn: return await fetch_data(new_conn)
    except Exception as e:
        logging.error(f"DB Error in get_telecom_total_profit: {e}")
    return Decimal('0.0')

async def get_agent_telecom_balance(conn=None) -> Decimal:
    """جلب رصيد بوابة التسديدات الخاص بالوكيل"""
    try:
        async def fetch_data(connection):
            val = await connection.fetchval("SELECT telecom_balance FROM agent_wallet WHERE id = 1")
            # 🌟 حماية حسابية
            return Decimal(str(val)) if val else Decimal('0.0')
            
        if conn: return await fetch_data(conn)
        elif pool:
            async with pool.acquire() as new_conn: return await fetch_data(new_conn)
    except Exception as e:
        logging.error(f"DB Error in get_agent_telecom_balance: {e}")
    return Decimal('0.0')

async def get_user_telecom_data(user_id: int, conn=None) -> dict:
    """جلب رصيد ودين التسديدات الخاص بالعميل"""
    try:
        async def fetch_data(connection):
            row = await connection.fetchrow('SELECT telecom_balance, telecom_debt FROM users WHERE user_id = $1', user_id)
            if row:
                return {
                    # 🌟 حماية حسابية
                    "balance": Decimal(str(row['telecom_balance'] or '0.0')), 
                    "debt": Decimal(str(row['telecom_debt'] or '0.0'))
                }
            return {"balance": Decimal('0.0'), "debt": Decimal('0.0')}
            
        if conn: return await fetch_data(conn)
        elif pool:
            async with pool.acquire() as new_conn: return await fetch_data(new_conn)
    except Exception as e:
        logging.error(f"DB Error in get_user_telecom_data: {e}")
    return {"balance": Decimal('0.0'), "debt": Decimal('0.0')}

# =====================================================================
# إغلاق قاعدة البيانات بأمان
# =====================================================================
async def close_db():
    global pool
    if pool:
        try:
            await pool.close()
            pool = None # 🌟 تصفير المتغير لمنع استخدامه بالخطأ (Memory Leak Prevention)
            print("🛑 تم إغلاق الاتصال بقاعدة البيانات بأمان.")
        except Exception as e:
            logging.error(f"DB Error in close_db: {e}")
