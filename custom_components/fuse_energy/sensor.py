"""Sensor platform for FUSE Energy."""
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
from .coordinator import FuseEnergyCoordinator, FusePremisesData


@dataclass(frozen=True, kw_only=True)
class FuseSensorDescription(SensorEntityDescription):
    value_fn: Callable[[FusePremisesData], float | str | None] = lambda _: None


_SENSORS: tuple[FuseSensorDescription, ...] = (
    FuseSensorDescription(
        key="balance",
        translation_key="balance",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.balance,
    ),
    FuseSensorDescription(
        key="energy_consumption",
        translation_key="energy_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: d.energy_consumption_kwh,
    ),
    FuseSensorDescription(
        key="gas_consumption",
        translation_key="gas_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: d.gas_consumption_kwh,
    ),
    FuseSensorDescription(
        key="current_tariff",
        translation_key="current_tariff",
        device_class=SensorDeviceClass.ENUM,
        options=["unknown"],
        value_fn=lambda d: d.tariff_name,
    ),
    FuseSensorDescription(
        key="current_tariff_standing_charge",
        translation_key="current_tariff_standing_charge",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.tariff_standing_charge,
    ),
    FuseSensorDescription(
        key="current_tariff_unit_rate",
        translation_key="current_tariff_unit_rate",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.tariff_unit_rate,
    ),
    FuseSensorDescription(
        key="direct_debit_status",
        translation_key="direct_debit_status",
        value_fn=lambda d: d.direct_debit_status,
    ),
    FuseSensorDescription(
        key="bill_amount",
        translation_key="bill_amount",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.bill_amount,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FuseEnergyCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(FuseEnergySensor(coordinator, desc) for desc in _SENSORS)


class FuseEnergySensor(CoordinatorEntity[FuseEnergyCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: FuseSensorDescription

    def __init__(
        self,
        coordinator: FuseEnergyCoordinator,
        description: FuseSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"fuse_energy_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, "fuse_energy")},
            name=self.coordinator.data.premises_name or "FUSE Energy",
            manufacturer="FUSE Energy",
        )

    @property
    def native_value(self) -> float | str | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.device_class == SensorDeviceClass.MONETARY:
            if self.entity_description.key == "balance":
                return self.coordinator.data.balance_currency or "GBP"
            if self.entity_description.key == "bill_amount":
                return self.coordinator.data.bill_currency or "GBP"
            return "GBP"
        return self.entity_description.native_unit_of_measurement
