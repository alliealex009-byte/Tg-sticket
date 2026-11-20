"""Admin-only: clone an existing Telegram sticker pack into the owner's account."""
from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .. import keyboards as kb
from ..context import AppContext
from ..utils.helpers import esc

log = logging.getLogger(__name__)
router = Router(name="clone")
_LINK = re.compile(r"(?:https?://)?t\.me/addstickers/([A-Za-z0-9_]{1,64})", re.I)
_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def parse_pack_name(text: str) -> str | None:
    text = (text or "").strip()
    m = _LINK.search(text)
    if m:
        return m.group(1)
    return text if _NAME.match(text) else None


async def do_clone(message: Message, ctx: AppContext, source: str) -> None:
    owner = ctx.owner_id
    if not owner:
        await message.answer("No owner set. Send /owner from your account (or set OWNER_ID in .env).")
        return
    status = await message.answer(f"📥 Cloning <code>{esc(source)}</code>…")

    async def progress(done: int, total: int) -> None:
        try:
            await status.edit_text(f"📥 Cloning… {done}/{total}")
        except TelegramBadRequest:
            pass

    try:
        res = await ctx.stickers.clone_set(message.bot, source, owner, ctx.settings.channel_id, progress)
    except ValueError as exc:
        await status.edit_text("❌ " + esc(exc))
        return
    except TelegramAPIError as exc:
        log.warning("Clone failed: %s", exc)
        text = str(exc).lower()
        hint = "That sticker pack wasn't found." if "invalid" in text or "not found" in text else esc(str(exc)[:200])
        await status.edit_text("❌ " + hint)
        return
    head = "ℹ️ Already cloned:" if res.already else f"✅ Cloned {res.count} sticker(s):"
    lines = [head] + [f"• <a href=\"{esc(p.link)}\">{esc(p.title)}</a> ({p.added})" for p in res.packs]
    await status.edit_text("\n".join(lines), reply_markup=kb.pack_links([(p.title, p.link) for p in res.packs]))


@router.message(Command("clone"))
async def cmd_clone(message: Message, command: CommandObject, ctx: AppContext) -> None:
    if not ctx.is_admin(message.from_user.id):
        await message.answer(f"🔒 /clone is admin-only. Your ID is <code>{message.from_user.id}</code> — "
                             "put it in ADMIN_IDS in .env and restart.")
        return
    source = parse_pack_name(command.args or "")
    if not source:
        await message.answer("Usage: /clone <i>https://t.me/addstickers/PackName</i>\nor just send me any sticker from the pack.")
        return
    await do_clone(message, ctx, source)


@router.message(F.sticker)
async def sticker_clone(message: Message, ctx: AppContext) -> None:
    if not ctx.is_admin(message.from_user.id):
        return
    if not message.sticker.set_name:
        await message.answer("That sticker isn't part of a pack.")
        return
    await do_clone(message, ctx, message.sticker.set_name)
