"""Sensor platform for Fuse Energy+."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FuseEnergyCoordinator, FuseEnergyData


@dataclass(frozen=True, kw_only=True)
class FuseSensorDescription(SensorEntityDescription):
    value_fn: Callable[[FuseEnergyData], float | str | None] = lambda _: None
    unit_fn: Callable[[FuseEnergyData], str | None] = lambda _: None


_SENSORS: tuple[FuseSensorDescription, ...] = (
    # Electricity — today
    FuseSensorDescription(
        key="electricity_kwh_today",
        translation_key="electricity_kwh_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: d.electricity_kwh_today,
        suggested_display_precision=2,
    ),
    FuseSensorDescription(
        key="electricity_cost_today",
        translation_key="electricity_cost_today",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.electricity_cost_today,
        unit_fn=lambda d: d.balance_currency or "GBP",
        suggested_display_precision=2,
    ),
    # Gas — today
    FuseSensorDescription(
        key="gas_kwh_today",
        translation_key="gas_kwh_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: d.gas_kwh_today,
        suggested_display_precision=2,
    ),
    FuseSensorDescription(
        key="gas_cost_today",
        translation_key="gas_cost_today",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.gas_cost_today,
        unit_fn=lambda d: d.balance_currency or "GBP",
        suggested_display_precision=2,
    ),
    # Account balance
    FuseSensorDescription(
        key="balance",
        translation_key="balance",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.balance,
        unit_fn=lambda d: d.balance_currency or "GBP",
        suggested_display_precision=2,
    ),
    # Electricity tariff
    FuseSensorDescription(
        key="electricity_tariff_title",
        translation_key="electricity_tariff_title",
        value_fn=lambda d: d.electricity_tariff_title,
    ),
    FuseSensorDescription(
        key="electricity_unit_rate",
        translation_key="electricity_unit_rate",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.electricity_unit_rate,
        unit_fn=lambda d: d.balance_currency or "GBP",
        suggested_display_precision=4,
    ),
    FuseSensorDescription(
        key="electricity_standing_charge",
        translation_key="electricity_standing_charge",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.electricity_standing_charge,
        unit_fn=lambda d: d.balance_currency or "GBP",
        suggested_display_precision=4,
    ),
    # Gas tariff
    FuseSensorDescription(
        key="gas_tariff_title",
        translation_key="gas_tariff_title",
        value_fn=lambda d: d.gas_tariff_title,
    ),
    FuseSensorDescription(
        key="gas_unit_rate",
        translation_key="gas_unit_rate",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.gas_unit_rate,
        unit_fn=lambda d: d.balance_currency or "GBP",
        suggested_display_precision=4,
    ),
    FuseSensorDescription(
        key="gas_standing_charge",
        translation_key="gas_standing_charge",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.gas_standing_charge,
        unit_fn=lambda d: d.balance_currency or "GBP",
        suggested_display_precision=4,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FuseEnergyCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(FuseEnergySensor(coordinator, desc) for desc in _SENSORS)


class FuseEnergySensor(CoordinatorEntity[FuseEnergyCoordinator], SensorEntity):
    """One Fuse Energy sensor."""

    _attr_has_entity_name = True
    entity_description: FuseSensorDescription

    def __init__(
        self,
        coordinator: FuseEnergyCoordinator,
        description: FuseSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{coordinator.data.premises_fid}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        d = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, d.premises_fid or "fuse_energy")},
            name=d.premises_name or "Fuse Energy",
            manufacturer="Fuse Energy",
        )

    @property
    def native_value(self) -> float | str | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def native_unit_of_measurement(self) -> str | None:
        unit = self.entity_description.unit_fn(self.coordinator.data)
        if unit:
            return unit
        return self.entity_description.native_unit_of_measurement
