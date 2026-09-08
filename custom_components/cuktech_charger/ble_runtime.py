"""Home Assistant-routed BLE runtime for CUKTECH chargers."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant

from .ble_controller import AuthenticationError, CuktechBLEController
from .ble_protocol import (
    CHAR_FW_VERSION,
    PORT_BITS,
    PORT_NAMES,
    READABLE_SETTINGS_PIIDS,
    SIID_CHARGER,
    parse_hex_secret,
    parse_property_value,
)
from .ble_state import ChargerState
from .const import PROTOCOL_BITS
from .protocol_codec import encode_protocol_switches

_LOGGER = logging.getLogger(__name__)

SnapshotCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class CuktechBLERuntime:
    """Own connection lifecycle, serialized commands, state, and reconnects."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        token: str | bytes,
        snapshot_callback: SnapshotCallback,
        *,
        ble_key: str | bytes | None = None,
        reconnect_base_delay: float = 5.0,
        reconnect_max_delay: float = 60.0,
    ) -> None:
        self.hass = hass
        self.address = address
        self.token = parse_hex_secret(token, 12, "token")
        self.ble_key = (
            None if ble_key in (None, "", b"") else parse_hex_secret(ble_key, 16, "ble_key")
        )
        self._snapshot_callback = snapshot_callback
        self._base_delay = reconnect_base_delay
        self._max_delay = reconnect_max_delay
        self.state = ChargerState()
        self._client: BleakClient | None = None
        self._controller: CuktechBLEController | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._disconnected_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._command_lock = asyncio.Lock()
        self._last_payload = 0.0
        self._last_probe = 0.0

    @property
    def running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    async def async_start(self) -> None:
        """Start the background connection and notification loop."""
        if self.running:
            return
        self._stop_event.clear()
        self._run_task = self.hass.async_create_background_task(
            self._run(), name=f"cuktech_ble_{self.address}"
        )

    async def async_stop(self) -> None:
        """Stop reconnects, notifications, and the active BLE connection."""
        self._stop_event.set()
        self._disconnected_event.set()
        task = self._run_task
        self._run_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._disconnect()

    async def async_set_value(self, piid: int, value: int) -> dict[str, Any] | None:
        """Set a charger service property and publish the resulting snapshot."""
        if not isinstance(piid, int) or piid not in {
            5,
            6,
            8,
            9,
            10,
            11,
            12,
            13,
            15,
            16,
            19,
            20,
            21,
        }:
            raise ValueError(f"PIID {piid} is not writable")
        if not isinstance(value, int) or value < 0 or value > 0xFFFFFFFF:
            raise ValueError("value must be an unsigned 32-bit integer")
        ranges = {5: (1, 4), 6: (1, 5), 13: (0, 1), 15: (0, 1), 16: (0, 15), 19: (0, 1), 20: (0, 1)}
        if piid in (9, 10, 11, 12):
            ranges[piid] = (0, 1440)
        if piid in ranges and not ranges[piid][0] <= value <= ranges[piid][1]:
            raise ValueError(f"Value outside allowed range for PIID {piid}")

        async def send(controller: CuktechBLEController) -> dict[str, Any]:
            result = await controller.send_miot_command(SIID_CHARGER, piid, value)
            if result is None:
                raise ConnectionError(f"No response while setting PIID {piid}")
            self.state.update_setting(piid, value)
            return result

        result = await self._execute_when_ready(send, f"set PIID {piid}")
        return result

    async def async_port_control(self, port: str, action: str) -> int:
        """Enable or disable c1/c2/c3/a/all via atomic PIID 16 update."""
        port = port.lower()
        action = action.lower()
        if port not in (*PORT_BITS, "all"):
            raise ValueError(f"Unknown port: {port}")
        if action not in ("on", "off"):
            raise ValueError("action must be 'on' or 'off'")

        async def control(controller: CuktechBLEController) -> int:
            if port == "all":
                await controller.drain_command_notifications()
                current_value = self.state.settings.get("16")
                new_value = 0x0F if action == "on" else 0
            else:
                current_value = await self._known_setting(controller, 16)
                mask = 1 << PORT_BITS[port]
                new_value = current_value | mask if action == "on" else current_value & ~mask
            if new_value != current_value:
                result = await controller.send_miot_command(SIID_CHARGER, 16, new_value)
                if result is None:
                    raise ConnectionError("No response while setting port control")
            self.state.update_setting(16, new_value)
            return new_value

        new_value = await self._execute_when_ready(control, f"turn port {port} {action}")
        return new_value

    async def _known_setting(self, controller: CuktechBLEController, piid: int) -> int:
        # Reconcile queued device-side changes before computing a new bitmask.
        await controller.drain_command_notifications()
        if str(piid) not in self.state.settings:
            result = await controller.send_miot_command(SIID_CHARGER, piid)
            if result is None or result.get("value") is None:
                raise ConnectionError(f"Cannot change unknown PIID {piid} bitmask")
            self.state.update_setting(piid, int(result["value"]))
        return self.state.settings[str(piid)]

    async def async_set_protocol(self, port: str, protocol: str, on: bool) -> None:
        """Read/modify/write PIID 21 in the same transaction as other commands."""
        if port not in PROTOCOL_BITS or protocol not in PROTOCOL_BITS[port]:
            raise ValueError(f"Unknown protocol switch: {port}.{protocol}")

        async def control(controller: CuktechBLEController) -> None:
            await self._known_setting(controller, 21)
            switches = self.state.protocol_switches
            switches[port][protocol] = on
            value = encode_protocol_switches(switches)
            result = await controller.send_miot_command(SIID_CHARGER, 21, value)
            if result is None:
                raise ConnectionError("No response while setting protocol switches")
            self.state.update_setting(21, value)

        await self._execute_when_ready(control, f"set {port}.{protocol}")

    async def _execute_when_ready(
        self,
        operation: Callable[[CuktechBLEController], Awaitable[Any]],
        description: str,
    ) -> Any:
        """Wait for an authenticated session and execute one command transaction."""
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=45.0)
        except TimeoutError as err:
            raise ConnectionError(
                "Charger did not become connected and authenticated in time"
            ) from err
        async with self._command_lock:
            try:
                async with asyncio.timeout(30):
                    return await operation(self._require_controller())
            except TimeoutError as err:
                self._ready_event.clear()
                self._disconnected_event.set()
                raise ConnectionError(f"Timed out while trying to {description}") from err
            finally:
                # Commands can consume unsolicited pushes, even when they fail.
                await self._emit_snapshot()

    async def _run(self) -> None:
        attempt = 0
        auth_failures = 0
        while not self._stop_event.is_set():
            try:
                await self._connect_and_consume()
                attempt = 0
                auth_failures = 0
            except asyncio.CancelledError:
                raise
            except AuthenticationError:
                auth_failures += 1
                _LOGGER.warning(
                    "CUKTECH authentication failed for %s; retrying after cooldown", self.address
                )
            except Exception:
                auth_failures = 0
                _LOGGER.warning("CUKTECH BLE session failed for %s", self.address, exc_info=True)
            finally:
                await self._disconnect()
            if self._stop_event.is_set():
                break
            delay = min(self._base_delay * (2 ** min(attempt, 6)), self._max_delay)
            if auth_failures:
                delay = (
                    600
                    if auth_failures >= 5
                    else 300
                    if auth_failures >= 3
                    else min(120 * auth_failures, 180)
                )
            attempt += 1
            try:
                await asyncio.wait_for(self._stop_event.wait(), delay)
            except TimeoutError:
                pass

    async def _connect_and_consume(self) -> None:
        self.state = ChargerState()
        self._ready_event.clear()
        device = async_ble_device_from_address(self.hass, self.address, connectable=True)
        if device is None:
            raise ConnectionError(f"No connectable HA Bluetooth route for {self.address}")
        self._disconnected_event.clear()
        self._client = await establish_connection(
            BleakClient,
            device,
            device.name or self.address,
            disconnected_callback=self._on_disconnected,
            ble_device_callback=lambda: (
                async_ble_device_from_address(self.hass, self.address, connectable=True) or device
            ),
        )
        self.state.connected = True
        await self._emit_snapshot()
        controller = CuktechBLEController(
            self._client, self.token, self.ble_key, push_callback=self._process_push
        )
        self._controller = controller
        await controller.async_setup()
        model, firmware = await controller.read_device_info()
        self.state.model = model
        self.state.firmware = firmware
        await controller.authenticate()
        self.state.authenticated = True
        await self._read_initial_settings(controller)
        self._ready_event.set()
        self._last_payload = self._last_probe = asyncio.get_running_loop().time()
        await self._emit_snapshot()

        while not self._stop_event.is_set() and not self._disconnected_event.is_set():
            if not self._client.is_connected:
                raise ConnectionError("BLE client disconnected")
            async with self._command_lock:
                payload = await controller.receive_command_payload(1.0)
                if payload is not None:
                    self._process_push(payload)
                controller.drain_spec_notifications()
                await self._probe_if_silent(controller)
            if payload is not None:
                await self._emit_snapshot()
            elif self._disconnected_event.is_set():
                break
        if not self._stop_event.is_set():
            raise ConnectionError("BLE device disconnected")

    async def _probe_if_silent(self, controller: CuktechBLEController) -> None:
        """Probe a silent link without powering down HA's shared adapter."""
        now = asyncio.get_running_loop().time()
        if now - max(self._last_payload, self._last_probe) < 60:
            return
        try:
            await asyncio.wait_for(controller.client.read_gatt_char(CHAR_FW_VERSION), timeout=3)
        except Exception as err:
            raise ConnectionError("Silent BLE link probe failed") from err
        self._last_probe = now
        # An idle charger may legitimately stop pushing. Verify the encrypted
        # session before deciding it is stale; a GATT read alone cannot do this.
        if now - self._last_payload >= 180:
            result = await controller.send_miot_command(SIID_CHARGER, 5, timeout=3)
            if result is None or result.get("value") is None:
                raise ConnectionError("BLE session unresponsive after three minutes of silence")
            self.state.update_setting(5, int(result["value"]))
            self._last_payload = now
            await self._emit_snapshot()

    def _on_disconnected(self, _client: BleakClient) -> None:
        self._ready_event.clear()
        self.state.connected = False
        self.state.authenticated = False
        self._disconnected_event.set()

    async def _read_initial_settings(self, controller: CuktechBLEController | None = None) -> None:
        controller = controller or self._require_controller()
        failures = 0
        for piid in READABLE_SETTINGS_PIIDS:
            try:
                result = await controller.send_miot_command(SIID_CHARGER, piid)
            except ConnectionError as err:
                failures += 1
                _LOGGER.debug("Unable to read initial PIID %d: %s", piid, err)
                await asyncio.sleep(0.2)
                continue
            if result is None or result.get("value") is None:
                failures += 1
                continue
            value = int(result["value"])
            self.state.update_setting(piid, value)
            await asyncio.sleep(0.1)
        if failures:
            _LOGGER.debug(
                "Initial settings completed with %d/%d unavailable values",
                failures,
                len(READABLE_SETTINGS_PIIDS),
            )

    def _process_push(self, payload: bytes) -> None:
        if len(payload) < 9 or payload[4] != 0x04 or payload[6] != SIID_CHARGER:
            return
        piid = int.from_bytes(payload[7:9], "little")
        value = parse_property_value(payload)
        if value is None:
            return
        self._last_payload = asyncio.get_running_loop().time()
        if piid in PORT_NAMES:
            if len(payload) in (16, 17):
                self.state.update_port(piid, payload)
        elif piid in READABLE_SETTINGS_PIIDS:
            self.state.update_setting(piid, value)

    def _require_controller(self) -> CuktechBLEController:
        controller = self._controller
        if (
            controller is None
            or not self._ready_event.is_set()
            or not self.state.authenticated
            or not controller.client.is_connected
        ):
            raise ConnectionError("Charger is not connected and authenticated")
        return controller

    async def _disconnect(self) -> None:
        # Do not close a client while an entity command is still using it.
        async with self._command_lock:
            await self._disconnect_locked()

    async def _disconnect_locked(self) -> None:
        controller, client = self._controller, self._client
        self._controller = None
        self._client = None
        self._ready_event.clear()
        self.state.connected = False
        self.state.authenticated = False
        if controller is not None:
            try:
                await controller.async_close()
            except Exception:
                _LOGGER.debug("Failed to stop CUKTECH notifications", exc_info=True)
        if client is not None and client.is_connected:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=10)
            except Exception:
                _LOGGER.debug("Failed to disconnect CUKTECH client", exc_info=True)
        await self._emit_snapshot()

    async def _emit_snapshot(self) -> None:
        try:
            result = self._snapshot_callback(self.state.snapshot())
            if inspect.isawaitable(result):
                await result
        except Exception:
            _LOGGER.exception("CUKTECH snapshot callback failed")


def resolve_ha_ble_device(hass: HomeAssistant, address: str) -> BLEDevice | None:
    """Expose HA's current connectable route for diagnostics/integration glue."""
    return async_ble_device_from_address(hass, address, connectable=True)
