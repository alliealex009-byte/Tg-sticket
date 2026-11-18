"""Logging with secret redaction (bot tokens, OpenAI keys, Mongo URLs, API keys in URLs)."""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),            # Telegram bot token
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),                    # OpenAI key
    re.compile(r"mongodb(?:\+srv)?://[^\s'\"<>]+"),             # Mongo URL (may hold password)
    re.compile(r"([?&](?:key|api_key|apikey|token)=)[^&\s'\"]+", re.I),
]


def redact(text: str) -> str:
    text = _PATTERNS[0].sub("<bot-token>", text)
    text = _PATTERNS[1].sub("<api-key>", text)
    text = _PATTERNS[2].sub("<mongo-url>", text)
    text = _PATTERNS[3].sub(r"\1<redacted>", text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO", log_dir: str | None = None) -> None:
    fmt = RedactingFormatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if log_dir:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(Path(log_dir) / "bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:
            root.warning("Could not open log directory %s; logging to stdout only", log_dir)
    # Third-party libraries can log full request URLs; keep them quiet.
    for noisy in ("httpx", "httpcore", "PIL", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
