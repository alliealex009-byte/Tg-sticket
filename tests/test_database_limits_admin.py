import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from bot.config import Settings
from bot.database import Database, is_premium_doc
from bot.limits import effective_limit
from bot.utils.helpers import RateLimiter, TaskGate, TTLCache, retry_async, utcnow
from conftest import requires_aiogram, run
from fakes import FakeClient


def make_db():
    db = Database("mongodb://x", "test", client=FakeClient())
    run(db.connect())
    return db


def test_database_connection_pings_creates_indexes_and_loads_bans():
    client = FakeClient()
    client["test"]["banned_users"].docs[5] = {"_id": 5}
    db = Database("mongodb://x", "test", client=client)
    run(db.connect())
    client.admin.command.assert_awaited_with("ping")
    assert db.banned == {5} and client["test"]["sticker_packs"].indexes
    assert run(db.ping()) is True
    client.admin.command = AsyncMock(side_effect=RuntimeError("down"))
    assert run(db.ping()) is False


def test_upsert_user_is_idempotent():
    db = make_db()
    a = run(db.upsert_user(1, "ann", "Ann"))
    b = run(db.upsert_user(1, "ann2", "Ann"))
    assert a["created_at"] == b["created_at"] and b["username"] == "ann2"
    assert a["stats"]["stickers"] == 0 and a["settings"]["bg_mode"] == "auto"
    assert run(db.count_users()) == 1


def test_daily_quota_enforced_atomically_and_refunded():
    db = make_db()
    run(db.upsert_user(1, "a", "A"))
    assert [run(db.consume(1, "gen", 3)) for _ in range(4)] == [True, True, True, False]
    run(db.refund(1, "gen"))
    assert run(db.consume(1, "gen", 3)) is True
    assert run(db.consume(1, "search", None)) is True          # unlimited (admin)
    with pytest.raises(ValueError):
        run(db.consume(1, "bogus", 3))


def test_daily_quota_resets_on_new_day():
    db = make_db()
    run(db.upsert_user(1, "a", "A"))
    for _ in range(2):
        run(db.consume(1, "gen", 2))
    assert run(db.consume(1, "gen", 2)) is False
    db.db["users"].docs[1]["daily"]["date"] = "2000-01-01"
    assert run(db.consume(1, "gen", 2)) is True


def test_limits_free_premium_admin_and_override():
    s = Settings.from_env({"BOT_TOKEN": "1:x", "MONGO_URL": "m", "MAX_DAILY_GENERATIONS": "10", "PREMIUM_MULTIPLIER": "4"})
    assert effective_limit(s, {}, "gen", False, False) == 10
    assert effective_limit(s, {}, "gen", True, False) == 40
    assert effective_limit(s, {}, "gen", False, True) is None
    assert effective_limit(s, {"gen": 2}, "gen", True, False) == 8


def test_premium_grant_expiry_and_revoke():
    db = make_db()
    run(db.upsert_user(1, "a", "A"))
    assert not is_premium_doc(run(db.get_user(1)))
    run(db.set_premium(1, 30, by=9))
    assert is_premium_doc(run(db.get_user(1))) and run(db.count_premium()) == 1
    assert not is_premium_doc({"premium": True, "premium_until": utcnow() - timedelta(days=1)})
    assert is_premium_doc({"premium": True, "premium_until": None})
    run(db.revoke_premium(1))
    assert not is_premium_doc(run(db.get_user(1))) and not db.db["premium_users"].docs


def test_ban_unban_and_broadcast_skips_banned():
    db = make_db()
    for uid in (1, 2, 3):
        run(db.upsert_user(uid, "", ""))
    run(db.ban(2, "spam", by=1))
    assert db.is_banned(2)

    async def ids():
        return [i async for i in db.iter_user_ids()]
    assert sorted(run(ids())) == [1, 3]
    assert run(db.unban(2)) is True and not db.is_banned(2) and run(db.unban(2)) is False


