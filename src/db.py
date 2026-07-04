from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import Column, ForeignKey, Integer, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Field, SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

_DB_PATH = "/data/muvult.db"
_session_factory: async_sessionmaker | None = None


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    tg_id: int = Field(unique=True)
    username: str = Field(unique=True)
    navidrome_user_id: str
    navidrome_library_id: int
    settings: str = Field(default="{}")


class Track(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    pool_path: str = Field(unique=True)
    musicbrainz_id: str | None = None
    format: str
    bitrate: int
    # True for as-is (unmatched) imports, whose library symlinks are flat.
    is_asis: bool = Field(default=False)


class TrackOwnership(SQLModel, table=True):
    __tablename__ = "track_ownership"
    track_id: int = Field(
        sa_column=Column(Integer, ForeignKey("track.id", ondelete="CASCADE"), primary_key=True)
    )
    user_id: int = Field(
        sa_column=Column(Integer, ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    )
    symlink_path: str


async def init_db(db_path: str = _DB_PATH) -> None:
    global _session_factory
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA foreign_keys = ON"))
        await conn.run_sync(SQLModel.metadata.create_all)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _session_factory() as session:
        await session.execute(text("PRAGMA foreign_keys = ON"))
        yield session
