from __future__ import annotations

import logging
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..context import AppContext
from ..database import is_premium_doc
from ..services.ai_generator import STYLES, friendly_error, is_premium_style, style_label
from ..utils.helpers import esc
from ..utils.validators import clean_query
from .common import (deliver_sticker, edit_or_send, priority, refund, resolve_category, safe_answer, spend,
                     user_options)

log = logging.getLogger(__name__)
router = Router(name="ai_sticker")
MAX_PROMPT = 300


class AIState(StatesGroup):
    prompt = State()
    style = State()


async def _ask_style(message: Message, state: FSMContext, user: dict, prompt: str) -> None:
    await state.set_state(AIState.style)
    await state.update_data(prompt=prompt)
    default = user.get("settings", {}).get("style", "cute")
    await message.answer(
        f"🎨 <b>{esc(prompt)}</b>\n\nChoose a style 💗  <i>(💎 = premium styles)</i>",
        reply_markup=kb.style_picker(is_premium_doc(user), default))


async def _not_configured(message: Message) -> None:
    await message.answer("🎨 AI generation isn't set up on this bot yet — the owner needs to add an OpenAI key. "
                         "Meanwhile you can search images or send me a photo 📸", reply_markup=kb.main_menu())


@router.message(Command("sticker"))
async def cmd_sticker(message: Message, command: CommandObject, ctx: AppContext, user: dict, state: FSMContext) -> None:
    await state.clear()
    if not ctx.ai.enabled:
        return await _not_configured(message)
    prompt = clean_query(command.args or "", MAX_PROMPT)
    if not prompt:
        await state.set_state(AIState.prompt)
        await message.answer("🎨 Describe your sticker, e.g. <i>cute pink cat holding a strawberry</i>")
        return
    await _ask_style(message, state, user, prompt)


@router.callback_query(F.data == "menu:ai")
async def cb_menu_ai(cb: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await safe_answer(cb)
    if not cb.message:
        return
    if not ctx.ai.enabled:
        return await _not_configured(cb.message)
    await state.set_state(AIState.prompt)
    await edit_or_send(cb.message, "🎨 <b>AI Sticker Generator</b>\n\nDescribe your sticker, e.g.\n"
                       "<i>cute pink cat holding a strawberry</i>", kb.back_menu())


@router.message(AIState.prompt, F.text, ~F.text.startswith("/"))
async def got_prompt(message: Message, user: dict, state: FSMContext) -> None:
    if (message.text or "").startswith("/"):
        return
    prompt = clean_query(message.text or "", MAX_PROMPT)
    if len(prompt) < 3:
        await message.answer("Please describe your sticker in a few words 💗")
        return
    await _ask_style(message, state, user, prompt)


@router.callback_query(F.data.startswith("ai:s:"))
async def cb_style(cb: CallbackQuery, ctx: AppContext, user: dict, state: FSMContext) -> None:
    style = (cb.data or "").split(":", 2)[2]
    data = await state.get_data()
    prompt = data.get("prompt")
    if style not in STYLES or not cb.message:
        return await safe_answer(cb)
    if not prompt:
        await safe_answer(cb, "That request expired — send /sticker <idea> again 💗", alert=True)
        return
    if is_premium_style(style) and not is_premium_doc(user) and not ctx.is_admin(user["_id"]):
        await safe_answer(cb, "💎 This style is for premium members.", alert=True)
        return
    await safe_answer(cb)
    msg = cb.message
    if not await spend(ctx, user, "gen", msg):
        return
    status = await msg.answer(f"🎨 Painting your <b>{esc(style_label(style))}</b> sticker… this can take ~30s")
    try:
        async with ctx.gate.slot(priority(user)):
            raw = await ctx.ai.generate(prompt, style)
        await ctx.db.inc_stat("openai_calls")
        result = await ctx.builder.build(raw, user_options(user))
        category = await resolve_category(ctx, prompt, style_label(style), image=raw)
        await ctx.db.inc_user_stat(user["_id"], "ai")
        await ctx.db.inc_stat("ai_generations")
        await ctx.db.log_generation(user["_id"], prompt, style, True)
        await deliver_sticker(ctx, user, msg, result.webp, category, {"prompt": prompt, "style": style},
                              result.has_alpha)
        note = "✨ Transparent background ✅" if result.has_alpha else \
            "ℹ️ This sticker has a solid background (the model didn't return transparency)."
        await msg.answer(note + "\nPick another style above to remix it, or /cancel to finish.")
    except Exception as exc:  # noqa: BLE001
        await refund(ctx, user, "gen")
        await ctx.db.log_generation(user["_id"], prompt, style, False)
        log.warning("AI generation failed: %s", type(exc).__name__)
        await msg.answer("❌ " + friendly_error(exc))
    finally:
        with suppress(TelegramBadRequest):
            await status.delete()
