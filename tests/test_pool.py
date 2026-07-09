from pathlib import Path
import pytest
from src.pool import (
    absorb_user_lyrics,
    create_symlink,
    ensure_cover_symlink,
    find_cover,
    find_sidecars,
    links_to,
    promote_pool_file,
    reconcile_sidecars,
    remove_link_sidecars,
    remove_sidecars,
    remove_symlink,
    remove_pool_file,
    update_symlinks,
    user_library_root,
)


@pytest.fixture
def dirs(tmp_path):
    pool = tmp_path / ".pool" / "Artist" / "Album"
    pool.mkdir(parents=True)
    user_dir = tmp_path / "alice"
    user_dir.mkdir()
    return pool, user_dir, tmp_path


def test_create_symlink(dirs):
    pool_dir, user_dir, root = dirs
    pool_file = pool_dir / "01 - Song.mp3"
    pool_file.write_bytes(b"audio")

    link = create_symlink(pool_file, user_dir)

    assert link.is_symlink()
    assert link.resolve() == pool_file.resolve()
    assert link.parent == user_dir / "Artist" / "Album"


def test_remove_symlink(dirs):
    pool_dir, user_dir, root = dirs
    pool_file = pool_dir / "01 - Song.mp3"
    pool_file.write_bytes(b"audio")
    link = create_symlink(pool_file, user_dir)

    remove_symlink(link)

    assert not link.exists()


def test_remove_symlink_preserves_user_dir(dirs):
    pool_dir, user_dir, root = dirs
    pool_file = pool_dir / "01 - Song.mp3"
    pool_file.write_bytes(b"audio")
    link = create_symlink(pool_file, user_dir)  # user_dir/Artist/Album/...

    remove_symlink(link, library_root=user_dir)

    assert not link.exists()
    assert user_dir.is_dir()  # empty user dir must survive
    assert not (user_dir / "Artist").exists()  # empty intermediates cleaned up


def test_user_library_root(dirs):
    pool_dir, user_dir, root = dirs
    link = user_dir / "Artist" / "Album" / "01 - Song.mp3"
    assert user_library_root(link, root) == user_dir


def test_remove_pool_file(dirs):
    pool_dir, user_dir, root = dirs
    pool_file = pool_dir / "01 - Song.mp3"
    pool_file.write_bytes(b"audio")

    remove_pool_file(pool_file)

    assert not pool_file.exists()


def test_promote_pool_file_moves_staged_onto_canonical(dirs):
    pool_dir, user_dir, root = dirs
    dest = pool_dir / "01 - Song.mp3"
    dest.write_bytes(b"OLD")
    staged = pool_dir / ".incoming-abc-01 - Song.mp3"
    staged.write_bytes(b"NEW")

    result = promote_pool_file(staged, dest)

    assert result == dest
    assert dest.read_bytes() == b"NEW"
    assert not staged.exists()


def test_promote_pool_file_noop_when_already_canonical(dirs):
    pool_dir, user_dir, root = dirs
    dest = pool_dir / "01 - Song.mp3"
    dest.write_bytes(b"DATA")

    result = promote_pool_file(dest, dest)

    assert result == dest
    assert dest.read_bytes() == b"DATA"


def test_find_cover_detects_front_image(dirs):
    pool_dir, user_dir, root = dirs
    assert find_cover(pool_dir) is None
    (pool_dir / "front.png").write_bytes(b"img")
    # a track literally named "front" must not be mistaken for a cover
    (pool_dir / "front.mp3").write_bytes(b"audio")
    assert find_cover(pool_dir) == pool_dir / "front.png"


def test_ensure_cover_symlink_creates_and_is_idempotent(dirs):
    pool_dir, user_dir, root = dirs
    cover = pool_dir / "front.jpg"
    cover.write_bytes(b"COVER")
    album = user_dir / "Artist" / "Album"

    link = ensure_cover_symlink(cover, album)

    assert link == album / "front.jpg"
    assert link.is_symlink()
    assert link.resolve() == cover.resolve()
    # second call is a no-op (already has a front.*)
    assert ensure_cover_symlink(cover, album) is None


