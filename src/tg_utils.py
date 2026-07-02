from contextlib import suppress

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery


async def safe_answer(callback: CallbackQuery, *args, **kwargs) -> None:
    """Answer a callback query, ignoring stale/expired-query errors.

    Answering is best-effort UI acknowledgement. Telegram rejects it once the
    query has expired (e.g. a button clicked on an old message after a restart,
    or a double click), which must not crash the handler.
    """
    with suppress(TelegramBadRequest):
        await callback.answer(*args, **kwargs)
