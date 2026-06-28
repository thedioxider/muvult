import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from src.handlers.user import cmd_start, cmd_id


@pytest.mark.asyncio
async def test_cmd_id_replies_with_tg_id():
    msg = AsyncMock()
    msg.from_user = MagicMock(id=12345)
    await cmd_id(msg)
    msg.answer.assert_called_once()
    assert "12345" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cmd_start_allowed_user():
    msg = AsyncMock()
    msg.from_user = MagicMock(id=12345)

    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = MagicMock(id=1, settings="{}")
    session.exec = AsyncMock(return_value=result)

    @asynccontextmanager
    async def _ctx():
        yield session

    with patch("src.handlers.user.get_session", _ctx):
        await cmd_start(msg)

    msg.answer.assert_called_once()
    assert "send" in msg.answer.call_args[0][0].lower() or "upload" in msg.answer.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_cmd_start_unknown_user():
    msg = AsyncMock()
    msg.from_user = MagicMock(id=99999)

    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = None
    session.exec = AsyncMock(return_value=result)

    @asynccontextmanager
    async def _ctx():
        yield session

    with patch("src.handlers.user.get_session", _ctx):
        await cmd_start(msg)

    text = msg.answer.call_args[0][0]
    assert "99999" in text
