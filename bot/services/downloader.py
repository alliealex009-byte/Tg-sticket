"""SSRF-safe, size-limited, cached asynchronous downloader.

Design notes
* Every hop (including redirects) is re-validated.
* DNS is resolved once, every address must be public, and the socket connects to
  that pinned IP (TLS still verifies the original hostname) -> no DNS rebinding.
* Only ports 80/443 by default, hard byte cap, per-operation and total timeouts.
* Image bodies are decoded with Pillow before being returned or cached.
* Identical concurrent downloads are coalesced; successful ones are cached on disk.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import logging
import os
import socket
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from ..utils.validators import ValidationError, resolve_ips, validate_image_bytes, validate_url

log = logging.getLogger(__name__)
REDIRECT_CODES = {301, 302, 303, 307, 308}


class DownloadError(Exception):
    """Download failed (network error, bad status, too large, wrong type...)."""


@dataclass
class Fetched:
    data: bytes
    content_type: str
    final_url: str


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float, context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip = ip
        self._tls_context = context

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._tls_context.wrap_socket(sock, server_hostname=self.host)


class Downloader:
    def __init__(
        self,
        max_bytes: int = 15 * 1024 * 1024,
        timeout: float = 15.0,
        total_timeout: float = 30.0,
        cache_dir: str | os.PathLike | None = None,
        cache_ttl: float = 3600.0,
        allow_private: bool = False,
        allowed_ports: tuple | None = (80, 443),
        resolver: Callable[[str, int], list[str]] | None = None,
        max_redirects: int = 3,
        concurrency: int = 8,
        user_agent: str = "TelegramAIStickerBot/1.0 (+https://github.com/)",
    ):
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.total_timeout = total_timeout
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl
        self.allow_private = allow_private
        self.allowed_ports = allowed_ports
        self._resolver = resolver
        self.max_redirects = max_redirects
        self.user_agent = user_agent
        self._sem = asyncio.Semaphore(concurrency)
        self._inflight: dict[str, asyncio.Task] = {}
        self._ssl = ssl.create_default_context()
        self.network_requests = 0  # for tests / stats
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ sync core
    def _resolve(self, host: str, port: int) -> list[str]:
        if self._resolver:
            return self._resolver(host, port)
        return resolve_ips(host, port, allow_private=self.allow_private)

    def _get_sync(self, url: str, max_bytes: int, accept: tuple, headers: dict | None) -> Fetched:
        deadline = time.monotonic() + self.total_timeout
        current = url
        for _ in range(self.max_redirects + 1):
            try:
                parsed = validate_url(current, self.allowed_ports, self.allow_private)
                ips = self._resolve(parsed.host, parsed.port)
            except ValidationError as exc:
                raise DownloadError(f"Blocked URL: {exc}") from exc
            conn = None
            last_err: Exception | None = None
            for ip in ips[:3]:
                try:
                    if parsed.scheme == "https":
                        conn = _PinnedHTTPS(parsed.host, parsed.port, ip, self.timeout, self._ssl)
                    else:
                        conn = _PinnedHTTP(parsed.host, parsed.port, ip, self.timeout)
                    req_headers = {
                        "User-Agent": self.user_agent,
                        "Accept": "image/*,application/json;q=0.9,*/*;q=0.5",
                        "Accept-Encoding": "identity",
                        **(headers or {}),
                    }
                    self.network_requests += 1
                    conn.request("GET", parsed.target, headers=req_headers)
                    resp = conn.getresponse()
                    break
                except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                    last_err = exc
                    try:
                        conn.close()  # type: ignore[union-attr]
                    except Exception:  # pragma: no cover
                        pass
                    conn = None
            else:
                raise DownloadError(f"Connection failed: {last_err.__class__.__name__}")
            try:
                if resp.status in REDIRECT_CODES:
                    location = resp.getheader("Location")
                    if not location:
                        raise DownloadError("Redirect without Location.")
                    current = urljoin(current, location)
                    continue
                if resp.status != 200:
                    raise DownloadError(f"HTTP {resp.status}")
                ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
                if accept and not any(ctype.startswith(a) for a in accept):
                    raise DownloadError(f"Unexpected content type: {ctype or 'unknown'}")
                length = resp.getheader("Content-Length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise DownloadError("File is too large.")
                buf = bytearray()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise DownloadError("File is too large.")
                    if time.monotonic() > deadline:
                        raise DownloadError("Download timed out.")
                return Fetched(bytes(buf), ctype, current)
            except (OSError, http.client.HTTPException) as exc:
                raise DownloadError(f"Download failed: {exc.__class__.__name__}") from exc
            finally:
                try:
                    conn.close()
                except Exception:  # pragma: no cover
                    pass
        raise DownloadError("Too many redirects.")

    # ------------------------------------------------------------------ async API
    async def fetch(self, url: str, accept: tuple = ("image/",), max_bytes: int | None = None,
                    headers: dict | None = None) -> Fetched:
        limit = max_bytes or self.max_bytes
        async with self._sem:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._get_sync, url, limit, accept, headers),
                    timeout=self.total_timeout + 5,
                )
            except asyncio.TimeoutError as exc:
                raise DownloadError("Download timed out.") from exc

    async def fetch_json(self, url: str, headers: dict | None = None, max_bytes: int = 2_000_000) -> Any:
        res = await self.fetch(url, accept=("application/json", "text/json", "text/plain"),
                               max_bytes=max_bytes, headers=headers)
        try:
            return json.loads(res.data.decode("utf-8", "replace"))
        except ValueError as exc:
            raise DownloadError("Invalid JSON response.") from exc

    def _cache_path(self, key: str) -> Path | None:
        return self.cache_dir / f"{key}.img" if self.cache_dir else None

    def _read_cache(self, key: str) -> bytes | None:
        path = self._cache_path(key)
        if not path:
            return None
        try:
            if path.exists() and time.time() - path.stat().st_mtime < self.cache_ttl:
                return path.read_bytes()
        except OSError:
            pass
        return None

    def _write_cache(self, key: str, data: bytes) -> None:
        path = self._cache_path(key)
        if not path:
            return
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError:
            log.warning("Could not write download cache")

    async def _fetch_image_uncached(self, url: str, key: str) -> bytes:
        res = await self.fetch(url, accept=("image/",))
        try:
            await asyncio.to_thread(validate_image_bytes, res.data, self.max_bytes)
        except ValidationError as exc:
            raise DownloadError(str(exc)) from exc
        await asyncio.to_thread(self._write_cache, key, res.data)
        return res.data

    async def fetch_image(self, url: str) -> bytes:
        """Download + decode-validate an image. Cached and de-duplicated."""
        key = hashlib.sha256(url.encode()).hexdigest()
        cached = await asyncio.to_thread(self._read_cache, key)
        if cached is not None:
            return cached
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch_image_uncached(url, key))
            self._inflight[key] = task
            task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))
        return await asyncio.shield(task)

    def purge_cache(self) -> int:
        """Delete expired cache files; returns number removed."""
        if not self.cache_dir:
            return 0
        removed = 0
        now = time.time()
        for path in self.cache_dir.glob("*"):
            try:
                if now - path.stat().st_mtime > self.cache_ttl:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
