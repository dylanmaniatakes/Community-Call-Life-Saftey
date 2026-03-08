"""
Relay / dome-light control
Supports two controller types:
  rcm  — RCM relay boards accessed via Telnet (port 23)
  esp  — ESP8266/ESP32-based relay boards accessed via HTTP
Call activate_relay() / deactivate_relay() from asyncio.to_thread().
"""

from __future__ import annotations

import logging
import telnetlib
import urllib.request

log = logging.getLogger("relay")

TIMEOUT = 5


# ---------------------------------------------------------------------------
# RCM — Telnet protocol
# Command format: S<nn>%  to set (activate), C<nn>%  to clear (deactivate)
# ---------------------------------------------------------------------------

def _rcm_command(host: str, port: int, command: str) -> None:
    with telnetlib.Telnet(host, port, timeout=TIMEOUT) as tn:
        tn.write(command.encode("ascii") + b"\r\n")
        tn.read_until(b"%", timeout=2)


def _rcm_activate(host: str, port: int, relay_number: int) -> None:
    _rcm_command(host, port, f"S{relay_number:02d}%")


def _rcm_deactivate(host: str, port: int, relay_number: int) -> None:
    _rcm_command(host, port, f"C{relay_number:02d}%")


# ---------------------------------------------------------------------------
# ESP — ESPHome native web_server REST API (HTTP POST)
# ESPHome exposes switches at:
#   POST http://<host>:<port>/switch/relay-<n>/turn_on
#   POST http://<host>:<port>/switch/relay-<n>/turn_off
# Switch names must match the ESPHome config (e.g. name: "relay-1").
# ---------------------------------------------------------------------------

_ESP_ON_URL  = "http://{host}:{port}/switch/relay-{relay}/turn_on"
_ESP_OFF_URL = "http://{host}:{port}/switch/relay-{relay}/turn_off"


def _esp_activate(host: str, port: int, relay_number: int) -> None:
    url = _ESP_ON_URL.format(host=host, port=port, relay=relay_number)
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        log.debug("ESP response: %s", resp.read(256))


def _esp_deactivate(host: str, port: int, relay_number: int) -> None:
    url = _ESP_OFF_URL.format(host=host, port=port, relay=relay_number)
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        log.debug("ESP response: %s", resp.read(256))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def activate_relay(host: str, port: int, relay_type: str, relay_number: int) -> None:
    """
    Activate (energise) a relay.
    Synchronous — call via asyncio.to_thread() from async contexts.
    """
    rt = relay_type.lower()
    log.info("Activating relay #%d on %s (%s:%s)", relay_number, rt, host, port)
    if rt == "rcm":
        _rcm_activate(host, port, relay_number)
    elif rt == "esp":
        _esp_activate(host, port, relay_number)
    else:
        raise ValueError(f"Unknown relay type: {relay_type}")


def deactivate_relay(host: str, port: int, relay_type: str, relay_number: int) -> None:
    """Deactivate (de-energise) a relay."""
    rt = relay_type.lower()
    log.info("Deactivating relay #%d on %s (%s:%s)", relay_number, rt, host, port)
    if rt == "rcm":
        _rcm_deactivate(host, port, relay_number)
    elif rt == "esp":
        _esp_deactivate(host, port, relay_number)
    else:
        raise ValueError(f"Unknown relay type: {relay_type}")
