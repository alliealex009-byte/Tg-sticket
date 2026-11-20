"""Helpers shared by all handlers."""
from __future__ import annotations

import logging
from contextlib import suppress

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from .. import keyboards as kb
from ..context import AppContext
from ..database import is_premium_doc
from ..limits import LIMIT_LABELS
from ..services.ai_generator import friendly_error
from ..services.background_removal import BackgroundRemovalUnavailable
from ..services.category_detector import DEFAULT_CATEGORY, detect_category
from ..services.downloader import DownloadError
from ..services.image_processor import StickerOptions, make_preview_jpeg, options_from_settings
from ..utils.helpers import esc
from ..utils.validators import ValidationError

log = logging.getLogger(__name__)


def priority(user: dict) -> int:
    """Premium jobs are served first by the TaskGate."""
    return 0 if is_premium_doc(user) else 1


def user_options(user: dict, **overrides) -> StickerOptions:
    return options_from_settings(user.get("settings", {}), **overrides)


async def safe_answer(cb: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    with suppress(TelegramBadRequest):
        await cb.answer(text, show_alert=alert)


async def edit_or_send(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit a text message in place; fall back to a new message (photos/stickers can't be text-edited)."""
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        await message.answer(text, reply_markup=markup)


async def spend(ctx: AppContext, user: dict, kind: str, message: Message) -> bool:
    """Consume one unit of a daily quota; tell the user nicely when exhausted."""
    uid = user["_id"]
    premium = is_premium_doc(user)
    limit = ctx.limit(kind, premium, uid)
    if await ctx.db.consume(uid, kind, limit):
        return True
    extra = "" if premium else f"\n💎 Premium members get {ctx.settings.premium_multiplier}× more."
    await message.answer(f"💗 You've used all {limit} daily {LIMIT_LABELS[kind]}. Come back tomorrow!{extra}")
    return False


async def refund(ctx: AppContext, user: dict, kind: str) -> None:
    with suppress(Exception):
        await ctx.db.refund(user["_id"], kind)


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        return str(exc)
    if isinstance(exc, DownloadError):
        return f"I couldn't download that image ({esc(exc)}). Try another one."
    if isinstance(exc, BackgroundRemovalUnavailable):
        return "Background removal isn't available right now."
    return friendly_error(exc)


async def resolve_category(ctx: AppContext, *hints: str, image: bytes | None = None) -> str:
    key = detect_category(*hints)
    if key:
        return key
    if image and ctx.settings.enable_vision_category and ctx.ai.enabled:
        try:
            thumb = make_preview_jpeg(image, 512)
            guess = await ctx.ai.classify(thumb)
            await ctx.db.inc_stat("openai_calls")
            if guess:
                return guess
        except Exception:  # noqa: BLE001 - classification is best-effort
            log.info("Vision category detection failed", exc_info=True)
    return DEFAULT_CATEGORY


async def deliver_sticker(ctx: AppContext, user: dict, message: Message, webp: bytes, category: str,
                          source: dict | None, has_alpha: bool, emojis: list | None = None,
                          queued: bool = False) -> dict:
    """Persist a draft sticker and send it with action buttons."""
    uid = user["_id"]
    doc = await ctx.stickers.save_draft(uid, webp, category, emojis, source, has_alpha)
    pack_url, auto_note = "", ""
    if ctx.settings.auto_pack and ctx.owner_id:
        try:
            res = await ctx.stickers.auto_publish(message.bot, doc, ctx.owner_id, ctx.settings.channel_id)
            pack_url = res.pack.link
            auto_note = (f"📦 {'Already in' if res.duplicate else 'Added to'} <b>{esc(res.pack.title)}</b>"
                         + (" ✨ (new pack!)" if res.created else ""))
        except Exception as exc:  # noqa: BLE001 - the sticker itself must still be delivered
            log.exception("Automatic pack publishing failed (has the owner pressed Start in the bot?)")
            if ctx.is_admin(uid):
                await message.answer(f"⚠️ Couldn't add to the owner pack: <code>{esc(str(exc)[:200])}</code>\n"
                                     f"Owner ID in use: <code>{ctx.owner_id}</code>. Fix: send /owner from your own account.")
    quiet = ctx.settings.quiet_mode and bool(pack_url)
    await message.answer_sticker(
        BufferedInputFile(webp, filename="sticker.webp"),
        reply_markup=(kb.pack_only(pack_url) if quiet
                      else kb.sticker_actions(doc["_id"], category, queued, ctx.is_admin(uid), pack_url)))
    if auto_note and not quiet:
        await message.answer(auto_note)
    await ctx.db.inc_user_stat(uid, "stickers")
    await ctx.db.inc_stat("stickers_generated")
    return doc


async def queue_message(ctx: AppContext, user: dict, message: Message, sticker_id: str) -> str:
    if ctx.settings.auto_pack and ctx.owner_id:
        doc = await ctx.db.get_sticker(sticker_id)
        if doc and doc.get("status") == "published":
            await message.answer("📦 Already in the public pack 💗")
            return "queued"
        try:
            res = await ctx.stickers.auto_publish(message.bot, doc, ctx.owner_id, ctx.settings.channel_id)
            await message.answer(f"📦 Added to <b>{esc(res.pack.title)}</b>", reply_markup=kb.pack_only(res.pack.link))
            return "queued"
        except Exception as exc:  # noqa: BLE001
            log.warning("Auto pack add failed: %s", exc)
            await message.answer("⚠️ I couldn't add it to the pack right now.")
            return "missing"
    res = await ctx.stickers.queue(user["_id"], sticker_id)
    cap = ctx.settings.max_pack_stickers
    texts = {
        "queued": f"✅ Added to your pack queue ({res.count}/{cap}).\nTap <b>Preview &amp; publish</b> when you're ready 📦",
        "duplicate": "💗 That sticker is already in your pack — I skipped the duplicate.",
        "full": f"📦 Your queue is full ({cap}). Publish it first, then add more.",
        "missing": "That sticker expired. Please create it again.",
    }
    await message.answer(texts[res.status], reply_markup=kb.packs_menu(res.status in ("queued", "duplicate", "full")))
    return res.status


async def make_and_deliver(ctx: AppContext, user: dict, message: Message, data: bytes, opts: StickerOptions,
                           hints: tuple = (), source: dict | None = None, emojis: list | None = None,
                           spend_kind: str | None = "sticker", queue: bool = False) -> dict | None:
    """Full pipeline: quota -> convert -> send sticker (-> optional queue). Returns the sticker doc."""
    if spend_kind and not await spend(ctx, user, spend_kind, message):
        return None
    quiet = ctx.settings.quiet_mode
    status = None if quiet else await message.answer("🎀 Creating your sticker…")
    try:
        async with ctx.gate.slot(priority(user)):
            result = await ctx.builder.build(data, opts)
        category = await resolve_category(ctx, *hints, image=data)
        doc = await deliver_sticker(ctx, user, message, result.webp, category, source, result.has_alpha, emojis)
        notes = []
        if "bg_unavailable" in result.notes:
            notes.append("Background removal isn't installed on this server, so I kept the original background.")
        elif "bg_failed" in result.notes:
            notes.append("I couldn't remove the background from that image, so I kept it as is.")
        if not result.has_alpha and opts.bg_mode in ("auto", "transparent"):
            notes.append("This sticker has a solid background (no transparency).")
        if notes and not quiet:
            await message.answer("ℹ️ " + " ".join(notes))
        if queue and not quiet:
            await queue_message(ctx, user, message, doc["_id"])
        return doc
    except Exception as exc:  # noqa: BLE001
        if spend_kind:
            await refund(ctx, user, spend_kind)
        if not isinstance(exc, (ValidationError, DownloadError)):
            log.exception("Sticker creation failed")
        await message.answer("❌ " + describe_error(exc))
        return None
    finally:
        if status is not None:
            with suppress(TelegramBadRequest):
                await status.delete()
