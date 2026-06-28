import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from .auth import AuthMiddleware
from .beets_svc import setup_beets
from .config import settings
from .db import init_db
from .handlers.admin import admin_router
from .handlers.upload import upload_router
from .handlers.user import user_router

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    await init_db(settings.db_path)
    setup_beets(settings.music_root)

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.middleware(AuthMiddleware())
    dp.include_router(admin_router)
    dp.include_router(user_router)
    dp.include_router(upload_router)

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
