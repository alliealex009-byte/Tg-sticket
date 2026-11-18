from __future__ import annotations

from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..context import AppContext
from ..database import is_premium_doc
from ..services.ai_generator import STYLES, is_premium_style
from ..utils.helpers import ensure_aware, esc, today_str
from ..utils.validators import ValidationError, parse_hex_color
from .common import edit_or_send, safe_answer

router = Router(name="profile")
BG_CYCLE = ["auto", "keep", "transparent", "white"]
OUTLINE_CYCLE = ["none", "white", "black"]


async def profile_text(ctx: AppContext, user: dict) -> str:
    uid = user["_id"]
    fresh = await ctx.db.get_user(uid) or user
    premium = is_premium_doc(fresh)
    daily = fresh.get("daily", {})
    if daily.get("date") != today_str():
        daily = {"gen": 0, "search": 0, "sticker": 0}
    st = fresh.get("stats", {})

    def usage(kind: str) -> str:
        lim = ctx.limit(kind, premium, uid)
        return f"{daily.get(kind, 0)}/{'∞' if lim is None else lim}"

    if premium:
        until = ensure_aware(fresh.get("premium_until"))
        prem = "💎 Premium" + (f" until {until:%Y-%m-%d}" if until else " (no expiry)")
    else:
        prem = "Free"
    created = ensure_aware(fresh.get("created_at"))
    member = f"{created:%Y-%m-%d}" if created else "—"
    return (
        "👤 <b>Your profile</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📛 Username: {('@' + esc(fresh['username'])) if fresh.get('username') else '—'}\n"
        f"✨ Stickers generated: <b>{st.get('stickers', 0)}</b>\n"
        f"⬇️ Images downloaded: <b>{st.get('downloads', 0)}</b>\n"
        f"📦 Sticker packs: <b>{await ctx.db.count_user_packs(uid)}</b>\n\n"
        f"📊 <b>Today</b>\n🎨 AI: {usage('gen')}   🔎 Searches: {usage('search')}   🧸 Stickers: {usage('sticker')}\n\n"
        f"💎 Status: {prem}\n"
        f"📅 Member since: {member}"
    )


@router.message(Command("profile"))
async def cmd_profile(message: Message, ctx: AppContext, user: dict) -> None:
    await message.answer(await profile_text(ctx, user), reply_markup=kb.back_menu())


@router.message(Command("settings"))
async def cmd_settings(message: Message, ctx: AppContext, user: dict) -> None:
    await message.answer("⚙️ <b>Settings</b>\nThese apply to every sticker you create 💗",
                         reply_markup=kb.settings_menu(user.get("settings", {})))


@router.callback_query(F.data == "menu:settings")
async def cb_settings(cb: CallbackQuery, ctx: AppContext) -> None:
    await safe_answer(cb)
    fresh = await ctx.db.get_user(cb.from_user.id)
    if cb.message and fresh:
        await edit_or_send(cb.message, "⚙️ <b>Settings</b>\nThese apply to every sticker you create 💗",
                           kb.settings_menu(fresh.get("settings", {})))


@router.message(Command("bgcolor"))
async def cmd_bgcolor(message: Message, command: CommandObject, ctx: AppContext, user: dict) -> None:
    try:
        parse_hex_color(command.args or "")
    except ValidationError:
        await message.answer("Usage: /bgcolor #FFAACC — sets a custom background colour for stickers.")
        return
    color = "#" + (command.args or "").strip().lstrip("#").upper()
    await ctx.db.set_user_setting(user["_id"], "custom_color", color)
    await ctx.db.set_user_setting(user["_id"], "bg_mode", "custom")
    await message.answer(f"🎨 Background set to <code>{esc(color)}</code>. Switch back any time in /settings.")


@router.callback_query(F.data.startswith("set:"))
async def cb_set(cb: CallbackQuery, ctx: AppContext, user: dict) -> None:
    uid = cb.from_user.id
    parts = (cb.data or "").split(":")
    action = parts[1]
    fresh = await ctx.db.get_user(uid) or user
    s = fresh.get("settings", {})
    msg = cb.message
    if action == "profile" and msg:
        await safe_answer(cb)
        await msg.answer(await profile_text(ctx, fresh), reply_markup=kb.back_menu())
        return
    if action == "style" and msg:
        await safe_answer(cb)
        await edit_or_send(msg, "🎨 Choose your default AI style:", kb.default_style_picker())
        return
    if action == "st" and len(parts) == 3 and parts[2] in STYLES:
        if is_premium_style(parts[2]) and not is_premium_doc(fresh) and not ctx.is_admin(uid):
            return await safe_answer(cb, "💎 Premium style", alert=True)
        await ctx.db.set_user_setting(uid, "style", parts[2])
        await safe_answer(cb, "Default style saved ✅")
    elif action == "bg":
        cur = s.get("bg_mode", "auto")
        nxt = BG_CYCLE[(BG_CYCLE.index(cur) + 1) % len(BG_CYCLE)] if cur in BG_CYCLE else "auto"
        await ctx.db.set_user_setting(uid, "bg_mode", nxt)
        await safe_answer(cb)
    elif action == "outline":
        cur = s.get("outline", "none")
        nxt = OUTLINE_CYCLE[(OUTLINE_CYCLE.index(cur) + 1) % len(OUTLINE_CYCLE)] if cur in OUTLINE_CYCLE else "none"
        await ctx.db.set_user_setting(uid, "outline", nxt)
        await safe_answer(cb)
    elif action == "shadow":
        await ctx.db.set_user_setting(uid, "shadow", not s.get("shadow", False))
        await safe_answer(cb)
    else:
        return await safe_answer(cb)
    fresh = await ctx.db.get_user(uid) or fresh
    if msg:
        with suppress(TelegramBadRequest):
            await msg.edit_text("⚙️ <b>Settings</b>\nThese apply to every sticker you create 💗\n"
                                "<i>Tip: /bgcolor #FFAACC sets a custom background colour.</i>",
                                reply_markup=kb.settings_menu(fresh.get("settings", {})))
