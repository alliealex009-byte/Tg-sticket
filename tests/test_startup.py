"""Needs the real aiogram (skipped automatically when it is not installed)."""

import pytest

from bot.config import ConfigError, Settings
from conftest import requires_aiogram, run


@requires_aiogram
def test_dispatcher_builds_with_all_routers(settings):
    from bot.main import build_context, build_dispatcher

    ctx = build_context(settings)
    dp = build_dispatcher(ctx)
    assert dp.workflow_data["ctx"] is ctx
    names = {r.name for r in dp.sub_routers}
    assert {"admin", "start", "search", "ai_sticker", "photo", "packs", "profile", "text_sticker", "fallback"} <= names
    assert {"message", "callback_query"} <= set(dp.resolve_used_update_types())


@requires_aiogram
def test_keyboards_respect_callback_data_limit():
    from bot import keyboards as kb

    markups = [kb.main_menu(), kb.search_categories(), kb.category_picker("a" * 16), kb.style_picker(False, "cute"),
               kb.search_nav("a" * 12, 19, 20, "https://x.io/p"), kb.sticker_actions("a" * 16, "cats", False, True),
               kb.photo_options({}), kb.text_presets(), kb.settings_menu({}), kb.default_style_picker(),
               kb.admin_panel(False), kb.pack_preview()]
    for m in markups:
        for row in m.inline_keyboard:
            for b in row:
                assert b.callback_data is None or len(b.callback_data.encode()) <= 64, b.callback_data


@requires_aiogram
def test_main_exits_cleanly_without_credentials(monkeypatch, capsys):
    from bot import main as bot_main

    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("MONGO_URL", raising=False)
    assert run(bot_main.run()) == 2
    assert "BOT_TOKEN" in capsys.readouterr().err


def test_missing_credentials_raise_config_error():
    with pytest.raises(ConfigError):
        Settings.from_env({"BOT_TOKEN": "x"})
