"""MongoDB access layer (Motor). All persistence goes through this class."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, AsyncIterator

from .utils.helpers import ensure_aware, short_id, today_str, utcnow

log = logging.getLogger(__name__)

try:  # pymongo constant (True) – fall back if the driver is absent (tests)
    from pymongo import ReturnDocument
    AFTER = ReturnDocument.AFTER
except ImportError:  # pragma: no cover
    AFTER = True

DEFAULT_USER_SETTINGS = {"bg_mode": "auto", "outline": "none", "shadow": False, "style": "cute",
                         "custom_color": "#FFD6E8"}
DAILY_KINDS = ("gen", "search", "sticker")


def is_premium_doc(doc: dict | None, now: datetime | None = None) -> bool:
    if not doc or not doc.get("premium"):
        return False
    until = ensure_aware(doc.get("premium_until"))
    return until is None or until > (now or utcnow())


def _daily_doc() -> dict:
    return {"date": today_str(), "gen": 0, "search": 0, "sticker": 0}


class Database:
    def __init__(self, url: str, name: str = "sticker_bot", client: Any = None):
        self.url, self.name = url, name
        self.client = client
        self.db: Any = None
        self.banned: set = set()

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        if self.client is None:
            from motor.motor_asyncio import AsyncIOMotorClient

            self.client = AsyncIOMotorClient(self.url, serverSelectionTimeoutMS=8000, tz_aware=True)
        self.db = self.client[self.name]
        await self.client.admin.command("ping")
        await self.ensure_indexes()
        self.banned = {d["_id"] async for d in self.db["banned_users"].find({}, {"_id": 1})}
        log.info("MongoDB connected (db=%s, banned=%d)", self.name, len(self.banned))

    async def close(self) -> None:
        if self.client is not None:
            self.client.close()

    async def ping(self) -> bool:
        try:
            await self.client.admin.command("ping")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def ensure_indexes(self) -> None:
        c = self.db
        await c["users"].create_index([("last_seen", -1)])
        await c["users"].create_index([("created_at", -1)])
        await c["stickers"].create_index([("user_id", 1), ("status", 1)])
        await c["stickers"].create_index([("user_id", 1), ("sha256", 1)])
        await c["sticker_packs"].create_index("name", unique=True)
        await c["sticker_packs"].create_index([("owner_id", 1), ("category", 1), ("kind", 1), ("full", 1)])
        await c["search_history"].create_index([("user_id", 1), ("created_at", -1)])
        await c["search_history"].create_index("created_at", expireAfterSeconds=7 * 86400)
        await c["generation_history"].create_index([("user_id", 1), ("created_at", -1)])
        await c["downloads"].create_index([("user_id", 1), ("url_hash", 1)])
        await c["premium_users"].create_index("until")

    # ----------------------------------------------------------------------- users
    async def upsert_user(self, uid: int, username: str | None, first_name: str | None) -> dict:
        now = utcnow()
        return await self.db["users"].find_one_and_update(
            {"_id": uid},
            {"$set": {"username": username or "", "first_name": first_name or "", "last_seen": now},
             "$setOnInsert": {"created_at": now,
                              "stats": {"stickers": 0, "ai": 0, "downloads": 0, "searches": 0},
                              "settings": dict(DEFAULT_USER_SETTINGS), "daily": _daily_doc(),
                              "premium": False, "premium_until": None}},
            upsert=True, return_document=AFTER)

    async def get_user(self, uid: int) -> dict | None:
        return await self.db["users"].find_one({"_id": uid})

    async def inc_user_stat(self, uid: int, field: str, n: int = 1) -> None:
        await self.db["users"].update_one({"_id": uid}, {"$inc": {f"stats.{field}": n}})

    async def set_user_setting(self, uid: int, key: str, value: Any) -> None:
        await self.db["users"].update_one({"_id": uid}, {"$set": {f"settings.{key}": value}})

    async def consume(self, uid: int, kind: str, limit: int | None) -> bool:
        """Atomically spend one unit of a daily quota. ``limit=None`` means unlimited."""
        if kind not in DAILY_KINDS:
            raise ValueError(kind)
        users = self.db["users"]
        today = today_str()
        await users.update_one({"_id": uid, "daily.date": {"$ne": today}}, {"$set": {"daily": _daily_doc()}})
        flt: dict = {"_id": uid}
        if limit is not None:
            flt[f"daily.{kind}"] = {"$lt": limit}
        res = await users.update_one(flt, {"$inc": {f"daily.{kind}": 1}})
        return res.modified_count == 1

    async def refund(self, uid: int, kind: str) -> None:
        await self.db["users"].update_one({"_id": uid, f"daily.{kind}": {"$gt": 0}}, {"$inc": {f"daily.{kind}": -1}})

    async def count_users(self) -> int:
        return await self.db["users"].count_documents({})

    async def count_active_users(self, days: int = 1) -> int:
        return await self.db["users"].count_documents({"last_seen": {"$gte": utcnow() - timedelta(days=days)}})

    async def recent_users(self, limit: int = 15) -> list:
        return await self.db["users"].find({}).sort("last_seen", -1).limit(limit).to_list(limit)

    async def iter_user_ids(self) -> AsyncIterator[int]:
        async for doc in self.db["users"].find({"blocked": {"$ne": True}}, {"_id": 1}):
            if doc["_id"] not in self.banned:
                yield doc["_id"]

    async def mark_blocked(self, uid: int) -> None:
        await self.db["users"].update_one({"_id": uid}, {"$set": {"blocked": True}})

    # --------------------------------------------------------------- premium / bans
    async def set_premium(self, uid: int, days: int | None, by: int | None = None) -> None:
        until = utcnow() + timedelta(days=days) if days else None
        await self.db["users"].update_one({"_id": uid}, {"$set": {"premium": True, "premium_until": until}})
        await self.db["premium_users"].update_one(
            {"_id": uid}, {"$set": {"until": until, "granted_by": by, "granted_at": utcnow()}}, upsert=True)

    async def revoke_premium(self, uid: int) -> None:
        await self.db["users"].update_one({"_id": uid}, {"$set": {"premium": False, "premium_until": None}})
        await self.db["premium_users"].delete_many({"_id": uid})

    async def count_premium(self) -> int:
        return await self.db["users"].count_documents({"premium": True})

    async def ban(self, uid: int, reason: str = "", by: int | None = None) -> None:
        await self.db["banned_users"].update_one(
            {"_id": uid}, {"$set": {"reason": reason, "by": by, "at": utcnow()}}, upsert=True)
        self.banned.add(uid)

    async def unban(self, uid: int) -> bool:
        res = await self.db["banned_users"].delete_many({"_id": uid})
        self.banned.discard(uid)
        return res.deleted_count > 0

    def is_banned(self, uid: int) -> bool:
        return uid in self.banned

    # -------------------------------------------------------------------- settings
    async def get_setting(self, key: str, default: Any = None) -> Any:
        doc = await self.db["bot_settings"].find_one({"_id": key})
        return doc["value"] if doc else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.db["bot_settings"].update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)

    # ------------------------------------------------------------------ statistics
    async def inc_stat(self, key: str, n: int = 1) -> None:
        await self.db["statistics"].update_one({"_id": "global"}, {"$inc": {key: n}}, upsert=True)
        await self.db["statistics"].update_one({"_id": f"day:{today_str()}"}, {"$inc": {key: n}}, upsert=True)

    async def set_stat_text(self, key: str, value: str) -> None:
        await self.db["statistics"].update_one({"_id": "global"}, {"$set": {key: value}}, upsert=True)

    async def get_stats(self) -> dict:
        return (await self.db["statistics"].find_one({"_id": "global"})) or {}

    # -------------------------------------------------------------------- stickers
    async def add_sticker(self, doc: dict) -> dict:
        doc.setdefault("_id", short_id(8))
        doc.setdefault("created_at", utcnow())
        await self.db["stickers"].insert_one(doc)
        return doc

    async def get_sticker(self, sid: str) -> dict | None:
        return await self.db["stickers"].find_one({"_id": sid})

    async def update_sticker(self, sid: str, fields: dict) -> None:
        await self.db["stickers"].update_one({"_id": sid}, {"$set": fields})

    async def find_sticker_by_hash(self, uid: int, sha: str, statuses: tuple = ("queued", "published"),
                                   category: str | None = None) -> dict | None:
        flt: dict = {"user_id": uid, "sha256": sha, "status": {"$in": list(statuses)}}
        if category:
            flt["category"] = category
        return await self.db["stickers"].find_one(flt)

    async def queued_stickers(self, uid: int) -> list:
        return await self.db["stickers"].find({"user_id": uid, "status": "queued"}).sort("created_at", 1).to_list(500)

    async def count_queued(self, uid: int) -> int:
        return await self.db["stickers"].count_documents({"user_id": uid, "status": "queued"})

    async def latest_draft(self, uid: int) -> dict | None:
        rows = await self.db["stickers"].find({"user_id": uid, "status": {"$in": ["draft", "queued"]}}) \
            .sort("created_at", -1).limit(1).to_list(1)
        return rows[0] if rows else None

    async def clear_queue(self, uid: int) -> int:
        res = await self.db["stickers"].update_many({"user_id": uid, "status": "queued"}, {"$set": {"status": "draft"}})
        return res.modified_count

    async def count_stickers(self) -> int:
        return await self.db["stickers"].count_documents({})

    # ----------------------------------------------------------------------- packs
    async def active_pack(self, owner: int, category: str, kind: str) -> dict | None:
        rows = await self.db["sticker_packs"].find(
            {"owner_id": owner, "category": category, "kind": kind, "full": False, "gone": {"$ne": True}}
        ).sort("part", -1).limit(1).to_list(1)
        return rows[0] if rows else None

    async def pack_parts(self, owner: int, category: str, kind: str) -> int:
        return await self.db["sticker_packs"].count_documents({"owner_id": owner, "category": category, "kind": kind})

    async def create_pack_doc(self, doc: dict) -> dict:
        doc.setdefault("_id", short_id(8))
        doc.setdefault("created_at", utcnow())
        doc.setdefault("count", 0)
        doc.setdefault("full", False)
        await self.db["sticker_packs"].insert_one(doc)
        return doc

    async def update_pack(self, name: str, fields: dict | None = None, inc: dict | None = None) -> None:
        upd: dict = {}
        if fields:
            upd["$set"] = fields
        if inc:
            upd["$inc"] = inc
        if upd:
            await self.db["sticker_packs"].update_one({"name": name}, upd)

    async def user_packs(self, uid: int) -> list:
        return await self.db["sticker_packs"].find({"owner_id": uid, "kind": "user", "gone": {"$ne": True}}) \
            .sort("created_at", 1).to_list(200)

    async def get_pack(self, name: str) -> dict | None:
        return await self.db["sticker_packs"].find_one({"name": name})

    async def find_published(self, sha: str, category: str) -> dict | None:
        return await self.db["stickers"].find_one({"sha256": sha, "category": category, "status": "published", "auto": True})

    async def unposted_packs(self) -> list:
        return await self.db["sticker_packs"].find(
            {"kind": "owner", "channel_posted": {"$ne": True}, "gone": {"$ne": True}}).sort("created_at", 1).to_list(500)

    async def first_sticker_of_pack(self, name: str) -> dict | None:
        rows = await self.db["stickers"].find({"pack": name}).sort("created_at", 1).limit(1).to_list(1)
        return rows[0] if rows else None

    async def count_user_packs(self, uid: int) -> int:
        return await self.db["sticker_packs"].count_documents({"owner_id": uid, "kind": "user", "gone": {"$ne": True}})

    async def count_packs(self) -> int:
        return await self.db["sticker_packs"].count_documents({"gone": {"$ne": True}})

    # --------------------------------------------------------------------- history
    async def save_search(self, uid: int, query: str, results: list) -> str:
        sid = short_id(6)
        await self.db["search_history"].insert_one(
            {"_id": sid, "user_id": uid, "query": query, "results": results, "created_at": utcnow()})
        return sid

    async def get_search(self, sid: str) -> dict | None:
        return await self.db["search_history"].find_one({"_id": sid})

    async def log_generation(self, uid: int, prompt: str, style: str, ok: bool, kind: str = "ai") -> None:
        await self.db["generation_history"].insert_one(
            {"user_id": uid, "prompt": prompt[:300], "style": style, "ok": ok, "kind": kind, "created_at": utcnow()})

    async def log_download(self, uid: int, url_hash: str, url: str) -> None:
        await self.db["downloads"].insert_one(
            {"user_id": uid, "url_hash": url_hash, "url": url[:500], "created_at": utcnow()})

    async def count_collection(self, name: str) -> int:
        return await self.db[name].count_documents({})
