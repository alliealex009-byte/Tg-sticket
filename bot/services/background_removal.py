"""Background removal via rembg (lazy import, one shared model session)."""
from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import os
import threading
from typing import Callable

from PIL import Image

log = logging.getLogger(__name__)


class BackgroundRemovalUnavailable(RuntimeError):
    """rembg (or its model) is not installed / could not be loaded."""


class BackgroundRemover:
    def __init__(self, model: str = "u2netp", concurrency: int = 1, model_dir: str | None = None,
                 backend: Callable[[bytes], bytes] | None = None, max_side: int = 1024):
        self.model = model
        self.max_side = max_side
        self._backend = backend            # injectable for tests
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._lock = threading.Lock()
        self._sessions: dict = {}
        if model_dir:
            os.environ.setdefault("U2NET_HOME", os.path.abspath(model_dir))

    @property
    def available(self) -> bool:
        return self._backend is not None or importlib.util.find_spec("rembg") is not None

    def _remove_bytes(self, png: bytes, model: str | None = None) -> bytes:
        if self._backend is not None:
            return self._backend(png)
        try:
            from rembg import new_session, remove  # type: ignore
        except Exception as exc:  # ImportError, onnxruntime load errors...
            raise BackgroundRemovalUnavailable("rembg is not installed.") from exc
        name = model or self.model
        with self._lock:
            if name not in self._sessions:
                try:
                    self._sessions[name] = new_session(name)
                except Exception as exc:  # model download blocked, etc.
                    if model and name != self.model:   # special model unavailable -> default model
                        return self._remove_bytes(png, None)
                    raise BackgroundRemovalUnavailable(f"Could not load model {name}: {exc}") from exc
            session = self._sessions[name]
        return remove(png, session=session)

    def _remove_sync(self, img: Image.Image, model: str | None = None) -> Image.Image:
        work = img.convert("RGBA")
        original = work.size
        if max(work.size) > self.max_side:
            work = work.copy()
            work.thumbnail((self.max_side, self.max_side), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        work.save(buf, "PNG")
        out = Image.open(io.BytesIO(self._remove_bytes(buf.getvalue(), model))).convert("RGBA")
        if out.size != original and max(original) <= self.max_side:
            out = out.resize(original, Image.Resampling.LANCZOS)
        return out

    async def remove(self, img: Image.Image, model: str | None = None) -> Image.Image:
        async with self._sem:
            return await asyncio.to_thread(self._remove_sync, img, model)
