"""Pillow based sticker pipeline: orientation, crop, backgrounds, outline, shadow,
text, WEBP export (Telegram static sticker spec: 512x512 canvas, WEBP, <= 512 KB)."""
from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from ..utils.validators import parse_hex_color, validate_image_bytes

log = logging.getLogger(__name__)

STICKER_SIZE = 512
MAX_STICKER_BYTES = 512 * 1024
WORK_MAX_SIDE = 1536

BG_MODES = ("auto", "keep", "transparent", "white", "custom")
OUTLINES = ("none", "white", "black")


@dataclass(frozen=True)
class StickerOptions:
    bg_mode: str = "auto"          # auto | keep | transparent | white | custom
    custom_color: str = "#FFD6E8"
    outline: str = "none"          # none | white | black
    outline_px: int = 7
    shadow: bool = False
    crop: bool = True
    caption: str = ""              # optional text overlay at the bottom


@dataclass
class BuildResult:
    webp: bytes
    has_alpha: bool
    removed_bg: bool
    notes: list = field(default_factory=list)


class Remover(Protocol):
    async def remove(self, img: Image.Image) -> Image.Image: ...


# --------------------------------------------------------------------- loading
def load_image(data: bytes, max_bytes: int, work_max: int = WORK_MAX_SIDE) -> Image.Image:
    """Validate (decode), fix EXIF orientation, convert to RGBA and bound the size."""
    validate_image_bytes(data, max_bytes)
    with Image.open(io.BytesIO(data)) as im:
        if getattr(im, "is_animated", False):
            im.seek(0)
        im = ImageOps.exif_transpose(im)
        rgba = im.convert("RGBA")
    if max(rgba.size) > work_max:
        rgba.thumbnail((work_max, work_max), Image.Resampling.LANCZOS)
    return rgba


def has_transparency(img: Image.Image) -> bool:
    """True only if the image really has an alpha channel with non-opaque pixels."""
    if img.mode != "RGBA":
        return False
    return img.getchannel("A").getextrema()[0] < 255


def transparent_fraction(img: Image.Image) -> float:
    if img.mode != "RGBA":
        return 0.0
    hist = img.getchannel("A").histogram()
    total = sum(hist) or 1
    return sum(hist[:250]) / total


# --------------------------------------------------------------------- effects
def crop_to_subject(img: Image.Image, pad_ratio: float = 0.03) -> Image.Image:
    alpha = img.getchannel("A").point(lambda v: 255 if v > 16 else 0)
    bbox = alpha.getbbox()
    if not bbox:
        return img
    cropped = img.crop(bbox)
    pad = int(max(cropped.size) * pad_ratio)
    if pad <= 0:
        return cropped
    canvas = Image.new("RGBA", (cropped.width + 2 * pad, cropped.height + 2 * pad), (0, 0, 0, 0))
    canvas.paste(cropped, (pad, pad))
    return canvas


def flatten_on_color(img: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
    bg = Image.new("RGBA", img.size, rgb + (255,))
    bg.alpha_composite(img)
    return bg


def add_outline(img: Image.Image, color: tuple[int, int, int], width: int) -> Image.Image:
    """Dilate the alpha channel and paint it under the image (sticker border)."""
    if width <= 0:
        return img
    alpha = img.getchannel("A")
    grown = alpha.filter(ImageFilter.MaxFilter(2 * width + 1))
    grown = grown.filter(ImageFilter.GaussianBlur(1.1)).point(lambda v: 255 if v > 110 else 0)
    layer = Image.new("RGBA", img.size, color + (255,))
    layer.putalpha(grown)
    layer.alpha_composite(img)
    return layer


def add_shadow(img: Image.Image, offset: tuple[int, int] = (0, 5), blur: float = 5.0,
               opacity: float = 0.35) -> Image.Image:
    alpha = img.getchannel("A")
    shifted = Image.new("L", img.size, 0)
    shifted.paste(alpha, offset)
    shifted = shifted.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * opacity))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    shadow.putalpha(shifted)
    shadow.alpha_composite(img)
    return shadow


