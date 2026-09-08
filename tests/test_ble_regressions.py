"""Regression coverage for main fixes adapted to the HA Bluetooth transport."""

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from custom_components.cuktech_charger.ble_controller import CuktechBLEController
from custom_components.cuktech_charger.ble_protocol import parse_property_value
from custom_components.cuktech_charger.ble_runtime import CuktechBLERuntime
from custom_components.cuktech_charger.ble_state import ChargerState


def property_frame(piid, value, *, opcode=4, size=4, compact=False, error=0, siid=2):
    header = bytearray(12 if compact else 13)
    header[4] = opcode
    header[6] = siid
    header[7:9] = piid.to_bytes(2, "little")
    if not compact:
        header[9] = error
        header[11] = size
        header[12] = 5 if size == 4 else 1
    return bytes(header) + value.to_bytes(size, "little")


@pytest.fixture
def controller():
    client = MagicMock(is_connected=True)
    client.write_gatt_char = AsyncMock()
    client.read_gatt_char = AsyncMock(return_value=b"1.0")
    client.disconnect = AsyncMock()
    return CuktechBLEController(client, bytes(12))


@pytest.fixture
def runtime(mock_hass, controller):
    rt = CuktechBLERuntime(mock_hass, "AA:BB:CC:DD:EE:FF", bytes(12), AsyncMock())
    rt._client = controller.client
    rt._controller = controller
    controller.authenticated = True
    controller.send_miot_command = AsyncMock(
        side_effect=lambda siid, piid, value=None, **kwargs: {"piid": piid, "value": value}
    )
    rt.state.connected = rt.state.authenticated = True
    rt._ready_event.set()
    return rt


def encrypted_frame(controller, counter, data=b"telemetry"):
    key, iv = bytes(range(16)), bytes(range(4))
    controller._session_keys = {"dev_key": key, "dev_iv": iv}
    nonce = iv + bytes(4) + struct.pack("<I", counter)
    return struct.pack("<H", counter & 0xFFFF) + AESCCM(key, tag_length=4).encrypt(
        nonce, data, None
    )


def test_decrypt_wraparound_and_delayed_frames(controller):
    for counter in (65534, 65535, 65536, 65537, 65535, 65538):
        assert controller.decrypt(encrypted_frame(controller, counter)) == b"telemetry"
    assert controller._device_counter == 65538


def test_corrupt_frame_does_not_advance_nonce(controller):
    frame = encrypted_frame(controller, 65536)
    assert controller.decrypt(frame[:-1] + bytes([frame[-1] ^ 1])) is None
    assert controller._device_counter == 0
    assert controller.decrypt(frame) == b"telemetry"
    assert controller._device_counter == 65536
    assert controller._decrypt_failures == 0


def test_repeated_decrypt_failure_triggers_reconnect(controller):
    frame = encrypted_frame(controller, 5)
    bad = frame[:-1] + bytes([frame[-1] ^ 1])
    assert controller.decrypt(bad) is None
    assert controller.decrypt(bad) is None
    with pytest.raises(ConnectionError, match="decrypt"):
        controller.decrypt(bad)


@pytest.mark.parametrize("compact", [True, False])
@pytest.mark.asyncio
async def test_push_updates_settings_and_hardware_protocols(runtime, compact):
    # C1/C2 both output 9V; firmware distinguishes UFCS from PPS.
    port_value = int.from_bytes(bytes([1, 4, 10, 90]), "little")
    for piid in (1, 2, 3, 4):
        runtime._process_push(property_frame(piid, port_value, compact=compact))
    runtime._process_push(property_frame(17, 0x0A640850, compact=compact))
    runtime._process_push(property_frame(18, 0x07640632, compact=compact))
    assert [port.protocol for port in runtime.state.ports.values()] == ["UFCS", "PPS", "PD", "SCP"]
    # Update protocols without waiting for another port-power push.
    runtime._process_push(property_frame(17, 0x07640950, compact=compact))
    assert runtime.state.ports[1].protocol == "PD"
    assert runtime.state.ports[2].protocol == "PPS"
    runtime._process_push(property_frame(21, 0x03030F0E, compact=compact))
    assert runtime.state.protocol_switches["c1"]["pd"] is False
    runtime._process_push(property_frame(16, 14, size=1, compact=compact))
    assert runtime.state.ports[1].power == 0
    assert runtime.state.snapshot()["ports"]["c1"]["enabled"] is False
    runtime._process_push(property_frame(6, 5, size=1, compact=compact))
    assert runtime.state.settings["6"] == 5


