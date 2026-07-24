import json

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from sqlmodel import select

from ..db import User, get_session
from ..models import DEFAULT_SETTINGS
from ..tg_utils import safe_answer

user_router = Router()


_USER_HELP = (
    "/help — this message\n"
    "/start — welcome message\n"
    "/settings — change bot preferences\n"
    "/id — show your Telegram ID\n\n"
    "<b>Uploading</b>\n"
    "Uploaded audio is matched against MusicBrainz and added to your library. "
    "When a match is uncertain, a confirmation prompt lists the candidates:\n"
    "  — Each candidate shows a confidence % — higher is a closer match.\n"
    "  — <b>®</b> marks the canonically-registered recording (one with an ISRC), "
    "usually the official studio version — a good default when unsure.\n"
    "  — <b>Import</b> / <b>#n</b> selects a candidate; <b>See others</b> and "
    "<b>Show all results</b> list more; <b>Import as-is</b> keeps the file's own "
    "tags; <b>Skip</b> discards the file.\n\n"
    "/settings controls how often prompts appear and album metadata enrichment."
)

_ADMIN_SECTION = (
    "\n\n<b>Admin:</b>\n"
    "/adduser [username] [tg_id]\n"
    "/removeuser [username]\n"
    "/settgid [username] [new_tg_id]\n"
    "/setusername [old] [new]\n"
    "/users\n"
    "/recreatelinks [username]\n"
    "/removetrack [path|prefix/*] [username]\n"
    "/retag [path|prefix/*|empty for whole library]"
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

    s = json.loads(row.settings)
    do_tag = s.get("tag", DEFAULT_SETTINGS["tag"])
    current = s.get("confirmation", DEFAULT_SETTINGS["confirmation"])
    enrich = s.get("enrich", DEFAULT_SETTINGS["enrich"])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if do_tag == v else ''}tagging {'on' if v else 'off'}",
                callback_data=f"set_tag:{'on' if v else 'off'}",
            )
            for v in (False, True)
        ],
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if current == m else ''}{m}", callback_data=f"set_conf:{m}"
            )
            for m in ("off", "auto", "on")
        ],
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if enrich == v else ''}album metadata {'on' if v else 'off'}",
                callback_data=f"set_enrich:{'on' if v else 'off'}",
            )
            for v in (False, True)
        ],
    ])
    await message.answer(
        "Track tagging (match each file against MusicBrainz):\n"
        "  — off: import everything as-is, no lookups\n"
        "  — on: identify and tag tracks (confirmation + album metadata below apply)\n\n"
        "Confirmation mode for metadata search:\n"
        "  — off: don't ask anything\n"
        "  — auto: ask only when no exact matches found\n"
        "  — on: ask even if exact match is found\n\n"
        "Album metadata fetch (album, track number, disc, year, cover art):\n"
        "  — off: faster, tags title and artist only\n"
        "  — on: slower, adds a MusicBrainz lookup per track",
        reply_markup=keyboard
    )


@user_router.callback_query(lambda c: c.data and c.data.startswith("set_tag:"))
async def cb_set_tag(callback: CallbackQuery) -> None:
    choice = callback.data.split(":")[1]
    if choice not in ("on", "off"):
        await safe_answer(callback, "Invalid option")
        return

    tg_id = callback.from_user.id
    async with get_session() as session:
        result = await session.exec(select(User).where(User.tg_id == tg_id))
        row = result.first()
        if not row:
            await safe_answer(callback, "Not found")
            return
        s = json.loads(row.settings)
        s["tag"] = choice == "on"
        row.settings = json.dumps(s)
        await session.commit()

    await callback.message.edit_text(f"Track tagging: <b>{choice}</b>", parse_mode="HTML")
    await safe_answer(callback)


@user_router.callback_query(lambda c: c.data and c.data.startswith("set_conf:"))
async def cb_set_confirmation(callback: CallbackQuery) -> None:
    mode = callback.data.split(":")[1]
    if mode not in ("off", "auto", "on"):
        await safe_answer(callback, "Invalid option")
        return

    tg_id = callback.from_user.id
    async with get_session() as session:
        result = await session.exec(select(User).where(User.tg_id == tg_id))
        row = result.first()
        if not row:
            await safe_answer(callback, "Not found")
            return
        s = json.loads(row.settings)
        s["confirmation"] = mode
        row.settings = json.dumps(s)
        await session.commit()

    await callback.message.edit_text(f"Confirmation mode set to: <b>{mode}</b>", parse_mode="HTML")
    await safe_answer(callback)


@user_router.callback_query(lambda c: c.data and c.data.startswith("set_enrich:"))
async def cb_set_enrich(callback: CallbackQuery) -> None:
    choice = callback.data.split(":")[1]
    if choice not in ("on", "off"):
        await safe_answer(callback, "Invalid option")
        return

    tg_id = callback.from_user.id
    async with get_session() as session:
        result = await session.exec(select(User).where(User.tg_id == tg_id))
        row = result.first()
        if not row:
            await safe_answer(callback, "Not found")
            return
        s = json.loads(row.settings)
        s["enrich"] = choice == "on"
        row.settings = json.dumps(s)
        await session.commit()

    await callback.message.edit_text(f"Album metadata fetch: <b>{choice}</b>", parse_mode="HTML")
    await safe_answer(callback)
