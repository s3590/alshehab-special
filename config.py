import os
# 🌟 الإصلاح الجذري: تحميل متغيرات البيئة من ملف .env (يمنع انهيار السيرفر في بيئات التطوير والـ VPS)
from dotenv import load_dotenv
load_dotenv()

# ================= المتغيرات الأساسية =================
# جلب المتغيرات مباشرة من بيئة الاستضافة
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# ================= حماية الفشل السريع (Fail Fast) =================
if not BOT_TOKEN:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'BOT_TOKEN' في متغيرات البيئة!")

if not DATABASE_URL:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'DATABASE_URL' في متغيرات البيئة!")

# ================= دالة مساعدة لجلب الأرقام بأمان =================
def get_env_int(key: str, default: int = 0) -> int:
    val = os.getenv(key)
    if val and val.strip().lstrip('-').isdigit(): # lstrip لدعم الأرقام السالبة إن وجدت
        return int(val.strip())
    return default

# ================= معرفات الإدارة =================
# سيقوم بجلبها من الاستضافة، وإذا لم يجدها سيضع 0 مؤقتاً
ADMIN_ID = get_env_int("ADMIN_ID", 0)
NETWORK_OWNER_ID = get_env_int("NETWORK_OWNER_ID", 0)

# ================= المفاتيح السرية للنظام (تأمين صارم) =================
_env_enc_key = os.getenv("ENCRYPTION_KEY")
if not _env_enc_key:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'ENCRYPTION_KEY' في متغيرات البيئة!")

# 🌟 الإصلاح السحري: تنظيف المفتاح من المسافات وإصلاح النقص تلقائياً
_env_enc_key = _env_enc_key.strip()
_env_enc_key += "=" * ((4 - len(_env_enc_key) % 4) % 4)
ENCRYPTION_KEY = _env_enc_key.encode()

API_SECRET_KEY = os.getenv("API_SECRET_KEY")
if not API_SECRET_KEY:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'API_SECRET_KEY' في متغيرات البيئة!")
API_SECRET_KEY = API_SECRET_KEY.strip()

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'JWT_SECRET' في متغيرات البيئة!")
JWT_SECRET = JWT_SECRET.strip()

# ================= وضع الاختبار (Sandbox Mode) =================
# إذا كان True، يمكنك تجربة النظام بدون خصم حقيقي من شركة الاتصالات
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ("true", "1", "t", "yes")

# ================= إعدادات الواتساب (مركزية) =================
# الروابط والأسماء يمكن أن يكون لها قيم افتراضية لأنها ليست أسراراً خطيرة
WA_API_URL = os.getenv("WA_API_URL", "https://whatsapp-api-fhhi.onrender.com" ).strip()
WA_INSTANCE = os.getenv("WA_INSTANCE", "alshehappro").strip()
GM_WA_NUMBER = os.getenv("GM_WA_NUMBER", "None").strip()

# 🌟 الإصلاح الأمني: منع كتابة مفتاح الواتساب داخل الكود
WA_API_KEY = os.getenv("WA_API_KEY")
if not WA_API_KEY:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'WA_API_KEY' في متغيرات البيئة!")
WA_API_KEY = WA_API_KEY.strip()

# ================= الرمز السري للعمليات الخطيرة (النووية) =================
# 🌟 الإصلاح الأمني: منع كتابة رمز الفورمات داخل الكود
SYSTEM_VAULT_PIN = os.getenv("SYSTEM_VAULT_PIN")
if not SYSTEM_VAULT_PIN:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'SYSTEM_VAULT_PIN' في متغيرات البيئة!")
SYSTEM_VAULT_PIN = SYSTEM_VAULT_PIN.strip()

# ================= إعدادات الخزنة السحابية (النسخ الاحتياطي) =================
BACKUP_EMAIL_SENDER = os.getenv("BACKUP_EMAIL_SENDER", "").strip()
BACKUP_EMAIL_PASSWORD = os.getenv("BACKUP_EMAIL_PASSWORD", "").strip()
BACKUP_EMAIL_RECEIVER = os.getenv("BACKUP_EMAIL_RECEIVER", "").strip()

# ================= إعدادات إشعارات الويب (Firebase VAPID) =================
# المفتاح العام لمتصفحات الويب
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()

# 🌟 تمت إضافتها كقيم فارغة فقط لمنع انهيار السيرفر (ImportError) في ملف web_api.py
# لأن اعتمادك الأساسي هو على Firebase لتطبيق الأندرويد
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_ADMIN_EMAIL = os.getenv("VAPID_ADMIN_EMAIL", "mailto:admin@example.com").strip()
