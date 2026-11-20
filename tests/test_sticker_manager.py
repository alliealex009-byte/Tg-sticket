
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from unittest.mock import MagicMock

from bot.config import Settings
from bot.database import Database
from bot.services import sticker_manager as sm
from bot.services.image_processor import fit_to_canvas, load_image, to_webp
from conftest import run
from fakes import FakeBot, FakeClient
from helpers import cutout_png


def bad_request(msg):
    return TelegramBadRequest(method=MagicMock(), message=msg)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr(sm.asyncio, "sleep", fast)


def setup(env, **over):
    env = {**env, "MAX_PACK_STICKERS": "3", "MAX_PACKS_FREE": "5", **over}
    settings = Settings.from_env(env)
    db = Database("m", "t", client=FakeClient())
    run(db.connect())
    run(db.upsert_user(42, "u", "U"))
    return settings, db, sm.StickerManager(settings, db, "TestStickerBot")


_counter = {"n": 0}


def sticker_bytes():
    """A visually distinct webp each call so de-duplication doesn't trigger."""
    _counter["n"] += 1
    img = load_image(cutout_png(box=(10 + _counter["n"], 10, 150, 100)), 10**7)
    return to_webp(fit_to_canvas(img, 20))


def add(mgr, category="cats", webp=None, emojis=None):
    return run(mgr.save_draft(42, webp or sticker_bytes(), category, emojis))


def test_pack_names_follow_telegram_rules():
    for cat in ("food", "3d art", "Pink Aesthetic", "baby_animals", "___", "ß"):
        name = sm.make_pack_name(cat, 1234567890, "My_Long_Bot_Username_Bot")
        assert sm.PACK_NAME_RE.match(name) and len(name) <= 64
        assert name.endswith("_by_my_long_bot_username_bot") and "__" not in name
    assert sm.make_pack_name("x", 1, "b").startswith("x_1_")
    assert len(sm.make_pack_name("a" * 99, 10**10, "a" * 32)) <= 64


def test_queue_deduplicates_and_respects_capacity(env):
    _, db, mgr = setup(env)
    data = sticker_bytes()
    a, b = add(mgr, webp=data), add(mgr, webp=data)             # identical bytes
    assert run(mgr.queue(42, a["_id"])).status == "queued"
    assert run(mgr.queue(42, a["_id"])).status == "duplicate"   # same sticker twice
    assert run(mgr.queue(42, b["_id"])).status == "duplicate"   # same image, different draft
    for _ in range(2):
        assert run(mgr.queue(42, add(mgr)["_id"])).status == "queued"
    assert run(mgr.queue(42, add(mgr)["_id"])).status == "full"   # cap = 3
    assert run(mgr.queue(7, a["_id"])).status == "missing"        # someone else's sticker


def test_publish_creates_packs_and_rolls_over_at_limit(env):
    _, db, mgr = setup(env)
    for _ in range(5):
        run(mgr.queue(42, add(mgr)["_id"])) if run(db.count_queued(42)) < 3 else None
    # cap is 3 -> publish 3, queue 2 more, publish again to exercise rollover
    bot = FakeBot()
    res = run(mgr.publish(bot, 42))
    assert res.published == 3 and not res.error and res.remaining == 0
    assert len(bot.created) == 1 and len(bot.added) == 2
    created = bot.created[0]
    assert created.title == "Cats : @Advik_prakashh"
    assert sm.PACK_NAME_RE.match(created.name) and created.name.endswith("_by_teststickerbot")
    first = created.stickers[0]
    assert first.format == "static" and first.emoji_list == ["🐱"]
    assert res.packs[0].link == f"https://t.me/addstickers/{created.name}"

    for _ in range(2):
        run(mgr.queue(42, add(mgr)["_id"]))
    res2 = run(mgr.publish(bot, 42))
    assert res2.published == 2 and len(bot.created) == 2          # pack 1 was full -> new pack
    assert bot.created[1].title == "Cats Vibes : @Advik_prakashh"
    assert run(db.count_user_packs(42)) == 2 and run(db.count_queued(42)) == 0


def test_publish_adds_to_existing_pack_and_groups_categories(env):
    _, db, mgr = setup(env, MAX_PACK_STICKERS="10")
    bot = FakeBot()
    run(mgr.queue(42, add(mgr, "food")["_id"]))
    run(mgr.queue(42, add(mgr, "anime")["_id"]))
    res = run(mgr.publish(bot, 42))
    assert {c.title for c in bot.created} == {"Food : @Advik_prakashh", "Anime : @Advik_prakashh"}
    run(mgr.queue(42, add(mgr, "food")["_id"]))
    res = run(mgr.publish(bot, 42))
    assert len(bot.created) == 2 and len(bot.added) == 1 and res.published == 1


def test_floodwait_is_retried(env):
    _, db, mgr = setup(env)
    run(mgr.queue(42, add(mgr)["_id"]))
    bot = FakeBot()
    bot.fail = [TelegramRetryAfter(method=MagicMock(), message="Flood", retry_after=1)]
    res = run(mgr.publish(bot, 42))
    assert res.published == 1 and not res.error and len(bot.created) == 1


def test_name_collision_retries_with_new_name(env):
    _, db, mgr = setup(env)
    run(mgr.queue(42, add(mgr)["_id"]))
    bot = FakeBot()
    bot.fail = [bad_request("sticker set name is already occupied")]
    assert run(mgr.publish(bot, 42)).published == 1


