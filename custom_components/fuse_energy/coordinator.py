"""DataUpdateCoordinator for Fuse Energy+."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import FuseAuthError, FuseEnergyAPI, FuseError
from .const import DOMAIN, SUPPLY_ELECTRICITY, SUPPLY_GAS

_LOGGER = logging.getLogger(__name__)
_SCAN_INTERVAL = timedelta(hours=1)


@dataclass
class FuseSupply:
    """One supply (electricity or gas meter) under a premises."""
    supply_fid: str
    supply_type: str          # ELECTRICITY_IMPORT / GAS
    identifier: str           # MPAN (elec) or MPRN (gas)
    serial_number: str
    meter_type: str           # SMART / TRADITIONAL
    premises_fid: str
    premises_name: str


@dataclass
class FuseEnergyData:
    """Snapshot of all data fetched in one coordinator cycle."""
    premises_fid: str = ""
    premises_name: str = ""
    supplies: list[FuseSupply] = field(default_factory=list)

    # Today's consumption from chart (year+month daily bars)
    electricity_kwh_today: float | None = None
    electricity_cost_today: float | None = None
    gas_kwh_today: float | None = None
    gas_cost_today: float | None = None

    # Account balance (GET api/v1/balance)
    balance: float | None = None
    balance_currency: str = "GBP"

    # Tariff — electricity supply
    electricity_tariff_title: str | None = None
    electricity_unit_rate: float | None = None       # £/kWh
    electricity_standing_charge: float | None = None  # £/day

    # Tariff — gas supply
    gas_tariff_title: str | None = None
    gas_unit_rate: float | None = None
    gas_standing_charge: float | None = None


def _extract_fid(premises_obj: dict) -> str:
    return (
        premises_obj.get("fid")
        or premises_obj.get("premises_fid")
        or premises_obj.get("id")
        or ""
    )


def _extract_address(premises_obj: dict) -> str:
    addr = premises_obj.get("address") or {}
    parts = [
        addr.get("street_line1") or addr.get("streetLine1"),
        addr.get("postcode"),
    ]
    return ", ".join(p for p in parts if p) or premises_obj.get("name", "")


def _money_amount(obj: dict | None) -> float | None:
    if not isinstance(obj, dict):
        return None
    v = obj.get("amount") or obj.get("value")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _kwh_value(obj: dict | None) -> float | None:
    if not isinstance(obj, dict):
        return None
    v = (
        obj.get("decimal_value")
        or obj.get("decimalValue")
        or obj.get("value")
    )
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _today_bar(bars: list, today: date) -> dict | None:
    for bar in bars:
        idx = bar.get("index") or {}
        try:
            if (
                int(idx.get("year", 0)) == today.year
                and int(idx.get("month", 0)) == today.month
                and int(idx.get("day", 0)) == today.day
            ):
                return bar
        except (TypeError, ValueError):
            continue
    return None


class FuseEnergyCoordinator(DataUpdateCoordinator[FuseEnergyData]):
    """Single coordinator for all Fuse Energy entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: FuseEnergyAPI,
        premises_fid: str = "",
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=_SCAN_INTERVAL)
        self.api = api
        self._premises_fid = premises_fid

    async def _async_update_data(self) -> FuseEnergyData:
        result = FuseEnergyData()

        # --- Premises + supplies ---
        try:
            raw_premises = await self.api.get_premises()
        except FuseAuthError as err:
            raise UpdateFailed(f"Auth error (re-auth needed): {err}") from err
        except FuseError as err:
            raise UpdateFailed(str(err)) from err

        if not raw_premises:
            raise UpdateFailed("Fuse Energy returned empty premises list")

        # Pick the configured premises or fall back to first
        target: dict = {}
        for p in raw_premises:
            prem_obj = p.get("premises") or p
            fid = _extract_fid(prem_obj)
            if fid == self._premises_fid or not self._premises_fid:
                target = p
                if fid:
                    self._premises_fid = fid
                break
        if not target:
            target = raw_premises[0]
            prem_obj = target.get("premises") or target
            self._premises_fid = _extract_fid(prem_obj)

        prem_obj = target.get("premises") or target
        result.premises_fid = self._premises_fid
        result.premises_name = _extract_address(prem_obj) or "Fuse Energy"

        for s in target.get("supplies", []):
            supply_type = s.get("supply_type") or s.get("supplyType") or ""
            result.supplies.append(FuseSupply(
                supply_fid=s.get("supply_fid") or s.get("supplyFid") or "",
                supply_type=supply_type,
                identifier=s.get("identifier") or s.get("mpan") or s.get("mprn") or "",
                serial_number=s.get("serial_number") or s.get("serialNumber") or "",
                meter_type=s.get("meter_type") or s.get("meterType") or "",
                premises_fid=result.premises_fid,
                premises_name=result.premises_name,
            ))

        # --- Chart (year+month = daily bars) ---
        if result.premises_fid:
            today = date.today()
            try:
                chart = await self.api.get_chart(
                    result.premises_fid, today.year, today.month
                )
                await self._parse_chart(result, chart, today)
            except FuseError:
                _LOGGER.warning("FuseEnergy: chart fetch failed")

        # --- Balance ---
        try:
            bal = await self.api.get_balance()
            result.balance = _money_amount(bal)
            result.balance_currency = bal.get("currency", "GBP")
        except FuseError:
            _LOGGER.warning("FuseEnergy: balance fetch failed")

        # --- Tariff ---
        await self._fetch_tariffs(result)

        return result

    async def _parse_chart(
        self, result: FuseEnergyData, chart: dict, today: date
    ) -> None:
        total_bars = chart.get("total_bars") or chart.get("totalBars") or []
        supplies_data = chart.get("supplies") or []

        if supplies_data:
            for supply_info in supplies_data:
                stype = (
                    supply_info.get("supply_type")
                    or supply_info.get("supplyType")
                    or ""
                ).upper()
                bars = (
                    supply_info.get("bars")
                    or supply_info.get("total_bars")
                    or supply_info.get("totalBars")
                    or []
                )
                bar = _today_bar(bars, today)
                if bar is None:
                    continue
                kwh = _kwh_value(bar.get("kWh") or bar.get("kwh"))
                cost = _money_amount(bar.get("money"))
                if SUPPLY_ELECTRICITY in stype:
                    result.electricity_kwh_today = kwh
                    result.electricity_cost_today = cost
                elif SUPPLY_GAS in stype:
                    result.gas_kwh_today = kwh
                    result.gas_cost_today = cost
        else:
            bar = _today_bar(total_bars, today)
            if bar:
                result.electricity_kwh_today = _kwh_value(bar.get("kWh") or bar.get("kwh"))
                result.electricity_cost_today = _money_amount(bar.get("money"))

    async def _fetch_tariffs(self, result: FuseEnergyData) -> None:
        if not result.premises_fid:
            return
        for supply in result.supplies:
            if not supply.supply_fid:
                continue
            try:
                contracts = await self.api.get_current_contracts(
                    result.premises_fid, supply.supply_fid
                )
                current = contracts.get("current") or {}
                tariff = current.get("tariff") or {}
                tariff_id = tariff.get("tariff_id") or tariff.get("tariffId")
                title = tariff.get("title")

                if SUPPLY_ELECTRICITY in supply.supply_type:
                    result.electricity_tariff_title = title
                elif SUPPLY_GAS in supply.supply_type:
                    result.gas_tariff_title = title

                if tariff_id:
                    await self._fetch_tariff_details(result, supply, tariff_id)
            except FuseError:
                _LOGGER.debug(
                    "FuseEnergy: contracts fetch skipped for supply %s", supply.supply_fid
                )

    async def _fetch_tariff_details(
        self, result: FuseEnergyData, supply: FuseSupply, tariff_id: str
    ) -> None:
        try:
            details = await self.api.get_tariff_details(supply.supply_fid, tariff_id)
        except FuseError:
            return

        rates = details.get("rates") or details.get("tariff_rates") or []
        if isinstance(rates, list):
            for rate in rates:
                per_kwh_obj = (
                    rate.get("price_per_kWh")
                    or rate.get("pricePerKwh")
                    or rate.get("unit_rate")
                )
                per_kwh: float | None = None
                if isinstance(per_kwh_obj, dict):
                    per_kwh = _money_amount(per_kwh_obj)
                elif isinstance(per_kwh_obj, (int, float)):
                    try:
                        per_kwh = float(per_kwh_obj)
                    except (TypeError, ValueError):
                        pass

                if per_kwh is not None:
                    if SUPPLY_ELECTRICITY in supply.supply_type:
                        result.electricity_unit_rate = per_kwh
                    elif SUPPLY_GAS in supply.supply_type:
                        result.gas_unit_rate = per_kwh

        sc_obj = (
            details.get("standing_charge")
            or details.get("standingCharge")
            or details.get("standing_charges")
        )
        sc: float | None = None
        if isinstance(sc_obj, dict):
            sc = _money_amount(sc_obj.get("amount") or sc_obj.get("price") or sc_obj)
        elif isinstance(sc_obj, (int, float)):
            try:
                sc = float(sc_obj)
            except (TypeError, ValueError):
                pass

        if sc is not None:
            if SUPPLY_ELECTRICITY in supply.supply_type:
                result.electricity_standing_charge = sc
            elif SUPPLY_GAS in supply.supply_type:
                result.gas_standing_charge = sc
