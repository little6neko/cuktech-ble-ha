"""Select platform for CUKTECH Charger."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CuktechBluetoothCoordinator
from .base_entity import CuktechBaseEntity
from .const import DOMAIN, PIID_DISPLAY, SELECT_OPTION_MAP, SELECT_PIIDS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up CUKTECH Charger selects from a config entry."""
    coord = hass.data[DOMAIN][entry.entry_id]
    entities = [
        CuktechSelect(coord, entry, piid, cfg["name"], cfg["icon"], cfg["options"])
        for piid, cfg in SELECT_PIIDS.items()
    ]
    async_add_entities(entities)


class CuktechSelect(CuktechBaseEntity, SelectEntity):
    """Select entity for CUKTECH Charger settings."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coord: CuktechBluetoothCoordinator,
        entry: ConfigEntry,
        piid: int,
        name: str,
        icon: str,
        options: list[str],
    ) -> None:
        """Initialize the select entity."""
        super().__init__(coord, entry)
        self._piid = piid
        self._attr_unique_id = f"{entry.entry_id}_select_{piid}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_options = options

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.available

    @property
    def current_option(self) -> str | None:
        """Return the current option."""
        if not self.coordinator.data:
            return None
        v = self.coordinator.data.get(str(self._piid))
        if v is None:
            return None
        display = PIID_DISPLAY.get(self._piid, {}).get(v)
        if display is not None:
            return display
        return None

    async def async_select_option(self, option: str) -> None:
        """Select an option."""
        option_map = SELECT_OPTION_MAP.get(self._piid, {})
        value = option_map.get(option)
        if value is not None:
            await self.coordinator.async_set_value(self._piid, value)
