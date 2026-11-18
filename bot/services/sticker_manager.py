"""Sticker storage, queueing and Telegram sticker-pack publishing.

Uses the official Bot API methods ``createNewStickerSet`` and ``addStickerToSet``.
Sticker *files* have no Telegram-visible filename; what users see is the pack
title ("Food : @Advik_prakashh"), each sticker's emoji list and the pack link.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramNetworkError, TelegramRetryAfter, TelegramServerError)
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputSticker

from ..config import Settings
from ..database import Database
from ..utils.helpers import sha256_hex, short_id
from .category_detector import category_emoji, pack_title

log = logging.getLogger(__name__)
PACK_NAME_RE = re.compile(r"^[a-z](?:[a-z0-9]|_(?!_))*$")
MAX_NEW_SET_ATTEMPTS = 3


def make_pack_name(category: str, owner_id: int, bot_username: str) -> str:
    """Telegram rules: a-z0-9_ only, starts with a letter, no double underscores,
    must end with ``_by_<bot_username>``, max 64 chars."""
    bot = re.sub(r"[^a-z0-9_]", "", bot_username.lower())
    suffix = f"_{owner_id}_{short_id(2)}_by_{bot}"
    slug = re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_") or "stickers"
    if not slug[0].isalpha():
        slug = "p" + slug
    slug = slug[: max(1, 64 - len(suffix))]
    name = re.sub(r"_+", "_", f"{slug}{suffix}")
    return name[:64]


def pack_link(name: str) -> str:
    return f"https://t.me/addstickers/{name}"


async def tg_call(factory, attempts: int = 4):
    """Run a Telegram API call, honouring FloodWait (RetryAfter) and retrying network errors."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await factory()
        except TelegramRetryAfter as exc:
            last = exc
            wait = min(int(exc.retry_after) + 1, 120)
            log.warning("Telegram FloodWait: sleeping %ss", wait)
            await asyncio.sleep(wait)
        except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError) as exc:
            last = exc
            await asyncio.sleep(min(2 ** i, 10))
    assert last is not None
    raise last


class _StopPublish(Exception):
    """Internal control-flow signal: stop publishing (limit reached)."""


@dataclass
class QueueResult:
    status: str            # queued | duplicate | full | missing
    count: int = 0


@dataclass
class PublishedPack:
    name: str
    title: str
    link: str
    added: int


@dataclass
class AutoResult:
    pack: PublishedPack
    created: bool = False
    duplicate: bool = False


@dataclass
class PublishResult:
    packs: list = field(default_factory=list)
    published: int = 0
    error: str = ""
    remaining: int = 0


