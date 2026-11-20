from __future__ import annotations

import io
import json
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..context import AppContext
from ..database import is_premium_doc
from ..services.ai_generator import friendly_error, is_premium_style, style_label
from ..services.image_processor import StickerOptions, load_image, split_emoji
from ..utils.helpers import esc
from ..utils.validators import ValidationError, clean_query
from .common import (edit_or_send, make_and_deliver, priority, queue_message, refund, safe_answer, spend,
                     user_options)

log = logging.getLogger(__name__)
router = Router(name="photo")


class PhotoState(StatesGroup):
    menu = State()
    text = State()
    emoji = State()


def _default_opts(user: dict) -> dict:
    s = user.get("settings", {})
    return {"bg": s.get("bg_mode", "auto"), "outline": s.get("outline", "none"), "shadow": bool(s.get("shadow")),
            "crop": True, "text": "", "emoji": ""}


def _sticker_opts(user: dict, o: dict) -> StickerOptions:
    return user_options(user, bg_mode=o["bg"], outline=o["outline"], shadow=o["shadow"], crop=o["crop"],
                        caption=o.get("text", ""))


async def _download(bot: Bot, file_id: str, limit: int) -> bytes:
    buf = await bot.download(file_id)
    if buf is None:
        raise ValidationError("I couldn't fetch that file from Telegram.")
    data = buf.read()
    if len(data) > limit:
        raise ValidationError(f"Please send an image smaller than {limit // (1024 * 1024)} MB.")
    return data


@router.callback_query(F.data == "menu:photo")
async def cb_menu_photo(cb: CallbackQuery) -> None:
    await safe_answer(cb)
    if cb.message:
        await edit_or_send(cb.message, "📸 Send me a photo (or an image file) and I'll turn it into a sticker 💗",
                           kb.back_menu())


@router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def got_photo(message: Message, ctx: AppContext, user: dict, state: FSMContext) -> None:
    item = message.photo[-1] if message.photo else message.document
    if item.file_size and item.file_size > ctx.settings.max_image_bytes:
        await message.answer(f"Please send an image smaller than {ctx.settings.max_image_size_mb} MB 💗")
        return
    hint = clean_query(message.caption or "", 120)
    if ctx.settings.quiet_mode:   # image in -> sticker out, nothing else
        try:
            raw = await _download(message.bot, item.file_id, ctx.settings.max_image_bytes)
        except Exception as exc:  # noqa: BLE001
            await message.answer("❌ " + (str(exc) if isinstance(exc, ValidationError) else "Could not read that image."))
            return
        await ctx.db.inc_user_stat(user["_id"], "downloads")
        await ctx.db.inc_stat("images_downloaded")
        await make_and_deliver(ctx, user, message, raw, user_options(user), hints=(hint,), source={"type": "photo"})
        return
    opts = _default_opts(user)
    await state.clear()
    await state.set_state(PhotoState.menu)
    await state.update_data(file_id=item.file_id, opts=opts, hint=hint, last_sticker=None, last_sig="")
    await message.answer("Choose your sticker style 💗", reply_markup=kb.photo_options(opts))


async def _state(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("file_id"):
        await safe_answer(cb, "Please send the photo again 📸", alert=True)
        return None
    await safe_answer(cb)
    return data


@router.callback_query(F.data.startswith("ph:t:"))
async def cb_toggle(cb: CallbackQuery, state: FSMContext, user: dict) -> None:
    data = await _state(cb, state)
    if data is None or not cb.message:
        return
    what, o = (cb.data or "").split(":")[2], data["opts"]
    if what == "bg":
        o["bg"] = "transparent" if o["bg"] == "keep" else "keep"
    elif what in ("white", "black"):
        o["outline"] = "none" if o["outline"] == what else what
    elif what == "shadow":
        o["shadow"] = not o["shadow"]
    elif what == "crop":
        o["crop"] = not o["crop"]
    await state.update_data(opts=o)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.photo_options(o))
    except Exception:  # noqa: BLE001 - "message is not modified" etc.
        pass


@router.callback_query(F.data == "ph:text")
async def cb_text(cb: CallbackQuery, state: FSMContext) -> None:
    if await _state(cb, state) is None or not cb.message:
        return
    await state.set_state(PhotoState.text)
    await cb.message.answer("✍️ Send the text to put on your sticker (max 60 characters), or send - to remove it.")


@router.message(PhotoState.text, F.text, ~F.text.startswith("/"))
async def got_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    o = data.get("opts") or {}
    text = clean_query(message.text or "", 60)
    o["text"] = "" if text == "-" else text
    await state.update_data(opts=o)
    await state.set_state(PhotoState.menu)
    await message.answer("Text saved ✅ Now choose more options or tap Create Sticker 💗", reply_markup=kb.photo_options(o))


@router.callback_query(F.data == "ph:emoji")
async def cb_emoji(cb: CallbackQuery, state: FSMContext) -> None:
    if await _state(cb, state) is None or not cb.message:
        return
    await state.set_state(PhotoState.emoji)
    await cb.message.answer("😊 Send one or more emoji for this sticker (they appear when people search "
                            "their emoji keyboard), or send - to clear.")


