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

DEFAULT_MIN_INTERVAL = 60
DEFAULT_MAX_INTERVAL = 3600

os.makedirs(SESSIONS_DIR, exist_ok=True)