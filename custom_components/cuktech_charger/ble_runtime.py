"""Home Assistant-routed BLE runtime for CUKTECH chargers."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
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

_PORT_POLL_INTERVAL = 3.0
_PORT_POLL_TIMEOUT = 3.0
_BACKGROUND_COMMAND_TIMEOUT = 15.0
_CONSECUTIVE_FAILURE_LIMIT = 5

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
        self._next_port_poll = {3: 0.0, 4: 0.0}
        self._port_poll_failures = 0
        self._command_failures = 0

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
            if piid == 16:
                return await self._set_port_mask(controller, value)
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
                new_mask = 0x0F if action == "on" else 0
            else:
                live_mask = await self._read_port_mask(controller)
                current_mask = live_mask if live_mask is not None else self.state.settings.get("16")
                if current_mask is None or not 0 <= current_mask <= 0xFF:
                    raise ConnectionError("Cannot change unknown PIID 16 bitmask")
                bit = 1 << PORT_BITS[port]
                new_mask = current_mask | bit if action == "on" else current_mask & ~bit
                if new_mask == live_mask:
                    return live_mask
            result = await self._set_port_mask(controller, new_mask)
            return result["value"]

        return await self._execute_when_ready(control, f"turn port {port} {action}")

    async def _read_port_mask(self, controller: CuktechBLEController) -> int | None:
        try:
            result = await controller.send_miot_command(
                SIID_CHARGER, 16, timeout=_PORT_POLL_TIMEOUT
            )
        except (OSError, BleakError):
            if self._disconnected_event.is_set() or not controller.client.is_connected:
                raise
            _LOGGER.debug("Unable to read the device's port mask", exc_info=True)
            return None
        value = None if result is None else result.get("value")
        if not isinstance(value, int) or not 0 <= value <= 0xFF:
            return None
        self.state.update_setting(16, value)
        return value

    async def _set_port_mask(self, controller: CuktechBLEController, mask: int) -> dict[str, Any]:
        result = await controller.send_miot_command(SIID_CHARGER, 16, mask)
        if result is None:
            raise ConnectionError("No response while setting port control")
        effective = result.get("value")
        if result.get("transport_only"):
            # Gateway firmware can acknowledge transport without replying to
            # SET. Prefer a device read-back and retain the existing fallback
            # for firmware that also fails to answer GET.
            effective = await self._read_port_mask(controller)
        if not isinstance(effective, int) or not 0 <= effective <= 0xFF:
            effective = mask
        self.state.update_setting(16, effective)
        return {**result, "value": effective}

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
            controller = self._require_controller()
            try:
                async with asyncio.timeout(30):
                    result = await operation(controller)
            except TimeoutError as err:
                self._ready_event.clear()
                self._disconnected_event.set()
                raise ConnectionError(f"Timed out while trying to {description}") from err
            except (OSError, BleakError):
                self._command_failures += 1
                if self._command_failures >= _CONSECUTIVE_FAILURE_LIMIT:
                    _LOGGER.warning(
                        "Repeated BLE command failures for %s; reconnecting", self.address
                    )
                    self._ready_event.clear()
                    self._disconnected_event.set()
                raise
            else:
                self._command_failures = 0
                return result
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
        self._command_failures = self._port_poll_failures = 0
        self._next_port_poll = {3: 0.0, 4: 0.0}
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
                polled = False
                if not self._disconnected_event.is_set():
                    polled = await self._poll_next_port(controller)
                    await self._probe_if_silent(controller)
            if payload is not None or polled:
                await self._emit_snapshot()
            elif self._disconnected_event.is_set():
                break
        if not self._stop_event.is_set():
            raise ConnectionError("BLE device disconnected")

    async def _poll_next_port(self, controller: CuktechBLEController) -> bool:
        """Check one due port while holding the same lock as entity commands."""
        loop = asyncio.get_running_loop()
        piid = min(self._next_port_poll, key=self._next_port_poll.__getitem__)
        if loop.time() < self._next_port_poll[piid]:
            return False
        try:
            async with asyncio.timeout(_BACKGROUND_COMMAND_TIMEOUT):
                result = await controller.send_miot_command(
                    SIID_CHARGER, piid, timeout=_PORT_POLL_TIMEOUT
                )
        except TimeoutError as err:
            # A cancelled transport exchange may leave the device waiting for an ACK.
            raise ConnectionError(f"Reading port PIID {piid} timed out") from err
        except (OSError, BleakError):
            if self._disconnected_event.is_set() or not controller.client.is_connected:
                raise
            result = None
            _LOGGER.debug("Unable to read port PIID %d", piid, exc_info=True)
        self._next_port_poll[piid] = loop.time() + _PORT_POLL_INTERVAL

        payload = None if result is None else result.get("raw")
        # GET results have a four-byte value with an explicit length. A
        # matching port push may also arrive and has already been forwarded.
        if payload is not None and (
            (
                len(payload) == 17
                and payload[4] == 0x03
                and payload[11] == 4
                and result.get("value") is not None
            )
            or (len(payload) >= 12 and payload[4] == 0x04)
        ):
            self.state.update_port(piid, payload)
            self._last_payload = loop.time()
            self._port_poll_failures = 0
        else:
            self._port_poll_failures += 1
            if self._port_poll_failures >= _CONSECUTIVE_FAILURE_LIMIT:
                # Missing port telemetry alone can be a firmware limitation.
                # Check a separate property before reconnecting the device.
                await self._probe_miot_session(controller, "consecutive port reads failed")
                self._port_poll_failures = 0
        # A GET may consume C1/C2 pushes even if no result arrived.
        return True

    async def _probe_miot_session(self, controller: CuktechBLEController, reason: str) -> None:
        result = await asyncio.wait_for(
            controller.send_miot_command(SIID_CHARGER, 5, timeout=_PORT_POLL_TIMEOUT),
            timeout=_BACKGROUND_COMMAND_TIMEOUT,
        )
        if result is None or result.get("value") is None:
            raise ConnectionError(f"BLE session unresponsive after {reason}")
        self.state.update_setting(5, int(result["value"]))
        self._last_payload = asyncio.get_running_loop().time()

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
            await self._probe_miot_session(controller, "three minutes of silence")
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
        if len(payload) < 9 or payload[4] != 0x04:
            return
        piid = int.from_bytes(payload[7:9], "little")
        if piid in PORT_NAMES:
            # Preserve gateway's port telemetry format: the final four bytes
            # contain status/code/current/voltage. Port push headers are not
            # GET Result headers and vary between firmware versions.
            if len(payload) < 12:
                return
            self.state.update_port(piid, payload)
            self._last_payload = asyncio.get_running_loop().time()
            return
        if payload[6] != SIID_CHARGER or piid not in READABLE_SETTINGS_PIIDS:
            return
        value = parse_property_value(payload)
        if value is None:
            _LOGGER.debug("Ignored unsupported setting push: PIID %d, %d bytes", piid, len(payload))
            return
        self._last_payload = asyncio.get_running_loop().time()
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