class StickerManager:
    def __init__(self, settings: Settings, db: Database, bot_username: str = ""):
        self.settings = settings
        self.db = db
        self.bot_username = bot_username
        self.root = Path(settings.data_dir) / "stickers"
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ storage
    def _path(self, user_id: int, sha: str) -> Path:
        return self.root / str(user_id) / f"{sha}.webp"

    async def save_draft(self, user_id: int, webp: bytes, category: str, emojis: list | None = None,
                         source: dict | None = None, has_alpha: bool = False) -> dict:
        sha = sha256_hex(webp)
        path = self._path(user_id, sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            await asyncio.to_thread(path.write_bytes, webp)
        return await self.db.add_sticker({
            "user_id": user_id, "sha256": sha, "path": str(path), "category": category,
            "emojis": emojis or [category_emoji(category)], "source": source or {},
            "has_alpha": has_alpha, "status": "draft", "size": len(webp)})

    async def read_file(self, doc: dict) -> bytes:
        return await asyncio.to_thread(Path(doc["path"]).read_bytes)

    # -------------------------------------------------------------------- queue
    async def queue(self, user_id: int, sticker_id: str) -> QueueResult:
        doc = await self.db.get_sticker(sticker_id)
        if not doc or doc["user_id"] != user_id:
            return QueueResult("missing")
        if doc["status"] == "queued":
            return QueueResult("duplicate", await self.db.count_queued(user_id))
        dup = await self.db.find_sticker_by_hash(user_id, doc["sha256"], category=doc["category"])
        if dup and dup["_id"] != sticker_id:
            return QueueResult("duplicate", await self.db.count_queued(user_id))
        count = await self.db.count_queued(user_id)
        if count >= self.settings.max_pack_stickers:
            return QueueResult("full", count)
        await self.db.update_sticker(sticker_id, {"status": "queued"})
        return QueueResult("queued", count + 1)

    async def set_category(self, user_id: int, sticker_id: str, category: str) -> bool:
        doc = await self.db.get_sticker(sticker_id)
        if not doc or doc["user_id"] != user_id:
            return False
        fields = {"category": category}
        if doc.get("emojis") == [category_emoji(doc["category"])]:
            fields["emojis"] = [category_emoji(category)]
        await self.db.update_sticker(sticker_id, fields)
        return True

    # ------------------------------------------------------------------ publish
    @staticmethod
    def _input(webp: bytes, emojis: list, index: int) -> InputSticker:
        return InputSticker(sticker=BufferedInputFile(webp, filename=f"sticker_{index}.webp"),
                            format="static", emoji_list=(emojis or ["✨"])[:20])

    async def _create_set(self, bot, owner: int, category: str, kind: str, first: dict, webp: bytes,
                          pack_limit: int | None) -> dict:
        """Create a brand-new Telegram sticker set with its first sticker."""
        part = await self.db.pack_parts(owner, category, kind) + 1
        title = pack_title(category, self.settings.pack_credit, part)
        last_err: Exception | None = None
        for _ in range(MAX_NEW_SET_ATTEMPTS):
            name = make_pack_name(category, owner, self.bot_username)
            try:
                await tg_call(lambda n=name: bot.create_new_sticker_set(
                    user_id=owner, name=n, title=title,
                    stickers=[self._input(webp, first.get("emojis", []), 1)]))
            except TelegramBadRequest as exc:
                last_err = exc
                if "occupied" in str(exc).lower() or "already" in str(exc).lower():
                    continue  # random suffix collided: try another name
                raise
            return await self.db.create_pack_doc({
                "name": name, "title": title, "owner_id": owner, "category": category, "kind": kind,
                "part": part, "count": 1, "full": False, "link": pack_link(name)})
        assert last_err is not None
        raise last_err

    async def publish(self, bot, user_id: int, kind: str = "user", only: list | None = None,
                      premium: bool = False, admin: bool = False, progress=None) -> PublishResult:
        """Publish queued stickers (grouped by category) into per-user packs."""
        result = PublishResult()
        queued = [s for s in await self.db.queued_stickers(user_id) if not only or s["_id"] in only]
        by_cat: dict = {}
        for s in queued:
            by_cat.setdefault(s["category"], []).append(s)
        cap = self.settings.max_pack_stickers
        pack_limit = self.settings_pack_limit(premium, admin)
        touched: dict = {}
        total, done = len(queued), 0
        try:
            for category, items in by_cat.items():
                for sticker in items:
                    webp = await self.read_file(sticker)
                    pack = await self.db.active_pack(user_id, category, kind)
                    if pack is None or pack["count"] >= cap:
                        if pack_limit is not None and kind == "user" and await self.db.count_user_packs(user_id) >= pack_limit:
                            result.error = "pack_limit"
                            raise _StopPublish
                        pack = await self._create_set(bot, user_id, category, kind, sticker, webp, pack_limit)
                        await self.db.update_sticker(sticker["_id"], {"status": "published", "pack": pack["name"]})
                        await self.db.inc_stat("packs_created")
                    else:
                        try:
                            await tg_call(lambda p=pack, w=webp, s=sticker: bot.add_sticker_to_set(
                                user_id=user_id, name=p["name"],
                                sticker=self._input(w, s.get("emojis", []), p["count"] + 1)))
                        except TelegramBadRequest as exc:
                            text = str(exc).lower()
                            if "stickerset_invalid" in text or "not found" in text:
                                await self.db.update_pack(pack["name"], {"gone": True})
                                pack = await self._create_set(bot, user_id, category, kind, sticker, webp, pack_limit)
                            elif "too much" in text or "stickers_too_much" in text:
                                await self.db.update_pack(pack["name"], {"full": True})
                                pack = await self._create_set(bot, user_id, category, kind, sticker, webp, pack_limit)
                            else:
                                raise
                        else:
                            await self.db.update_pack(pack["name"], inc={"count": 1})
                            pack["count"] += 1
                        await self.db.update_sticker(sticker["_id"], {"status": "published", "pack": pack["name"]})
                    if pack["count"] >= cap:
                        await self.db.update_pack(pack["name"], {"full": True})
                    entry = touched.setdefault(pack["name"], PublishedPack(pack["name"], pack["title"], pack["link"], 0))
                    entry.added += 1
                    done += 1
                    result.published = done
                    if progress and (done % 10 == 0):
                        await progress(done, total)
        except _StopPublish:
            pass
        except TelegramForbiddenError:
            result.error = "forbidden"
        except TelegramBadRequest as exc:
            log.warning("Pack publish rejected: %s", exc)
            result.error = f"telegram: {str(exc)[:160]}"
        except TelegramAPIError as exc:
            log.warning("Pack publish failed: %s", exc)
            result.error = f"telegram: {str(exc)[:160]}"
        result.packs = list(touched.values())
        result.remaining = await self.db.count_queued(user_id)
        return result

    def settings_pack_limit(self, premium: bool, admin: bool) -> int | None:
        if admin:
            return None
        base = self.settings.max_packs_free
        return base * self.settings.premium_multiplier if premium else base

    async def add_to_category_pack(self, bot, sticker: dict, owner: int) -> PublishedPack:
        """Add one sticker to the shared, bot-maintained pack for its category."""
        webp = await self.read_file(sticker)
        category, cap = sticker["category"], self.settings.max_pack_stickers
        pack = await self.db.active_pack(owner, category, "category")
        if pack is None or pack["count"] >= cap:
            pack = await self._create_set(bot, owner, category, "category", sticker, webp, None)
        else:
            await tg_call(lambda: bot.add_sticker_to_set(
                user_id=owner, name=pack["name"],
                sticker=self._input(webp, sticker.get("emojis", []), pack["count"] + 1)))
            await self.db.update_pack(pack["name"], inc={"count": 1})
            pack["count"] += 1
        if pack["count"] >= cap:
            await self.db.update_pack(pack["name"], {"full": True})
        return PublishedPack(pack["name"], pack["title"], pack["link"], 1)

    # ------------------------------------------------------- automatic owner packs
    async def auto_publish(self, bot, sticker: dict, owner: int, channel_id: str = "") -> AutoResult:
        """Add a sticker straight to the owner's pack for its category (max N per pack).

        When a pack is full a new pack with a slightly different title is created. The first sticker of every
        new pack is posted to the public channel (if configured).
        """
        category, cap = sticker["category"], self.settings.max_pack_stickers
        async with self._lock:  # serialise so concurrent users can't overfill/duplicate packs
            dup = await self.db.find_published(sticker["sha256"], category)
            if dup and dup.get("pack"):
                existing = await self.db.get_pack(dup["pack"])
                if existing:
                    await self.db.update_sticker(sticker["_id"], {"status": "published", "pack": existing["name"], "auto": False})
                    return AutoResult(PublishedPack(existing["name"], existing["title"], existing["link"], 0), duplicate=True)
            webp = await self.read_file(sticker)
            pack = await self.db.active_pack(owner, category, "owner")
            created = False
            if pack is not None and pack["count"] < cap:
                try:
                    await tg_call(lambda p=pack: bot.add_sticker_to_set(
                        user_id=owner, name=p["name"], sticker=self._input(webp, sticker.get("emojis", []), p["count"] + 1)))
                    await self.db.update_pack(pack["name"], inc={"count": 1})
                    pack["count"] += 1
                except TelegramBadRequest as exc:
                    text = str(exc).lower()
                    if "stickerset_invalid" in text or "not found" in text:
                        await self.db.update_pack(pack["name"], {"gone": True})
                    elif "too much" in text:
                        await self.db.update_pack(pack["name"], {"full": True})
                    else:
                        raise
                    pack = None
            else:
                pack = None
            if pack is None:
                pack = await self._create_set(bot, owner, category, "owner", sticker, webp, None)
                created = True
            if pack["count"] >= cap:
                await self.db.update_pack(pack["name"], {"full": True})
            await self.db.update_sticker(sticker["_id"], {"status": "published", "pack": pack["name"], "auto": True})
            if created and channel_id:
                await self.post_to_channel(bot, channel_id, pack, webp)
            return AutoResult(PublishedPack(pack["name"], pack["title"], pack["link"], 1), created=created)

    async def post_to_channel(self, bot, channel_id: str, pack: dict, webp: bytes) -> bool:
        """Post one sticker of the pack (with an 'Add pack' button) to the public channel."""
        chat = int(channel_id) if channel_id.lstrip("-").isdigit() else channel_id
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=f"➕ Add {pack['title']}"[:60], url=pack["link"])]])
        try:
            await tg_call(lambda: bot.send_sticker(chat_id=chat, sticker=BufferedInputFile(webp, filename="sticker.webp"),
                                                   reply_markup=markup))
        except TelegramAPIError as exc:
            log.warning("Channel post failed for %s: %s", pack["name"], exc)
            return False
        await self.db.update_pack(pack["name"], {"channel_posted": True})
        return True

    async def post_unposted(self, bot, channel_id: str) -> tuple[int, int]:
        """Backfill: post one sticker for every owner pack not yet announced. Returns (posted, failed)."""
        ok = bad = 0
        for pack in await self.db.unposted_packs():
            first = await self.db.first_sticker_of_pack(pack["name"])
            if not first:
                bad += 1
                continue
            if await self.post_to_channel(bot, channel_id, pack, await self.read_file(first)):
                ok += 1
            else:
                bad += 1
        return ok, bad