@router.message(PhotoState.emoji, F.text, ~F.text.startswith("/"))
async def got_emoji(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    o = data.get("opts") or {}
    _, emojis = split_emoji(message.text or "")
    if (message.text or "").strip() == "-":
        o["emoji"] = ""
    elif not emojis:
        await message.answer("I couldn't find an emoji in that — try again 😊")
        return
    else:
        o["emoji"] = "".join(emojis[:10])
    await state.update_data(opts=o)
    await state.set_state(PhotoState.menu)
    await message.answer("Emoji saved ✅", reply_markup=kb.photo_options(o))


def _emojis(o: dict) -> list | None:
    return split_emoji(o.get("emoji", ""))[1] or None


async def _build(cb: CallbackQuery, ctx: AppContext, user: dict, state: FSMContext, data: dict,
                 opts: StickerOptions, queue: bool = False, sig: str = "") -> None:
    try:
        raw = await _download(cb.bot, data["file_id"], ctx.settings.max_image_bytes)
    except Exception as exc:  # noqa: BLE001
        await cb.message.answer("❌ " + (str(exc) if isinstance(exc, ValidationError) else "Could not download the photo."))
        return
    await ctx.db.inc_user_stat(user["_id"], "downloads")
    await ctx.db.inc_stat("images_downloaded")
    doc = await make_and_deliver(ctx, user, cb.message, raw, opts, hints=(data.get("hint", ""),),
                                 source={"type": "photo"}, emojis=_emojis(data["opts"]), queue=queue)
    if doc:
        await state.update_data(last_sticker=doc["_id"], last_sig=sig)


@router.callback_query(F.data == "ph:go")
async def cb_go(cb: CallbackQuery, ctx: AppContext, user: dict, state: FSMContext) -> None:
    data = await _state(cb, state)
    if data is None or not cb.message:
        return
    await _build(cb, ctx, user, state, data, _sticker_opts(user, data["opts"]), sig=json.dumps(data["opts"], sort_keys=True))


@router.callback_query(F.data == "ph:webp")
async def cb_webp(cb: CallbackQuery, ctx: AppContext, user: dict, state: FSMContext) -> None:
    data = await _state(cb, state)
    if data is None or not cb.message:
        return
    plain = StickerOptions(bg_mode="keep", crop=False)
    await _build(cb, ctx, user, state, data, plain)


@router.callback_query(F.data == "ph:pack")
async def cb_pack(cb: CallbackQuery, ctx: AppContext, user: dict, state: FSMContext) -> None:
    data = await _state(cb, state)
    if data is None or not cb.message:
        return
    sig = json.dumps(data["opts"], sort_keys=True)
    if data.get("last_sticker") and data.get("last_sig") == sig:
        await queue_message(ctx, user, cb.message, data["last_sticker"])
        return
    await _build(cb, ctx, user, state, data, _sticker_opts(user, data["opts"]), queue=True, sig=sig)


@router.callback_query(F.data.startswith("ph:style:"))
async def cb_style(cb: CallbackQuery, ctx: AppContext, user: dict, state: FSMContext) -> None:
    data = await _state(cb, state)
    if data is None or not cb.message:
        return
    style = (cb.data or "").split(":", 2)[2]
    msg = cb.message
    if not ctx.ai.enabled:
        await msg.answer("🎨 Photo styles need the AI service, which isn't configured on this bot yet.")
        return
    if is_premium_style(style) and not is_premium_doc(user) and not ctx.is_admin(user["_id"]):
        await msg.answer("💎 That style is for premium members.")
        return
    if not await spend(ctx, user, "gen", msg):
        return
    status = await msg.answer(f"🎨 Turning your photo into a <b>{esc(style_label(style))}</b> sticker…")
    try:
        raw = await _download(cb.bot, data["file_id"], ctx.settings.max_image_bytes)

        def _png() -> bytes:
            img = load_image(raw, ctx.settings.max_image_bytes, work_max=1024)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()

        import asyncio
        png = await asyncio.to_thread(_png)
        async with ctx.gate.slot(priority(user)):
            styled = await ctx.ai.stylize_photo(png, style)
        await ctx.db.inc_stat("openai_calls")
        await ctx.db.inc_stat("ai_generations")
        await ctx.db.inc_user_stat(user["_id"], "ai")
        await ctx.db.log_generation(user["_id"], f"photo:{style}", style, True, kind="photo")
        await make_and_deliver(ctx, user, msg, styled, _sticker_opts(user, data["opts"]),
                               hints=(data.get("hint", ""), style_label(style)), spend_kind=None,
                               source={"type": "photo", "style": style}, emojis=_emojis(data["opts"]))
    except Exception as exc:  # noqa: BLE001
        await refund(ctx, user, "gen")
        await ctx.db.log_generation(user["_id"], f"photo:{style}", style, False, kind="photo")
        await msg.answer("❌ " + (str(exc) if isinstance(exc, ValidationError) else friendly_error(exc)))
    finally:
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
