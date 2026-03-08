"""
Paging sender — TAP / COMP2 / WaveWare
Adapted from paging-ui.py for headless / async use.
Call send_page() from a thread (asyncio.to_thread) since socket I/O is blocking.
"""

from __future__ import annotations

import logging
import select
import socket

log = logging.getLogger("paging")

TIMEOUT = 5  # seconds


def _recv(s: socket.socket) -> str:
    try:
        readable, _, _ = select.select([s], [], [], TIMEOUT)
        if readable:
            return s.recv(4096).decode(errors="ignore")
    except Exception:
        pass
    return ""


def _send(s: socket.socket, data: str) -> None:
    s.sendall(data.encode("utf-8"))


# ---------------------------------------------------------------------------
# Protocol implementations
# ---------------------------------------------------------------------------

def _tap(host: str, port: int, capcode: str, message: str) -> None:
    """TAP (Telocator Alphanumeric Protocol) via TCP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    s.connect((host, port))
    try:
        _send(s, "\r")
        _recv(s)
        _send(s, "\x1BPG1\r")
        _recv(s)

        body = f"{capcode}\r{message}\r"
        checksum = sum(ord(c) for c in body) + 5
        hexnum = hex(checksum)
        chksum = "".join(chr(int(x, 16) + 48) for x in hexnum[-3:])
        msg = f"\x02{capcode}\r{message}\r\x03{chksum}\r"
        _send(s, msg)
        resp = _recv(s)
        if "211" not in resp:
            log.warning("TAP unexpected response: %r", resp)

        _send(s, "\x04\r")
        _recv(s)
    finally:
        s.close()


def _comp2(host: str, port: int, capcode: str, message: str) -> None:
    """COMP2 (WaveNet) protocol via TCP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    s.connect((host, port))
    try:
        _recv(s)
        payload = f"\x01A5{capcode}\x02{message}\x03\x04"
        _send(s, payload)
        _recv(s)
    finally:
        s.close()


def _waveware(host: str, port: int, capcode: str, message: str) -> None:
    """WaveWare tilde-delimited protocol via TCP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    s.connect((host, port))
    try:
        payload = f"{capcode}~{message}~"
        _send(s, payload + "\r")
        _recv(s)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def send_page(host: str, port: int, protocol: str, capcode: str, message: str) -> None:
    """
    Synchronous — run via asyncio.to_thread() from async callers.
    Raises on connection failure so the caller can log/handle.
    """
    proto = protocol.upper()
    log.info("Sending %s page to %s:%s capcode=%s", proto, host, port, capcode)
    if proto == "TAP":
        _tap(host, port, capcode, message)
    elif proto == "COMP2":
        _comp2(host, port, capcode, message)
    elif proto == "WAVEWARE":
        _waveware(host, port, capcode, message)
    else:
        raise ValueError(f"Unknown paging protocol: {protocol}")
    log.info("Page sent successfully.")
