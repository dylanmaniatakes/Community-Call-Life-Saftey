"""
Telegram Bot notification module.
Uses the Bot API via stdlib urllib — no extra dependencies required.
Call send_telegram() from asyncio.to_thread() — urllib is blocking.
"""

from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger("telegram_notifier")


def send_telegram(cfg: dict, chat_id: str, message: str) -> None:
    """
    Send a Telegram message.  cfg is a dict from the telegram_config DB row.
    Raises on HTTP / API error.
    """
    token = cfg.get("bot_token", "").strip()
    if not token:
        raise ValueError("Telegram bot_token is not configured")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text":    message,
        "parse_mode": "Markdown",
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    log.info("Sending Telegram message to chat_id=%s", chat_id)
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode())

    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result.get('description', result)}")

    log.info("Telegram message sent successfully.")
