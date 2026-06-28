from typing import Any, Callable, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlmodel import select

from .db import User, get_session


def is_admin(tg_id: int, admin_ids: list[int]) -> bool:
    return tg_id in admin_ids


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from .config import settings

        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        tg_id = user.id

        if is_admin(tg_id, settings.admin_tg_ids):
            data["is_admin"] = True
            return await handler(event, data)

        async with get_session() as session:
            result = await session.exec(select(User).where(User.tg_id == tg_id))
            row = result.first()

        if row is not None:
            data["is_admin"] = False
            return await handler(event, data)

        # Silently ignore unauthorized
