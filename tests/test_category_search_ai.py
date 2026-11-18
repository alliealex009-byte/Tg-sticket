import base64
from types import SimpleNamespace

import pytest

from bot.services import ai_generator as ai
from bot.services import category_detector as cd
from bot.services import image_search as s
from bot.services import pinterest
from bot.services.search_types import SearchResult
from conftest import run


@pytest.mark.parametrize("text,expected", [
    ("cute cats", "cats"), ("yummy pizza", "food"), ("lovely flowers", "flowers"), ("anime girl", "anime"),
    ("iced latte", "coffee"), ("baby animals", "baby_animals"), ("pink aesthetic wallpaper", "pink"),
    ("chocolate cake", "desserts"), ("strawberries", "fruits"), ("golden retriever puppy", "dogs"),
])
def test_category_detection(text, expected):
    assert cd.detect_category(text) == expected


def test_category_detection_unknown_and_empty():
    assert cd.detect_category("zzzz qqqq") is None and cd.detect_category("", None) is None


def test_pack_title_format():
    assert cd.pack_title("food", "@Advik_prakashh") == "Food : @Advik_prakashh"
    assert cd.pack_title("cats", "@Advik_prakashh", 2) == "Cats Vibes : @Advik_prakashh"
    assert cd.pack_title("food", "@A", 3) == "Food Treats : @A"
    assert len(cd.pack_title("baby_animals", "@" + "x" * 80)) <= 64


# ------------------------------------------------------------------------------- search
def R(i, url=None, w=800, h=800, thumb=None):
    url = url or f"https://img.example.com/{i}.jpg"
    return SearchResult(id=str(i), title=f"t{i}", image_url=url, thumb_url=thumb or url + "?t", page_url="", provider="x",
                        width=w, height=h)


def test_filter_results_dedup_and_unsuitable():
    results = [R(1), R(1), R(2, "https://x.com/a.svg"), R(3, "ftp://x.com/a.jpg"), R(4, w=50, h=50),
               R(5, "javascript:alert(1)"), R(6, "https://x.com/doc.pdf"), R(7)]
    out = s.filter_results(results, 10)
    assert [r.id for r in out] == ["1", "7"]


def test_pinterest_only_keeps_real_cdn_images_not_page_urls():
    items = [
        {"image": "https://i.pinimg.com/736x/aa/bb.jpg", "thumbnail": "https://tse.example/t.jpg",
         "url": "https://www.pinterest.com/pin/123/", "title": "cat", "width": 700, "height": 900},
        {"image": "https://www.pinterest.com/pin/123/", "url": "https://www.pinterest.com/pin/123/"},   # page URL
        {"image": "https://evil.example/pinimg.jpg"},
    ]
    out = pinterest.convert(items, 10)
    assert len(out) == 1 and out[0].image_url.startswith("https://i.pinimg.com/")
    assert "unknown" in out[0].license.lower()


class FakeProvider:
    def __init__(self, name, results=None, error=None):
        self.name, self._r, self._e, self.calls = name, results or [], error, 0

    async def search(self, q, limit, page=1):
        self.calls += 1
        if self._e:
            raise self._e
        return self._r


def test_search_falls_back_between_providers_and_caches(settings):
    bad = FakeProvider("pinterest", error=RuntimeError("blocked"))
    good = FakeProvider("openverse", [R(i) for i in range(30)])
    svc = s.ImageSearchService(settings, None, providers=[bad, good])
    first = run(svc.search("cute cats", 5))
    assert len(first) == 5 and good.calls == 1
    run(svc.search("Cute  cats", 5))                      # normalised + cached
    assert good.calls == 1 and bad.calls == 1
    assert run(svc.search("   ")) == []


def test_search_all_providers_fail_returns_empty(settings):
    svc = s.ImageSearchService(settings, None, providers=[FakeProvider("a", error=ValueError("x"))])
    assert run(svc.search("food")) == []


def test_openverse_provider_maps_license_and_attribution():
    class DL:
        async def fetch_json(self, url):
            assert "api.openverse.org" in url and "q=cute%20cats" in url
            return {"results": [{"url": "https://x.org/a.jpg", "thumbnail": "https://x.org/t.jpg", "title": "Cat",
                                 "creator": "Ann", "license": "by", "license_version": "4.0",
                                 "license_url": "https://creativecommons.org/licenses/by/4.0/",
                                 "foreign_landing_url": "https://x.org/page", "attribution": "Cat by Ann", "width": 900, "height": 700}]}
    out = run(s.OpenverseProvider(DL()).search("cute cats", 5))
    assert out[0].license == "CC BY 4.0" and out[0].creator == "Ann" and out[0].page_url == "https://x.org/page"


def test_search_result_roundtrip():
    r = R(1)
    assert SearchResult.from_dict({**r.to_dict(), "unknown": 1}) == r


# ----------------------------------------------------------------------------- AI generator
def test_prompt_contains_style_and_subject():
    p = ai.build_prompt("pink cat holding a strawberry", "kawaii")
    assert "pink cat holding a strawberry" in p and "kawaii" in p.lower() and "transparent" in p
    assert len(ai.STYLES) == 13


def test_premium_styles_flagged():
    assert ai.is_premium_style("3d") and not ai.is_premium_style("anime")


class FakeImages:
    def __init__(self, payload=b"PNGDATA", error=None):
        self.kwargs, self.payload, self.error = None, payload, error

    async def generate(self, **kw):
        self.kwargs = kw
        if self.error:
            raise self.error
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(self.payload).decode())])

    async def edit(self, **kw):
        self.kwargs = kw
        return await self.generate(**kw)


def test_generate_uses_images_api_with_transparency():
    images = FakeImages()
    gen = ai.AIGenerator("sk-test", client=SimpleNamespace(images=images))
    assert run(gen.generate("a cat", "anime")) == b"PNGDATA"
    assert images.kwargs["model"] == "gpt-image-1" and images.kwargs["extra_body"]["background"] == "transparent"


def test_dalle_model_requests_b64():
    images = FakeImages()
    gen = ai.AIGenerator("sk-test", model="dall-e-3", client=SimpleNamespace(images=images))
    run(gen.generate("a cat", "anime"))
    assert images.kwargs["response_format"] == "b64_json" and "extra_body" not in images.kwargs
    with pytest.raises(ai.AIUnavailable):
        run(gen.stylize_photo(b"x", "anime"))


def test_not_configured_and_error_mapping():
    gen = ai.AIGenerator("")
    assert not gen.enabled
    with pytest.raises(ai.AINotConfigured):
        run(gen.generate("x", "anime"))

    class RateLimitError(Exception): ...
    class AuthenticationError(Exception): ...
    assert "busy" in ai.friendly_error(RateLimitError("429"))
    msg = ai.friendly_error(AuthenticationError("Incorrect API key provided: sk-secret123456789012345"))
    assert "sk-" not in msg
    assert "safety" in ai.friendly_error(RuntimeError("blocked by moderation_blocked")).lower()


def test_classify_returns_valid_key_only():
    def client(answer):
        async def create(**kw):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert run(ai.AIGenerator("k", client=client("Cats")).classify(b"x")) == "cats"
    assert run(ai.AIGenerator("k", client=client("spaceship")).classify(b"x")) is None
