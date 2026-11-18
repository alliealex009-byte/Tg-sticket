from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, LinkPreviewOptions, Message

from .. import keyboards as kb
from ..context import AppContext
from ..database import is_premium_doc
from ..services.category_detector import CATEGORIES, category_label, pack_title
from ..services.image_processor import make_contact_sheet
from ..utils.helpers import esc
from .common import edit_or_send, queue_message, safe_answer

log = logging.getLogger(__name__)
router = Router(name="packs")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_publishing: set = set()


async def packs_text(ctx: AppContext, user: dict) -> tuple[str, bool]:
    uid = user["_id"]
    packs = await ctx.db.user_packs(uid)
    queued = await ctx.db.count_queued(uid)
    lines = ["📦 <b>My Sticker Packs</b>\n"]
    if packs:
        for p in packs:
            lines.append(f"• <a href=\"{esc(p['link'])}\">{esc(p['title'])}</a> — {p['count']} stickers")
    else:
        lines.append("You don't have any packs yet.")
    lines.append(f"\n🕒 Waiting in queue: <b>{queued}</b>")
    if not queued:
        lines.append("Create stickers and tap <b>Add to pack</b> to queue them 💗")
    return "\n".join(lines), queued > 0


async def show_packs(target: Message, ctx: AppContext, user: dict, edit: bool = False) -> None:
    text, has_queue = await packs_text(ctx, user)
    if edit:
        await edit_or_send(target, text, kb.packs_menu(has_queue))
    else:
        await target.answer(text, reply_markup=kb.packs_menu(has_queue), link_preview_options=NO_PREVIEW)


@router.message(Command("mypacks"))
async def cmd_mypacks(message: Message, ctx: AppContext, user: dict) -> None:
    await show_packs(message, ctx, user)


@router.callback_query(F.data == "menu:packs")
async def cb_packs(cb: CallbackQuery, ctx: AppContext, user: dict) -> None:
    await safe_answer(cb)
    if cb.message:
        await show_packs(cb.message, ctx, user, edit=True)


@router.message(Command("addpack"))
async def cmd_addpack(message: Message, ctx: AppContext, user: dict) -> None:
    draft = await ctx.db.latest_draft(user["_id"])
    if not draft:
        await message.answer("Create a sticker first, then use /addpack 💗")
        return
    await queue_message(ctx, user, message, draft["_id"])


@router.callback_query(F.data.startswith("sk:q:"))
async def cb_queue(cb: CallbackQuery, ctx: AppContext, user: dict) -> None:
    await safe_answer(cb)
    if cb.message:
        sid = (cb.data or "").split(":")[2]
        status = await queue_message(ctx, user, cb.message, sid)
        if status in ("queued", "duplicate"):
            doc = await ctx.db.get_sticker(sid)
            if doc:
                with suppress(TelegramBadRequest):
                    await cb.message.edit_reply_markup(
                        reply_markup=kb.sticker_actions(sid, doc["category"], True, ctx.is_admin(user["_id"])))


@router.callback_query(F.data.startswith("sk:c:"))
async def cb_category(cb: CallbackQuery) -> None:
    await safe_answer(cb)
    if cb.message:
        sid = (cb.data or "").split(":")[2]
        await cb.message.answer("🏷 Choose the right category for this sticker:", reply_markup=kb.category_picker(sid))


@router.callback_query(F.data.startswith("sc:"))
async def cb_set_category(cb: CallbackQuery, ctx: AppContext, user: dict) -> None:
    try:
        _, sid, cat = (cb.data or "").split(":")
    except ValueError:
        return await safe_answer(cb)
    if cat not in CATEGORIES or not await ctx.stickers.set_category(user["_id"], sid, cat):
        return await safe_answer(cb, "That sticker expired.", alert=True)
    await safe_answer(cb, f"Category set to {category_label(cat)} ✅")
    if cb.message:
        with suppress(TelegramBadRequest):
            await cb.message.edit_text(f"✅ Category set to <b>{esc(category_label(cat))}</b>.\n"
                                       f"Pack title: <b>{esc(pack_title(cat, ctx.settings.pack_credit))}</b>")


@router.callback_query(F.data.startswith("sk:g:"))
async def cb_category_pack(cb: CallbackQuery, ctx: AppContext, user: dict) -> None:
    if not ctx.is_admin(user["_id"]):
        return await safe_answer(cb, "Admins only.", alert=True)
    await safe_answer(cb)
    owner = ctx.settings.category_pack_owner_id or user["_id"]
    doc = await ctx.db.get_sticker((cb.data or "").split(":")[2])
    if not doc or not cb.message:
        return
    try:
        pack = await ctx.stickers.add_to_category_pack(cb.bot, doc, owner)
        await cb.message.answer(f"🌐 Added to <a href=\"https://t.me/addstickers/{esc(pack.name)}\">{esc(pack.title)}</a>",
                                link_preview_options=NO_PREVIEW)
    except Exception as exc:  # noqa: BLE001
        log.warning("Category pack add failed: %s", exc)
        await cb.message.answer(f"❌ Telegram rejected that: {esc(str(exc)[:200])}")


