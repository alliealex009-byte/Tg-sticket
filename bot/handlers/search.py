from __future__ import annotations

import hashlib
import logging
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaPhoto, Message

from .. import keyboards as kb
from ..context import AppContext
from ..services.category_detector import CATEGORIES, QUICK_QUERIES
from ..services.downloader import DownloadError
from ..services.image_processor import make_preview_jpeg
from ..services.search_types import SearchResult
from ..utils.helpers import esc
from ..utils.validators import ValidationError, clean_query, sanitize_filename
from .common import (describe_error, edit_or_send, make_and_deliver, refund, safe_answer, spend, user_options)

log = logging.getLogger(__name__)
router = Router(name="search")


class SearchState(StatesGroup):
    query = State()


def _caption(r: SearchResult, query: str) -> str:
    lines = [f"🔎 <b>{esc(r.title)}</b>", f"📷 {esc(r.provider.title())}" + (f" · by {esc(r.creator)}" if r.creator else "")]
    if r.license:
        lines.append(f"📜 License: {esc(r.license)}")
    if r.attribution:
        lines.append(f"<i>{esc(r.attribution[:220])}</i>")
    lines.append("⚠️ Check the source's terms before reusing an image.")
    return "\n".join(lines)[:1000]


async def _preview(ctx: AppContext, r: SearchResult) -> bytes | None:
    for url in (r.thumb_url, r.image_url):
        if not url:
            continue
        try:
            raw = await ctx.downloader.fetch_image(url)
            return make_preview_jpeg(raw)
        except (DownloadError, ValidationError, OSError):
            continue
        except Exception:  # noqa: BLE001
            log.info("Preview failed", exc_info=True)
    return None


async def _load(ctx: AppContext, sid: str, idx: int):
    doc = await ctx.db.get_search(sid)
    if not doc or not doc.get("results"):
        return None, None, 0
    total = len(doc["results"])
    idx = max(0, min(idx, total - 1))
    return doc, SearchResult.from_dict(doc["results"][idx]), idx


async def render_result(target: Message, ctx: AppContext, sid: str, idx: int, replace: bool) -> None:
    doc, r, idx = await _load(ctx, sid, idx)
    if not doc:
        await target.answer("That search expired. Start a new one with /find 💗")
        return
    total = len(doc["results"])
    markup = kb.search_nav(sid, idx, total, r.page_url)
    caption = _caption(r, doc["query"])
    jpeg = await _preview(ctx, r)
    if jpeg:
        media = InputMediaPhoto(media=BufferedInputFile(jpeg, "preview.jpg"), caption=caption)
        if replace:
            try:
                await target.edit_media(media=media, reply_markup=markup)
                return
            except TelegramBadRequest:
                with suppress(TelegramBadRequest):
                    await target.delete()
        await target.answer_photo(BufferedInputFile(jpeg, "preview.jpg"), caption=caption, reply_markup=markup)
    else:
        if replace:
            with suppress(TelegramBadRequest):
                await target.delete()
        await target.answer(caption + "\n<i>(preview unavailable — you can still create a sticker)</i>", reply_markup=markup)


async def run_search(target: Message, ctx: AppContext, user: dict, query: str) -> None:
    query = clean_query(query)
    if not query:
        await target.answer("Type what to search for, e.g. /find cute cats 💗")
        return
    if not await spend(ctx, user, "search", target):
        return
    status = await target.answer(f"🔎 Searching for <b>{esc(query)}</b>…")
    try:
        results = await ctx.search.search(query)
        await ctx.db.inc_stat("searches")
        await ctx.db.inc_stat("search_api_calls", len(ctx.search.providers))
        await ctx.db.inc_user_stat(user["_id"], "searches")
        if not results:
            await refund(ctx, user, "search")
            await status.edit_text("😢 I couldn't find usable images for that. Try another keyword!",
                                   reply_markup=kb.search_categories())
            return
        if ctx.settings.quiet_mode:   # no previews: turn the best results straight into stickers
            made = 0
            for r in results[:12]:
                if made >= ctx.settings.quiet_find_count:
                    break
                try:
                    data = await ctx.downloader.fetch_image(r.image_url)
                except Exception:  # noqa: BLE001 - skip broken results
                    continue
                if await make_and_deliver(ctx, user, target, data, user_options(user), hints=(query, r.title),
                                          source={"provider": r.provider, "url": r.page_url, "license": r.license}):
                    made += 1
            with suppress(TelegramBadRequest):
                await status.delete()
            if not made:
                await refund(ctx, user, "search")
                await target.answer("😢 I couldn't make stickers from that search. Send me your own images instead 💗")
            return
        sid = await ctx.db.save_search(user["_id"], query, [r.to_dict() for r in results])
        with suppress(TelegramBadRequest):
            await status.delete()
        await render_result(target, ctx, sid, 0, replace=False)
    except Exception as exc:  # noqa: BLE001
        await refund(ctx, user, "search")
        log.exception("Search failed")
        with suppress(TelegramBadRequest):
            await status.edit_text("❌ " + describe_error(exc))


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject, ctx: AppContext, user: dict, state: FSMContext) -> None:
    await state.clear()
    if not command.args:
        await state.set_state(SearchState.query)
        await message.answer("🔍 What should I look for? Example: <i>cute cats</i>", reply_markup=kb.search_categories())
        return
    await run_search(message, ctx, user, command.args)


