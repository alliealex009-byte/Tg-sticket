"""Small shared helpers: time, hashing, TTL cache, priority task gate, retries."""
from __future__ import annotations

import asyncio
import hashlib
import heapq
import html
import itertools
import secrets
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_str() -> str:
    return utcnow().strftime("%Y-%m-%d")


def ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def short_id(nbytes: int = 8) -> str:
    return secrets.token_hex(nbytes)


def esc(value: Any) -> str:
    """HTML-escape any user-controlled text before sending with parse_mode=HTML."""
    return html.escape(str(value), quote=False)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{d}d"] if d else []
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    if not d:
        parts.append(f"{s}s")
    return " ".join(parts)


def chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


class TTLCache:
    """Tiny in-process TTL + size-bounded cache."""

    def __init__(self, ttl: float, maxsize: int = 256):
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: "OrderedDict[Any, tuple[float, Any]]" = OrderedDict()

    def get(self, key: Any, default: Any = None) -> Any:
        item = self._data.get(key)
        if item is None:
            return default
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return default
        self._data.move_to_end(key)
        return value

    def set(self, key: Any, value: Any) -> None:
        self._data[key] = (time.monotonic() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)


class TaskGate:
    """Concurrency limiter with priorities (lower number = served first).

    Premium users get priority 0, everyone else 1, so premium jobs jump the queue.
    """

    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.active = 0
        self._heap: list = []
        self._seq = itertools.count()

    @property
    def waiting(self) -> int:
        return sum(1 for _, _, f in self._heap if not f.cancelled())

    @asynccontextmanager
    async def slot(self, priority: int = 1):
        await self._acquire(priority)
        try:
            yield
        finally:
            self._release()

    async def _acquire(self, priority: int) -> None:
        if self.active < self.limit and not self._heap:
            self.active += 1
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (priority, next(self._seq), fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self._release()  # slot had been handed to us; give it back
            raise

    def _release(self) -> None:
        self.active -= 1
        while self._heap and self.active < self.limit:
            _, _, fut = heapq.heappop(self._heap)
            if fut.cancelled():
                continue
            self.active += 1
            fut.set_result(None)
            break


async def retry_async(
    factory: Callable[[], Awaitable[Any]],
    attempts: int = 3,
    base_delay: float = 0.5,
    retry_on: tuple = (Exception,),
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> Any:
    """Run ``factory()`` up to ``attempts`` times with exponential backoff."""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except retry_on as exc:  # noqa: PERF203
            last = exc
            if attempt == attempts:
                break
            if on_retry:
                on_retry(attempt, exc)
            await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
    assert last is not None
    raise last


class RateLimiter:
    """Per-user token bucket (in memory)."""

    def __init__(self, rate: float, burst: int = 25):
        self.rate, self.burst = max(rate, 0.1), burst
        self._buckets: dict = {}

    def allow(self, uid: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.get(uid, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1:
            self._buckets[uid] = (tokens, now)
            return False
        self._buckets[uid] = (tokens - 1, now)
        if len(self._buckets) > 5000:  # bound memory
            cutoff = now - 600
            self._buckets = {k: v for k, v in self._buckets.items() if v[1] > cutoff}
        return True
