import io

from PIL import Image


def png_bytes(size=(64, 64), color=(200, 30, 30, 255), mode="RGBA"):
    im = Image.new(mode, size, color if mode == "RGBA" else color[:3])
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def cutout_png(size=(200, 120), box=(60, 30, 140, 90)):
    """Transparent PNG with an opaque rectangle (a fake 'subject')."""
    from PIL import ImageDraw
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle(box, fill=(255, 100, 150, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def jpeg_bytes(size=(80, 60), color=(10, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def fake_remover_backend(png: bytes) -> bytes:
    """Pretend rembg: makes everything outside a centred ellipse transparent."""
    from PIL import ImageDraw
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    mask = Image.new("L", im.size, 0)
    w, h = im.size
    ImageDraw.Draw(mask).ellipse((w * .2, h * .2, w * .8, h * .8), fill=255)
    im.putalpha(mask)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()