@pytest.mark.asyncio
async def test_wrong_service_malformed_and_error_pushes_are_ignored(runtime):
    runtime._process_push(property_frame(16, 0, size=1, siid=3))
    runtime._process_push(property_frame(16, 0, size=1, error=1))
    runtime._process_push(b"short")
    assert "16" not in runtime.state.settings


def test_no_protocol_guess_after_firmware_reports_unknown():
    state = ChargerState()
    state.update_port(1, bytes(12) + bytes([1, 4, 10, 90]))
    assert state.ports[1].protocol == "PD"  # Cold-start fallback only.
    state.update_setting(17, 0x55640000)
    assert state.ports[1].protocol == "Unknown"
    state.update_setting(17, 0)
    assert state.ports[1].protocol == "idle"


def test_c3_a_merged_status_survives_snapshot():
    state = ChargerState()
    state.update_port(3, bytes(12) + bytes([0x11, 4, 10, 50]))
    assert state.snapshot()["ports"]["c3"]["status_raw"] == 0x11


@pytest.mark.asyncio
async def test_unknown_bitmask_cannot_disable_other_ports(runtime):
    runtime._controller.send_miot_command = AsyncMock(return_value=None)
    with pytest.raises(ConnectionError, match="unknown PIID 16"):
        await runtime.async_port_control("c1", "off")
    runtime._controller.send_miot_command.assert_awaited_once_with(2, 16)
    assert "16" not in runtime.state.settings


@pytest.mark.asyncio
async def test_concurrent_port_commands_preserve_other_bits(runtime):
    runtime.state.update_setting(16, 15)
    await asyncio.gather(
        runtime.async_port_control("c1", "off"), runtime.async_port_control("c2", "off")
    )
    assert runtime.state.settings["16"] == 12
    assert [call.args[2] for call in runtime._controller.send_miot_command.await_args_list] == [
        14,
        12,
    ]


@pytest.mark.asyncio
async def test_concurrent_protocol_commands_preserve_other_bits(runtime):
    runtime.state.update_setting(21, 0x03030F0F)
    await asyncio.gather(
        runtime.async_set_protocol("c1", "pd", False),
        runtime.async_set_protocol("c2", "ufcs", False),
    )
    assert runtime.state.settings["21"] == 0x03030B0E


@pytest.mark.asyncio
async def test_failed_write_leaves_cached_state_unchanged(runtime):
    runtime.state.update_setting(16, 15)
    runtime._controller.send_miot_command = AsyncMock(side_effect=ConnectionError("failed"))
    with pytest.raises(ConnectionError):
        await runtime.async_port_control("c1", "off")
    assert runtime.state.settings["16"] == 15


@pytest.mark.asyncio
async def test_get_error_does_not_become_a_setting(controller):
    controller.authenticated = True
    controller.drain_command_notifications = AsyncMock()
    controller._send_encrypted = AsyncMock()
    controller.receive_command_payload = AsyncMock(
        return_value=property_frame(6, 0, opcode=3, size=1, error=1)
    )
    assert await controller.send_miot_command(2, 6) is None


@pytest.mark.asyncio
async def test_get_does_not_swallow_interleaved_push(controller):
    controller.authenticated = True
    controller.drain_command_notifications = AsyncMock()
    controller._send_encrypted = AsyncMock()
    push = property_frame(21, 0x03030F0F)
    result = property_frame(6, 5, opcode=3, size=1)
    controller.receive_command_payload = AsyncMock(side_effect=[push, result])
    controller._push_callback = MagicMock()
    assert (await controller.send_miot_command(2, 6))["value"] == 5
    controller._push_callback.assert_called_once_with(push)


@pytest.mark.asyncio
async def test_drain_delivers_pushes_instead_of_discarding(controller):
    controller._queues["cmd_recv"] = asyncio.Queue()
    controller._queues["cmd_recv"].put_nowait(b"queued frame")
    push = property_frame(21, 0x03030F0F)

    async def receive(_timeout):
        controller._queues["cmd_recv"].get_nowait()
        return push

    controller.receive_command_payload = receive
    controller._push_callback = MagicMock()
    await controller.drain_command_notifications()
    controller._push_callback.assert_called_once_with(push)


@pytest.mark.asyncio
async def test_notification_overflow_is_bounded_and_reconnects(controller):
    handler = controller._notification_handler("cmd_recv")
    for _ in range(300):
        handler(None, bytearray(b"data"))
    assert controller._queues["cmd_recv"].qsize() == 256
    with pytest.raises(ConnectionError, match="overflow"):
        await controller.wait_notify("cmd_recv")


