"""Tests for the direct Bluetooth coordinator."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.cuktech_charger import CuktechBluetoothCoordinator


@pytest.fixture
def coordinator(mock_hass, mock_entry):
    with patch("custom_components.cuktech_charger.CuktechBLERuntime") as runtime_cls:
        runtime = runtime_cls.return_value
        runtime.running = True
        runtime.async_start = AsyncMock()
        runtime.async_stop = AsyncMock()
        runtime.async_set_value = AsyncMock()
        runtime.async_set_protocol = AsyncMock()
        runtime.async_port_control = AsyncMock()
        yield CuktechBluetoothCoordinator(mock_hass, mock_entry)


def test_initial_state(coordinator):
    assert coordinator.available is False
    assert coordinator.ble_enabled is True
    assert coordinator.port_data == {}
    assert coordinator.data == {}


def test_data_returns_copies(coordinator):
    coordinator._settings = {"5": 1}
    coordinator._port_data = {"1": {"power": 10.0}}
    assert coordinator.data == {"5": 1}
    assert coordinator.data is not coordinator._settings
    assert coordinator.port_data["1"] is not coordinator._port_data["1"]


@pytest.mark.asyncio
async def test_snapshot_updates_state(coordinator):
    callback = MagicMock()
    coordinator.register_callback(callback)
    await coordinator._async_handle_snapshot(
        {
            "connected": True,
            "authenticated": True,
            "ports": {"c1": {"power": 40.0}},
            "settings": {"16": 15, "21": 9},
            "model": "test-model",
            "firmware": "1.2.3",
        }
    )
    assert coordinator.available is True
    assert coordinator.ble_connected is True
    assert coordinator.port_data["1"]["power"] == 40.0
    assert coordinator.data["16"] == 15
    assert coordinator.device_info["model"] == "test-model"
    callback.assert_called_once()


def test_callback_registration(coordinator):
    callback = MagicMock()
    coordinator.register_callback(callback)
    coordinator.register_callback(callback)
    assert coordinator._callbacks == [callback]
    coordinator.unregister_callback(callback)
    assert coordinator._callbacks == []


@pytest.mark.asyncio
async def test_setup_and_unload(coordinator):
    await coordinator.async_setup()
    coordinator._runtime.async_start.assert_awaited_once()
    await coordinator.async_unload()
    coordinator._runtime.async_stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_enable_disable(coordinator):
    await coordinator.async_enable_ble(False)
    coordinator._runtime.async_stop.assert_awaited_once()
    assert coordinator.ble_enabled is False
    assert coordinator.ble_pending is False

    coordinator._runtime.running = False
    await coordinator.async_enable_ble(True)
    coordinator._runtime.async_start.assert_awaited_once()
    assert coordinator.ble_enabled is True


@pytest.mark.asyncio
async def test_set_value_and_port_control(coordinator):
    await coordinator.async_set_value(5, 2)
    coordinator._runtime.async_set_value.assert_awaited_once_with(5, 2)
    await coordinator.async_port_control("c1", "off")
    coordinator._runtime.async_port_control.assert_awaited_once_with("c1", "off")


def test_protocol_switches_and_encoding(coordinator):
    coordinator._settings = {"21": 0x0201080F}
    switches = coordinator.protocol_switches
    assert switches["c1"] == {"pd": True, "pps": True, "ufcs": True}
    assert switches["c2"] == {"pd": False, "pps": False, "ufcs": False}
    assert switches["c3"] == {"ufcs": True, "scp": False}
    assert switches["a"] == {"ufcs": False, "scp": True}
    assert coordinator._encode_protocol_extend(switches) == 0x0201080F


@pytest.mark.asyncio
async def test_set_protocol(coordinator):
    coordinator._settings = {"21": 0x03030F0F}
    await coordinator.async_set_protocol("c1", "pd", False)
    coordinator._runtime.async_set_protocol.assert_awaited_once_with("c1", "pd", False)


@pytest.mark.asyncio
async def test_unknown_protocol_is_ignored(coordinator):
    await coordinator.async_set_protocol("invalid", "pd", True)
    coordinator._runtime.async_set_value.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_connection_toggle_rolls_back(coordinator):
    coordinator._runtime.async_stop.side_effect = ConnectionError("failed")
    with pytest.raises(ConnectionError):
        await coordinator.async_enable_ble(False)
    assert coordinator.ble_enabled is True
    assert coordinator.ble_pending is False


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1.5, "bad", None])
@pytest.mark.asyncio
async def test_invalid_number_is_not_sent(coordinator, value):
    with pytest.raises(ValueError):
        await coordinator.async_set_value(9, value)
    coordinator._runtime.async_set_value.assert_not_awaited()
