"""
Twilio SMS notification module.
Uses the Twilio REST API via stdlib urllib — no extra dependencies required.
Call send_sms() from asyncio.to_thread() — urllib is blocking.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger("twilio_notifier")


def send_sms(cfg: dict, to_number: str, message: str) -> None:
    """
    Send an SMS via Twilio.  cfg is a dict from the twilio_config DB row.
    Raises on HTTP / API error.
    """
    sid         = cfg.get("account_sid", "").strip()
    token       = cfg.get("auth_token",  "").strip()
    from_number = cfg.get("from_number", "").strip()

    if not sid or not token or not from_number:
        raise ValueError("Twilio account_sid, auth_token, and from_number must all be configured")

    url  = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({
        "To":   to_number,
        "From": from_number,
        "Body": message,
    }).encode("utf-8")

    credentials = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    log.info("Sending Twilio SMS from %s → %s", from_number, to_number)
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode())

    # Twilio returns 201 Created on success; urlopen raises HTTPError on non-2xx.
    # Still surface the SID so it appears in logs.
    log.info("Twilio SMS queued: SID=%s status=%s", result.get("sid"), result.get("status"))
