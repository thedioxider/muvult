from pathlib import Path
import pytest
from src.pool import (
    create_symlink,
    ensure_cover_symlink,
    find_cover,
    promote_pool_file,
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
