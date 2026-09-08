"""Port decoding using firmware protocol numbers (ported from main's v3)."""

from __future__ import annotations

MIJIA_PROTOCOLS = {
    0: "idle",
    1: "5V",
    2: "5V",
    3: "QC",
    4: "AFC",
    5: "FCP",
    6: "SCP",
    7: "PD",
    8: "PPS",
    9: "PPS",
    10: "UFCS",
}


def protocol_name(piid: int, voltage: float, hw_protocol: int | None) -> str:
    """Prefer PIID 17/18; voltage is only a coarse cold-start fallback."""
    if hw_protocol is not None:
        code = {0x60: 1, 0x70: 3, 0x80: 7}.get(hw_protocol, hw_protocol)
        return MIJIA_PROTOCOLS.get(code, "Unknown")
    if piid == 4:
        return "QC" if voltage > 5.5 else "5V"
    if piid == 3:
        return "PD" if voltage >= 15 else "QC" if voltage >= 8.5 else "5V"
    if voltage <= 5.5:
        return "5V"
    if any(abs(voltage - fixed) < 0.25 for fixed in (5, 9, 12, 15, 20)):
        return "PD"
    return "PPS"


def decode_port(piid: int, payload: bytes, hw_protocol: int | None = None) -> dict | None:
    """Decode the four-byte port value; its code byte is not a protocol ID."""
    if len(payload) < 12:
        return None
    status, _code, current_raw, voltage_raw = payload[-4:]
    voltage, current = voltage_raw / 10, current_raw / 10
    active = bool(status or voltage or current)
    return {
        "voltage": voltage,
        "current": current,
        "power": round(voltage * current, 1),
        "active": active,
        "status_raw": status,
        "protocol": protocol_name(piid, voltage, hw_protocol) if active else "idle",
    }
