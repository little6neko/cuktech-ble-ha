"""Run separately from tests/: use real HA, mocking only the Bluetooth session."""

import asyncio
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry,
    device_registry,
    entity_registry,
    floor_registry,
    label_registry,
)

from custom_components.cuktech_charger.ble_runtime import CuktechBLERuntime
from custom_components.cuktech_charger.const import DOMAIN


@pytest.mark.parametrize("shutdown", [False, True])
@pytest.mark.asyncio
async def test_real_ha_setup_entities_updates_and_unload(tmp_path, shutdown):
    hass = HomeAssistant(str(tmp_path))
    hass.config.skip_pip = True
    (tmp_path / "custom_components").symlink_to(
        Path(__file__).resolve().parents[1] / "custom_components", target_is_directory=True
    )
    loader.async_setup(hass)
    for registry in (
        floor_registry,
        label_registry,
        area_registry,
        device_registry,
        entity_registry,
    ):
        await registry.async_load(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    entry = ConfigEntry(
        version=2,
        minor_version=1,
        domain=DOMAIN,
        title="CUKTECH",
        data={"name": "CUKTECH", "mac": "AA:BB:CC:DD:EE:FF", "token": "00" * 12},
        options={},
        source="user",
        unique_id="AA:BB:CC:DD:EE:FF",
        discovery_keys=MappingProxyType({}),
    )
    started = asyncio.Event()

    async def session(runtime):
        started.set()
        await asyncio.Event().wait()

    try:
        with (
            patch.object(CuktechBLERuntime, "_connect_and_consume", session),
            patch(
                "homeassistant.setup._async_process_dependencies", new=AsyncMock(return_value=[])
            ),
        ):
            await hass.config_entries.async_add(entry)
            await asyncio.wait_for(started.wait(), 5)
            # An infinite BLE task must not block HA's normal startup task drain.
            await asyncio.wait_for(hass.async_block_till_done(), 10)
            assert entry.state is ConfigEntryState.LOADED
            coordinator = hass.data[DOMAIN][entry.entry_id]
            states = hass.states.async_all()
            # 17 sensors, 18 switches, 5 binary sensors, 3 selects, 4 numbers.
            assert len(states) == 47
            await coordinator._async_handle_snapshot(
                {
                    "connected": True,
                    "authenticated": True,
                    "ports": {
                        "c1": {
                            "power": 9.0,
                            "voltage": 9.0,
                            "current": 1.0,
                            "protocol": "UFCS",
                            "active": True,
                        }
                    },
                    "settings": {"6": 5, "16": 15, "21": 0x03030F0F},
                }
            )
            assert any(state.state == "UFCS" for state in hass.states.async_all("sensor"))
            assert any(state.state == "1分钟" for state in hass.states.async_all("select"))
            task = coordinator._runtime._run_task
            if shutdown:
                await hass.async_stop(force=True)
                assert task.done()
                assert not coordinator._runtime.running
                return
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
            assert task.done()
            assert coordinator._callbacks == []
            assert entry.entry_id not in hass.data[DOMAIN]
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_real_config_flow_schema_and_validation(tmp_path):
    from custom_components.cuktech_charger.config_flow import ConfigFlow

    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    flow = ConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.context = {"source": "user"}
    try:
        form = await flow.async_step_user()
        data = form["data_schema"]({"mac": "aa-bb-cc-dd-ee-ff", "token": "00" * 12})
        result = await flow.async_step_user(data)
        assert result["type"] == "create_entry"
        assert result["data"]["mac"] == "AA:BB:CC:DD:EE:FF"
        assert result["version"] == 2
        invalid = dict(data, token="invalid")
        result = await flow.async_step_user(invalid)
        assert result["errors"] == {"base": "invalid_credentials"}
    finally:
        await hass.async_stop(force=True)
