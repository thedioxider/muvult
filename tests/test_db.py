import pytest
from sqlmodel import select
from src.db import init_db, get_session, User, Track, TrackOwnership


@pytest.mark.asyncio
async def test_tables_created(tmp_path):
    await init_db(str(tmp_path / "test.db"))
    async with get_session() as session:
        await session.exec(select(User))
        await session.exec(select(Track))
        await session.exec(select(TrackOwnership))


@pytest.mark.asyncio
async def test_idempotent_init(tmp_path):
    db_path = str(tmp_path / "test.db")
    await init_db(db_path)
    await init_db(db_path)


@pytest.mark.asyncio
async def test_insert_and_query(tmp_path):
    await init_db(str(tmp_path / "test.db"))
    async with get_session() as session:
        user = User(tg_id=42, username="alice", navidrome_user_id="nd-1", navidrome_library_id=1)
        session.add(user)
        await session.commit()
        result = await session.exec(select(User).where(User.tg_id == 42))
        found = result.first()
    assert found is not None
    assert found.username == "alice"
