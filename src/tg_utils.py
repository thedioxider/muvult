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
_FLOOD_NOTIFY_AFTER = 60  # seconds of accumulated wait before we explain the stall to the user

# Cumulative seconds we've blocked on flood control per chat during the current
# episode; reset the moment a request to that chat succeeds. Chats we're mid-way
# through notifying are tracked so the notice's own send can't recurse into
# another notice.
_flood_waited: dict[int, float] = {}
_flood_notifying: set[int] = set()


class FloodControlMiddleware(BaseRequestMiddleware):
    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        chat_id = getattr(method, "chat_id", None)
        for attempt in range(_FLOOD_MAX_ATTEMPTS):
            try:
                result = await make_request(bot, method)
            except TelegramRetryAfter as e:
                # Exhausted, or an implausibly long ban: give up (re-raise) and
                # hand back to the caller's own error handling.
                if attempt == _FLOOD_MAX_ATTEMPTS - 1 or e.retry_after > _FLOOD_MAX_WAIT:
                    raise
                wait = e.retry_after + 0.5  # exact server value + small buffer past the window
                if chat_id is not None:
                    _flood_waited[chat_id] = _flood_waited.get(chat_id, 0.0) + wait
                log.warning(
                    "flood control on %s: waiting %ss then retrying (%d/%d)",
                    type(method).__name__, e.retry_after, attempt + 1, _FLOOD_MAX_ATTEMPTS - 1,
                )
                await asyncio.sleep(wait)
                continue
            # Success: if this chat was held up long enough, tell the user why the
            # updates stalled (once per episode; fire-and-forget so we don't delay
            # the result we just got back).
            if chat_id is not None:
                waited = _flood_waited.pop(chat_id, 0.0)
                if waited > _FLOOD_NOTIFY_AFTER and chat_id not in _flood_notifying:
                    asyncio.create_task(self._explain_delay(bot, chat_id, waited))
            return result
        raise AssertionError("unreachable: loop returns or raises")

    async def _explain_delay(self, bot: Bot, chat_id: int, waited: float) -> None:
        _flood_notifying.add(chat_id)  # guard: this send flooding must not spawn another notice
        try:
            from .config import settings

            contact = getattr(settings, "support_contact", None)
            whom = contact if contact else "the bot administrator"
            await bot.send_message(
                chat_id,
                f"⚠️ Telegram temporarily rate-limited this chat, so status updates "
                f"paused for about {round(waited)}s. This can happen on large uploads "
                f"(many status and confirmation messages at once) — your files were "
                f"still processed in the background. If it keeps happening, please contact {whom}.",
            )
        except Exception:
            log.warning("failed to send flood-delay notice to chat %s", chat_id, exc_info=True)
        finally:
            _flood_notifying.discard(chat_id)
