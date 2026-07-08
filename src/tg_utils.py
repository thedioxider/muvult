import asyncio
import logging
from contextlib import suppress

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import Response, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery

log = logging.getLogger(__name__)


async def safe_answer(callback: CallbackQuery, *args, **kwargs) -> None:
    """Answer a callback query, ignoring stale/expired-query errors.

    Answering is best-effort UI acknowledgement. Telegram rejects it once the
    query has expired (e.g. a button clicked on an old message after a restart,
    or a double click), which must not crash the handler.
    """
    with suppress(TelegramBadRequest):
        await callback.answer(*args, **kwargs)


# Residual net for Telegram flood control. The upload handler throttles its own
# message churn so we normally stay under the per-chat limit, but a burst can
# still occasionally earn a 429, and aiogram does not retry these itself. A 429
# is chat-wide -- it blocks *every* send/edit/delete in the chat until it clears,
# so we can't reach the user at all -- so giving up wouldn't fail just one file,
# it would leave the user with no updates whatsoever. We therefore wait out the
# exact server-provided retry_after (the API's own guidance) and retry, rather
# than dropping the call. The bounds are only guards against a pathological,
# endlessly-escalating ban: a capped attempt count, and a ceiling above which a
# single retry_after is too absurd to block on (all realistic per-chat bans are
# well under this, so in practice we always wait them out).
_FLOOD_MAX_ATTEMPTS = 5
_FLOOD_MAX_WAIT = 600  # seconds (10 min); a longer ban than this we refuse to block on


class FloodControlMiddleware(BaseRequestMiddleware):
    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        for attempt in range(_FLOOD_MAX_ATTEMPTS):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as e:
                # Exhausted, or an implausibly long ban: give up (re-raise) and
                # hand back to the caller's own error handling.
                if attempt == _FLOOD_MAX_ATTEMPTS - 1 or e.retry_after > _FLOOD_MAX_WAIT:
                    raise
                log.warning(
                    "flood control on %s: waiting %ss then retrying (%d/%d)",
                    type(method).__name__, e.retry_after, attempt + 1, _FLOOD_MAX_ATTEMPTS - 1,
                )
                await asyncio.sleep(e.retry_after + 0.5)  # exact server value + small buffer past the window
        raise AssertionError("unreachable: loop returns or raises")