@router.callback_query(F.data == "menu:search")
async def cb_menu_search(cb: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(cb)
    await state.set_state(SearchState.query)
    if cb.message:
        await edit_or_send(cb.message, "🔍 Type what you want to find (e.g. <i>cute cats</i>) or pick a category 👇",
                           kb.search_categories())


@router.message(SearchState.query, F.text, ~F.text.startswith("/"))
async def got_query(message: Message, ctx: AppContext, user: dict, state: FSMContext) -> None:
    if (message.text or "").startswith("/"):
        return
    await state.clear()
    await run_search(message, ctx, user, message.text or "")


@router.callback_query(F.data.startswith("qs:"))
async def cb_quick(cb: CallbackQuery, ctx: AppContext, user: dict, state: FSMContext) -> None:
    key = (cb.data or "").split(":", 1)[1]
    await safe_answer(cb)
    if key not in CATEGORIES or not cb.message:
        return
    await state.clear()
    query = QUICK_QUERIES.get(key) or CATEGORIES[key][0].lower()
    await run_search(cb.message, ctx, user, query)


def _parse(data: str) -> tuple[str, str, int] | None:
    try:
        _, act, sid, idx = data.split(":")
        return act, sid, int(idx)
    except ValueError:
        return None


@router.callback_query(F.data.startswith("sr:"))
async def cb_result(cb: CallbackQuery, ctx: AppContext, user: dict) -> None:
    parsed = _parse(cb.data or "")
    if not parsed or not cb.message:
        await safe_answer(cb)
        return
    act, sid, idx = parsed
    await safe_answer(cb)
    if act == "n":
        await render_result(cb.message, ctx, sid, idx, replace=True)
        return
    doc, r, idx = await _load(ctx, sid, idx)
    if not doc or doc["user_id"] != user["_id"]:
        await cb.message.answer("That search expired. Start a new one with /find 💗")
        return
    if act == "d":
        await _download(cb.message, ctx, user, r)
    elif act in ("c", "a"):
        await _sticker(cb.message, ctx, user, r, doc["query"], queue=(act == "a"))


async def _download(message: Message, ctx: AppContext, user: dict, r: SearchResult) -> None:
    status = await message.answer("⬇️ Downloading…")
    try:
        data = await ctx.downloader.fetch_image(r.image_url)
        ext = "png" if data[:4] == b"\x89PNG" else "webp" if data[8:12] == b"WEBP" else "jpg"
        name = sanitize_filename(f"{r.title[:40]}.{ext}", default=f"image.{ext}")
        caption = f"💗 {esc(r.title)[:150]}\n{esc(r.attribution[:200]) if r.attribution else ''}\n📜 {esc(r.license or 'License unknown')}"
        await message.answer_document(BufferedInputFile(data, filename=name), caption=caption[:1000])
        await ctx.db.log_download(user["_id"], hashlib.sha256(r.image_url.encode()).hexdigest()[:24], r.image_url)
        await ctx.db.inc_user_stat(user["_id"], "downloads")
        await ctx.db.inc_stat("images_downloaded")
    except Exception as exc:  # noqa: BLE001
        if not isinstance(exc, (DownloadError, ValidationError)):
            log.exception("Download failed")
        await message.answer("❌ " + describe_error(exc))
    finally:
        with suppress(TelegramBadRequest):
            await status.delete()


async def _sticker(message: Message, ctx: AppContext, user: dict, r: SearchResult, query: str, queue: bool) -> None:
    try:
        data = await ctx.downloader.fetch_image(r.image_url)
    except Exception as exc:  # noqa: BLE001
        await message.answer("❌ " + describe_error(exc))
        return
    await ctx.db.inc_stat("images_downloaded")
    await make_and_deliver(
        ctx, user, message, data, user_options(user), hints=(query, r.title),
        source={"provider": r.provider, "url": r.page_url, "license": r.license}, queue=queue)
