import io

import pytest
from PIL import Image

from bot.services import image_processor as ip
from bot.services.background_removal import BackgroundRemovalUnavailable, BackgroundRemover
from conftest import run
from helpers import cutout_png, fake_remover_backend, jpeg_bytes, png_bytes

MAXB = 15 * 1024 * 1024


def test_exif_orientation_is_corrected():
    im = Image.new("RGB", (100, 40), (255, 0, 0))
    exif = Image.Exif()
    exif[0x0112] = 6   # rotate 90 CW when displayed -> becomes 40x100
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    assert ip.load_image(buf.getvalue(), MAXB).size == (40, 100)


def test_webp_output_matches_telegram_spec():
    for src in (png_bytes((300, 100)), jpeg_bytes((90, 400)), cutout_png()):
        res = run(ip.StickerBuilder(None, MAXB).build(src, ip.StickerOptions(bg_mode="keep")))
        w, h, _ = ip.webp_info(res.webp)
        assert max(w, h) == 512 and w <= 512 and h <= 512
        assert len(res.webp) <= 512 * 1024
        assert Image.open(io.BytesIO(res.webp)).format == "WEBP"


def test_noisy_image_stays_under_size_limit():
    noisy = Image.effect_noise((512, 512), 80).convert("RGBA")
    assert len(ip.to_webp(noisy)) <= 512 * 1024


def test_transparency_is_reported_honestly():
    opaque = run(ip.StickerBuilder(None, MAXB).build(jpeg_bytes(), ip.StickerOptions(bg_mode="keep")))
    assert opaque.has_alpha is False                      # JPEG has no alpha -> never claimed transparent
    cut = run(ip.StickerBuilder(None, MAXB).build(cutout_png(), ip.StickerOptions(bg_mode="auto")))
    assert cut.has_alpha is True and not cut.removed_bg   # already transparent -> bg removal skipped
    white = run(ip.StickerBuilder(None, MAXB).build(cutout_png(), ip.StickerOptions(bg_mode="white")))
    assert white.has_alpha is False                       # flattened on white


def test_crop_to_subject_and_outline_and_shadow():
    img = ip.load_image(cutout_png(), MAXB)
    cropped = ip.crop_to_subject(img, pad_ratio=0)
    assert cropped.size == (81, 61)
    canvas = ip.fit_to_canvas(cropped, 20)
    base = sum(1 for a in canvas.getchannel("A").getdata() if a > 128)
    with_outline = ip.add_outline(canvas, (255, 255, 255), 8)
    assert sum(1 for a in with_outline.getchannel("A").getdata() if a > 128) > base
    shadow = ip.add_shadow(canvas)
    assert sum(1 for a in shadow.getchannel("A").getdata() if a > 0) > base


def test_outline_and_shadow_fit_inside_canvas():
    res = run(ip.StickerBuilder(None, MAXB).build(
        cutout_png(), ip.StickerOptions(bg_mode="keep", outline="white", shadow=True, crop=True)))
    w, h, alpha = ip.webp_info(res.webp)
    assert (w, h) == (512, 512) and alpha
    edge = Image.open(io.BytesIO(res.webp)).convert("RGBA")
    assert edge.getpixel((0, 0))[3] == 0 and edge.getpixel((511, 511))[3] == 0   # nothing clipped at corners


def test_custom_background_color():
    res = run(ip.StickerBuilder(None, MAXB).build(
        cutout_png(), ip.StickerOptions(bg_mode="custom", custom_color="#00FF00", crop=False)))
    im = Image.open(io.BytesIO(res.webp)).convert("RGBA")
    r, g, b, a = im.getpixel((256, 256))
    assert a == 255


# ---------------------------------------------------------------- background removal
def test_background_removal_with_backend():
    remover = BackgroundRemover(backend=fake_remover_backend)
    res = run(ip.StickerBuilder(remover, MAXB).build(jpeg_bytes((120, 120)), ip.StickerOptions(bg_mode="auto")))
    assert res.removed_bg and res.has_alpha


def test_keep_mode_skips_removal():
    remover = BackgroundRemover(backend=lambda b: (_ for _ in ()).throw(AssertionError("must not run")))
    res = run(ip.StickerBuilder(remover, MAXB).build(jpeg_bytes(), ip.StickerOptions(bg_mode="keep")))
    assert not res.removed_bg and not res.has_alpha


def test_missing_rembg_falls_back_gracefully():
    def boom(_):
        raise BackgroundRemovalUnavailable("no rembg")
    res = run(ip.StickerBuilder(BackgroundRemover(backend=boom), MAXB).build(jpeg_bytes(), ip.StickerOptions()))
    assert "bg_unavailable" in res.notes and not res.has_alpha and res.webp


def test_removal_crash_is_reported_not_raised():
    def boom(_):
        raise RuntimeError("onnx exploded")
    res = run(ip.StickerBuilder(BackgroundRemover(backend=boom), MAXB).build(jpeg_bytes(), ip.StickerOptions()))
    assert "bg_failed" in res.notes


def test_real_rembg_if_installed():
    pytest.importorskip("rembg")
    r = BackgroundRemover()
    if not r.available:
        pytest.skip("rembg unavailable")
    try:
        out = run(r.remove(ip.load_image(jpeg_bytes((64, 64)), MAXB)))
    except BackgroundRemovalUnavailable:
        pytest.skip("rembg model could not be downloaded (offline)")
    assert out.mode == "RGBA"


def test_invalid_image_is_rejected_by_pipeline():
    from bot.utils.validators import ValidationError
    with pytest.raises(ValidationError):
        run(ip.StickerBuilder(None, MAXB).build(b"GIF89a nonsense", ip.StickerOptions()))


# ---------------------------------------------------------------------- text stickers
def test_text_sticker_variants():
    for bg in ip.TEXT_BACKGROUNDS:
        canvas = ip.render_text_sticker("Good Morning", background=bg, color="pink", outline="white")
        assert canvas.size == (512, 512)
        assert ip.has_transparency(canvas)   # transparent bg or rounded corners
    t = ip.render_text_sticker("Happy Birthday", background="transparent")
    assert t.getchannel("A").getbbox() is not None


def test_text_sticker_rejects_empty_and_emoji_only():
    with pytest.raises(ValueError):
        ip.render_text_sticker("😀😀")


def test_split_emoji():
    assert ip.split_emoji("Love you 💗🌸") == ("Love you", ["💗", "🌸"])
    assert ip.split_emoji("plain") == ("plain", [])


def test_contact_sheet_and_preview():
    webps = [ip.to_webp(ip.fit_to_canvas(ip.load_image(cutout_png(), MAXB), 10)) for _ in range(5)]
    sheet = ip.make_contact_sheet(webps)
    assert Image.open(io.BytesIO(sheet)).format == "JPEG"
    assert Image.open(io.BytesIO(ip.make_preview_jpeg(png_bytes((3000, 2000))))).width <= 900


def test_available_fonts_never_crashes(tmp_path):
    fonts = ip.available_fonts([tmp_path])
    assert isinstance(fonts, dict)
    assert ip.load_font("nonexistent", 40, fonts) is not None


def test_illustration_vs_photo_heuristic():
    import random
    flat = Image.new("RGB", (200, 300), (250, 230, 200))
    from PIL import ImageDraw
    ImageDraw.Draw(flat).ellipse((40, 60, 160, 200), fill=(230, 60, 60))
    assert ip.looks_like_illustration(flat.convert("RGBA"))
    random.seed(1)
    noisy = Image.effect_noise((200, 300), 60).convert("RGB")
    assert not ip.looks_like_illustration(noisy.convert("RGBA"))
