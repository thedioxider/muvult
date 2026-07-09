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
from .library import recreate_links
from .handlers.admin import admin_router
from .handlers.upload import upload_router
from .handlers.user import user_router
from .tg_utils import FloodControlMiddleware

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

_RECONCILE_INTERVAL = 24 * 60 * 60
# Hold a strong reference: asyncio only weakly references tasks, so a bare
# create_task() could be garbage-collected mid-sleep and silently stop the loop.
_daily_task: asyncio.Task | None = None


async def _daily_link_reconcile() -> None:
    """Re-run ``recreatelinks`` for all users once a day so lyrics that landed in
    the pool (or a user's library) out-of-band get shared to every owner."""
    while True:
        await asyncio.sleep(_RECONCILE_INTERVAL)
        try:
            count, missing = await recreate_links()
            log.info("daily link reconcile: %d rebuilt, %d missing pool file(s)", count, len(missing))
        except Exception:
            log.exception("daily link reconcile failed")


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

    global _daily_task
    _daily_task = asyncio.create_task(_daily_link_reconcile())
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
