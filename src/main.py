import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.memory import MemoryStorage

from .auth import AuthMiddleware
from .beets_svc import setup_beets
from .config import settings
from .db import init_db
from .handlers.admin import admin_router
from .handlers.upload import upload_router
from .handlers.user import user_router
from .tg_utils import FloodControlMiddleware

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    await init_db()
    setup_beets(settings.music_root, settings.mb_search_limit, settings.acoustid_api_key)

    session = None
    if settings.bot_api_url:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(settings.bot_api_url, is_local=settings.bot_api_local)
        )
    bot = Bot(token=settings.bot_token, session=session)
    bot.session.middleware(FloodControlMiddleware())  # wait out 429s instead of going silent
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.middleware(AuthMiddleware())
    dp.include_router(admin_router)
    dp.include_router(user_router)
    dp.include_router(upload_router)

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