@router.message(Command("createpack"))
async def cmd_createpack(message: Message, ctx: AppContext, user: dict) -> None:
    await preview(message, ctx, user)


@router.callback_query(F.data == "pk:preview")
async def cb_preview(cb: CallbackQuery, ctx: AppContext, user: dict) -> None:
    await safe_answer(cb)
    if cb.message:
        await preview(cb.message, ctx, user)


async def preview(message: Message, ctx: AppContext, user: dict) -> None:
    uid = user["_id"]
    queued = await ctx.db.queued_stickers(uid)
    if not queued:
        await message.answer("Your queue is empty. Create a sticker and tap <b>Add to pack</b> first 💗",
                             reply_markup=kb.main_menu())
        return
    cap = ctx.settings.max_pack_stickers
    groups: dict = {}
    for s in queued:
        groups.setdefault(s["category"], []).append(s)
    lines = ["📦 <b>Pack preview</b>\n"]
    for cat, items in groups.items():
        existing = await ctx.db.active_pack(uid, cat, "user")
        title = pack_title(cat, ctx.settings.pack_credit)
        if existing and existing["count"] < cap:
            lines.append(f"• <b>{esc(existing['title'])}</b> — adds {len(items)} to your existing pack ({existing['count']}/{cap})")
        else:
            lines.append(f"• <b>{esc(title)}</b> — new pack with {len(items)} sticker(s)")
    lines.append("\nTap <b>Publish pack</b> to create it on Telegram.")
    files = [await ctx.stickers.read_file(s) for s in queued[:20]]
    sheet = await asyncio.to_thread(make_contact_sheet, files)
    await message.answer_photo(BufferedInputFile(sheet, "preview.jpg"), caption="\n".join(lines)[:1000],
                               reply_markup=kb.pack_preview())


@router.callback_query(F.data == "pk:clear")
async def cb_clear(cb: CallbackQuery, ctx: AppContext, user: dict) -> None:
    n = await ctx.db.clear_queue(user["_id"])
    await safe_answer(cb, f"Cleared {n} sticker(s) 🗑")
    if cb.message:
        await cb.message.answer("🗑 Queue cleared. Your stickers are still saved — you can add them again anytime.",
                                reply_markup=kb.main_menu())


@router.message(Command("packclear"))
async def cmd_packclear(message: Message, ctx: AppContext, user: dict) -> None:
    n = await ctx.db.clear_queue(user["_id"])
    await message.answer(f"🗑 Cleared {n} queued sticker(s).")


@router.callback_query(F.data == "pk:go")
async def cb_publish(cb: CallbackQuery, ctx: AppContext, user: dict) -> None:
    uid = user["_id"]
    if not cb.message:
        return await safe_answer(cb)
    if uid in _publishing:
        return await safe_answer(cb, "Already publishing — one moment 💗", alert=True)
    await safe_answer(cb)
    _publishing.add(uid)
    status = await cb.message.answer("📦 Publishing your sticker pack…")
    try:
        async def progress(done: int, total: int) -> None:
            with suppress(TelegramBadRequest):
                await status.edit_text(f"📦 Publishing… {done}/{total}")

        res = await ctx.stickers.publish(cb.bot, uid, "user", premium=is_premium_doc(user),
                                         admin=ctx.is_admin(uid), progress=progress)
    except Exception:  # noqa: BLE001
        log.exception("Publish crashed")
        await status.edit_text("❌ Something went wrong while publishing. Your stickers are still queued — try again.")
        return
    finally:
        _publishing.discard(uid)
    lines = []
    if res.published:
        lines.append(f"✅ Published <b>{res.published}</b> sticker(s)!")
        for p in res.packs:
            lines.append(f"• <a href=\"{esc(p.link)}\">{esc(p.title)}</a> (+{p.added})")
    if res.error == "pack_limit":
        lines.append("\n📦 You've reached your pack limit. Premium members can have more packs 💎")
    elif res.error == "forbidden":
        lines.append("\n⚠️ Telegram wouldn't let me create the pack — press Start in this chat and try again.")
    elif res.error:
        lines.append(f"\n❌ Telegram said: {esc(res.error)}")
    if res.remaining:
        lines.append(f"\n🕒 {res.remaining} sticker(s) are still queued — tap Publish again to retry.")
    markup = kb.pack_links([(p.title, p.link) for p in res.packs]) if res.packs else kb.packs_menu(bool(res.remaining))
    await status.edit_text("\n".join(lines) or "Nothing was published.", reply_markup=markup,
                           link_preview_options=NO_PREVIEW)
