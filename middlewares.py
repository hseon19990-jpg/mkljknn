"""وسيطات عامة: تتبّع المستخدمين (الاسم/آخر ظهور) + وضع الصيانة."""
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery

import database as db
from config import OWNER_ID


class TrackAndGuard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or user.is_bot:
            return await handler(event, data)

        # تسجيل/تحديث بيانات العضو
        try:
            full = (user.full_name or "")[:100]
            await db.ensure_user(user.id, user.username or "", full)
        except Exception as e:
            print(f"[Track] {e}")

        # وضع الصيانة (المالك مستثنى)
        if user.id != OWNER_ID:
            try:
                if await db.get_setting_bool("maintenance", False):
                    text = "🛠 البوت تحت الصيانة حالياً، حاول لاحقاً."
                    if isinstance(event, CallbackQuery):
                        await event.answer(text, show_alert=True)
                    elif isinstance(event, Message):
                        await event.answer(text)
                    return None
            except Exception as e:
                print(f"[Maintenance] {e}")
        return await handler(event, data)
