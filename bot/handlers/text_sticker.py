from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..context import AppContext
from ..services.category_detector import detect_category
from ..services.image_processor import (PRESET_TEXTS, TEXT_BACKGROUNDS, TEXT_COLORS,
                                        available_fonts, render_text_sticker, split_emoji)
from ..utils.helpers import esc
from ..utils.validators import clean_query
from .common import deliver_sticker, describe_error, edit_or_send, refund, safe_answer, spend

log = logging.getLogger(__name__)
router = Router(name="text_sticker")

COLORS = list(TEXT_COLORS)
OUTLINES = ["white", "black", "pink", "none"]
BGS = list(TEXT_BACKGROUNDS)


class TextState(StatesGroup):
    custom = State()
    options = State()


def _fonts() -> dict:
    return available_fonts()


def _font_names() -> list:
    return list(_fonts()) or ["default"]


def _markup(d: dict):
    fonts = _font_names()
    return kb.text_options(fonts[d["font"] % len(fonts)], COLORS[d["color"] % len(COLORS)],
                           OUTLINES[d["outline"] % len(OUTLINES)], BGS[d["bg"] % len(BGS)])


async def _show_options(message: Message, state: FSMContext, text: str) -> None:
    d = {"text": text, "font": 0, "color": 0, "outline": 0, "bg": 0}
    await state.set_state(TextState.options)
    await state.update_data(**d)
    await message.answer(f"✍️ <b>{esc(text)}</b>\nStyle it, then tap Create 💗", reply_markup=_markup(d))


@router.message(Command("text"))
async def cmd_text(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    text = clean_query(command.args or "", 60)
    if text:
        await _show_options(message, state, text)
    else:
        await message.answer("✍️ Pick a phrase or type your own:", reply_markup=kb.text_presets())


@router.callback_query(F.data == "menu:text")
async def cb_menu(cb: CallbackQuery) -> None:
    await safe_answer(cb)
    if cb.message:
        await edit_or_send(cb.message, "✍️ <b>Text stickers</b>\nPick a phrase or type your own:", kb.text_presets())


@router.callback_query(F.data.startswith("tx:p:"))
async def cb_preset(cb: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(cb)
    try:
        text = PRESET_TEXTS[int((cb.data or "").split(":")[2])]
    except (ValueError, IndexError):
        return
    if cb.message:
        await _show_options(cb.message, state, text)


@router.callback_query(F.data == "tx:custom")
async def cb_custom(cb: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(cb)
    await state.set_state(TextState.custom)
    if cb.message:
        await cb.message.answer("✏️ Send your text (max 60 characters). Emoji are welcome — they become the sticker's emoji 😊")


@router.message(TextState.custom, F.text, ~F.text.startswith("/"))
async def got_custom(message: Message, state: FSMContext) -> None:
    if (message.text or "").startswith("/"):
        return
    text = clean_query(message.text or "", 60)
    if not split_emoji(text)[0]:
        await message.answer("Please include some words (emoji alone can't be drawn) 💗")
        return
    await _show_options(message, state, text)


@router.callback_query(TextState.options, F.data.in_({"tx:font", "tx:color", "tx:outline", "tx:bg"}))
async def cb_cycle(cb: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(cb)
    d = await state.get_data()
    key = (cb.data or "").split(":")[1]
    d[key] = d.get(key, 0) + 1
    await state.update_data(**{key: d[key]})
    if cb.message:
        try:
            await cb.message.edit_reply_markup(reply_markup=_markup(d))
        except Exception:  # noqa: BLE001
            pass


@router.callback_query(F.data == "tx:go")
async def cb_go(cb: CallbackQuery, ctx: AppContext, user: dict, state: FSMContext) -> None:
    d = await state.get_data()
    if not d.get("text") or not cb.message:
        await safe_answer(cb, "That expired — send /text again 💗", alert=True)
        return
    await safe_answer(cb)
    msg = cb.message
    if not await spend(ctx, user, "sticker", msg):
        return
    fonts = _fonts()
    fnames = _font_names()
    try:
        canvas = await asyncio.to_thread(
            render_text_sticker, d["text"], font_name=fnames[d["font"] % len(fnames)],
            color=COLORS[d["color"] % len(COLORS)], outline=OUTLINES[d["outline"] % len(OUTLINES)],
            background=BGS[d["bg"] % len(BGS)], fonts=fonts)
        result = await ctx.builder.from_canvas(canvas)
        category = detect_category(d["text"]) or "cute"
        emojis = split_emoji(d["text"])[1] or None
        await deliver_sticker(ctx, user, msg, result.webp, category, {"type": "text", "text": d["text"]},
                              result.has_alpha, emojis)
        if result.has_alpha:
            await msg.answer("✨ Transparent background ✅ — change colours above and tap Create again to remix.")
    except Exception as exc:  # noqa: BLE001
        await refund(ctx, user, "sticker")
        log.exception("Text sticker failed")
        await msg.answer("❌ " + describe_error(exc))
