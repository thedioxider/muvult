import json

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from sqlmodel import select

from ..db import User, get_session

user_router = Router()


_USER_HELP = (
    "/help — this message\n"
    "/start — welcome message\n"
    "/settings — change bot preferences\n"
    "/id — show your Telegram ID\n\n"
    "Send audio files to upload them to your library."
)

_ADMIN_SECTION = (
    "\n\n<b>Admin:</b>\n"
    "/adduser [username] [tg_id]\n"
    "/removeuser [username]\n"
    "/settgid [username] [new_tg_id]\n"
    "/setusername [old] [new]\n"
    "/users\n"
    "/recreatelinks [username]\n"
    "/removetrack [path|prefix/*] [username]"
)


@user_router.message(Command("help"))
async def cmd_help(message: Message, is_admin: bool = False) -> None:
    text = _USER_HELP + (_ADMIN_SECTION if is_admin else "")
    await message.answer(text, parse_mode="HTML")


@user_router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Your Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@user_router.message(Command("start"))
async def cmd_start(message: Message, is_admin: bool = False) -> None:
    tg_id = message.from_user.id
    async with get_session() as session:
        result = await session.exec(select(User).where(User.tg_id == tg_id))
        row = result.first()

    if row:
        await message.answer(
            "Send me audio files to upload them to your music library.\n"
            "Use /settings to change bot preferences."
        )
    elif is_admin:
        await message.answer(
            "You don't have a user account yet.\n"
            "Use /adduser to add yourself."
        )
    else:
        await message.answer(
            f"You are not authorized. Ask an admin to add you.\n"
            f"Your Telegram ID: `{tg_id}`",
            parse_mode="Markdown",
        )


@user_router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    tg_id = message.from_user.id
    async with get_session() as session:
        result = await session.exec(select(User).where(User.tg_id == tg_id))
        row = result.first()

    if not row:
        await message.answer("You are not in the system.")
        return

    current = json.loads(row.settings).get("confirmation", "auto")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if current == m else ''}{m}", callback_data=f"set_conf:{m}"
            )
            for m in ("off", "auto", "on")
        ]
    ])
    await message.answer(
        "Confirmation mode for metadata search:\n"
        "  — off: don't ask anything\n"
        "  — auto: ask only when no exact matches found\n"
        "  — on: ask even if exact match is found",
        reply_markup=keyboard
    )


@user_router.callback_query(lambda c: c.data and c.data.startswith("set_conf:"))
async def cb_set_confirmation(callback: CallbackQuery) -> None:
    mode = callback.data.split(":")[1]
    if mode not in ("off", "auto", "on"):
        await callback.answer("Invalid option")
        return

    tg_id = callback.from_user.id
    async with get_session() as session:
        result = await session.exec(select(User).where(User.tg_id == tg_id))
        row = result.first()
        if not row:
            await callback.answer("Not found")
            return
        s = json.loads(row.settings)
        s["confirmation"] = mode
        row.settings = json.dumps(s)
        await session.commit()

    await callback.message.edit_text(f"Confirmation mode set to: <b>{mode}</b>", parse_mode="HTML")
    await callback.answer()
