"""Tests for the Home Assistant-routed BLE runtime."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.cuktech_charger.ble_controller import CuktechBLEController
from custom_components.cuktech_charger.ble_runtime import (
    READABLE_SETTINGS_PIIDS,
    CuktechBLERuntime,
)


def test_parse_result_value_uint8():
    payload = bytes([0, 0, 0, 0, 3, 0, 2, 5, 0, 0, 0, 1, 1, 7])
    assert CuktechBLEController._parse_result_value(payload) == 7


def test_parse_result_value_uint32():
    value = 0x0201080F
    payload = bytes([0, 0, 0, 0, 3, 0, 2, 21, 0, 0, 0, 4, 5]) + value.to_bytes(4, "little")
    assert CuktechBLEController._parse_result_value(payload) == value


@pytest.mark.asyncio
async def test_runtime_uses_ha_connectable_route(mock_hass):
    mock_hass.async_create_task = MagicMock()
    runtime = CuktechBLERuntime(
        mock_hass,
        "AA:BB:CC:DD:EE:FF",
        "00112233445566778899aabb",
        MagicMock(),
    )
    device = SimpleNamespace(name="CUKTECH", address="AA:BB:CC:DD:EE:FF")
    client = MagicMock(is_connected=True)
    controller = MagicMock()
    controller.async_setup = AsyncMock()
    controller.read_device_info = AsyncMock(return_value=("model", "1.0"))
    controller.authenticate = AsyncMock()
    runtime._read_initial_settings = AsyncMock()
    runtime._emit_snapshot = AsyncMock()
    runtime._stop_event.set()

    with (
        patch(
            "custom_components.cuktech_charger.ble_runtime.async_ble_device_from_address",
            return_value=device,
        ) as get_device,
        patch(
            "custom_components.cuktech_charger.ble_runtime.establish_connection",
            new=AsyncMock(return_value=client),
        ) as connect,
        patch(
            "custom_components.cuktech_charger.ble_runtime.CuktechBLEController",
            return_value=controller,
        ),
    ):
        await runtime._connect_and_consume()

    get_device.assert_called_with(mock_hass, "AA:BB:CC:DD:EE:FF", connectable=True)
    assert connect.await_args.args[1] is device
    controller.async_setup.assert_awaited_once()
    controller.authenticate.assert_awaited_once()
    assert runtime.state.authenticated is True
    assert runtime._ready_event.is_set()


@pytest.mark.asyncio
async def test_runtime_rejects_missing_connectable_route(mock_hass):
    runtime = CuktechBLERuntime(
        mock_hass,
        "AA:BB:CC:DD:EE:FF",
        "00112233445566778899aabb",
        MagicMock(),
    )
    with patch(
        "custom_components.cuktech_charger.ble_runtime.async_ble_device_from_address",
        return_value=None,
    ):
        with pytest.raises(ConnectionError, match="No connectable HA Bluetooth route"):
            await runtime._connect_and_consume()


@pytest.mark.asyncio
async def test_cmd_send_spec_notifications_do_not_interrupt_handshake():
    controller = CuktechBLEController.__new__(CuktechBLEController)
    controller.client = MagicMock(is_connected=True)
    controller.client.write_gatt_char = AsyncMock()
    controller._queues = {"cmd_send": __import__("asyncio").Queue()}
    controller._session_keys = {}
    controller._send_iteration = 0
    controller.encrypt = MagicMock(return_value=b"encrypted")
    controller.wait_notify = AsyncMock(
        side_effect=[
            b"spec-before-ready",
            b"\x00\x00\x01\x01",
            b"spec-before-ok",
            b"\x00\x00\x01\x00",
        ]
    )

    await controller._send_encrypted(b"command")

    assert controller.wait_notify.await_count == 4
    assert controller.client.write_gatt_char.await_count == 2
    assert controller.client.write_gatt_char.await_args_list[1].args[1] == b"\x01\x00encrypted"


@pytest.mark.asyncio
async def test_cmd_send_missing_ready_restores_unsent_nonce():
    controller = CuktechBLEController.__new__(CuktechBLEController)
    controller.client = MagicMock(is_connected=True)
    controller.client.write_gatt_char = AsyncMock()
    controller._send_iteration = 7
    controller.encrypt = MagicMock(
        side_effect=lambda _payload: setattr(controller, "_send_iteration", 8) or b"encrypted"
    )
    controller._wait_cmd_send_ack = AsyncMock(return_value=False)

    with pytest.raises(ConnectionError, match="did not become ready"):
        await controller._send_encrypted(b"command")

    assert controller._send_iteration == 7
    assert controller.client.write_gatt_char.await_count == 1


@pytest.mark.asyncio
async def test_set_command_completes_after_transport_ack_without_result():
    controller = CuktechBLEController.__new__(CuktechBLEController)
    controller.authenticated = True
    controller._miot_sequence = 4
    controller.drain_command_notifications = AsyncMock()
    controller._send_encrypted = AsyncMock()

    async def no_response(timeout):
        await __import__("asyncio").sleep(timeout)

    controller.receive_command_payload = AsyncMock(side_effect=no_response)
    result = await controller.send_miot_command(2, 16, 0x0E, timeout=0.01)

    assert result == {"piid": 16, "value": 0x0E, "raw": None, "transport_only": True}
    controller.receive_command_payload.assert_awaited()
    controller._send_encrypted.assert_awaited_once_with(controller._build_miot_tlv(4, 2, 16, 0x0E))


@pytest.mark.asyncio
async def test_port_control_uses_cached_value_when_get_fails(mock_hass):
    runtime = CuktechBLERuntime(
        mock_hass,
        "AA:BB:CC:DD:EE:FF",
        "00112233445566778899aabb",
        MagicMock(),
    )
    client = MagicMock(is_connected=True)
    controller = MagicMock(client=client)
    controller.send_miot_command = AsyncMock(side_effect=[None, {"piid": 16, "value": 14}])
    controller.drain_command_notifications = AsyncMock()
    runtime._client = client
    runtime._controller = controller
    runtime.state.authenticated = True
    runtime.state.update_setting(16, 0x0F)
    runtime._ready_event.set()
    runtime._emit_snapshot = AsyncMock()

    assert await runtime.async_port_control("c1", "off") == 0x0E

    assert [request.args for request in controller.send_miot_command.await_args_list] == [
        (2, 16),
        (2, 16, 0x0E),
    ]
    assert runtime.state.settings["16"] == 0x0E


@pytest.mark.asyncio
async def test_initial_settings_continues_after_one_channel_failure(mock_hass):
    runtime = CuktechBLERuntime(
        mock_hass,
        "AA:BB:CC:DD:EE:FF",
        "00112233445566778899aabb",
        MagicMock(),
    )
    client = MagicMock(is_connected=True)
    controller = MagicMock(client=client)
    responses = [ConnectionError("missing ACK")]
    responses.extend({"piid": piid, "value": piid} for piid in READABLE_SETTINGS_PIIDS[1:])
    controller.send_miot_command = AsyncMock(side_effect=responses)
    runtime._client = client
    runtime._controller = controller
    runtime.state.authenticated = True
    runtime._ready_event.set()

    with patch("custom_components.cuktech_charger.ble_runtime.asyncio.sleep", new=AsyncMock()):
        await runtime._read_initial_settings()

    assert controller.send_miot_command.await_count == len(READABLE_SETTINGS_PIIDS)
    assert str(READABLE_SETTINGS_PIIDS[0]) not in runtime.state.settings
    assert runtime.state.settings[str(READABLE_SETTINGS_PIIDS[1])] == READABLE_SETTINGS_PIIDS[1]


@pytest.mark.asyncio
async def test_command_waits_for_ready_session(mock_hass):
    runtime = CuktechBLERuntime(
        mock_hass,
        "AA:BB:CC:DD:EE:FF",
        "00112233445566778899aabb",
        MagicMock(),
    )
    client = MagicMock(is_connected=True)
    controller = MagicMock(client=client)
    runtime._client = client
    runtime._controller = controller
    runtime.state.authenticated = True
    operation = AsyncMock(return_value="ok")

    task = __import__("asyncio").create_task(runtime._execute_when_ready(operation, "test command"))
    await __import__("asyncio").sleep(0)
    operation.assert_not_awaited()
    runtime._ready_event.set()

    assert await task == "ok"
    operation.assert_awaited_once_with(controller)


@pytest.mark.asyncio
async def test_command_failure_does_not_disconnect_healthy_session(mock_hass):
    runtime = CuktechBLERuntime(
        mock_hass,
        "AA:BB:CC:DD:EE:FF",
        "00112233445566778899aabb",
        MagicMock(),
    )
    client = MagicMock(is_connected=True)
    client.disconnect = AsyncMock()
    controller = MagicMock(client=client)
    runtime._client = client
    runtime._controller = controller
    runtime.state.authenticated = True
    runtime._ready_event.set()

    async def operation(_controller):
        raise ConnectionError("transport response missing")

    with pytest.raises(ConnectionError, match="transport response missing"):
        await runtime._execute_when_ready(operation, "test command")

    client.disconnect.assert_not_awaited()
    assert runtime._disconnected_event.is_set() is False
    assert runtime._ready_event.is_set() is True
