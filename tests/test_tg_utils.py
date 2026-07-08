import asyncio

import pytest
from aiogram.exceptions import TelegramRetryAfter

import src.tg_utils as tg_utils
from src.tg_utils import FloodControlMiddleware


class _Method:
    """Stand-in TelegramMethod: the exception only reads its type name / chat_id."""
    chat_id = 123


def _retry(after: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=_Method(), message="flood", retry_after=after)


@pytest.mark.asyncio
async def test_waits_exact_retry_after_then_succeeds(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    calls = 0

    async def make_request(bot, method):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _retry(3)
        return "ok"

    result = await FloodControlMiddleware()(make_request, bot=None, method=_Method())

    assert result == "ok"
    assert calls == 2
    assert slept == [3.5]  # the server's exact retry_after + the 0.5s buffer


@pytest.mark.asyncio
async def test_gives_up_on_ban_longer_than_ceiling(monkeypatch):
    monkeypatch.setattr(tg_utils, "_FLOOD_MAX_WAIT", 600)

    async def fake_sleep(s):  # must not be called -- the ban is over the ceiling
        raise AssertionError("should not sleep on an over-ceiling ban")

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def make_request(bot, method):
        raise _retry(601)

    with pytest.raises(TelegramRetryAfter):
        await FloodControlMiddleware()(make_request, bot=None, method=_Method())


@pytest.mark.asyncio
async def test_gives_up_after_exhausting_attempts(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    calls = 0

    async def make_request(bot, method):
        nonlocal calls
        calls += 1
        raise _retry(1)

    with pytest.raises(TelegramRetryAfter):
        await FloodControlMiddleware()(make_request, bot=None, method=_Method())

    assert calls == tg_utils._FLOOD_MAX_ATTEMPTS  # tried the full budget before re-raising
