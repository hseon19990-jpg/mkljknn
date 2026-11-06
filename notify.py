"""إرسال إشعارات من الخلفية (المجدول / الحسابات) إلى الأعضاء والمالك."""
import database as db
from config import OWNER_ID

_bot = None


def set_bot(bot):
    global _bot
    _bot = bot


async def notify_user(user_id, text, force=False):
    """يرسل إشعاراً لعضو (يحترم خيار الإشعارات إلا إذا force=True)."""
    if _bot is None or not user_id:
        return False
    try:
        if not force and not await db.wants_notify(user_id):
            return False
        await _bot.send_message(user_id, text)
        return True
    except Exception as e:
        print(f"[Notify] user {user_id}: {e}")
        return False


async def notify_owner(text):
    """إشعار للمالك (يحترم إعداد notify_owner_errors)."""
    if _bot is None or not OWNER_ID:
        return False
    try:
        if not await db.get_setting_bool("notify_owner", True):
            return False
        await _bot.send_message(OWNER_ID, text)
        return True
    except Exception as e:
        print(f"[Notify] owner: {e}")
        return False
