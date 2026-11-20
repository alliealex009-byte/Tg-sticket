"""Environment-driven configuration.

Only BOT_TOKEN and MONGO_URL are mandatory. Everything else has a default, and
OPENAI_API_KEY is only needed for the AI generator features.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping


class ConfigError(RuntimeError):
    """Raised when mandatory configuration is missing or invalid."""


def _int(env: Mapping[str, str], key: str, default: int, lo: int = 0, hi: int = 10**9) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    return max(lo, min(hi, value))


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _ids(raw: str) -> frozenset[int]:
    out = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"ADMIN_IDS contains a non-numeric id: {part!r}") from exc
    return frozenset(out)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    mongo_url: str
    mongo_db: str = "sticker_bot"
    bot_username: str = ""
    pack_credit: str = "@Advik_prakashh"
    admin_ids: frozenset = field(default_factory=frozenset)

    openai_api_key: str = ""
    image_model: str = "gpt-image-1"
    image_quality: str = "medium"
    vision_model: str = "gpt-4o-mini"
    enable_vision_category: bool = False

    pixabay_api_key: str = ""
    enable_pinterest: bool = True
    enable_openverse: bool = True
    search_results: int = 20

    max_daily_generations: int = 15
    max_daily_searches: int = 30
    max_daily_stickers: int = 60
    max_packs_free: int = 3
    premium_multiplier: int = 5

    max_concurrent_tasks: int = 5
    max_image_size_mb: int = 15
    max_pack_stickers: int = 120
    request_timeout: int = 20
    rate_limit_per_sec: float = 2.0

    rembg_model: str = "u2netp"
    category_pack_owner_id: int = 0
    owner_id: int = 0
    auto_pack: bool = True
    quiet_mode: bool = True
    quiet_find_count: int = 4
    channel_id: str = ""
    data_dir: str = "data"
    log_dir: str = "logs"
    log_level: str = "INFO"

    @property
    def max_image_bytes(self) -> int:
        return self.max_image_size_mb * 1024 * 1024

    @property
    def ai_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        missing = [k for k in ("BOT_TOKEN", "MONGO_URL") if not (env.get(k) or "").strip()]
        if missing:
            raise ConfigError(
                "Missing required environment variable(s): " + ", ".join(missing)
                + ". Copy .env.example to .env and fill them in."
            )
        admins = _ids(env.get("ADMIN_IDS", ""))
        owner = _int(env, "CATEGORY_PACK_OWNER_ID", 0)
        if not owner and admins:
            owner = sorted(admins)[0]
        owner_id = _int(env, "OWNER_ID", 0) or owner
        credit = (env.get("PACK_CREDIT") or "@Advik_prakashh").strip()
        return cls(
            bot_token=env["BOT_TOKEN"].strip(),
            mongo_url=env["MONGO_URL"].strip(),
            mongo_db=(env.get("MONGO_DB") or "sticker_bot").strip(),
            bot_username=(env.get("BOT_USERNAME") or "").strip().lstrip("@"),
            pack_credit=credit,
            admin_ids=admins,
            openai_api_key=(env.get("OPENAI_API_KEY") or "").strip(),
            image_model=(env.get("IMAGE_MODEL") or "gpt-image-1").strip(),
            image_quality=(env.get("IMAGE_QUALITY") or "medium").strip(),
            vision_model=(env.get("VISION_MODEL") or "gpt-4o-mini").strip(),
            enable_vision_category=_bool(env, "ENABLE_VISION_CATEGORY", False),
            pixabay_api_key=(env.get("PIXABAY_API_KEY") or "").strip(),
            enable_pinterest=_bool(env, "ENABLE_PINTEREST", True),
            enable_openverse=_bool(env, "ENABLE_OPENVERSE", True),
            search_results=_int(env, "SEARCH_RESULTS", 20, 3, 50),
            max_daily_generations=_int(env, "MAX_DAILY_GENERATIONS", 15, 0, 10_000),
            max_daily_searches=_int(env, "MAX_DAILY_SEARCHES", 30, 0, 10_000),
            max_daily_stickers=_int(env, "MAX_DAILY_STICKERS", 60, 0, 10_000),
            max_packs_free=_int(env, "MAX_PACKS_FREE", 3, 0, 1000),
            premium_multiplier=_int(env, "PREMIUM_MULTIPLIER", 5, 1, 100),
            max_concurrent_tasks=_int(env, "MAX_CONCURRENT_TASKS", 5, 1, 64),
            max_image_size_mb=_int(env, "MAX_IMAGE_SIZE_MB", 15, 1, 20),
            max_pack_stickers=_int(env, "MAX_PACK_STICKERS", 50, 1, 50),
            request_timeout=_int(env, "REQUEST_TIMEOUT", 20, 3, 120),
            rembg_model=(env.get("REMBG_MODEL") or "u2netp").strip(),
            category_pack_owner_id=owner,
            owner_id=owner_id,
            auto_pack=_bool(env, "AUTO_PACK", True),
            quiet_mode=_bool(env, "QUIET_MODE", True),
            quiet_find_count=_int(env, "QUIET_FIND_COUNT", 4, 1, 10),
            channel_id=(env.get("CHANNEL_ID") or "").strip(),
            data_dir=(env.get("DATA_DIR") or "data").strip(),
            log_dir=(env.get("LOG_DIR") or "logs").strip(),
            log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
        )
