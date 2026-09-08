"""CUKTECH Charger integration using Home Assistant Bluetooth."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, callback

from .ble_runtime import CuktechBLERuntime
from .const import CONF_BLE_KEY, CONF_MAC, CONF_TOKEN, DEVICE_INFO, DOMAIN, PROTOCOL_BITS
from .protocol_codec import decode_protocol_switches, encode_protocol_switches

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a CUKTECH charger from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    coordinator = CuktechBluetoothCoordinator(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = coordinator
    try:
        await coordinator.async_setup()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_unload()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise

    async def stop(_event) -> None:
        await coordinator.async_unload()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Only entries containing direct Bluetooth credentials are supported."""
    if entry.version > 2:
        return False
    if not entry.data.get(CONF_MAC) or not entry.data.get(CONF_TOKEN):
        _LOGGER.error(
            "Re-add CUKTECH Charger with its Bluetooth MAC and token; the old entry has no Bluetooth credentials"
        )
        return False
    hass.config_entries.async_update_entry(entry, version=2)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and its BLE connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_unload()
    return unloaded


class CuktechBluetoothCoordinator:
    """Expose BLE runtime state to Home Assistant entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._port_data: dict[str, dict[str, Any]] = {}
        self._settings: dict[str, int] = {}
        self._callbacks: list = []
        self._available = False
        self._ble_connected = False
        self._ble_enabled = True
        self._ble_pending = False
        self._ble_lock = asyncio.Lock()
        self._device_model = DEVICE_INFO["model"]
        self._firmware_version = ""
        self._runtime = CuktechBLERuntime(
            hass,
            entry.data[CONF_MAC],
            entry.data[CONF_TOKEN],
            self._async_handle_snapshot,
            ble_key=entry.data.get(CONF_BLE_KEY),
        )

    @property
    def available(self) -> bool:
        """Return whether the charger is connected and authenticated."""
        return self._available

    @property
    def ble_connected(self) -> bool:
        return self._ble_connected

    @property
    def ble_enabled(self) -> bool:
        return self._ble_enabled

    @property
    def ble_pending(self) -> bool:
        return self._ble_pending

    @property
    def port_data(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self._port_data.items()}

    @property
    def data(self) -> dict[str, int]:
        return dict(self._settings)

    @property
    def protocol_switches(self) -> dict[str, dict[str, bool]]:
        return decode_protocol_switches(self._settings.get("21"))

    _encode_protocol_extend = staticmethod(encode_protocol_switches)

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            **DEVICE_INFO,
            "model": self._device_model or DEVICE_INFO["model"],
            "sw_version": self._firmware_version or None,
        }

    async def async_setup(self) -> None:
        """Start connecting in the background."""
        await self._runtime.async_start()

    async def async_unload(self) -> None:
        """Stop reconnects and release the shared HA Bluetooth connection."""
        await self._runtime.async_stop()

    def register_callback(self, cb) -> None:
        if cb not in self._callbacks:
            self._callbacks.append(cb)

    def unregister_callback(self, cb) -> None:
        if cb in self._callbacks:
            self._callbacks.remove(cb)

    @callback
    def _notify_callbacks(self) -> None:
        for cb in tuple(self._callbacks):
            try:
                cb()
            except Exception:
                _LOGGER.exception("CUKTECH entity update callback failed")

    async def _async_handle_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Apply one runtime snapshot and notify all entities."""
        self._ble_connected = bool(snapshot.get("connected"))
        self._available = bool(snapshot.get("authenticated"))
        ports = snapshot.get("ports", {})
        self._port_data = {
            str(index): dict(ports.get(name, {}))
            for index, name in enumerate(("c1", "c2", "c3", "a"), start=1)
        }
        self._settings = dict(snapshot.get("settings", {}))
        self._device_model = snapshot.get("model") or self._device_model
        self._firmware_version = snapshot.get("firmware") or self._firmware_version
        self._notify_callbacks()

    async def async_enable_ble(self, enable: bool) -> bool:
        """Enable or disable this integration's connection without touching adapters."""
        async with self._ble_lock:
            if self._ble_enabled == enable and (not enable or self._runtime.running):
                return True
            previous = self._ble_enabled
            self._ble_enabled = enable
            self._ble_pending = True
            self._notify_callbacks()
            try:
                if enable:
                    await self._runtime.async_start()
                else:
                    await self._runtime.async_stop()
                return True
            except BaseException:
                self._ble_enabled = previous
                raise
            finally:
                self._ble_pending = False
                self._notify_callbacks()

    async def async_set_value(self, piid: int, value: Any) -> None:
        if not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value:
            raise ValueError("Setting value must be a finite integer")
        await self._runtime.async_set_value(piid, int(value))

    async def async_port_control(self, port: str, action: str) -> None:
        await self._runtime.async_port_control(port, action)

    async def async_set_protocol(self, port: str, protocol: str, on: bool) -> None:
        async with self._ble_lock:
            if port not in PROTOCOL_BITS or protocol not in PROTOCOL_BITS[port]:
                _LOGGER.error("Unknown protocol switch: %s.%s", port, protocol)
                return
            await self._runtime.async_set_protocol(port, protocol, on)