@pytest.mark.asyncio
async def test_silent_link_probe_fails_even_if_client_claims_connected(runtime):
    runtime._last_payload = runtime._last_probe = asyncio.get_running_loop().time() - 61
    runtime._controller.client.read_gatt_char = AsyncMock(side_effect=TimeoutError)
    with pytest.raises(ConnectionError, match="probe failed"):
        await runtime._probe_if_silent(runtime._controller)


@pytest.mark.asyncio
async def test_live_but_stale_session_is_reconnected(runtime):
    runtime._last_payload = asyncio.get_running_loop().time() - 181
    runtime._last_probe = 0
    with pytest.raises(ConnectionError, match="three minutes"):
        await runtime._probe_if_silent(runtime._controller)


@pytest.mark.asyncio
async def test_probe_is_throttled_when_link_is_healthy(runtime):
    runtime._last_payload = runtime._last_probe = asyncio.get_running_loop().time() - 61
    await runtime._probe_if_silent(runtime._controller)
    await runtime._probe_if_silent(runtime._controller)
    runtime._controller.client.read_gatt_char.assert_awaited_once()


@pytest.mark.asyncio
async def test_idle_but_authenticated_device_is_not_disconnected(runtime):
    runtime._last_payload = asyncio.get_running_loop().time() - 181
    runtime._last_probe = 0
    runtime._controller.send_miot_command = AsyncMock(return_value={"piid": 5, "value": 1})
    await runtime._probe_if_silent(runtime._controller)
    assert runtime.state.settings["5"] == 1
    runtime._controller.client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_start_and_stop_cleanup(mock_hass):
    rt = CuktechBLERuntime(mock_hass, "AA:BB:CC:DD:EE:FF", bytes(12), AsyncMock())
    started = asyncio.Event()

    async def session():
        started.set()
        await asyncio.Event().wait()

    rt._connect_and_consume = session
    await rt.async_start()
    await started.wait()
    task = rt._run_task
    await rt.async_start()
    assert rt._run_task is task
    await rt.async_stop()
    assert task.done()
    assert not rt.running
    assert not rt._ready_event.is_set()


@pytest.mark.asyncio
async def test_new_session_does_not_reuse_old_settings(runtime):
    runtime.state.update_setting(21, 0x03030F0F)
    runtime.state.update_setting(17, 0x0A640850)
    with patch(
        "custom_components.cuktech_charger.ble_runtime.async_ble_device_from_address",
        return_value=None,
    ):
        with pytest.raises(ConnectionError):
            await runtime._connect_and_consume()
    assert runtime.state.settings == {}
    assert runtime.state.hw_protocols == {}
    assert not runtime._ready_event.is_set()


@pytest.mark.parametrize(
    ("piid", "value"), [(6, 0), (5, 0), (9, 1441), (16, 16), (13, 2), (21, -1)]
)
@pytest.mark.asyncio
async def test_invalid_setting_is_not_sent(runtime, piid, value):
    with pytest.raises(ValueError):
        await runtime.async_set_value(piid, value)
    runtime._controller.send_miot_command.assert_not_awaited()


@pytest.mark.parametrize("payload", [b"", bytes(12), property_frame(6, 5, opcode=3, size=1)[:-1]])
def test_invalid_property_result(payload):
    assert parse_property_value(payload) is None


@pytest.mark.asyncio
async def test_pending_device_changes_are_applied_before_port_bitmask(runtime):
    runtime.state.update_setting(16, 15)

    async def drain():
        runtime._process_push(property_frame(16, 13, size=1))  # C2 was disabled on device.

    runtime._controller.drain_command_notifications = drain
    await runtime.async_port_control("c1", "off")
    assert runtime.state.settings["16"] == 12
    runtime._controller.send_miot_command.assert_awaited_once_with(2, 16, 12)


@pytest.mark.asyncio
async def test_disconnect_waits_for_active_command(runtime):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def send(siid, piid, value):
        entered.set()
        await finish.wait()
        return {"piid": piid, "value": value}

    runtime.state.update_setting(16, 15)
    runtime._controller.send_miot_command = send
    client = runtime._client
    command = asyncio.create_task(runtime.async_port_control("c1", "off"))
    await entered.wait()
    disconnect = asyncio.create_task(runtime._disconnect())
    await asyncio.sleep(0)
    client.disconnect.assert_not_awaited()
    finish.set()
    await asyncio.gather(command, disconnect)
    client.disconnect.assert_awaited_once()
    assert not runtime.connected


@pytest.mark.asyncio
async def test_explicit_all_ports_command_does_not_require_old_bitmask(runtime):
    assert "16" not in runtime.state.settings
    await runtime.async_port_control("all", "off")
    runtime._controller.send_miot_command.assert_awaited_once_with(2, 16, 0)
    assert runtime.state.settings["16"] == 0
