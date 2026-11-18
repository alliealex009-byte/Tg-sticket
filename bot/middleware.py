"""Outer middleware: private-chat only, ban / maintenance / rate-limit checks, user upsert."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Update

from .context import AppContext
from .database import is_premium_doc
from .utils.helpers import RateLimiter

log = logging.getLogger(__name__)


class GuardMiddleware(BaseMiddleware):
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.limiter = RateLimiter(ctx.settings.rate_limit_per_sec)

    async def __call__(self, handler: Callable[[Update, dict], Awaitable[Any]], event: Update, data: dict) -> Any:
        ctx = self.ctx
        msg, cb = event.message, event.callback_query
        tg_user = (msg or cb).from_user if (msg or cb) else None
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data) if not (msg or cb) else None
        if msg and msg.chat.type != "private":
            return None  # the bot only works in private chats
        uid = tg_user.id
        is_admin = ctx.is_admin(uid)

        async def tell(text: str) -> None:
            try:
                if cb:
                    await cb.answer(text, show_alert=True)
                elif msg:
                    await msg.answer(text)
            except Exception:  # noqa: BLE001
                pass

        if ctx.db.is_banned(uid) and not is_admin:
            await tell("🚫 You have been banned from using this bot.")
            return None
        if ctx.runtime.get("maintenance") and not is_admin:
            await tell("🛠 The bot is under maintenance right now. Please try again soon 💗")
            return None
        if not self.limiter.allow(uid):
            if cb:
                await tell("Slow down a little 💗")
            return None
        data["user"] = await ctx.db.upsert_user(uid, tg_user.username, tg_user.first_name)
        data["premium"] = is_premium_doc(data["user"])
        return await handler(event, data)
