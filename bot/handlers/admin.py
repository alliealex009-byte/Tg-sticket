"""Admin panel. Every handler in this router is guarded by AdminFilter (IDs from ADMIN_IDS)."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, Message, TelegramObject

from .. import keyboards as kb
from ..context import AppContext
from ..database import is_premium_doc
from ..limits import LIMIT_ALIASES, LIMIT_LABELS
from ..utils.helpers import esc, human_duration, human_size
from .common import safe_answer

log = logging.getLogger(__name__)
router = Router(name="admin")
BROADCAST_DELAY = 0.06   # ~16 msgs/sec, safely under Telegram's ~30/sec broadcast ceiling


class AdminFilter(BaseFilter):
    async def __call__(self, event: TelegramObject, ctx: AppContext) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and ctx.is_admin(user.id))


router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


def _meminfo() -> str:
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        total, avail = info["MemTotal"], info["MemAvailable"]
        return f"{human_size(total - avail)} / {human_size(total)}"
    except Exception:  # noqa: BLE001
        return "n/a"


async def dashboard(ctx: AppContext) -> str:
    db = ctx.db
    stats = await db.get_stats()
    db_ok = await db.ping()
    total, a1, a7 = await db.count_users(), await db.count_active_users(1), await db.count_active_users(7)
    try:
        load = os.getloadavg()
        load_txt = f"{load[0]:.2f} {load[1]:.2f} {load[2]:.2f}"
    except (OSError, AttributeError):
        load_txt = "n/a"
    disk = shutil.disk_usage(ctx.settings.data_dir if os.path.isdir(ctx.settings.data_dir) else ".")
    last_err = stats.get("last_error", "—")
    return (
        "🛠 <b>Admin dashboard</b>\n\n"
        f"👥 Users: <b>{total}</b> · active 24h: <b>{a1}</b> · 7d: <b>{a7}</b>\n"
        f"💎 Premium: <b>{await db.count_premium()}</b> · 🚫 Banned: <b>{len(db.banned)}</b>\n\n"
        f"✨ Sticker generations: <b>{stats.get('stickers_generated', 0)}</b>\n"
        f"🎨 AI generations: <b>{stats.get('ai_generations', 0)}</b>\n"
        f"⬇️ Image downloads: <b>{stats.get('images_downloaded', 0)}</b>\n"
        f"🔎 Searches: <b>{stats.get('searches', 0)}</b>\n"
        f"📦 Sticker packs: <b>{await db.count_packs()}</b>\n\n"
        f"🔌 API usage — OpenAI calls: <b>{stats.get('openai_calls', 0)}</b> · search-provider calls: "
        f"<b>{stats.get('search_api_calls', 0)}</b>\n"
        f"⚠️ Errors: <b>{stats.get('errors', 0)}</b>\n<code>{esc(str(last_err)[:200])}</code>\n\n"
        f"🖥 Uptime: {human_duration(time.time() - ctx.started_at)} · load {load_txt}\n"
        f"🧠 RAM: {_meminfo()} · 💾 Disk free: {human_size(disk.free)}\n"
        f"⚙️ Tasks: {ctx.gate.active}/{ctx.gate.limit} running, {ctx.gate.waiting} waiting\n"
        f"🗄 Database: {'✅ online' if db_ok else '❌ DOWN'}\n"
        f"🤖 AI: {'on' if ctx.ai.enabled else 'off'} · search: {', '.join(ctx.search.provider_names) or 'none'}\n"
        f"🔧 Maintenance: {'ON' if ctx.runtime['maintenance'] else 'off'}"
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, ctx: AppContext) -> None:
    await message.answer(await dashboard(ctx), reply_markup=kb.admin_panel(ctx.runtime["maintenance"]))


@router.message(Command("stats"))
async def cmd_stats(message: Message, ctx: AppContext) -> None:
    await message.answer(await dashboard(ctx))


@router.callback_query(F.data == "adm:refresh")
async def cb_refresh(cb: CallbackQuery, ctx: AppContext) -> None:
    await safe_answer(cb, "Refreshed")
    if cb.message:
        with suppress(TelegramBadRequest):
            await cb.message.edit_text(await dashboard(ctx), reply_markup=kb.admin_panel(ctx.runtime["maintenance"]))


@router.callback_query(F.data == "adm:maint")
async def cb_maint(cb: CallbackQuery, ctx: AppContext) -> None:
    await _set_maintenance(ctx, not ctx.runtime["maintenance"])
    await cb_refresh(cb, ctx)


@router.callback_query(F.data == "adm:users")
async def cb_users(cb: CallbackQuery, ctx: AppContext) -> None:
    await safe_answer(cb)
    if cb.message:
        await cb.message.answer(await _users_text(ctx))


async def _users_text(ctx: AppContext) -> str:
    rows = await ctx.db.recent_users(15)
    lines = ["👥 <b>Most recently active users</b>"]
    for u in rows:
        flag = "💎" if is_premium_doc(u) else ("🚫" if ctx.db.is_banned(u["_id"]) else "•")
        name = f"@{esc(u['username'])}" if u.get("username") else esc(u.get("first_name", ""))
        lines.append(f"{flag} <code>{u['_id']}</code> {name} — {u.get('stats', {}).get('stickers', 0)} stickers")
    lines.append("\nUse /users <i>id</i> for details.")
    return "\n".join(lines)


@router.message(Command("users"))
async def cmd_users(message: Message, command: CommandObject, ctx: AppContext) -> None:
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(await _users_text(ctx))
        return
    uid = _uid(arg)
    doc = await ctx.db.get_user(uid) if uid else None
    if not doc:
        await message.answer("User not found.")
        return
    await message.answer(
        f"👤 <code>{uid}</code> @{esc(doc.get('username', ''))}\nPremium: {is_premium_doc(doc)} · "
        f"Banned: {ctx.db.is_banned(uid)}\nStats: {esc(doc.get('stats', {}))}\nToday: {esc(doc.get('daily', {}))}")


def _uid(text: str) -> int | None:
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, ctx: AppContext) -> None:
    parts = (command.args or "").split(maxsplit=1)
    uid = _uid(command.args or "")
    if uid is None:
        return await message.answer("Usage: /ban <i>user_id</i> [reason]")
    if ctx.is_admin(uid):
        return await message.answer("You can't ban an admin.")
    await ctx.db.ban(uid, parts[1] if len(parts) > 1 else "", by=message.from_user.id)
    await message.answer(f"🚫 Banned <code>{uid}</code>.")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, ctx: AppContext) -> None:
    uid = _uid(command.args or "")
    if uid is None:
        return await message.answer("Usage: /unban <i>user_id</i>")
    ok = await ctx.db.unban(uid)
    await message.answer(f"✅ Unbanned <code>{uid}</code>." if ok else "That user wasn't banned.")


@router.message(Command("setlimit"))
async def cmd_setlimit(message: Message, command: CommandObject, ctx: AppContext) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2 or parts[0].lower() not in LIMIT_ALIASES or not parts[1].isdigit():
        kinds = "|".join(sorted({k for k in LIMIT_ALIASES if len(k) > 4}))
        return await message.answer(f"Usage: /setlimit <i>{kinds}</i> <i>number</i>\n"
                                    "Sets the FREE-user daily limit (premium = × multiplier).")
    kind, value = LIMIT_ALIASES[parts[0].lower()], int(parts[1])
    ctx.runtime["limits"][kind] = value
    await ctx.db.set_setting("limits", ctx.runtime["limits"])
    await message.answer(f"✅ Free limit for {LIMIT_LABELS[kind]} is now <b>{value}</b>"
                         f" (premium: {value * ctx.settings.premium_multiplier}).")


@router.message(Command("premium"))
async def cmd_premium(message: Message, command: CommandObject, ctx: AppContext) -> None:
    parts = (command.args or "").split()
    usage = "Usage:\n/premium grant <i>id</i> [days]\n/premium revoke <i>id</i>\n/premium status <i>id</i>"
    if len(parts) < 2 or parts[0].lower() not in ("grant", "revoke", "status") or not parts[1].isdigit():
        return await message.answer(usage)
    action, uid = parts[0].lower(), int(parts[1])
    doc = await ctx.db.get_user(uid)
    if not doc:
        return await message.answer("That user hasn't started the bot yet.")
    if action == "grant":
        days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        await ctx.db.set_premium(uid, days, by=message.from_user.id)
        await message.answer(f"💎 Premium granted to <code>{uid}</code> " + (f"for {days} days." if days else "(no expiry)."))
    elif action == "revoke":
        await ctx.db.revoke_premium(uid)
        await message.answer(f"Premium revoked for <code>{uid}</code>.")
    else:
        await message.answer(f"Premium: <b>{is_premium_doc(doc)}</b> · until: {esc(doc.get('premium_until'))}")


async def _set_maintenance(ctx: AppContext, on: bool) -> None:
    ctx.runtime["maintenance"] = on
    await ctx.db.set_setting("maintenance", on)


@router.message(Command("maintenance"))
async def cmd_maintenance(message: Message, command: CommandObject, ctx: AppContext) -> None:
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        return await message.answer(f"Maintenance is <b>{'ON' if ctx.runtime['maintenance'] else 'off'}</b>.\nUsage: /maintenance on|off")
    await _set_maintenance(ctx, arg == "on")
    await message.answer(f"🔧 Maintenance mode {'enabled — only admins can use the bot' if arg == 'on' else 'disabled'}.")


@router.message(Command("owner"))
async def cmd_owner(message: Message, ctx: AppContext) -> None:
    ctx.runtime["owner_id"] = message.from_user.id
    await ctx.db.set_setting("owner_id", message.from_user.id)
    await message.answer(f"👑 Pack owner is now your account (<code>{message.from_user.id}</code>). "
                         "All new stickers and clones go to packs owned by you.")


@router.message(Command("postpacks"))
async def cmd_postpacks(message: Message, ctx: AppContext) -> None:
    if not ctx.settings.channel_id:
        return await message.answer("Set CHANNEL_ID in .env first (the bot must be an admin of the channel).")
    ok, bad = await ctx.stickers.post_unposted(message.bot, ctx.settings.channel_id)
    await message.answer(f"📣 Posted {ok} pack(s) to the channel · {bad} failed.")


@router.message(Command("restart"))
async def cmd_restart(message: Message, ctx: AppContext) -> None:
    await message.answer("♻️ Restarting… (Docker/systemd will bring me back in a few seconds)")
    ctx.restart_event.set()


# ----------------------------------------------------------------------- broadcast
@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject, ctx: AppContext) -> None:
    text = (command.args or "").strip()
    if not text:
        return await message.answer("Usage: /broadcast <i>message</i> (HTML allowed). You'll be asked to confirm.")
    n = await ctx.db.count_users()
    ctx.pending_broadcasts[message.from_user.id] = text
    await message.answer(f"📣 <b>Preview</b>\n\n{text}", reply_markup=kb.confirm_broadcast(n))


@router.callback_query(F.data == "adm:bc_no")
async def cb_bc_no(cb: CallbackQuery, ctx: AppContext) -> None:
    ctx.pending_broadcasts.pop(cb.from_user.id, None)
    await safe_answer(cb, "Cancelled")
    if cb.message:
        with suppress(TelegramBadRequest):
            await cb.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "adm:bc_yes")
async def cb_bc_yes(cb: CallbackQuery, ctx: AppContext) -> None:
    text = ctx.pending_broadcasts.pop(cb.from_user.id, None)
    await safe_answer(cb)
    if not text or not cb.message:
        return
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)
    asyncio.create_task(_run_broadcast(cb.bot, ctx, cb.from_user.id, text))
    await cb.message.answer("📤 Broadcast started. I'll report when it's done.")


async def _run_broadcast(bot, ctx: AppContext, admin_id: int, text: str) -> None:
    sent = failed = blocked = 0
    async for uid in ctx.db.iter_user_ids():
        for _ in range(3):
            try:
                await bot.send_message(uid, text)
                sent += 1
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                continue
            except TelegramForbiddenError:
                blocked += 1
                await ctx.db.mark_blocked(uid)
            except Exception:  # noqa: BLE001
                failed += 1
            break
        await asyncio.sleep(BROADCAST_DELAY)
    log.info("Broadcast finished: sent=%d blocked=%d failed=%d", sent, blocked, failed)
    with suppress(Exception):
        await bot.send_message(admin_id, f"📣 Broadcast done: ✅ {sent} sent · 🚫 {blocked} blocked · ❌ {failed} failed")
