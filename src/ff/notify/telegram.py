"""Telegram alerts. The bot token comes from the environment (never from config
or the database): TELEGRAM_BOT_TOKEN, and optionally TELEGRAM_CHAT_ID."""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def bot_token() -> str | None:
    return os.environ.get("TELEGRAM_BOT_TOKEN") or None


def send(text: str, chat_id: str | None = None, timeout: float = 15.0) -> tuple[bool, str]:
    """Send a message. Returns (ok, detail); never raises, so a Telegram outage
    cannot break the trading loop."""
    token = bot_token()
    chat = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        return False, "TELEGRAM_BOT_TOKEN is not set"
    if not chat:
        return False, "no Telegram chat id (set it in Alert settings or TELEGRAM_CHAT_ID)"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=timeout,
        )
    except requests.RequestException as e:
        log.warning("telegram send failed: %s", e)
        return False, f"network error: {type(e).__name__}"
    if r.status_code != 200:
        return False, f"Telegram answered {r.status_code}: {r.text[:200]}"
    return True, "sent"
