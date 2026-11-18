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
        self._session = None
        if model_dir:
            os.environ.setdefault("U2NET_HOME", os.path.abspath(model_dir))

    @property
    def available(self) -> bool:
        return self._backend is not None or importlib.util.find_spec("rembg") is not None

    def _remove_bytes(self, png: bytes) -> bytes:
        if self._backend is not None:
            return self._backend(png)
        try:
            from rembg import new_session, remove  # type: ignore
        except Exception as exc:  # ImportError, onnxruntime load errors...
            raise BackgroundRemovalUnavailable("rembg is not installed.") from exc
        with self._lock:
            if self._session is None:
                try:
                    self._session = new_session(self.model)
                except Exception as exc:  # model download blocked, etc.
                    raise BackgroundRemovalUnavailable(f"Could not load model {self.model}: {exc}") from exc
        return remove(png, session=self._session)

    def _remove_sync(self, img: Image.Image) -> Image.Image:
        work = img.convert("RGBA")
        original = work.size
        if max(work.size) > self.max_side:
            work = work.copy()
            work.thumbnail((self.max_side, self.max_side), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        work.save(buf, "PNG")
        out = Image.open(io.BytesIO(self._remove_bytes(buf.getvalue()))).convert("RGBA")
        if out.size != original and max(original) <= self.max_side:
            out = out.resize(original, Image.Resampling.LANCZOS)
        return out

    async def remove(self, img: Image.Image) -> Image.Image:
        async with self._sem:
            return await asyncio.to_thread(self._remove_sync, img)
