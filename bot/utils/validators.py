"""Input validation: URLs (SSRF protection), image bytes, filenames, colours, queries."""
from __future__ import annotations

import io
import ipaddress
import re
import socket
from typing import NamedTuple
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError

MAX_PIXELS = 25_000_000
MIN_SIDE = 8
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # Pillow raises DecompressionBombError beyond 2x this


class ValidationError(ValueError):
    """Raised for any rejected user/remote input."""


class ParsedURL(NamedTuple):
    scheme: str
    host: str
    port: int
    target: str  # path + query


class ImageInfo(NamedTuple):
    format: str
    width: int
    height: int


def is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return bool(addr.is_global) and not addr.is_multicast


def validate_url(url: str, allowed_ports: tuple | None = (80, 443), allow_private: bool = False) -> ParsedURL:
    """Validate a URL for server-side fetching. Never trusts the hostname alone."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise ValidationError("Invalid URL.")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise ValidationError("Invalid URL.") from exc
    if parts.scheme not in ("http", "https"):
        raise ValidationError("Only http/https URLs are allowed.")
    if parts.username or parts.password:
        raise ValidationError("URLs with credentials are not allowed.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ValidationError("URL has no host.")
    port = port or (443 if parts.scheme == "https" else 80)
    if allowed_ports is not None and port not in allowed_ports:
        raise ValidationError("URL port is not allowed.")
    if not allow_private:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
            raise ValidationError("Local hostnames are not allowed.")
        try:
            ipaddress.ip_address(host)
            literal = True
        except ValueError:
            literal = False
        if literal and not is_public_ip(host):
            raise ValidationError("Private or reserved IP addresses are not allowed.")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return ParsedURL(parts.scheme, host, port, target)


def resolve_ips(host: str, port: int, allow_private: bool = False) -> list[str]:
    """Resolve host and require EVERY address to be public (blocks DNS tricks)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValidationError("Could not resolve host.") from exc
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if not allow_private and not is_public_ip(ip):
            raise ValidationError("Host resolves to a non-public address.")
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise ValidationError("Host did not resolve.")
    return ips


def validate_image_bytes(data: bytes, max_bytes: int, max_pixels: int = MAX_PIXELS) -> ImageInfo:
    """Validate by actually decoding the image (not by trusting extension/MIME)."""
    if not data:
        raise ValidationError("Empty file.")
    if len(data) > max_bytes:
        raise ValidationError(f"Image is larger than {max_bytes // (1024 * 1024)} MB.")
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format or ""
            width, height = im.size
            if fmt not in ALLOWED_FORMATS:
                raise ValidationError("Unsupported image format (use JPEG, PNG, WEBP or GIF).")
            if width < MIN_SIDE or height < MIN_SIDE:
                raise ValidationError("Image is too small.")
            if width * height > max_pixels:
                raise ValidationError("Image dimensions are too large.")
            im.load()  # full decode: catches truncated / corrupt files
    except ValidationError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError, EOFError) as exc:
        raise ValidationError("That file is not a valid image.") from exc
    return ImageInfo(fmt, width, height)


_FILENAME_BAD = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str, default: str = "image", max_len: int = 60) -> str:
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = _FILENAME_BAD.sub("_", name).strip("._")
    if not name:
        name = default
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = stem.strip("._")[:max_len] or default
    return f"{stem}.{ext[:5]}" if ext else stem


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean_query(query: str, max_len: int = 80) -> str:
    query = _CTRL.sub(" ", query or "")
    return re.sub(r"\s+", " ", query).strip()[:max_len]


_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_hex_color(value: str) -> tuple[int, int, int]:
    m = _HEX.match((value or "").strip())
    if not m:
        raise ValidationError("Colour must look like #FFAACC.")
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
