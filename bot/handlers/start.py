from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..context import AppContext
from .common import edit_or_send, safe_answer

router = Router(name="start")
fallback_router = Router(name="fallback")

WELCOME = (
    "✨ <b>Welcome to Telegram AI Sticker Bot!</b> 💗\n\n"
    "I can find images, turn photos into stickers, draw brand-new AI stickers and publish "
    "real Telegram sticker packs for you.\n\n"
    "🔍 /find <i>cute cats</i> — search images\n"
    "🎨 /sticker <i>pink cat holding a strawberry</i> — AI sticker\n"
    "📸 Send me any photo — photo to sticker\n"
    "✍️ /text <i>Good Morning</i> — text sticker\n"
    "📦 /mypacks — your sticker packs\n"
    "👤 /profile · ⚙️ /settings · ❓ /help\n\n"
    "Pick something from the menu below 👇"
)


@router.message(CommandStart())
@router.message(Command("help", "menu"))
async def cmd_start(message: Message, state: FSMContext, ctx: AppContext) -> None:
    await state.clear()
    if ctx.settings.quiet_mode:
        await message.answer("💗 Send me any image (or many) and I'll turn each one into a sticker automatically.\n"
                             "Extras: /find <i>cute cats</i> · /sticker <i>idea</i> · /text <i>hello</i>")
        return
    await message.answer(WELCOME, reply_markup=kb.main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Okay, cancelled 💗", reply_markup=kb.main_menu())


@router.callback_query(F.data == "menu:main")
async def cb_main(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(cb)
    if cb.message:
        await edit_or_send(cb.message, WELCOME, kb.main_menu())


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await safe_answer(cb)


@fallback_router.message(F.text & ~F.text.startswith("/"))
async def plain_text(message: Message, ctx: AppContext) -> None:
    if ctx.settings.quiet_mode:
        return
    await message.answer(
        "💗 Tell me what to do with the menu below, or use /sticker <i>your idea</i> "
        "to make an AI sticker.", reply_markup=kb.main_menu())
