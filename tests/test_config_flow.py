"""Tests for the direct Bluetooth config flow."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.cuktech_charger.config_flow import ConfigFlow, validate_input
from custom_components.cuktech_charger.const import CONF_BLE_KEY, CONF_MAC, CONF_TOKEN, DOMAIN

VALID = {
    "name": "Test Charger",
    CONF_MAC: "aa-bb-cc-dd-ee-ff",
    CONF_TOKEN: "00112233445566778899aabb",
    CONF_BLE_KEY: "",
}


def test_validate_input_normalizes_values():
    data = dict(VALID)
    assert validate_input(data) == {"title": "Test Charger"}
    assert data[CONF_MAC] == "AA:BB:CC:DD:EE:FF"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (CONF_MAC, "not-a-mac"),
        (CONF_TOKEN, "0011"),
        (CONF_TOKEN, "z" * 24),
        (CONF_BLE_KEY, "0011"),
    ],
)
def test_validate_input_rejects_invalid_values(field, value):
    data = dict(VALID)
    data[field] = value
    with pytest.raises(ValueError):
        validate_input(data)


def _make_flow():
    flow = ConfigFlow.__new__(ConfigFlow)
    flow.hass = MagicMock()
    flow.context = {}
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    return flow


@pytest.mark.asyncio
async def test_step_user_shows_form():
    flow = _make_flow()
    assert await flow.async_step_user() == {"type": "form"}


@pytest.mark.asyncio
async def test_step_user_creates_entry():
    flow = _make_flow()
    result = await flow.async_step_user(dict(VALID))
    assert result == {"type": "create_entry"}
    flow.async_set_unique_id.assert_awaited_once_with("AA:BB:CC:DD:EE:FF")
    assert flow.async_create_entry.call_args.kwargs["data"][CONF_TOKEN] == VALID[CONF_TOKEN]


@pytest.mark.asyncio
async def test_step_user_reports_invalid_credentials():
    flow = _make_flow()
    data = dict(VALID)
    data[CONF_TOKEN] = "bad"
    await flow.async_step_user(data)
    assert flow.async_show_form.call_args.kwargs["errors"]["base"] == "invalid_credentials"


def test_domain():
    assert DOMAIN == "cuktech_charger"
