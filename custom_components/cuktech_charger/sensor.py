"""Sensor platform for CUKTECH Charger."""

from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CuktechBluetoothCoordinator
from .base_entity import CuktechBaseEntity
from .const import DOMAIN, PORT_NAMES

_LOGGER = logging.getLogger(__name__)

PROTOCOL_OPTIONS = ["idle", "5V", "QC", "AFC", "FCP", "SCP", "PD", "PPS", "UFCS", "Unknown"]


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up CUKTECH Charger sensors from a config entry."""
    coord = hass.data[DOMAIN][entry.entry_id]
    entities = []

    for piid, pname in PORT_NAMES.items():
        for st in ("voltage", "current", "power"):
            entities.append(CuktechPortSensor(coord, entry, piid, pname, st))
        entities.append(CuktechPortProtocolSensor(coord, entry, piid, pname))

    entities.append(CuktechTotalPowerSensor(coord, entry))
    async_add_entities(entities)


class CuktechPortSensor(CuktechBaseEntity, SensorEntity):
    """Sensor for CUKTECH Charger port data."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    UNITS = {
        "voltage": UnitOfElectricPotential.VOLT,
        "current": UnitOfElectricCurrent.AMPERE,
        "power": UnitOfPower.WATT,
    }

    def __init__(
        self,
        coord: CuktechBluetoothCoordinator,
        entry: ConfigEntry,
        piid: int,
        port_name: str,
        sensor_type: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coord, entry)
        self._piid = piid
        self._port_name = port_name
        self._sensor_type = sensor_type
        self._attr_unique_id = f"{entry.entry_id}_port_{piid}_{sensor_type}"
        self._attr_name = f"{port_name} {sensor_type}"
        self._attr_native_unit_of_measurement = self.UNITS.get(sensor_type)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.available

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        pd = self.coordinator.port_data.get(str(self._piid))
        if pd is None:
            return None
        return _finite_number(pd.get(self._sensor_type))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        pd = self.coordinator.port_data.get(str(self._piid))
        if pd is None:
            return {}
        return {"port": self._port_name, "active": pd.get("active", False)}


class CuktechTotalPowerSensor(CuktechBaseEntity, SensorEntity):
    """Sensor for total power consumption."""

    _attr_has_entity_name = True
    _attr_name = "Total Power"
    _attr_icon = "mdi:flash"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: CuktechBluetoothCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_total_power"

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.available

    @property
    def native_value(self) -> float:
        """Return the total power."""
        total = 0.0
        for k in ("1", "2", "3", "4"):
            pd = self.coordinator.port_data.get(k)
            if pd and pd.get("active"):
                total += _finite_number(pd.get("power")) or 0.0
        return round(total, 1)


class CuktechPortProtocolSensor(CuktechBaseEntity, SensorEntity):
    """Sensor for CUKTECH Charger port protocol."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PROTOCOL_OPTIONS

    def __init__(
        self,
        coord: CuktechBluetoothCoordinator,
        entry: ConfigEntry,
        piid: int,
        port_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coord, entry)
        self._piid = piid
        self._port_name = port_name
        self._attr_unique_id = f"{entry.entry_id}_port_{piid}_protocol"
        self._attr_name = f"{port_name} Protocol"
        self._attr_icon = "mdi:usb-c-port"

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.available

    @property
    def native_value(self) -> str | None:
        """Return the current protocol."""
        pd = self.coordinator.port_data.get(str(self._piid))
        if pd is None:
            return None
        protocol = pd.get("protocol", "idle")
        if protocol in PROTOCOL_OPTIONS:
            return protocol
        return "Unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        pd = self.coordinator.port_data.get(str(self._piid))
        if pd is None:
            return {}
        return {"port": self._port_name, "active": pd.get("active", False)}
