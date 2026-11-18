"""Free / premium limit resolution (pure functions, easy to test)."""
from __future__ import annotations

from .config import Settings

LIMIT_FIELDS = {
    "gen": "max_daily_generations",
    "search": "max_daily_searches",
    "sticker": "max_daily_stickers",
    "pack": "max_packs_free",
}
LIMIT_ALIASES = {"generations": "gen", "gen": "gen", "searches": "search", "search": "search",
                 "stickers": "sticker", "sticker": "sticker", "packs": "pack", "pack": "pack"}
LIMIT_LABELS = {"gen": "AI generations", "search": "image searches", "sticker": "sticker creations", "pack": "sticker packs"}


def effective_limit(settings: Settings, overrides: dict, kind: str, premium: bool, admin: bool) -> int | None:
    """Return the limit for a user, or None for unlimited (admins)."""
    if admin:
        return None
    base = int(overrides.get(kind, getattr(settings, LIMIT_FIELDS[kind])))
    return base * settings.premium_multiplier if premium else base
