from pathlib import Path
import pytest
from src.pool import (
    create_symlink,
    promote_pool_file,
    remove_symlink,
    remove_pool_file,
    update_symlinks,
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
