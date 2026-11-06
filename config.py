import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
DB_PATH = os.getenv("DB_PATH", "bot.db")
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "sessions")

LEAVE_DELAY_MINUTES = 15
CHANNEL_LIMIT_LEAVE_COUNT = 200
FAILURES_BEFORE_COMA = 3
COMA_HOURS = 6

# ---- إعدادات جديدة ----
DEFAULT_TZ_OFFSET = 3            # فرق التوقيت عن UTC (قابل للتغيير من لوحة المالك)
LOG_RETENTION_DAYS = 30          # مدة الاحتفاظ بسجل النشر
EVENT_RETENTION_DAYS = 60        # مدة الاحتفاظ بسجل الأحداث
WATCHDOG_SECONDS = 300           # فحص اتصال الحسابات كل X ثانية
SEND_TIMEOUT = 45                # أقصى انتظار لعملية إرسال/جلب رسائل
JOIN_LINK_DEDUP_SECONDS = 600    # تجاهل نفس الرابط إن تكرر خلال هذه المدة
DEFAULT_MAX_BUNDLES = 10         # حدود افتراضية للأعضاء (يغيّرها المالك)
DEFAULT_MAX_NUMBERS = 10
DEFAULT_MAX_CLICHES = 10

DEFAULT_MIN_INTERVAL = 60
DEFAULT_MAX_INTERVAL = 3600

os.makedirs(SESSIONS_DIR, exist_ok=True)