def fit_to_canvas(img: Image.Image, margin: int, size: int = STICKER_SIZE) -> Image.Image:
    inner = max(16, size - 2 * margin)
    fitted = ImageOps.contain(img, (inner, inner), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2))
    return canvas


# ------------------------------------------------------------------------ fonts
FONT_DIRS = [
    Path("data/fonts"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype/noto"),
    Path("/usr/share/fonts/truetype/liberation"),
]
PREFERRED_FONTS = ("DejaVuSans-Bold", "DejaVuSerif-Bold", "DejaVuSansMono-Bold",
                   "LiberationSans-Bold", "NotoSans-Bold")


def available_fonts(extra_dirs: list | None = None) -> dict:
    """name -> path for usable .ttf/.otf files. Users can drop fonts into data/fonts."""
    found: dict = {}
    for d in (extra_dirs or []) + FONT_DIRS:
        try:
            if not Path(d).is_dir():
                continue
            for p in sorted(Path(d).glob("*")):
                if p.suffix.lower() in (".ttf", ".otf") and p.stem not in found:
                    found[p.stem] = str(p)
        except OSError:
            continue
    ordered = {k: found[k] for k in PREFERRED_FONTS if k in found}
    for k, v in found.items():
        ordered.setdefault(k, v)
    return dict(list(ordered.items())[:12])


def load_font(name: str | None, size: int, fonts: dict | None = None):
    fonts = fonts if fonts is not None else available_fonts()
    path = fonts.get(name or "") or (next(iter(fonts.values())) if fonts else None)
    try:
        if path:
            return ImageFont.truetype(path, size)
        return ImageFont.load_default(size=size)
    except (OSError, TypeError):
        return ImageFont.load_default()


# -------------------------------------------------------------------- text sticker
TEXT_COLORS = {
    "pink": "#FF4F9A", "white": "#FFFFFF", "black": "#1B1B1F", "purple": "#8E5CF7",
    "blue": "#3B82F6", "red": "#EF4444", "yellow": "#FACC15", "mint": "#34D399",
}
OUTLINE_COLORS = {"white": "#FFFFFF", "black": "#1B1B1F", "pink": "#FF4F9A", "none": ""}
TEXT_BACKGROUNDS = ("transparent", "gradient", "pink", "mint", "lavender", "white")
_BG_SOLID = {"pink": "#FFD1E3", "mint": "#CFF7E4", "lavender": "#E3D8FF", "white": "#FFFFFF"}
PRESET_TEXTS = ["Good Morning", "Love You", "Thank You", "Miss You", "Bestie",
                "LOL", "OMG", "Bye Bye", "Happy Birthday"]

_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿⌀-⏿️‍]")


