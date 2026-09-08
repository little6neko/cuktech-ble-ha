"""Config flow for the CUKTECH Charger Bluetooth integration."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult

from .ble_protocol import parse_hex_secret
from .const import CONF_BLE_KEY, CONF_MAC, CONF_TOKEN, DOMAIN

DEFAULT_NAME = "CUKTECH 10 GaN Charger Ultra"
MAC_PATTERN = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Optional(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)): str,
            vol.Required(CONF_MAC, default=defaults.get(CONF_MAC, "XX:XX:XX:XX:XX:XX")): str,
            vol.Required(CONF_TOKEN, default=defaults.get(CONF_TOKEN, "")): str,
            vol.Optional(CONF_BLE_KEY, default=defaults.get(CONF_BLE_KEY, "")): str,
        }
    )


def validate_input(data: dict[str, Any]) -> dict[str, str]:
    """Normalize and validate credentials without requiring the device online."""
    address = data[CONF_MAC].strip().upper().replace("-", ":")
    if not MAC_PATTERN.fullmatch(address):
        raise ValueError("invalid_mac")
    parse_hex_secret(data[CONF_TOKEN], 12, CONF_TOKEN)
    if data.get(CONF_BLE_KEY, "").strip():
        parse_hex_secret(data[CONF_BLE_KEY], 16, CONF_BLE_KEY)
    data[CONF_MAC] = address
    data[CONF_TOKEN] = data[CONF_TOKEN].strip().lower()
    data[CONF_BLE_KEY] = data.get(CONF_BLE_KEY, "").strip().lower()
    return {"title": data.get(CONF_NAME, DEFAULT_NAME)}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle CUKTECH Charger configuration."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = validate_input(user_input)
            except ValueError as err:
                reason = str(err)
                errors["base"] = reason if reason in {"invalid_mac"} else "invalid_credentials"
            else:
                await self.async_set_unique_id(user_input[CONF_MAC])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info["title"], data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema(user_input), errors=errors)
