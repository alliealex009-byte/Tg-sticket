"""Multi-provider image search: Pinterest-compatible index, Pixabay (API key), Openverse (no key).

Providers run in priority order; the first ones that return results fill the list.
Results are filtered (bad URLs, svg, tiny images), de-duplicated and cached.
"""
from __future__ import annotations

import logging
from urllib.parse import quote, urlsplit

from ..config import Settings
from ..utils.helpers import TTLCache
from ..utils.validators import clean_query
from .downloader import Downloader
from .pinterest import PinterestProvider
from .search_types import SearchResult, make_result_id

log = logging.getLogger(__name__)
BAD_EXTENSIONS = (".svg", ".ico", ".bmp", ".tiff", ".pdf")
MIN_DIM = 200


class OpenverseProvider:
    """Openverse API (WordPress/Creative Commons). No key needed; gives licence + attribution."""
    name = "openverse"

    def __init__(self, downloader: Downloader):
        self.dl = downloader

    async def search(self, query: str, limit: int, page: int = 1) -> list:
        url = ("https://api.openverse.org/v1/images/?q=" + quote(query)
               + f"&page_size={min(limit, 20)}&page={page}&mature=false")
        data = await self.dl.fetch_json(url)
        out = []
        for it in data.get("results", []):
            image = it.get("url") or ""
            if not image:
                continue
            lic = (it.get("license") or "").upper()
            ver = it.get("license_version") or ""
            out.append(SearchResult(
                id=make_result_id(image), title=(it.get("title") or "Untitled")[:120], image_url=image,
                thumb_url=it.get("thumbnail") or image, page_url=it.get("foreign_landing_url") or "",
                provider="openverse", creator=(it.get("creator") or "")[:80],
                license=f"CC {lic} {ver}".strip(), license_url=it.get("license_url") or "",
                attribution=(it.get("attribution") or "")[:300],
                width=int(it.get("width") or 0), height=int(it.get("height") or 0),
            ))
        return out


class PixabayProvider:
    """Pixabay API (needs PIXABAY_API_KEY). Content under the Pixabay Content License."""
    name = "pixabay"

    def __init__(self, downloader: Downloader, api_key: str):
        self.dl, self.key = downloader, api_key

    async def search(self, query: str, limit: int, page: int = 1) -> list:
        url = (f"https://pixabay.com/api/?key={quote(self.key)}&q={quote(query)}&safesearch=true"
               f"&per_page={max(3, min(limit, 50))}&page={page}&image_type=all")
        data = await self.dl.fetch_json(url)
        out = []
        for it in data.get("hits", []):
            image = it.get("largeImageURL") or it.get("webformatURL") or ""
            if not image:
                continue
            out.append(SearchResult(
                id=make_result_id(image), title=(it.get("tags") or "Pixabay image")[:120], image_url=image,
                thumb_url=it.get("webformatURL") or it.get("previewURL") or image, page_url=it.get("pageURL") or "",
                provider="pixabay", creator=(it.get("user") or "")[:80],
                license="Pixabay Content License", license_url="https://pixabay.com/service/license-summary/",
                attribution=f"Image by {it.get('user', 'a Pixabay creator')} on Pixabay",
                width=int(it.get("imageWidth") or 0), height=int(it.get("imageHeight") or 0),
            ))
        return out


def filter_results(results: list, limit: int) -> list:
    """Drop broken/unsuitable results and duplicates (by image URL and thumbnail)."""
    seen_img, seen_thumb, out = set(), set(), []
    for r in results:
        try:
            parts = urlsplit(r.image_url)
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        if parts.path.lower().endswith(BAD_EXTENSIONS):
            continue
        if r.width and r.height and min(r.width, r.height) < MIN_DIM:
            continue
        key = r.image_url.split("?")[0].lower()
        if key in seen_img or (r.thumb_url and r.thumb_url in seen_thumb):
            continue
        seen_img.add(key)
        seen_thumb.add(r.thumb_url)
        out.append(r)
        if len(out) >= limit:
            break
    return out


class ImageSearchService:
    def __init__(self, settings: Settings, downloader: Downloader, providers: list | None = None):
        self.settings = settings
        self.cache = TTLCache(ttl=600, maxsize=200)
        if providers is not None:
            self.providers = providers
        else:
            self.providers = []
            if settings.enable_pinterest:
                self.providers.append(PinterestProvider())
            if settings.pixabay_api_key:
                self.providers.append(PixabayProvider(downloader, settings.pixabay_api_key))
            if settings.enable_openverse:
                self.providers.append(OpenverseProvider(downloader))
        self.api_calls = 0

    @property
    def provider_names(self) -> list:
        return [p.name for p in self.providers]

    async def search(self, query: str, limit: int | None = None) -> list:
        query = clean_query(query)
        if not query:
            return []
        limit = limit or self.settings.search_results
        key = (query.lower(), limit)
        cached = self.cache.get(key)
        if cached is not None:
            return list(cached)
        collected: list = []
        for provider in self.providers:
            try:
                self.api_calls += 1
                collected.extend(await provider.search(query, limit))
            except Exception as exc:  # noqa: BLE001 - one broken provider must not kill search
                log.warning("Search provider %s failed: %s: %s", provider.name, exc.__class__.__name__, str(exc)[:150])
                continue
            collected = filter_results(collected, limit)
            if len(collected) >= limit:
                break
        results = filter_results(collected, limit)
        if results:
            self.cache.set(key, results)
        return list(results)
