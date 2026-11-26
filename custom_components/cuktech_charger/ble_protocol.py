"""CUKTECH MiOT BLE protocol constants and helpers."""

from __future__ import annotations

SIID_CHARGER = 2

UUID_FE95 = "0000fe95-0000-1000-8000-00805f9b34fb"
CHAR_DEVICE_INFO = "0000001c-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_CTRL = "00000010-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_DATA = "00000019-0000-1000-8000-00805f9b34fb"
CHAR_CMD_SEND = "0000001a-0000-1000-8000-00805f9b34fb"
CHAR_CMD_RECV = "0000001b-0000-1000-8000-00805f9b34fb"
CHAR_FW_VERSION = "00000004-0000-1000-8000-00805f9b34fb"

READABLE_SETTINGS_PIIDS = (5, 6, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19, 20, 21)
PORT_NAMES = {1: "c1", 2: "c2", 3: "c3", 4: "a"}
PORT_BITS = {"c1": 0, "c2": 1, "c3": 2, "a": 3}


def parse_property_value(payload: bytes, *, check_status: bool = True) -> int | None:
    """Decode Result TLV or compact property push; reject malformed/error frames."""
    if len(payload) < 12 or payload[4] not in (0x03, 0x04):
        return None
    # GET Result: status[9:11], length[11], type[12], value[13:].
    size = payload[11]
    if size in (1, 2, 4) and len(payload) == 13 + size:
        if check_status and (payload[9] or payload[10]):
            return None
        return int.from_bytes(payload[13:], "little")
    # Firmware pushes also use a compact header with the value at offset 12.
    if payload[4] == 0x04 and len(payload) in (13, 14, 16):
        return int.from_bytes(payload[12:], "little")
    return None


def parse_hex_secret(value: str | bytes, expected_length: int, name: str) -> bytes:
    """Parse and validate a fixed-length token/key."""
    if isinstance(value, bytes):
        raw = value
    else:
        try:
            raw = bytes.fromhex(value.strip())
        except ValueError as exc:
            raise ValueError(f"{name} must be a hex string") from exc
    if len(raw) != expected_length:
        raise ValueError(f"{name} must be {expected_length} bytes")
    return raw
