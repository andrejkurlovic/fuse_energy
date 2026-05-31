"""Sensor platform for Fuse Energy+.

Two-device model:
  - Electricity Meter device (supply_fid as identifier)
  - Gas Meter device (supply_fid as identifier)
  - Account device (premises_fid as identifier)

Each meter device carries its own today/cost/tariff/rate sensors.
History statistics are injected separately via async_add_external_statistics.
"""
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

from .const import DOMAIN, SUPPLY_ELECTRICITY, SUPPLY_GAS
from .coordinator import FuseEnergyCoordinator, FuseEnergyData, FuseSupplyData


# ---------------------------------------------------------------------------
# Sensor descriptors — one set per supply type
# ---------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class FuseSupplySensorDescription(SensorEntityDescription):
    """Sensor that reads from FuseSupplyData."""
    value_fn: Callable[[FuseSupplyData], float | str | None] = lambda _: None
    unit_fn: Callable[[FuseSupplyData], str | None] = lambda _: None


_SUPPLY_SENSORS: tuple[FuseSupplySensorDescription, ...] = (
    # Today's usage — daily snapshot, no state_class (not cumulative)
    FuseSupplySensorDescription(
        key="kwh_today",
        name="Today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: d.kwh_today,
        suggested_display_precision=2,
    ),
    # Yesterday — useful when today gas=0 (smart meter lag)
    FuseSupplySensorDescription(
        key="kwh_yesterday",
        name="Yesterday",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: d.kwh_yesterday,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
    ),
    # Today's cost
    FuseSupplySensorDescription(
        key="cost_today",
        name="Cost today",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda d: d.cost_today,
        unit_fn=lambda _: "GBP",
        suggested_display_precision=2,
    ),
    # Tariff name
    FuseSupplySensorDescription(
        key="tariff",
        name="Tariff",
        value_fn=lambda d: d.tariff_title,
    ),
    # Unit rate (£/kWh) — from tariff_details
    FuseSupplySensorDescription(
        key="unit_rate",
        name="Unit rate",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda d: d.unit_rate,
        unit_fn=lambda _: "GBP",
        suggested_display_precision=4,
    ),
    # Standing charge (£/day) — from tariff_details
    FuseSupplySensorDescription(
        key="standing_charge",
        name="Standing charge",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda d: d.standing_charge,
        unit_fn=lambda _: "GBP",
        suggested_display_precision=4,
    ),
)


@dataclass(frozen=True, kw_only=True)
class FuseAccountSensorDescription(SensorEntityDescription):
    """Sensor that reads from FuseEnergyData (account level)."""
    value_fn: Callable[[FuseEnergyData], float | str | None] = lambda _: None
    unit_fn: Callable[[FuseEnergyData], str | None] = lambda _: None


_ACCOUNT_SENSORS: tuple[FuseAccountSensorDescription, ...] = (
    FuseAccountSensorDescription(
        key="balance",
        name="Balance",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.balance,
        unit_fn=lambda d: d.balance_currency or "GBP",
        suggested_display_precision=2,
    ),
)


# ---------------------------------------------------------------------------
# Entity classes
# ---------------------------------------------------------------------------

class FuseSupplySensor(CoordinatorEntity[FuseEnergyCoordinator], SensorEntity):
    """One sensor attached to a specific supply (electricity or gas meter)."""

    _attr_has_entity_name = True
    entity_description: FuseSupplySensorDescription

    def __init__(
        self,
        coordinator: FuseEnergyCoordinator,
        description: FuseSupplySensorDescription,
        supply_data: FuseSupplyData,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._supply_fid = supply_data.supply.supply_fid
        self._attr_unique_id = f"{DOMAIN}_{self._supply_fid}_{description.key}"

    def _get_supply_data(self) -> FuseSupplyData | None:
        for sd in self.coordinator.data.supplies:
            if sd.supply.supply_fid == self._supply_fid:
                return sd
        return None

    @property
    def device_info(self) -> DeviceInfo:
        sd = self._get_supply_data()
        if sd is None:
            return DeviceInfo(identifiers={(DOMAIN, self._supply_fid)})
        s = sd.supply
        is_elec = SUPPLY_ELECTRICITY in s.supply_type
        return DeviceInfo(
            identifiers={(DOMAIN, s.supply_fid)},
            name=f"{'Electricity' if is_elec else 'Gas'} Meter",
            manufacturer="Fuse Energy",
            model=f"Smart {'Electricity' if is_elec else 'Gas'} Meter",
            serial_number=s.serial_number or None,
            hw_version=s.meter_type or None,
            via_device=(DOMAIN, s.premises_fid),
        )

    @property
    def extra_state_attributes(self) -> dict:
        sd = self._get_supply_data()
        if sd is None:
            return {}
        s = sd.supply
        attrs: dict = {}
        if SUPPLY_ELECTRICITY in s.supply_type and s.identifier:
            attrs["mpan"] = s.identifier
        elif SUPPLY_GAS in s.supply_type and s.identifier:
            attrs["mprn"] = s.identifier
        if s.serial_number:
            attrs["serial_number"] = s.serial_number
        if s.meter_type:
            attrs["meter_type"] = s.meter_type
        if s.meter_status:
            attrs["meter_status"] = s.meter_status
        return attrs

    @property
    def native_value(self) -> float | str | None:
        sd = self._get_supply_data()
        if sd is None:
            return None
        return self.entity_description.value_fn(sd)

    @property
    def native_unit_of_measurement(self) -> str | None:
        sd = self._get_supply_data()
        unit = self.entity_description.unit_fn(sd) if sd else None
        return unit or self.entity_description.native_unit_of_measurement


class FuseAccountSensor(CoordinatorEntity[FuseEnergyCoordinator], SensorEntity):
    """Account-level sensor (balance) under the property device."""

    _attr_has_entity_name = True
    entity_description: FuseAccountSensorDescription

    def __init__(
        self,
        coordinator: FuseEnergyCoordinator,
        description: FuseAccountSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        d = coordinator.data
        self._attr_unique_id = f"{DOMAIN}_{d.premises_fid}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        d = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, d.premises_fid or "fuse_energy_account")},
            name=d.premises_name or "Fuse Energy",
            manufacturer="Fuse Energy",
            model="Energy Account",
            suggested_area=d.premises_name,
        )

    @property
    def native_value(self) -> float | str | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def native_unit_of_measurement(self) -> str | None:
        unit = self.entity_description.unit_fn(self.coordinator.data)
        return unit or self.entity_description.native_unit_of_measurement


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FuseEnergyCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []

    # One set of supply sensors per meter
    for sd in coordinator.data.supplies:
        for desc in _SUPPLY_SENSORS:
            entities.append(FuseSupplySensor(coordinator, desc, sd))

    # Account-level sensors
    for desc in _ACCOUNT_SENSORS:
        entities.append(FuseAccountSensor(coordinator, desc))

    async_add_entities(entities)