def split_emoji(text: str) -> tuple[str, list]:
    """Split text into (plain text, list of emoji). Emoji become sticker emoji metadata
    because Pillow cannot reliably draw colour emoji without extra fonts."""
    emojis: list = []
    for ch in _EMOJI_RE.findall(text or ""):
        if ch not in ("️", "‍") and ch not in emojis:
            emojis.append(ch)
    clean = _EMOJI_RE.sub("", text or "")
    return re.sub(r"\s+", " ", clean).strip(), emojis[:20]


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int, stroke: int) -> list:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) + 2 * stroke <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def draw_text_block(canvas: Image.Image, text: str, color: str, outline: str, font_name: str | None,
                    box: tuple[int, int, int, int], fonts: dict | None = None, anchor_bottom: bool = False) -> None:
    """Draw word-wrapped, auto-sized, centred text with optional outline inside ``box``."""
    x0, y0, x1, y1 = box
    max_w, max_h = x1 - x0, y1 - y0
    draw = ImageDraw.Draw(canvas)
    fill = parse_hex_color(TEXT_COLORS.get(color, color))
    stroke_fill = parse_hex_color(OUTLINE_COLORS.get(outline, "#FFFFFF")) if OUTLINE_COLORS.get(outline, "x") else None
    size = 170
    while True:
        font = load_font(font_name, size, fonts)
        stroke = max(0, size // 14) if stroke_fill else 0
        lines = _wrap(draw, text, font, max_w, stroke)
        line_h = int(size * 1.15)
        widest = max((draw.textlength(l, font=font) for l in lines), default=0) + 2 * stroke
        if (widest <= max_w and len(lines) * line_h <= max_h) or size <= 18:
            break
        size -= 6
    total_h = len(lines) * line_h
    y = y1 - total_h if anchor_bottom else y0 + (max_h - total_h) // 2
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text((x0 + (max_w - w) / 2, y), line, font=font, fill=fill,
                  stroke_width=stroke, stroke_fill=stroke_fill)
        y += line_h


def render_text_sticker(text: str, *, font_name: str | None = None, color: str = "pink",
                        outline: str = "white", background: str = "transparent",
                        fonts: dict | None = None) -> Image.Image:
    text, _ = split_emoji(text)
    if not text:
        raise ValueError("Text is empty.")
    text = text[:80]
    canvas = Image.new("RGBA", (STICKER_SIZE, STICKER_SIZE), (0, 0, 0, 0))
    pad = 20
    if background != "transparent":
        mask = Image.new("L", canvas.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((8, 8, STICKER_SIZE - 8, STICKER_SIZE - 8), radius=72, fill=255)
        if background == "gradient":
            grad = Image.linear_gradient("L").resize(canvas.size)
            top = Image.new("RGBA", canvas.size, parse_hex_color("#FFC1E3") + (255,))
            bottom = Image.new("RGBA", canvas.size, parse_hex_color("#C9B6FF") + (255,))
            fill_img = Image.composite(bottom, top, grad)
        else:
            fill_img = Image.new("RGBA", canvas.size, parse_hex_color(_BG_SOLID.get(background, "#FFFFFF")) + (255,))
        canvas.paste(fill_img, (0, 0), mask)
        pad = 44
    draw_text_block(canvas, text, color, outline, font_name, (pad, pad, STICKER_SIZE - pad, STICKER_SIZE - pad), fonts)
    return canvas


# --------------------------------------------------------------------- export
def to_webp(img: Image.Image) -> bytes:
    """Encode a <=512px RGBA canvas as WEBP under Telegram's 512 KB static limit."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    if max(img.size) > STICKER_SIZE:
        img = ImageOps.contain(img, (STICKER_SIZE, STICKER_SIZE), Image.Resampling.LANCZOS)
    out = b""
    for quality in (92, 85, 78, 70, 60, 50, 40, 30, 20):
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=quality, method=4)
        out = buf.getvalue()
        if len(out) <= MAX_STICKER_BYTES:
            return out
    raise ValueError("Could not compress sticker below 512 KB.")


def webp_info(data: bytes) -> tuple[int, int, bool]:
    with Image.open(io.BytesIO(data)) as im:
        rgba = im.convert("RGBA")
        return im.width, im.height, rgba.getchannel("A").getextrema()[0] < 255


def make_preview_jpeg(data: bytes, max_side: int = 900) -> bytes:
    """Re-encode any valid image as a bounded JPEG for sending as a Telegram photo."""
    with Image.open(io.BytesIO(data)) as im:
        if getattr(im, "is_animated", False):
            im.seek(0)
        im = ImageOps.exif_transpose(im).convert("RGBA")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    bg.paste(im, mask=im.getchannel("A"))
    bg.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    bg.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def make_contact_sheet(webps: list, cols: int = 4, cell: int = 160, limit: int = 20) -> bytes:
    """Pack preview image: grid of sticker thumbnails on a soft checker background."""
    shown = webps[:limit]
    rows = max(1, -(-len(shown) // cols))
    sheet = Image.new("RGB", (cols * cell, rows * cell), (255, 240, 246))
    draw = ImageDraw.Draw(sheet)
    for r in range(rows):
        for c in range(cols):
            if (r + c) % 2:
                draw.rectangle((c * cell, r * cell, (c + 1) * cell - 1, (r + 1) * cell - 1), fill=(250, 228, 238))
    for i, data in enumerate(shown):
        try:
            with Image.open(io.BytesIO(data)) as im:
                thumb = im.convert("RGBA")
            thumb.thumbnail((cell - 12, cell - 12), Image.Resampling.LANCZOS)
            x = (i % cols) * cell + (cell - thumb.width) // 2
            y = (i // cols) * cell + (cell - thumb.height) // 2
            sheet.paste(thumb, (x, y), thumb)
        except Exception:  # noqa: BLE001 - one broken thumbnail must not break the preview
            continue
    if len(webps) > limit:
        draw.text((8, 8), f"+{len(webps) - limit} more", fill=(190, 60, 120))
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=88)
    return buf.getvalue()


# --------------------------------------------------------------------- pipeline
def needs_background_removal(img: Image.Image, mode: str) -> bool:
    if mode == "keep":
        return False
    if transparent_fraction(img) > 0.02:   # already a cut-out / transparent PNG
        return False
    return mode in ("auto", "transparent", "white", "custom")


def finalize(img: Image.Image, opts: StickerOptions) -> tuple[Image.Image, bool]:
    """Crop/background/outline/shadow -> 512x512 RGBA canvas.

    Returns (canvas, subject_has_alpha). The flag describes the *picture itself*: transparent padding
    around a photo does not make it a transparent sticker, so we never claim transparency for it.
    """
    if opts.crop and has_transparency(img):
        img = crop_to_subject(img)
    if opts.bg_mode == "white":
        img = flatten_on_color(img, (255, 255, 255))
    elif opts.bg_mode == "custom":
        img = flatten_on_color(img, parse_hex_color(opts.custom_color))
    subject_alpha = has_transparency(img)
    outline_w = opts.outline_px if opts.outline != "none" else 0
    margin = 4 + outline_w + (10 if opts.shadow else 0)
    canvas = fit_to_canvas(img, margin)
    if outline_w:
        color = (255, 255, 255) if opts.outline == "white" else (0, 0, 0)
        canvas = add_outline(canvas, color, outline_w)
    if opts.shadow:
        canvas = add_shadow(canvas)
    if opts.caption:
        text, _ = split_emoji(opts.caption)
        if text:
            draw_text_block(canvas, text[:60], "white", "black", None, (24, 340, 488, 500), anchor_bottom=True)
    return canvas, subject_alpha


class StickerBuilder:
    """Combines loading, optional background removal and finalisation."""

    def __init__(self, remover: Remover | None, max_bytes: int):
        self.remover = remover
        self.max_bytes = max_bytes

    async def build(self, data: bytes, opts: StickerOptions) -> BuildResult:
        from .background_removal import BackgroundRemovalUnavailable  # local: avoid import cycle

        notes: list = []
        img = await asyncio.to_thread(load_image, data, self.max_bytes)
        removed = False
        if needs_background_removal(img, opts.bg_mode):
            if self.remover is None:
                notes.append("bg_unavailable")
            else:
                try:
                    img = await self.remover.remove(img)
                    removed = True
                except BackgroundRemovalUnavailable:
                    notes.append("bg_unavailable")
                except Exception:  # noqa: BLE001
                    log.exception("Background removal failed")
                    notes.append("bg_failed")

        def _finish() -> tuple[bytes, bool]:
            canvas, alpha = finalize(img, opts)
            return to_webp(canvas), alpha

        webp, alpha = await asyncio.to_thread(_finish)
        return BuildResult(webp=webp, has_alpha=alpha, removed_bg=removed, notes=notes)

    async def from_canvas(self, canvas: Image.Image) -> BuildResult:
        webp = await asyncio.to_thread(to_webp, canvas)
        return BuildResult(webp=webp, has_alpha=has_transparency(canvas), removed_bg=False)


def options_from_settings(s: dict, **overrides) -> StickerOptions:
    base = StickerOptions(
        bg_mode=s.get("bg_mode", "auto") if s.get("bg_mode") in BG_MODES else "auto",
        custom_color=s.get("custom_color", "#FFD6E8"),
        outline=s.get("outline", "none") if s.get("outline") in OUTLINES else "none",
        shadow=bool(s.get("shadow", False)),
    )
    return replace(base, **overrides) if overrides else base
