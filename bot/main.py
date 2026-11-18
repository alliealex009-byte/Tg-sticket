"""Entry point: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiogram.types.error_event import ErrorEvent

from .config import ConfigError, Settings
from .context import AppContext
from .database import Database
from .handlers import admin, ai_sticker, packs, photo, profile, search, start, text_sticker
from .middleware import GuardMiddleware
from .services.ai_generator import AIGenerator
from .services.background_removal import BackgroundRemover
from .services.downloader import Downloader
from .services.image_processor import StickerBuilder
from .services.image_search import ImageSearchService
from .services.sticker_manager import StickerManager
from .utils.helpers import TaskGate
from .utils.logger import redact, setup_logging

log = logging.getLogger("bot")

COMMANDS = [
    ("start", "Open the main menu"), ("find", "Search images: /find cute cats"),
    ("sticker", "AI sticker: /sticker pink cat"), ("text", "Text sticker: /text Good Morning"),
    ("mypacks", "My sticker packs"), ("createpack", "Preview & publish queued stickers"),
    ("addpack", "Queue my latest sticker"), ("profile", "My profile & limits"),
    ("settings", "Sticker settings"), ("cancel", "Cancel current action"), ("help", "Help"),
]


def build_context(settings: Settings) -> AppContext:
    data = Path(settings.data_dir)
    downloader = Downloader(max_bytes=settings.max_image_bytes, timeout=settings.request_timeout,
                            cache_dir=data / "cache")
    remover = BackgroundRemover(settings.rembg_model, concurrency=1, model_dir=str(data / "models"))
    ctx = AppContext(
        settings=settings,
        db=Database(settings.mongo_url, settings.mongo_db),
        downloader=downloader,
        search=ImageSearchService(settings, downloader),
        remover=remover,
        ai=AIGenerator(settings.openai_api_key, settings.image_model, settings.image_quality,
                       settings.vision_model),
        builder=StickerBuilder(remover, settings.max_image_bytes),
        gate=TaskGate(settings.max_concurrent_tasks),
    )
    return ctx


def build_dispatcher(ctx: AppContext) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), ctx=ctx)
    dp.update.outer_middleware(GuardMiddleware(ctx))
    # Order matters: admin first, plain-text fallback last.
    for r in (admin.router, start.router, search.router, ai_sticker.router, text_sticker.router,
              photo.router, packs.router, profile.router, start.fallback_router):
        dp.include_router(r)

    @dp.errors()
    async def on_error(event: ErrorEvent, ctx: AppContext) -> bool:
        exc = event.exception
        log.error("Unhandled error: %s", redact(repr(exc)), exc_info=exc)
        try:
            await ctx.db.inc_stat("errors")
            await ctx.db.set_stat_text("last_error", redact(f"{type(exc).__name__}: {str(exc)[:200]}"))
        except Exception:  # noqa: BLE001
            pass
        upd = event.update
        try:
            if upd.callback_query:
                await upd.callback_query.answer("Oops, something went wrong. Please try again 💗", show_alert=True)
            elif upd.message:
                await upd.message.answer("Oops, something went wrong. Please try again 💗")
        except Exception:  # noqa: BLE001
            pass
        return True

    return dp


async def housekeeping(ctx: AppContext, heartbeat: Path) -> None:
    """Heartbeat for the Docker healthcheck + periodic temp/cache cleanup."""
    last_clean = 0.0
    while True:
        try:
            heartbeat.write_text(str(int(time.time())))
            if time.time() - last_clean > 1800:
                last_clean = time.time()
                removed = await asyncio.to_thread(ctx.downloader.purge_cache)
                tmp = Path(ctx.settings.data_dir) / "tmp"
                if tmp.is_dir():
                    await asyncio.to_thread(shutil.rmtree, tmp, True)
                if removed:
                    log.info("Cleaned %d expired cache files", removed)
        except Exception:  # noqa: BLE001
            log.exception("Housekeeping failed")
        await asyncio.sleep(30)


async def run() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level, settings.log_dir)
    for sub in ("cache", "models", "stickers", "fonts"):
        (Path(settings.data_dir) / sub).mkdir(parents=True, exist_ok=True)

    ctx = build_context(settings)
    await ctx.db.connect()
    ctx.runtime["maintenance"] = bool(await ctx.db.get_setting("maintenance", False))
    ctx.runtime["limits"] = dict(await ctx.db.get_setting("limits", {}) or {})

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    ctx.bot_username = settings.bot_username or me.username or ""
    ctx.stickers = StickerManager(settings, ctx.db, ctx.bot_username)
    if not ctx.bot_username:
        log.error("Could not determine the bot username; sticker packs cannot be created.")
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])

    dp = build_dispatcher(ctx)
    log.info("Starting @%s | AI=%s | search=%s | rembg=%s | admins=%d", ctx.bot_username, ctx.ai.enabled,
             ctx.search.provider_names, ctx.remover.available, len(settings.admin_ids))
    heartbeat = asyncio.create_task(housekeeping(ctx, Path(settings.data_dir) / "heartbeat"))
    poll = asyncio.create_task(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))
    stop = asyncio.create_task(ctx.restart_event.wait())
    code = 0
    try:
        done, _ = await asyncio.wait({poll, stop}, return_when=asyncio.FIRST_COMPLETED)
        if stop in done:
            log.info("Restart requested by admin")
            await dp.stop_polling()
            await asyncio.wait({poll}, timeout=15)
        elif poll in done and poll.exception():
            log.error("Polling stopped: %s", redact(repr(poll.exception())))
            code = 1
    finally:
        for task in (heartbeat, stop, poll):
            task.cancel()
        await asyncio.gather(heartbeat, stop, poll, return_exceptions=True)
        await bot.session.close()
        await ctx.db.close()
    return code


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # python-dotenv is optional at runtime
        pass
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
