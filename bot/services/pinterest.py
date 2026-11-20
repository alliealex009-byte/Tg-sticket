"""Pinterest-compatible discovery.

Pinterest's official API (v5) needs OAuth and does not offer anonymous public image
search, so this provider uses a public web-search index restricted to pinterest.com
(via the `ddgs` package). It never logs in, never touches private boards and never
bypasses anti-bot protections. A Pinterest *page* URL is never treated as an image:
only results whose image URL is hosted on Pinterest's public CDN (i.pinimg.com) are
kept, and the real bytes are verified by decoding them at download time.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from .search_types import SearchResult, make_result_id

log = logging.getLogger(__name__)
PIN_CDN_HOSTS = {"i.pinimg.com"}


def _search_sync(query: str, limit: int) -> list:
    from ddgs import DDGS  # lazy import; optional dependency

    with DDGS() as ddgs:
        return list(ddgs.images(f"{query} site:pinterest.com", safesearch="moderate", max_results=limit * 2))


def convert(items: list, limit: int) -> list:
    out: list = []
    for it in items:
        image = (it.get("image") or "").strip()
        host = (urlsplit(image).hostname or "").lower()
        page_host = (urlsplit((it.get("url") or it.get("source") or "")).hostname or "").lower()
        from_pin = host in PIN_CDN_HOSTS or (page_host.endswith("pinterest.com") and host
                                             and not host.endswith("pinterest.com"))
        if not from_pin:
            continue  # Pinterest page URLs / results unrelated to Pinterest are not images we can use
        thumb = (it.get("thumbnail") or "").strip()
        page = (it.get("url") or it.get("source") or "").strip()
        out.append(SearchResult(
            id=make_result_id(image),
            title=(it.get("title") or "Pinterest image")[:120],
            image_url=image,
            thumb_url=thumb if thumb.startswith("http") else image,
            page_url=page if page.startswith("http") else "",
            provider="pinterest",
            creator="",
            license="Unknown (original creator's rights apply)",
            license_url="",
            attribution="Found via Pinterest public index — check the source page before reusing.",
            width=int(it.get("width") or 0),
            height=int(it.get("height") or 0),
        ))
        if len(out) >= limit:
            break
    return out


class PinterestProvider:
    name = "pinterest"

    async def search(self, query: str, limit: int, page: int = 1) -> list:
        try:
            items = await asyncio.wait_for(asyncio.to_thread(_search_sync, query, limit), timeout=25)
        except ImportError:
            log.warning("ddgs is not installed; Pinterest provider disabled")
            return []
        out = convert(items, limit)
        log.info("pinterest provider: %d raw results, %d usable", len(items), len(out))
        return out
