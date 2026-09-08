"""Shared lifecycle for direct Bluetooth entities."""

from __future__ import annotations

from homeassistant.core import callback

from .const import DOMAIN


class CuktechBaseEntity:
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator, entry) -> None:
        super().__init__()
        self.coordinator = coordinator
        self._entry = entry

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.register_callback(self._update)

    async def async_will_remove_from_hass(self) -> None:
        self.coordinator.unregister_callback(self._update)
        await super().async_will_remove_from_hass()

    @callback
    def _update(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def device_info(self) -> dict:
        return {"identifiers": {(DOMAIN, self._entry.entry_id)}, **self.coordinator.device_info}

    @property
    def available(self) -> bool:
        return self.coordinator.available