def test_telegram_error_keeps_stickers_queued(env):
    _, db, mgr = setup(env)
    run(mgr.queue(42, add(mgr)["_id"]))
    bot = FakeBot()
    bot.fail = [bad_request("STICKER_PNG_DIMENSIONS")]
    res = run(mgr.publish(bot, 42))
    assert res.published == 0 and "STICKER_PNG_DIMENSIONS" in res.error and res.remaining == 1
    bot.fail = [TelegramForbiddenError(method=MagicMock(), message="bot was blocked")]
    assert run(mgr.publish(bot, 42)).error == "forbidden"
    assert run(mgr.publish(bot, 42)).published == 1          # retry succeeds, nothing lost


def test_deleted_pack_is_recreated(env):
    _, db, mgr = setup(env, MAX_PACK_STICKERS="10")
    bot = FakeBot()
    run(mgr.queue(42, add(mgr)["_id"]))
    run(mgr.publish(bot, 42))
    run(mgr.queue(42, add(mgr)["_id"]))
    bot.fail = [bad_request("STICKERSET_INVALID")]
    res = run(mgr.publish(bot, 42))
    assert res.published == 1 and len(bot.created) == 2


def test_pack_limit_stops_publishing(env):
    _, db, mgr = setup(env, MAX_PACKS_FREE="1")
    run(mgr.queue(42, add(mgr, "food")["_id"]))
    run(mgr.queue(42, add(mgr, "anime")["_id"]))
    res = run(mgr.publish(FakeBot(), 42))
    assert res.error == "pack_limit" and res.published == 1 and res.remaining == 1
    res = run(mgr.publish(FakeBot(), 42, premium=True))
    assert res.published == 1 and not res.error


def test_set_category_updates_default_emoji(env):
    _, db, mgr = setup(env)
    doc = add(mgr, "cats")
    assert run(mgr.set_category(42, doc["_id"], "food")) is True
    updated = run(db.get_sticker(doc["_id"]))
    assert updated["category"] == "food" and updated["emojis"] == ["🍔"]
    assert run(mgr.set_category(99, doc["_id"], "food")) is False


def test_category_pack_is_shared_and_owned_by_owner(env):
    _, db, mgr = setup(env, MAX_PACK_STICKERS="2")
    bot = FakeBot()
    for _ in range(3):
        run(mgr.add_to_category_pack(bot, add(mgr, "flowers"), 111))
    assert len(bot.created) == 2 and len(bot.added) == 1
    assert all(c.user_id == 111 for c in bot.created)


def test_files_are_persisted_to_disk(env):
    _, db, mgr = setup(env)
    doc = add(mgr)
    assert run(mgr.read_file(doc))[:4] == b"RIFF"


class ChannelBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.posts = []

    async def send_sticker(self, chat_id, sticker, reply_markup=None):
        self.posts.append((chat_id, reply_markup))


def test_auto_publish_owner_packs_roll_over_and_post_one_sticker_per_pack_to_channel(env):
    _, db, mgr = setup(env, MAX_PACK_STICKERS="3")
    bot = ChannelBot()
    results = [run(mgr.auto_publish(bot, add(mgr, "food"), 111, "@mychannel")) for _ in range(4)]
    assert [r.created for r in results] == [True, False, False, True]
    assert [c.user_id for c in bot.created] == [111, 111]                       # owned by the owner
    assert [c.title for c in bot.created] == ["Food : @Advik_prakashh", "Food Vibes : @Advik_prakashh"]
    assert len(bot.posts) == 2 and bot.posts[0][0] == "@mychannel"               # one post per pack
    assert run(db.get_pack(bot.created[0].name))["channel_posted"] is True


def test_auto_publish_skips_duplicates_and_backfills_channel(env):
    _, db, mgr = setup(env)
    bot = ChannelBot()
    data = sticker_bytes()
    run(mgr.auto_publish(bot, add(mgr, "cats", data), 111))             # no channel yet
    again = run(mgr.auto_publish(bot, add(mgr, "cats", data), 111))
    assert again.duplicate and len(bot.created) == 1 and not bot.posts
    assert run(mgr.post_unposted(bot, "-1001234")) == (1, 0)
    assert bot.posts[0][0] == -1001234 and run(mgr.post_unposted(bot, "-1001234")) == (0, 0)


def test_clone_pack_title_format_owner_and_formats(env):
    _, db, mgr = setup(env, MAX_PACK_STICKERS="3")
    bot = ChannelBot()
    bot.set_size = 5
    res = run(mgr.clone_set(bot, "anim", 111, "@chan"))
    assert res.count == 5 and not res.already and [p.added for p in res.packs] == [3, 2]
    assert [c.title for c in bot.created] == ["Food : @Advik_prakashh", "Food Vibes : @Advik_prakashh"]
    assert all(c.user_id == 111 for c in bot.created)
    assert bot.created[0].stickers[0].sticker == "fid0" and bot.created[0].stickers[0].format == "animated"
    assert len(bot.added) == 3 and len(bot.posts) == 2
    again = run(mgr.clone_set(bot, "anim", 111))
    assert again.already and len(bot.created) == 2          # never cloned twice


def test_clone_title_is_capped():
    from bot.services.category_detector import clone_title
    assert clone_title("food", "@advik_prakashh") == "food : @advik_prakashh"
    assert len(clone_title("x" * 200, "@Advik_prakashh", 2)) <= 64


def test_parse_pack_name():
    pytest.importorskip("aiogram.filters")
    from bot.handlers.clone import parse_pack_name
    assert parse_pack_name("https://t.me/addstickers/Cute_Cats_by_bot") == "Cute_Cats_by_bot"
    assert parse_pack_name("Cute_Cats") == "Cute_Cats" and parse_pack_name("not a name!") is None
