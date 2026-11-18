"""Shared test setup.

If `aiogram` is not installed (e.g. a sandbox without network), lightweight stand-ins for the few
aiogram names used by the *service* layer are registered so the pure logic can still be tested.
When aiogram IS installed (normal dev machine / CI) the real library is used and the
`requires_aiogram` tests run too.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import aiogram  # noqa: F401
    AIOGRAM_STUBBED = False
except ImportError:  # pragma: no cover - exercised only without aiogram
    AIOGRAM_STUBBED = True
    pkg = types.ModuleType("aiogram")
    exc = types.ModuleType("aiogram.exceptions")
    typ = types.ModuleType("aiogram.types")

    class TelegramAPIError(Exception):
        def __init__(self, method=None, message=""):
            super().__init__(message)
            self.method, self.message = method, message

    class TelegramBadRequest(TelegramAPIError): ...
    class TelegramForbiddenError(TelegramAPIError): ...
    class TelegramNetworkError(TelegramAPIError): ...
    class TelegramServerError(TelegramAPIError): ...

    class TelegramRetryAfter(TelegramAPIError):
        def __init__(self, method=None, message="", retry_after=0):
            super().__init__(method, message)
            self.retry_after = retry_after

    for cls in (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
                TelegramServerError, TelegramRetryAfter):
        setattr(exc, cls.__name__, cls)

    class BufferedInputFile:
        def __init__(self, file, filename="file"):
            self.data, self.filename = file, filename

    class InputSticker:
        def __init__(self, sticker, format, emoji_list, **kw):
            self.sticker, self.format, self.emoji_list = sticker, format, emoji_list

    class InlineKeyboardButton:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class InlineKeyboardMarkup:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    typ.InlineKeyboardButton, typ.InlineKeyboardMarkup = InlineKeyboardButton, InlineKeyboardMarkup
    typ.BufferedInputFile, typ.InputSticker = BufferedInputFile, InputSticker
    pkg.exceptions, pkg.types = exc, typ
    sys.modules.update({"aiogram": pkg, "aiogram.exceptions": exc, "aiogram.types": typ})


requires_aiogram = pytest.mark.skipif(AIOGRAM_STUBBED, reason="aiogram is not installed")


def run(coro):
    """Run a coroutine to completion (keeps the suite free of pytest-asyncio)."""
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path):
    return {"BOT_TOKEN": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", "MONGO_URL": "mongodb://localhost:27017",
            "DATA_DIR": str(tmp_path / "data"), "LOG_DIR": str(tmp_path / "logs"), "ADMIN_IDS": "111,222"}


@pytest.fixture
def settings(env):
    from bot.config import Settings
    return Settings.from_env(env)