def test_statistics_and_settings():
    db = make_db()
    run(db.inc_stat("searches"))
    run(db.inc_stat("searches", 2))
    assert run(db.get_stats())["searches"] == 3
    run(db.set_setting("maintenance", True))
    assert run(db.get_setting("maintenance")) is True and run(db.get_setting("nope", 7)) == 7


def test_search_history_roundtrip():
    db = make_db()
    sid = run(db.save_search(1, "cats", [{"a": 1}]))
    assert run(db.get_search(sid))["results"] == [{"a": 1}]


# ----------------------------------------------------------------------------- utilities
def test_task_gate_limits_concurrency_and_prioritises_premium():
    order, peak = [], {"now": 0, "max": 0}
    gate = TaskGate(1)

    async def job(name, prio):
        async with gate.slot(prio):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            order.append(name)
            await asyncio.sleep(0.01)
            peak["now"] -= 1

    async def main():
        first = asyncio.create_task(job("first", 1))
        await asyncio.sleep(0)
        waiting = [asyncio.create_task(job("free", 1)), asyncio.create_task(job("premium", 0))]
        await asyncio.gather(first, *waiting)

    run(main())
    assert order == ["first", "premium", "free"] and peak["max"] == 1 and gate.active == 0


def test_task_gate_cancelled_waiter_does_not_leak_slot():
    gate = TaskGate(1)

    async def main():
        async def hold():
            async with gate.slot():
                await asyncio.sleep(0.05)
        t1 = asyncio.create_task(hold())
        await asyncio.sleep(0)
        t2 = asyncio.create_task(hold())
        await asyncio.sleep(0)
        t2.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)
        async with gate.slot():
            pass
    run(main())
    assert gate.active == 0


def test_rate_limiter_token_bucket():
    rl = RateLimiter(rate=1, burst=3)
    assert [rl.allow(1, now=0) for _ in range(4)] == [True, True, True, False]
    assert rl.allow(1, now=2.0) and rl.allow(2, now=0)


def test_ttl_cache_expiry_and_bound():
    c = TTLCache(ttl=0.01, maxsize=2)
    c.set("a", 1)
    assert c.get("a") == 1
    import time
    time.sleep(0.02)
    assert c.get("a") is None
    c2 = TTLCache(ttl=10, maxsize=2)
    for k in "abc":
        c2.set(k, k)
    assert len(c2) == 2 and c2.get("a") is None


def test_retry_async_retries_then_succeeds_or_raises():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("slow")
        return "ok"
    assert run(retry_async(flaky, attempts=3, base_delay=0)) == "ok"

    async def always():
        raise TimeoutError("x")
    with pytest.raises(TimeoutError):
        run(retry_async(always, attempts=2, base_delay=0))


# -------------------------------------------------------------------- admin authorisation
@requires_aiogram
def test_admin_filter_allows_only_configured_admins(settings):
    from types import SimpleNamespace
    from bot.context import AppContext
    from bot.handlers.admin import AdminFilter

    ctx = AppContext(settings=settings, db=None, downloader=None, search=None, remover=None, ai=None,
                     builder=None, gate=TaskGate(1))
    f = AdminFilter()
    assert run(f(SimpleNamespace(from_user=SimpleNamespace(id=111)), ctx)) is True
    assert run(f(SimpleNamespace(from_user=SimpleNamespace(id=999)), ctx)) is False
    assert run(f(SimpleNamespace(from_user=None), ctx)) is False


def test_ctx_is_admin_and_limits(settings):
    from bot.context import AppContext
    ctx = AppContext(settings=settings, db=None, downloader=None, search=None, remover=None, ai=None,
                     builder=None, gate=TaskGate(1))
    assert ctx.is_admin(111) and not ctx.is_admin(5)
    assert ctx.limit("gen", False, 111) is None and ctx.limit("gen", False, 5) == 15
    assert ctx.limit("gen", True, 5) == 75
