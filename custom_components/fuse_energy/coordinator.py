"""DataUpdateCoordinator for Fuse Energy+ — field names proven by live API testing."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import FuseAuthError, FuseEnergyAPI, FuseError
from .const import DOMAIN, SUPPLY_ELECTRICITY, SUPPLY_GAS
from .statistics import async_inject_gas_yesterday, async_inject_today

_LOGGER = logging.getLogger(__name__)
_SCAN_INTERVAL = timedelta(hours=1)

# Regex to extract numeric value from Fuse's formatted strings like "£0.2082"
_PRICE_RE = re.compile(r"£?([\d.]+)")


def _parse_price(s: str | None) -> float | None:
    """Extract a float from a tariff price string like '£0.2082' or '0.4280'."""
    if not s:
        return None
    m = _PRICE_RE.search(str(s))
    return float(m.group(1)) if m else None


def _safe_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class FuseSupply:
    """One supply (electricity or gas) for a premises."""
    supply_fid: str
    supply_type: str          # "ELEC_IMPORT" or "GAS"
    identifier: str           # MPAN (elec) or MPRN (gas)
    serial_number: str
    meter_type: str           # "SMART" or "TRADITIONAL"
    meter_status: str         # "ONLINE" etc.
    premises_fid: str
    premises_name: str


@dataclass
class FuseSupplyData:
    """All live data for one supply."""
    supply: FuseSupply

    # Today's readings from chart (daily bars)
    kwh_today: float | None = None
    cost_today: float | None = None

    # Yesterday (fallback if today is 0 — normal for gas)
    kwh_yesterday: float | None = None
    cost_yesterday: float | None = None

    # Tariff
    tariff_title: str | None = None
    unit_rate: float | None = None          # £/kWh
    standing_charge: float | None = None    # £/day


@dataclass
class FuseEnergyData:
    """Snapshot from one coordinator poll."""
    premises_fid: str = ""
    premises_name: str = ""
    premises_address: str = ""

    supplies: list[FuseSupplyData] = field(default_factory=list)

    # Account balance
    balance: float | None = None
    balance_currency: str = "GBP"

    def supply_data(self, supply_type: str) -> FuseSupplyData | None:
        """Return the first FuseSupplyData whose supply_type contains supply_type."""
        for s in self.supplies:
            if supply_type in s.supply.supply_type:
                return s
        return None


def _extract_premises_fid(prem_obj: dict) -> str:
    return prem_obj.get("id") or prem_obj.get("fid") or prem_obj.get("premises_fid") or ""


def _extract_address(prem_obj: dict) -> str:
    addr = prem_obj.get("address") or {}
    parts = [
        addr.get("street_line_1") or addr.get("streetLine1"),
        addr.get("city"),
        addr.get("postcode"),
    ]
    return ", ".join(p for p in parts if p)


def _today_supply_bar(bars: list, target_date: date) -> dict | None:
    """Find bar for target_date in supplies[].bars[] (nested under 'bar' key)."""
    for wrapper in bars:
        bar = wrapper.get("bar") or {}
        idx = bar.get("index") or {}
        try:
            if (int(idx.get("year", 0)) == target_date.year
                    and int(idx.get("month", 0)) == target_date.month
                    and int(idx.get("day", 0)) == target_date.day):
                return bar
        except (TypeError, ValueError):
            continue
    return None


def _total_bar_for_date(bars: list, target_date: date) -> dict | None:
    """Find bar for target_date in total_bars[] (direct structure)."""
    for bar in bars:
        idx = bar.get("index") or {}
        try:
            if (int(idx.get("year", 0)) == target_date.year
                    and int(idx.get("month", 0)) == target_date.month
                    and int(idx.get("day", 0)) == target_date.day):
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
            # ConfigEntryAuthFailed tells HA to show re-auth notification in the UI
            raise ConfigEntryAuthFailed(str(err)) from err
        except FuseError as err:
            raise UpdateFailed(str(err)) from err

        if not raw_premises:
            raise UpdateFailed("Fuse Energy returned empty premises list")

        target: dict = {}
        for p in raw_premises:
            prem_obj = p.get("premises") or p
            fid = _extract_premises_fid(prem_obj)
            if fid == self._premises_fid or not self._premises_fid:
                target = p
                self._premises_fid = fid
                break
        if not target:
            target = raw_premises[0]
            prem_obj = target.get("premises") or target
            self._premises_fid = _extract_premises_fid(prem_obj)

        prem_obj = target.get("premises") or target
        result.premises_fid = self._premises_fid
        result.premises_name = (
            prem_obj.get("premises_name") or prem_obj.get("address_name") or "Fuse Energy"
        )
        result.premises_address = _extract_address(prem_obj)

        supplies: list[FuseSupply] = []
        for s in target.get("supplies", []):
            sd = s.get("supply_definition") or {}
            mts = s.get("meter_type_and_status") or {}
            supplies.append(FuseSupply(
                supply_fid=s.get("supply_fid") or "",
                supply_type=sd.get("supply_type") or s.get("supply_type") or "",
                identifier=sd.get("identifier") or s.get("identifier") or "",
                serial_number=mts.get("serial_number") or s.get("serial_number") or "",
                meter_type=mts.get("type") or s.get("meter_type") or "",
                meter_status=mts.get("status") or s.get("meter_status") or "",
                premises_fid=result.premises_fid,
                premises_name=result.premises_name,
            ))

        # Build supply data objects
        supply_data_map: dict[str, FuseSupplyData] = {
            s.supply_fid: FuseSupplyData(supply=s) for s in supplies
        }
        result.supplies = list(supply_data_map.values())

        # --- Chart — daily bars for current month + yesterday fallback ---
        if result.premises_fid:
            today = date.today()
            yesterday = today - timedelta(days=1)
            try:
                chart = await self.api.get_chart(
                    result.premises_fid, today.year, today.month
                )
                self._parse_chart(supply_data_map, chart, today, yesterday)
            except FuseError:
                _LOGGER.warning("FuseEnergy: chart fetch failed")

            # Inject today's completed hourly bars into the Fuse external statistics
            # so the Energy Dashboard grid consumption shows live data for today.
            try:
                await async_inject_today(
                    self.hass, self.api, result.premises_fid,
                    [sd.supply for sd in result.supplies],
                )
            except Exception:  # pylint: disable=broad-except
                pass  # Never crash the coordinator for statistics injection

            # Backfill any gas/cost days that have accumulated since the last
            # import_history run.  Reuses the already-fetched monthly chart so
            # no extra API call is needed for the common (current-month) case.
            try:
                await async_inject_gas_yesterday(
                    self.hass, self.api, result.premises_fid,
                    [sd.supply for sd in result.supplies],
                    current_month_chart=chart,
                )
            except Exception:  # pylint: disable=broad-except
                pass  # Never crash the coordinator for statistics injection

        # --- Balance ---
        try:
            bal = await self.api.get_balance()
            result.balance = _safe_float(bal.get("amount"))
            result.balance_currency = bal.get("currency", "GBP")
        except FuseError:
            _LOGGER.warning("FuseEnergy: balance fetch failed")

        # --- Tariff per supply ---
        for sd in result.supplies:
            await self._fetch_tariff(sd, result.premises_fid)

        return result

    def _parse_chart(
        self,
        supply_data_map: dict[str, FuseSupplyData],
        chart: dict,
        today: date,
        yesterday: date,
    ) -> None:
        """Parse chart response — fills today and yesterday bars per supply."""
        for supply_info in chart.get("supplies") or []:
            sfid = supply_info.get("supply_fid") or ""
            sd = supply_data_map.get(sfid)
            if sd is None:
                continue
            bars = supply_info.get("bars") or []
            today_bar = _today_supply_bar(bars, today)
            if today_bar:
                sd.kwh_today = _safe_float(today_bar.get("kWh"))
                sd.cost_today = _safe_float((today_bar.get("money") or {}).get("amount"))
            yd_bar = _today_supply_bar(bars, yesterday)
            if yd_bar:
                sd.kwh_yesterday = _safe_float(yd_bar.get("kWh"))
                sd.cost_yesterday = _safe_float((yd_bar.get("money") or {}).get("amount"))

    async def _fetch_tariff(self, sd: FuseSupplyData, premises_fid: str) -> None:
        """Fetch contract tariff title, unit_rate, and standing_charge for a supply."""
        if not sd.supply.supply_fid or not premises_fid:
            return
        try:
            resp = await self.api.get_current_contracts(premises_fid, sd.supply.supply_fid)
            by_fid = resp.get("supply_fid_to_contracts") or {}
            fid_data = by_fid.get(sd.supply.supply_fid) or {}
            current = fid_data.get("current") or {}
            tariff = current.get("tariff") or {}
            sd.tariff_title = tariff.get("title")
            tariff_id = tariff.get("tariff_id")
            if tariff_id:
                await self._fetch_tariff_details(sd, tariff_id)
        except FuseError:
            _LOGGER.debug("FuseEnergy: contracts fetch skipped for %s", sd.supply.supply_fid)

    async def _fetch_tariff_details(self, sd: FuseSupplyData, tariff_id: str) -> None:
        """Parse unit_rate and standing_charge from tariff_details description_items.

        description_items[].value.name tells us which item it is.
        description_items[].value.value is a human-readable price string like '£0.2082'.
        We parse with a regex — any item tagged EXIT_FEE is skipped.
        """
        try:
            resp = await self.api.get_tariff_details(sd.supply.supply_fid, tariff_id)
        except FuseError:
            return

        details = (resp.get("tariff_id_to_details") or {}).get(tariff_id) or {}
        for item in details.get("description_items") or []:
            v = item.get("value") or {}
            tags = v.get("tags") or []
            if "EXIT_FEE" in tags:
                continue
            name = (v.get("name") or "").lower()
            value_str = v.get("value")
            if "unit rate" in name:
                sd.unit_rate = _parse_price(value_str)
            elif "standing charge" in name:
                sd.standing_charge = _parse_price(value_str)
