import io

import pytest
from PIL import Image

from bot.utils import validators as v
from bot.utils.logger import redact
from helpers import jpeg_bytes, png_bytes


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a.png", "http://10.0.0.5/a.png", "https://192.168.1.1/x", "http://169.254.169.254/latest",
    "http://[::1]/x", "http://localhost/x", "http://foo.internal/x", "http://100.64.0.1/x",
    "ftp://example.com/a.png", "file:///etc/passwd", "https://user:pw@example.com/a.png",
    "https://example.com:8080/a.png", "http://[::ffff:127.0.0.1]/x", "", "javascript:alert(1)",
])
def test_ssrf_urls_rejected(url):
    with pytest.raises(v.ValidationError):
        v.validate_url(url)


def test_good_url_parsed():
    p = v.validate_url("https://i.pinimg.com/736x/ab/cd.jpg?x=1")
    assert (p.scheme, p.host, p.port, p.target) == ("https", "i.pinimg.com", 443, "/736x/ab/cd.jpg?x=1")


def test_is_public_ip():
    assert v.is_public_ip("8.8.8.8") and not v.is_public_ip("127.0.0.1") and not v.is_public_ip("garbage")
    assert not v.is_public_ip("::ffff:10.0.0.1")


def test_dns_resolving_to_private_is_blocked():
    with pytest.raises(v.ValidationError):
        v.resolve_ips("localhost", 80)


def test_image_validation_accepts_real_images():
    assert v.validate_image_bytes(png_bytes(), 10**6).format == "PNG"
    assert v.validate_image_bytes(jpeg_bytes(), 10**6).format == "JPEG"


def test_image_validation_rejects_bad_input():
    with pytest.raises(v.ValidationError):
        v.validate_image_bytes(b"", 10)
    with pytest.raises(v.ValidationError):
        v.validate_image_bytes(b"<html>not an image</html>", 10**6)
    with pytest.raises(v.ValidationError):
        v.validate_image_bytes(png_bytes(), 10)                       # too many bytes
    with pytest.raises(v.ValidationError):
        v.validate_image_bytes(png_bytes()[:-30], 10**6)              # truncated -> decode fails
    with pytest.raises(v.ValidationError):
        v.validate_image_bytes(png_bytes((4, 4)), 10**6)              # too small
    with pytest.raises(v.ValidationError):
        v.validate_image_bytes(png_bytes((300, 300)), 10**6, max_pixels=1000)   # too many pixels
    buf = io.BytesIO()
    Image.new("RGB", (20, 20)).save(buf, "BMP")
    with pytest.raises(v.ValidationError):
        v.validate_image_bytes(buf.getvalue(), 10**6)                 # unsupported format


def test_sanitize_filename():
    assert v.sanitize_filename("../../etc/passwd") == "passwd"
    assert v.sanitize_filename("my cute cat!!.png") == "my_cute_cat.png"
    assert v.sanitize_filename("...") == "image"
    assert len(v.sanitize_filename("a" * 500 + ".jpg")) <= 70


def test_query_and_colour():
    assert v.clean_query("  cute\x00 \n cats  ") == "cute cats"
    assert v.parse_hex_color("#ff00AA") == (255, 0, 170)
    with pytest.raises(v.ValidationError):
        v.parse_hex_color("red")


def test_logs_never_leak_secrets():
    text = ("token 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi key sk-proj-abcdefghijklmnop1234 "
            "mongodb://user:pw@host/db https://x.io/api/?key=SECRET123&q=cat")
    out = redact(text)
    for secret in ("ABCDEFGHIJKLMNOP", "sk-proj", "user:pw", "SECRET123"):
        assert secret not in out
