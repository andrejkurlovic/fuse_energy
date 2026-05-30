"""DataUpdateCoordinator for FUSE Energy."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import FuseEnergyAPI, FuseError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_SCAN_INTERVAL = timedelta(minutes=30)


@dataclass
class FuseSupply:
    supply_type: str
    premises_fid: str
    premises_name: str
    supply_id: str = ""
    mpan_mprn: str = ""
    meter_serial: str = ""


@dataclass
class FusePremisesData:
    balance: float | None = None
    balance_currency: str = "GBP"
    supplies: list[FuseSupply] = field(default_factory=list)
    tariff_name: str | None = None
    tariff_standing_charge: float | None = None
    tariff_unit_rate: float | None = None
    tariff_unit_rate_gas: float | None = None
    bill_amount: float | None = None
    bill_currency: str = "GBP"
    direct_debit_status: str | None = None
    energy_consumption_kwh: float | None = None
    gas_consumption_kwh: float | None = None


class FuseEnergyCoordinator(DataUpdateCoordinator[FusePremisesData]):
    def __init__(self, hass: HomeAssistant, api: FuseEnergyAPI) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=_SCAN_INTERVAL)
        self.api = api

    async def _async_update_data(self) -> FusePremisesData:
        data = FusePremisesData()

        try:
            premises = await self.api.get_premises()
        except FuseError as err:
            raise UpdateFailed(str(err)) from err

        supplies: list[FuseSupply] = []
        first_fid = ""
        premises_name = ""
        if isinstance(premises, list):
            for p in premises:
                fid = p.get("fid", "") or p.get("premises_fid", "") or p.get("id", "")
                name = p.get("name", "") or p.get("address", "")
                if not first_fid:
                    first_fid = fid
                    premises_name = name
                for s in p.get("supplies", []):
                    stype_raw = s.get("supply_type", "") or s.get("supplyType", "")
                    stype = stype_raw.upper()
                    supplies.append(FuseSupply(
                        supply_type=stype,
                        premises_fid=fid,
                        premises_name=name,
                        supply_id=str(s.get("id", "")),
                        mpan_mprn=s.get("mpan_mprn", "") or s.get("mpan", "") or s.get("mprn", ""),
                        meter_serial=s.get("meter_serial", "") or s.get("serial_number", ""),
                    ))
        data.supplies = supplies
        data.premises_name = premises_name

        try:
            balance_data = await self.api.get_balance()
            if isinstance(balance_data, dict):
                data.balance = balance_data.get("amount") or balance_data.get("value")
                data.balance_currency = balance_data.get("currency", "GBP")
        except FuseError:
            _LOGGER.warning("Failed to fetch balance")

        try:
            tariff_data = await self.api.get_tariff_details()
            if isinstance(tariff_data, dict):
                rates = tariff_data.get("rates", tariff_data)
                if isinstance(rates, list):
                    for rate in rates:
                        stype = (rate.get("supply_type", "") or rate.get("supplyType", "")).upper()
                        if "ELECTRICITY" in stype or not data.tariff_unit_rate:
                            data.tariff_name = rate.get("tariff_name") or rate.get("name")
                            data.tariff_standing_charge = rate.get("standing_charge") or rate.get("standingCharge")
                            data.tariff_unit_rate = rate.get("unit_rate") or rate.get("unitRate")
                        if "GAS" in stype:
                            data.tariff_unit_rate_gas = rate.get("unit_rate") or rate.get("unitRate")
                elif isinstance(rates, dict):
                    data.tariff_name = rates.get("tariff_name") or rates.get("name")
                    data.tariff_standing_charge = rates.get("standing_charge") or rates.get("standingCharge")
                    data.tariff_unit_rate = rates.get("unit_rate") or rates.get("unitRate")
        except FuseError:
            _LOGGER.warning("Failed to fetch tariff details")

        if first_fid:
            try:
                chart = await self.api.get_chart(first_fid, year=0)
                if isinstance(chart, dict):
                    total_kwh = chart.get("total_kwh") or chart.get("totalKwh")
                    if total_kwh is not None:
                        data.energy_consumption_kwh = float(total_kwh)
            except FuseError:
                _LOGGER.warning("Failed to fetch chart data")

            try:
                bill_data = await self.api.get_bill(first_fid)
                if isinstance(bill_data, dict):
                    data.bill_amount = bill_data.get("amount") or bill_data.get("total_amount")
                    data.bill_currency = bill_data.get("currency", "GBP")
            except FuseError:
                _LOGGER.warning("Failed to fetch bill data")

        try:
            dd_data = await self.api.get_direct_debit_status()
            if isinstance(dd_data, dict):
                data.direct_debit_status = dd_data.get("status") or dd_data.get("direct_debit_status")
        except FuseError:
            _LOGGER.warning("Failed to fetch direct debit status")

        return data
