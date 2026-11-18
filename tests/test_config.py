import pytest

from bot.config import ConfigError, Settings


def test_required_vars_missing():
    with pytest.raises(ConfigError) as e:
        Settings.from_env({})
    assert "BOT_TOKEN" in str(e.value) and "MONGO_URL" in str(e.value)


def test_defaults_and_optional_openai(env):
    s = Settings.from_env(env)
    assert s.pack_credit == "@Advik_prakashh"
    assert not s.ai_enabled
    assert s.max_pack_stickers == 50 and s.owner_id == 111 and s.auto_pack and s.max_daily_generations == 15
    assert s.admin_ids == frozenset({111, 222})
    assert s.category_pack_owner_id == 111


def test_values_are_clamped_and_parsed(env):
    env.update(MAX_PACK_STICKERS="9999", MAX_CONCURRENT_TASKS="0", OPENAI_API_KEY=" sk-x ", ENABLE_PINTEREST="no")
    s = Settings.from_env(env)
    assert s.max_pack_stickers == 50 and s.max_concurrent_tasks == 1
    assert s.openai_api_key == "sk-x" and s.ai_enabled and s.enable_pinterest is False


def test_bad_numbers_raise(env):
    env["MAX_DAILY_SEARCHES"] = "lots"
    with pytest.raises(ConfigError):
        Settings.from_env(env)
    env["MAX_DAILY_SEARCHES"] = "3"
    env["ADMIN_IDS"] = "abc"
    with pytest.raises(ConfigError):
        Settings.from_env(env)
