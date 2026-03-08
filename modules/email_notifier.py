"""
SMTP email notification module.
Adapted from scheduler-gui.py for headless use.
Call send_email() from asyncio.to_thread() — smtplib is blocking.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("email_notifier")


def send_email(
    smtp_cfg: dict,
    recipients: str,
    subject: str,
    body: str,
    attachment_path: str | None = None,
) -> None:
    """
    Synchronous email send.  smtp_cfg is a dict from the smtp_config DB row.
    recipients may be a comma-separated string.
    Raises on connection/auth failure.
    """
    server     = smtp_cfg["server"]
    port       = int(smtp_cfg["port"])
    sender     = smtp_cfg["email"]
    password   = smtp_cfg["password"]
    encryption = smtp_cfg.get("encryption", "STARTTLS").upper()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipients
    msg.set_content(body, subtype="plain", charset="utf-8")

    if attachment_path and os.path.isfile(attachment_path):
        import mimetypes
        with open(attachment_path, "rb") as f:
            data = f.read()
        mime, _ = mimetypes.guess_type(attachment_path)
        if not mime:
            mime = "application/octet-stream"
        main, sub = mime.split("/", 1)
        msg.add_attachment(data, maintype=main, subtype=sub,
                           filename=os.path.basename(attachment_path))

    to_addrs = [r.strip() for r in recipients.split(",") if r.strip()]

    log.info("Sending email '%s' → %s via %s:%s [%s]",
             subject, to_addrs, server, port, encryption)

    if encryption == "SSL":
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(server, port, context=ctx) as s:
            s.login(sender, password)
            s.send_message(msg, from_addr=sender, to_addrs=to_addrs)
    elif encryption == "STARTTLS":
        ctx = ssl.create_default_context()
        with smtplib.SMTP(server, port) as s:
            s.starttls(context=ctx)
            s.login(sender, password)
            s.send_message(msg, from_addr=sender, to_addrs=to_addrs)
    else:
        with smtplib.SMTP(server, port) as s:
            if password:
                s.login(sender, password)
            s.send_message(msg, from_addr=sender, to_addrs=to_addrs)

    log.info("Email sent successfully.")