def test_remove_symlink_drops_lone_cover_and_prunes(dirs):
    pool_dir, user_dir, root = dirs
    pool_file = pool_dir / "01 - Song.mp3"
    pool_file.write_bytes(b"audio")
    cover = pool_dir / "front.jpg"
    cover.write_bytes(b"COVER")
    link = create_symlink(pool_file, user_dir)  # user_dir/Artist/Album/...
    cover_link = ensure_cover_symlink(cover, link.parent)

    remove_symlink(link, library_root=user_dir)

    assert not link.exists()
    assert not cover_link.exists()  # lone cover removed, dir pruned
    assert not (user_dir / "Artist").exists()
    assert user_dir.is_dir()
    assert cover.exists()  # pool cover itself untouched


def test_remove_symlink_keeps_cover_when_tracks_remain(dirs):
    pool_dir, user_dir, root = dirs
    for name in ("01 - A.mp3", "02 - B.mp3"):
        (pool_dir / name).write_bytes(b"audio")
    cover = pool_dir / "front.jpg"
    cover.write_bytes(b"COVER")
    link_a = create_symlink(pool_dir / "01 - A.mp3", user_dir)
    create_symlink(pool_dir / "02 - B.mp3", user_dir)
    cover_link = ensure_cover_symlink(cover, link_a.parent)

    remove_symlink(link_a, library_root=user_dir)

    assert not link_a.exists()
    assert cover_link.exists()  # a track remains -> cover stays
    assert link_a.parent.is_dir()


def test_remove_pool_file_drops_lone_cover(dirs):
    pool_dir, user_dir, root = dirs
    pool_file = pool_dir / "01 - Song.mp3"
    pool_file.write_bytes(b"audio")
    cover = pool_dir / "front.jpg"
    cover.write_bytes(b"COVER")

    remove_pool_file(pool_file)

    assert not pool_file.exists()
    assert not cover.exists()  # last track gone -> pool cover removed, dir pruned
    assert not pool_dir.exists()


def test_remove_pool_file_keeps_cover_when_tracks_remain(dirs):
    pool_dir, user_dir, root = dirs
    keep = pool_dir / "02 - Keep.mp3"
    keep.write_bytes(b"audio")
    pool_file = pool_dir / "01 - Song.mp3"
    pool_file.write_bytes(b"audio")
    cover = pool_dir / "front.jpg"
    cover.write_bytes(b"COVER")

    remove_pool_file(pool_file)

    assert not pool_file.exists()
    assert cover.exists()  # another track remains -> cover stays
    assert keep.exists()


def test_find_sidecars_exact_stem_only(dirs):
    pool_dir, user_dir, root = dirs
    track = pool_dir / "01 - Song.flac"
    track.write_bytes(b"audio")
    (pool_dir / "01 - Song.lrc").write_text("synced")
    (pool_dir / "01 - Song.txt").write_text("plain")
    (pool_dir / "01 - Song.en.lrc").write_text("infix")  # not exact stem -> excluded
    (pool_dir / "02 - Other.lrc").write_text("other")     # other track -> excluded
    (pool_dir / "front.jpg").write_bytes(b"img")          # not a lyrics ext

    names = {p.name for p in find_sidecars(track)}
    assert names == {"01 - Song.lrc", "01 - Song.txt"}


def test_create_symlink_links_sidecars(dirs):
    pool_dir, user_dir, root = dirs
    track = pool_dir / "01 - Song.mp3"
    track.write_bytes(b"audio")
    (pool_dir / "01 - Song.lrc").write_text("synced")

    link = create_symlink(track, user_dir)

    sidecar_link = link.parent / "01 - Song.lrc"
    assert sidecar_link.is_symlink()
    assert sidecar_link.resolve() == (pool_dir / "01 - Song.lrc").resolve()


def test_remove_symlink_takes_lyrics(dirs):
    pool_dir, user_dir, root = dirs
    track = pool_dir / "01 - Song.mp3"
    track.write_bytes(b"audio")
    (pool_dir / "01 - Song.lrc").write_text("synced")
    link = create_symlink(track, user_dir)
    # a real lyrics file a plugin dropped next to the track link
    plugin_file = link.parent / "01 - Song.txt"
    plugin_file.write_text("plugin lyrics")

    remove_symlink(link, library_root=user_dir)

    assert not link.exists()
    assert not (user_dir / "Artist" / "Album" / "01 - Song.lrc").exists()
    assert not plugin_file.exists()          # real plugin file taken too
    assert (pool_dir / "01 - Song.lrc").exists()  # pool sidecar untouched
    assert not (user_dir / "Artist").exists()     # dir pruned


