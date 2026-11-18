"""Application context shared with all handlers via aiogram workflow data."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .database import Database
from .limits import effective_limit
from .services.ai_generator import AIGenerator
from .services.background_removal import BackgroundRemover
from .services.downloader import Downloader
from .services.image_processor import StickerBuilder
from .services.image_search import ImageSearchService
from .utils.helpers import TaskGate


@dataclass
class AppContext:
    settings: Settings
    db: Database
    downloader: Downloader
    search: ImageSearchService
    remover: BackgroundRemover
    ai: AIGenerator
    builder: StickerBuilder
    gate: TaskGate
    stickers: Any = None                    # StickerManager (set after construction)
    bot_username: str = ""
    started_at: float = field(default_factory=time.time)
    runtime: dict = field(default_factory=lambda: {"maintenance": False, "limits": {}})
    restart_event: asyncio.Event = field(default_factory=asyncio.Event)
    pending_broadcasts: dict = field(default_factory=dict)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_ids

    def limit(self, kind: str, premium: bool, user_id: int) -> int | None:
        return effective_limit(self.settings, self.runtime.get("limits", {}), kind, premium, self.is_admin(user_id))
