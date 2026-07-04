import pytest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from sqlmodel import select
from src.handlers.admin import cmd_adduser, cmd_users, cmd_settgid, cmd_recreatelinks
from src.db import init_db, get_session, Track, TrackOwnership, User


def _msg(text: str, tg_id: int = 1):
    msg = AsyncMock()
    msg.from_user = MagicMock(id=tg_id)
    msg.text = text
    return msg


def _mock_session(exec_return=None):
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = exec_return
    result.all.return_value = exec_return or []
    session.exec = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.delete = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield session

    return _ctx, session


@pytest.mark.asyncio
async def test_adduser_success():
    msg = _msg("/adduser alice 99999")
    ctx, session = _mock_session()

    mock_settings = MagicMock()
    mock_settings.music_root = "/music"
    mock_settings.nd_url = "http://nd.test"
    mock_settings.nd_admin_user = "admin"
    mock_settings.nd_admin_pass = "secret"
    mock_settings.nd_music_path = "/muvult"

    with (
        patch("src.handlers.admin.get_session", ctx),
        patch("src.handlers.admin.NavidromeClient") as MockND,
        patch("src.handlers.admin.Path.mkdir"),
        patch("src.config.settings", mock_settings),
    ):
        nd = AsyncMock()
        nd.create_library = AsyncMock(return_value=5)
        nd.get_user = AsyncMock(return_value={"id": "nd-uid-alice", "isAdmin": False})
        nd.set_user_library = AsyncMock()
        MockND.return_value = nd

        await cmd_adduser(msg)

    session.add.assert_called_once()
    session.commit.assert_called_once()
    assert "alice" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_adduser_wrong_args():
    msg = _msg("/adduser onlyonearg")
    await cmd_adduser(msg)
    assert "Usage" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_users_empty():
    msg = _msg("/users")
    ctx, _ = _mock_session(exec_return=[])

    with patch("src.handlers.admin.get_session", ctx):
        await cmd_users(msg)

    assert "No users" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_recreatelinks_relinks_asis_flat(tmp_path):
    # Regression: /recreatelinks must relink as-is imports flat, not nested under
    # their `users/<name>/<file>` pool-relative path.
    await init_db(str(tmp_path / "db"))
    music_root = tmp_path / "music"
    pool_file = music_root / ".pool" / "users" / "alice" / "song.mp3"
    pool_file.parent.mkdir(parents=True)
    pool_file.write_bytes(b"audio")
    user_dir = music_root / "alice"
    user_dir.mkdir()
    flat_link = user_dir / "song.mp3"  # where the flat link should live

    async with get_session() as s:
        s.add(User(id=1, tg_id=1, username="alice", navidrome_user_id="x", navidrome_library_id=1))
        s.add(Track(id=1, pool_path="users/alice/song.mp3", format="mp3", bitrate=320, is_asis=True))
        await s.commit()
        s.add(TrackOwnership(track_id=1, user_id=1, symlink_path=str(flat_link)))
        await s.commit()

    msg = _msg("/recreatelinks alice")
    with patch("src.config.settings", MagicMock(music_root=str(music_root))):
        await cmd_recreatelinks(msg)

    assert flat_link.is_symlink()
    assert flat_link.resolve() == pool_file.resolve()
    assert not (user_dir / "users").exists()  # never nested

    async with get_session() as s:
        own = (await s.exec(select(TrackOwnership))).first()
        assert Path(own.symlink_path) == flat_link
