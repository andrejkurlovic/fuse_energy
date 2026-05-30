"""DataUpdateCoordinator for Fuse Energy+ — field names proven by live API testing."""
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
    """One supply (electricity or gas) for a premises."""
    supply_fid: str
    supply_type: str          # "ELEC_IMPORT" or "GAS" (live verified)
    identifier: str           # MPAN (elec) or MPRN (gas)
    serial_number: str
    meter_type: str           # "SMART" or "TRADITIONAL"
    premises_fid: str
    premises_name: str


@dataclass
class FuseEnergyData:
    """Snapshot from one coordinator poll."""
    premises_fid: str = ""
    premises_name: str = ""
    supplies: list[FuseSupply] = field(default_factory=list)

    # Today's consumption (from chart year+month daily bars)
    electricity_kwh_today: float | None = None
    electricity_cost_today: float | None = None
    gas_kwh_today: float | None = None
    gas_cost_today: float | None = None

    # Account balance (from GET /api/v1/balance)
    balance: float | None = None
    balance_currency: str = "GBP"

    # Tariff info from current contracts
    electricity_tariff_title: str | None = None
    gas_tariff_title: str | None = None


def _safe_float(v: object) -> float | None:
    """Convert string or numeric to float, None on failure."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _today_total_bar(bars: list, today: date) -> dict | None:
    """Find the total_bars[] entry matching today's date.

    total_bars[] structure (live verified):
      {index: {year, month, day}, money: {amount: "5.73", currency: "GBP"},
       kWh: "25.470" (string), type: "REALISED"}
    """
    for bar in bars:
        idx = bar.get("index") or {}
        try:
            if (int(idx.get("year", 0)) == today.year
                    and int(idx.get("month", 0)) == today.month
                    and int(idx.get("day", 0)) == today.day):
                return bar
        except (TypeError, ValueError):
            continue
    return None


def _today_supply_bar(supply_bars: list, today: date) -> dict | None:
    """Find today's bar from the per-supply bars array.

    supplies[].bars[] structure (live verified):
      {bar: {index:{year,month,day}, money:{amount:"5.73"}, kWh:"25.470"}, breakdown:[...]}
    Note: actual bar data is nested under the "bar" key.
    """
    for wrapper in supply_bars:
        bar = wrapper.get("bar") or {}
        idx = bar.get("index") or {}
        try:
            if (int(idx.get("year", 0)) == today.year
                    and int(idx.get("month", 0)) == today.month
                    and int(idx.get("day", 0)) == today.day):
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

        # Pick configured premises_fid or fall back to first
        target: dict = {}
        for p in raw_premises:
            prem_obj = p.get("premises") or p
            fid = prem_obj.get("id") or prem_obj.get("fid") or ""
            if fid == self._premises_fid or not self._premises_fid:
                target = p
                self._premises_fid = fid
                break
        if not target:
            target = raw_premises[0]
            prem_obj = target.get("premises") or target
            self._premises_fid = prem_obj.get("id") or prem_obj.get("fid") or ""

        prem_obj = target.get("premises") or target
        result.premises_fid = self._premises_fid
        addr = prem_obj.get("address") or {}
        result.premises_name = (
            prem_obj.get("premises_name")
            or prem_obj.get("address_name")
            or addr.get("street_line_1")
            or "Fuse Energy"
        )

        # Parse supplies — field names proven from live API
        for s in target.get("supplies", []):
            sd = s.get("supply_definition") or {}
            mts = s.get("meter_type_and_status") or {}
            supply_type = sd.get("supply_type") or s.get("supply_type") or ""
            result.supplies.append(FuseSupply(
                supply_fid=s.get("supply_fid") or "",
                supply_type=supply_type,
                identifier=sd.get("identifier") or s.get("identifier") or "",
                serial_number=mts.get("serial_number") or s.get("serial_number") or "",
                meter_type=mts.get("type") or s.get("meter_type") or "",
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
                self._parse_chart(result, chart, today)
            except FuseError:
                _LOGGER.warning("FuseEnergy: chart fetch failed")

        # --- Balance ---
        try:
            bal = await self.api.get_balance()
            # amount is a string in the live API: {"amount": "0", "currency": "GBP"}
            result.balance = _safe_float(bal.get("amount"))
            result.balance_currency = bal.get("currency", "GBP")
        except FuseError:
            _LOGGER.warning("FuseEnergy: balance fetch failed")

        # --- Tariff titles from current contracts ---
        await self._fetch_tariff_titles(result)

        return result

    def _parse_chart(self, result: FuseEnergyData, chart: dict, today: date) -> None:
        """Parse chart response into today's kWh and cost per supply type.

        Two paths:
        1. Per-supply breakdown in chart.supplies[] — preferred, splits elec/gas
        2. Aggregate in chart.total_bars[] — fallback (combined)
        """
        supplies_data = chart.get("supplies") or []
        total_bars = chart.get("total_bars") or []

        if supplies_data:
            for supply_info in supplies_data:
                stype = (supply_info.get("supply_type") or "").upper()
                bars = supply_info.get("bars") or []
                bar = _today_supply_bar(bars, today)
                if bar is None:
                    continue
                # kWh is a plain string e.g. "25.470", money.amount is a string
                kwh = _safe_float(bar.get("kWh"))
                cost = _safe_float((bar.get("money") or {}).get("amount"))
                if SUPPLY_ELECTRICITY in stype:
                    result.electricity_kwh_today = kwh
                    result.electricity_cost_today = cost
                elif SUPPLY_GAS in stype:
                    result.gas_kwh_today = kwh
                    result.gas_cost_today = cost
        else:
            bar = _today_total_bar(total_bars, today)
            if bar:
                result.electricity_kwh_today = _safe_float(bar.get("kWh"))
                result.electricity_cost_today = _safe_float((bar.get("money") or {}).get("amount"))

    async def _fetch_tariff_titles(self, result: FuseEnergyData) -> None:
        """Fetch contract tariff titles — response is keyed by supply_fid.

        live response: {supply_fid_to_contracts: {<fid>: {current: {tariff:{title,...}}}}}
        """
        if not result.premises_fid:
            return
        for supply in result.supplies:
            if not supply.supply_fid:
                continue
            try:
                resp = await self.api.get_current_contracts(
                    result.premises_fid, supply.supply_fid
                )
                # Navigate: supply_fid_to_contracts → fid → current → tariff → title
                by_fid = resp.get("supply_fid_to_contracts") or {}
                fid_data = by_fid.get(supply.supply_fid) or {}
                current = fid_data.get("current") or {}
                tariff = current.get("tariff") or {}
                title = tariff.get("title")
                if SUPPLY_ELECTRICITY in supply.supply_type:
                    result.electricity_tariff_title = title
                elif SUPPLY_GAS in supply.supply_type:
                    result.gas_tariff_title = title
            except FuseError:
                _LOGGER.debug(
                    "FuseEnergy: contracts fetch skipped for %s", supply.supply_fid
                )
