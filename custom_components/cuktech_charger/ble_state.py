"""State owned by the direct Bluetooth runtime on HA's event loop."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .ble_protocol import PORT_NAMES
from .ble_state_protocol_v2 import decode_port, protocol_name
from .protocol_codec import decode_protocol_switches


@dataclass(slots=True)
class PortState:
    voltage: float = 0.0
    current: float = 0.0
    power: float = 0.0
    active: bool = False
    protocol: str = "idle"
    status_raw: int = 0


class ChargerState:
    """Keep settings and telemetry from one authenticated session."""

    def __init__(self) -> None:
        self.connected = False
        self.authenticated = False
        self.ports = {piid: PortState() for piid in PORT_NAMES}
        self.settings: dict[str, int] = {}
        self.hw_protocols: dict[int, int] = {}
        self.model = ""
        self.firmware = ""

    @property
    def protocol_switches(self) -> dict[str, dict[str, bool]]:
        return decode_protocol_switches(self.settings.get("21"))

    def update_setting(self, piid: int, value: int) -> None:
        self.settings[str(piid)] = value
        if piid in (17, 18):
            # u32 MSB->LSB: [first protocol][first power][second protocol][second power]
            first = 1 if piid == 17 else 3
            for port, protocol in ((first, (value >> 24) & 0xFF), (first + 1, (value >> 8) & 0xFF)):
                self.hw_protocols[port] = protocol
                state = self.ports[port]
                state.protocol = (
                    protocol_name(port, state.voltage, protocol) if state.active else "idle"
                )
        elif piid == 16:
            for port in PORT_NAMES:
                if not value & (1 << (port - 1)):
                    self.ports[port] = PortState()

    def update_port(self, piid: int, payload: bytes) -> bool:
        decoded = decode_port(piid, payload, self.hw_protocols.get(piid))
        if decoded is None:
            return False
        state = PortState(**decoded)
        changed = state != self.ports[piid]
        self.ports[piid] = state
        return changed

    def snapshot(self) -> dict[str, Any]:
        port_control = self.settings.get("16")
        return {
            "connected": self.connected,
            "authenticated": self.authenticated,
            "ports": {
                name: {
                    **asdict(self.ports[piid]),
                    "enabled": None
                    if port_control is None
                    else bool(port_control & (1 << (piid - 1))),
                }
                for piid, name in PORT_NAMES.items()
            },
            "settings": dict(self.settings),
            "model": self.model,
            "firmware": self.firmware,
        }
