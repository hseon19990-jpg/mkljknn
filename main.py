import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonCommands
from config import BOT_TOKEN
from database import init_db
from bot_handlers import router
from userbot import ub_manager
from scheduler import scheduler_mgr

logging.basicConfig(level=logging.INFO)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN غير مضبوط في .env")
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="🏠 القائمة الرئيسية / إعادة البدء"),
        ])
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        print(f"[Commands] {e}")
    scheduler_mgr.start()
    await ub_manager.load_all_sessions()
    await scheduler_mgr.reload_all()
    print("✅ Bot is running...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
