"""
Pydantic v2 models used by the API routes.
"""

from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

class DeviceCreate(BaseModel):
    device_id: str
    name: str
    location: Optional[str] = None
    device_type: str = "pendant"
    priority: str = "normal"
    vendor_type: str = "innovonics"   # "innovonics" | "arial_legacy" | "arial_900"
    apartment_id: Optional[int] = None
    relay_config_id: Optional[int] = None
    relay_number: Optional[int] = None
    aux_label: Optional[str] = None
    area_id: Optional[int] = None


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    location: Optional[str] = None
    device_type: Optional[str] = None
    priority: Optional[str] = None
    active: Optional[int] = None
    vendor_type: Optional[str] = None
    apartment_id: Optional[int] = None
    relay_config_id: Optional[int] = None
    relay_number: Optional[int] = None
    aux_label: Optional[str] = None
    area_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------

class CallAck(BaseModel):
    actor: str = "staff"
    notes: Optional[str] = None


class CallClear(BaseModel):
    actor: str = "staff"
    notes: Optional[str] = None


# Manual / simulated call injection
class CallInject(BaseModel):
    device_id: str
    raw_data: Optional[str] = None


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------

class SmtpConfig(BaseModel):
    server: str
    port: int = 587
    email: str
    password: str
    encryption: str = "STARTTLS"
    enabled: int = 1


class SmtpTest(BaseModel):
    to: str


# ---------------------------------------------------------------------------
# Pager
# ---------------------------------------------------------------------------

class PagerCreate(BaseModel):
    name: str
    host: str
    port: int
    protocol: str = "TAP"
    default_capcode: Optional[str] = None
    enabled: int = 1


class PagerUpdate(PagerCreate):
    pass


class PagerTest(BaseModel):
    capcode: str
    message: str = "TEST - Nurse Call System"


# ---------------------------------------------------------------------------
# Relay / dome-light
# ---------------------------------------------------------------------------

class RelayCreate(BaseModel):
    name: str
    relay_type: str          # "rcm" or "esp"
    host: str
    port: int = 23           # Telnet for RCM; HTTP port for ESP
    relay_number: int = 1
    enabled: int = 1


class RelayUpdate(RelayCreate):
    pass


class RelayTest(BaseModel):
    duration_seconds: int = 3


# ---------------------------------------------------------------------------
# ESP / external inputs
# ---------------------------------------------------------------------------

class InputCreate(BaseModel):
    name: str
    input_type: str = "esp"        # currently esp
    host: str
    port: int = 80
    input_number: int = 1
    input_name: Optional[str] = None
    device_id: Optional[str] = None  # optional legacy mapping to a registered device
    active_high: int = 1           # 1 => state=1/ON triggers alarm, 0 => inverted
    enabled: int = 1


class InputUpdate(InputCreate):
    pass


class InputEvent(BaseModel):
    # Any matching selector can be used:
    # - input_id
    # - host + input_number
    # - host + input_name
    input_id: Optional[int] = None
    host: Optional[str] = None
    port: Optional[int] = None
    input_number: Optional[int] = None
    input_name: Optional[str] = None
    state: Any
    raw_data: Optional[str] = None


class InputTest(BaseModel):
    duration_seconds: int = 3


class InputBatch(BaseModel):
    """Create multiple input configs at once — one entry per input channel."""
    name_prefix: str         # e.g. "Panic Alarm"
    host: str
    port: int = 80
    count: int               # number of inputs
    start_number: int = 1    # starting input_number
    input_name_prefix: Optional[str] = "input-"
    active_high: int = 1
    enabled: int = 1


class RelayBatch(BaseModel):
    """Create multiple relay configs at once — one entry per relay channel."""
    name_prefix: str          # e.g. "Floor 1 Controller"
    relay_type: str           # "rcm" or "esp"
    host: str
    port: int = 23
    count: int                # number of relay channels
    enabled: int = 1


# ---------------------------------------------------------------------------
# Innovonics coordinator
# ---------------------------------------------------------------------------

class InnoConfig(BaseModel):
    mode: str = "tcp"        # "tcp" or "serial"
    host: Optional[str] = None
    port: int = 3000
    serial_port: Optional[str] = None
    baud_rate: int = 9600
    nid: int = 16            # Network ID (1-31)
    enabled: int = 0


# ---------------------------------------------------------------------------
# Repeaters
# ---------------------------------------------------------------------------

class RepeaterCreate(BaseModel):
    serial_number: str
    name: Optional[str] = None


class RepeaterUpdate(BaseModel):
    name: Optional[str] = None


# ---------------------------------------------------------------------------
# Notification rules
# ---------------------------------------------------------------------------

class RuleCreate(BaseModel):
    name: str
    device_filter: str = "all"
    priority_filter: str = "all"
    area_filter: str = "all"
    notify_on: str = "call"   # "call" | "clear" | "both"
    action_type: str          # "email" | "page" | "relay" | "telegram" | "twilio"
    action_config: str        # JSON string
    enabled: int = 1


class RuleUpdate(RuleCreate):
    pass


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class TelegramConfig(BaseModel):
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    enabled: int = 0


class TelegramTest(BaseModel):
    chat_id: Optional[str] = None   # override default if provided


# ---------------------------------------------------------------------------
# Twilio
# ---------------------------------------------------------------------------

class TwilioConfig(BaseModel):
    account_sid: Optional[str] = None
    auth_token: Optional[str] = None
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    enabled: int = 0


class TwilioTest(BaseModel):
    to_number: Optional[str] = None  # override default if provided
