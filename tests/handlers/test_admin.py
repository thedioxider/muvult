import pytest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from sqlmodel import select
from src.handlers.admin import (
    cmd_adduser, cmd_users, cmd_settgid, cmd_recreatelinks,
    _resolve_retag_scope, _run_retag,
)
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


@pytest.mark.asyncio
async def test_recreatelinks_absorbs_user_lyrics_and_shares(tmp_path):
    # A real lyrics file in one owner's library is moved into the pool and linked
    # into every owner's library; a user's own non-pool lyrics are left alone.
    from src.library import recreate_links
    from src.pool import create_symlink

    await init_db(str(tmp_path / "db"))
    music_root = tmp_path / "music"
    pool_file = music_root / ".pool" / "Ar" / "Al" / "song.flac"
    pool_file.parent.mkdir(parents=True)
    pool_file.write_bytes(b"audio")

    async with get_session() as s:
        s.add(User(id=1, tg_id=1, username="alice", navidrome_user_id="a", navidrome_library_id=1))
        s.add(User(id=2, tg_id=2, username="bob", navidrome_user_id="b", navidrome_library_id=2))
        s.add(Track(id=1, pool_path="Ar/Al/song.flac", musicbrainz_id="m", format="flac", bitrate=1000))
        await s.commit()
        for uid, name in ((1, "alice"), (2, "bob")):
            link = create_symlink(pool_file, music_root / name)
            s.add(TrackOwnership(track_id=1, user_id=uid, symlink_path=str(link)))
        await s.commit()

    # alice's plugin dropped two real lyrics files (both lyrics extensions) that
    # never made it to the pool.
    alice_album = music_root / "alice" / "Ar" / "Al"
    bob_album = music_root / "bob" / "Ar" / "Al"
    (alice_album / "song.lrc").write_text("synced lyrics")
    (alice_album / "song.txt").write_text("plain lyrics")

    with patch("src.config.settings", MagicMock(music_root=str(music_root))):
        count, missing = await recreate_links()

    for name, body in (("song.lrc", "synced lyrics"), ("song.txt", "plain lyrics")):
        pooled = pool_file.parent / name
        assert pooled.exists() and pooled.read_text() == body  # absorbed into the pool
        # every owner points at the one pooled copy by symlink (no second copy)
        for album in (alice_album, bob_album):
            link = album / name
            assert link.is_symlink() and link.resolve() == pooled.resolve()


async def _seed_retag_tracks():
    async with get_session() as s:
        s.add(Track(id=1, pool_path="Ar/Al1/a.mp3", musicbrainz_id="m1", format="flac", bitrate=1000))
        s.add(Track(id=2, pool_path="Ar/Al1/b.mp3", musicbrainz_id="m2", format="flac", bitrate=1000))
        s.add(Track(id=3, pool_path="Ar/Al2/c.mp3", musicbrainz_id="m3", format="flac", bitrate=1000))
        # As-is import: excluded from re-tagging via is_asis, regardless of its path.
        s.add(Track(id=4, pool_path="Ar/Al2/d.mp3", musicbrainz_id=None, is_asis=True, format="mp3", bitrate=320))
        await s.commit()


@pytest.mark.asyncio
async def test_resolve_retag_scope_variants(tmp_path):
    await init_db(str(tmp_path / "db"))
    await _seed_retag_tracks()

    # No arg -> whole library except as-is imports; covers every touched album.
    ids, covers, albums = await _resolve_retag_scope(None)
    assert set(ids) == {1, 2, 3}
    assert covers == {"Ar/Al1", "Ar/Al2"} == albums

    # prefix/* -> that subtree, cover refetch on.
    ids, covers, albums = await _resolve_retag_scope("Ar/Al1/*")
    assert set(ids) == {1, 2} and covers == {"Ar/Al1"} == albums

    # As-is imports are excluded even under a matching prefix.
    ids, _, _ = await _resolve_retag_scope("Ar/Al2/*")
    assert set(ids) == {3}

    # Exact track path -> that one track, no cover refetch.
    ids, covers, albums = await _resolve_retag_scope("Ar/Al1/a.mp3")
    assert ids == [1] and covers == set() and albums == {"Ar/Al1"}

    # Bare front.<ext> -> cover-only refetch, no tracks, no confirm.
    ids, covers, albums = await _resolve_retag_scope("Ar/Al1/front.jpg")
    assert ids == [] and covers == {"Ar/Al1"} and albums == set()

    # Malformed wildcard (traversal in the prefix) -> None.
    assert await _resolve_retag_scope("../*") is None


@pytest.mark.asyncio
async def test_run_retag_clash_keeps_file_in_place(tmp_path):
    # When a re-tag resolves to a path another track already holds, the file is
    # re-tagged in place (dest redirected to its current path) rather than moved
    # onto -- and overwriting -- the other track.
    await init_db(str(tmp_path / "db"))
    await _seed_retag_tracks()
    music_root = tmp_path / "music"
    pool_root = music_root / ".pool"
    (pool_root / "Ar/Al1").mkdir(parents=True)
    (pool_root / "Ar/Al1/a.mp3").write_bytes(b"audio-a")

    # retag of track 1 (a.mp3) resolves to b.mp3's canonical path -- a clash.
    async def fake_retag(tmp, mb_id, enrich=True):
        return tmp, pool_root / "Ar/Al1/b.mp3"

    seen = {}
    async def fake_promote(session, proot, track, staged, dest):
        seen["dest"] = dest
        return dest

    settings = MagicMock(music_root=str(music_root), staging_root=str(tmp_path / "staging"))
    with patch("src.config.settings", settings), \
         patch("src.beets_svc.retag_by_id", fake_retag), \
         patch("src.library.promote_and_relink", fake_promote), \
         patch("src.library.ensure_album_cover", AsyncMock()):
        summary = await _run_retag(AsyncMock(), [1], [])

    assert seen["dest"] == pool_root / "Ar/Al1/a.mp3"  # kept in place, not b.mp3
    assert "Re-tagged 1 track(s)" in summary