def test_remove_pool_file_takes_pool_sidecars(dirs):
    pool_dir, user_dir, root = dirs
    track = pool_dir / "01 - Song.mp3"
    track.write_bytes(b"audio")
    lrc = pool_dir / "01 - Song.lrc"
    lrc.write_text("synced")

    remove_pool_file(track)

    assert not track.exists()
    assert not lrc.exists()
    assert not pool_dir.exists()  # last track + its lyrics gone -> dir pruned


def test_reconcile_sidecars_links_pool_and_keeps_user_files(dirs):
    pool_dir, user_dir, root = dirs
    track = pool_dir / "01 - Song.mp3"
    track.write_bytes(b"audio")
    (pool_dir / "01 - Song.lrc").write_text("pooled")
    album = user_dir / "Artist" / "Album"
    album.mkdir(parents=True)
    # a user/plugin lyrics file with NO pool counterpart -> must be kept
    own = album / "01 - Song.txt"
    own.write_text("user's own lyrics")
    # a real file whose name matches a pool sidecar -> linked to the pool
    dup = album / "01 - Song.lrc"
    dup.write_text("stale copy")

    reconcile_sidecars(track, album)

    assert own.exists() and not own.is_symlink()   # no pool counterpart -> kept as-is
    assert dup.is_symlink()                         # same name in pool -> linked
    assert dup.resolve() == (pool_dir / "01 - Song.lrc").resolve()


def test_reconcile_sidecars_prunes_stale_pool_link(dirs):
    pool_dir, user_dir, root = dirs
    track = pool_dir / "01 - Song.mp3"
    track.write_bytes(b"audio")
    lrc = pool_dir / "01 - Song.lrc"
    lrc.write_text("pooled")
    link = create_symlink(track, user_dir)  # links the .lrc too
    album = link.parent
    stale_link = album / "01 - Song.lrc"
    assert stale_link.is_symlink()
    # pool sidecar vanishes -> the library link is now a stale link into the pool
    lrc.unlink()

    reconcile_sidecars(track, album)

    assert not stale_link.exists() and not stale_link.is_symlink()  # stale pool link pruned


def test_absorb_user_lyrics_moves_new_file_into_pool(dirs):
    pool_dir, user_dir, root = dirs
    track = pool_dir / "01 - Song.mp3"
    track.write_bytes(b"audio")
    album = user_dir / "Artist" / "Album"
    album.mkdir(parents=True)
    new_lyrics = album / "01 - Song.lrc"
    new_lyrics.write_text("fetched by a plugin")

    absorb_user_lyrics(track, [album])

    pooled = pool_dir / "01 - Song.lrc"
    assert pooled.exists() and pooled.read_text() == "fetched by a plugin"
    assert not new_lyrics.exists()  # moved (not copied) out of the library
    # a following reconcile links it back
    reconcile_sidecars(track, album)
    assert new_lyrics.is_symlink() and new_lyrics.resolve() == pooled.resolve()


def test_reconcile_sidecars_is_noop_when_in_sync(dirs):
    pool_dir, user_dir, root = dirs
    track = pool_dir / "01 - Song.mp3"
    track.write_bytes(b"audio")
    (pool_dir / "01 - Song.lrc").write_text("pooled")
    link = create_symlink(track, user_dir)
    sidecar_link = link.parent / "01 - Song.lrc"
    before = sidecar_link.lstat().st_ino

    reconcile_sidecars(track, link.parent)

    assert sidecar_link.lstat().st_ino == before  # untouched -> not rewritten


def test_links_to(dirs):
    pool_dir, user_dir, root = dirs
    track = pool_dir / "01 - Song.mp3"
    track.write_bytes(b"audio")
    link = create_symlink(track, user_dir)
    assert links_to(link, track)
    other = pool_dir / "02 - Other.mp3"
    other.write_bytes(b"audio")
    assert not links_to(link, other)


def test_update_symlinks(dirs):
    pool_dir, user_dir, root = dirs
    old_pool = pool_dir / "01 - Song.mp3"
    old_pool.write_bytes(b"audio")
    link = create_symlink(old_pool, user_dir)

    new_pool = pool_dir / "01 - Song.flac"
    new_pool.write_bytes(b"better audio")

    new_links = update_symlinks(old_pool, new_pool, [link])

    assert not link.exists()
    assert len(new_links) == 1
    assert new_links[0].resolve() == new_pool.resolve()
