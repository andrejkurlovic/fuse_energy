"""Long-term statistics injection for Fuse Energy+.

Imports historical consumption and cost from the Fuse Energy chart API into HA's
recorder long-term statistics database, enabling the Energy Dashboard to show
full history from the Fuse switch-in date (proven: 2025-09-09).

Statistic IDs injected:
  fuse_energy:electricity_import_kwh  — hourly electricity (kWh, has_sum)
  fuse_energy:electricity_import_cost — hourly electricity cost (GBP, has_sum)
  fuse_energy:gas_import_kwh          — daily gas (kWh, has_sum)
  fuse_energy:gas_import_cost         — daily gas cost (GBP, has_sum)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant

from .api import FuseEnergyAPI, FuseError
from .const import (
    DOMAIN,
    STAT_ELECTRICITY_COST,
    STAT_ELECTRICITY_KWH,
    STAT_GAS_COST,
    STAT_GAS_KWH,
    SUPPLY_ELECTRICITY,
    SUPPLY_GAS,
    SWITCH_IN_DATE,
)

_LOGGER = logging.getLogger(__name__)
_TZ_LONDON = ZoneInfo("Europe/London")
_UTC = ZoneInfo("UTC")

_STAT_NAMES: dict[str, str] = {
    STAT_ELECTRICITY_KWH: "Fuse Energy Electricity Import",
    STAT_ELECTRICITY_COST: "Fuse Energy Electricity Cost",
    STAT_GAS_KWH: "Fuse Energy Gas Import",
    STAT_GAS_COST: "Fuse Energy Gas Cost",
}

# Try to import modern mean_type API (HA 2024.12+); fall back to has_mean for older HA
try:
    from homeassistant.components.recorder.models import StatisticData, StatisticMetaData, StatisticMeanType
    _MEAN_TYPE_NONE = StatisticMeanType.NONE
    _USE_MEAN_TYPE = True
except (ImportError, AttributeError):
    from homeassistant.components.recorder.models import StatisticData, StatisticMetaData  # type: ignore[assignment]
    _MEAN_TYPE_NONE = None  # type: ignore[assignment]
    _USE_MEAN_TYPE = False

from homeassistant.components.recorder import get_instance as _get_recorder
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)


def _make_metadata(statistic_id: str, unit: str) -> StatisticMetaData:
    name = _STAT_NAMES[statistic_id]
    # unit_class required from HA 2026.11; "energy" for kWh, "monetary" for GBP
    unit_class = "energy" if unit == "kWh" else "monetary"
    if _USE_MEAN_TYPE:
        return StatisticMetaData(
            source=DOMAIN,
            statistic_id=statistic_id,
            name=name,
            unit_of_measurement=unit,
            has_sum=True,
            mean_type=_MEAN_TYPE_NONE,
            unit_class=unit_class,
        )
    return StatisticMetaData(
        source=DOMAIN,
        statistic_id=statistic_id,
        name=name,
        unit_of_measurement=unit,
        has_sum=True,
        has_mean=False,
        unit_class=unit_class,
    )


def _index_to_date(idx: dict) -> date | None:
    try:
        return date(int(idx["year"]), int(idx["month"]), int(idx["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def _bar_start(idx: dict) -> datetime | None:
    """Bar index → UTC-aware datetime at the start of the hour (or day if no hour key)."""
    try:
        local = datetime(
            int(idx["year"]),
            int(idx["month"]),
            int(idx["day"]),
            int(idx.get("hour", 0)),
            0, 0,
            tzinfo=_TZ_LONDON,
        )
        return local.astimezone(_UTC)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _safe_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _last_stat_date_and_sum(hass: HomeAssistant, stat_id: str) -> tuple[date | None, float]:
    """Return (last_date, cumulative_sum) for a statistic ID, or (None, 0) if empty."""
    rows = get_last_statistics(hass, 1, stat_id, True, {"sum", "start"})
    lst = rows.get(stat_id) or []
    if not lst:
        return None, 0.0
    row = lst[0]
    start = row.get("start")
    if start is None:
        return None, 0.0
    start_ts = start.timestamp() if isinstance(start, datetime) else float(start)
    last_dt = datetime.fromtimestamp(start_ts, tz=_TZ_LONDON).date()
    return last_dt, float(row.get("sum") or 0.0)


async def async_inject_gas_yesterday(
    hass: HomeAssistant,
    api: FuseEnergyAPI,
    premises_fid: str,
    supplies: list,
) -> None:
    """Backfill gas kWh, gas cost, and electricity cost from the monthly chart.

    Each of the three series is tracked and filled independently — a cost series
    that lags behind its kWh counterpart is healed on the same poll.

    Electricity kWh is excluded — async_inject_today keeps that series current
    at hourly resolution.

    Accepts any non-FORECAST bar type (recent data is PROVISIONAL until settled).
    Skips silently when no baseline exists (import_history not yet run).
    """
    if not premises_fid:
        return

    gas_fid = next(
        (s.supply_fid for s in supplies if SUPPLY_GAS in s.supply_type), None
    )
    elec_fid = next(
        (s.supply_fid for s in supplies if SUPPLY_ELECTRICITY in s.supply_type), None
    )

    today = date.today()
    yesterday = today - timedelta(days=1)

    recorder = _get_recorder(hass)

    def _get_all_baselines() -> tuple[
        tuple[date | None, float],
        tuple[date | None, float],
        tuple[date | None, float],
    ]:
        return (
            _last_stat_date_and_sum(hass, STAT_GAS_KWH),
            _last_stat_date_and_sum(hass, STAT_GAS_COST),
            _last_stat_date_and_sum(hass, STAT_ELECTRICITY_COST),
        )

    (gas_kwh_last, gas_kwh_base), (gas_cost_last, gas_cost_base), (elec_cost_last, elec_cost_base) = \
        await recorder.async_add_executor_job(_get_all_baselines)

    # Require at least gas kWh baseline — signals import_history has been run.
    if gas_kwh_last is None:
        return

    # Per-series fill windows (None means nothing to do for that series)
    gas_kwh_fill  = (gas_kwh_last  + timedelta(days=1)) if gas_kwh_last  < yesterday else None
    gas_cost_fill = (gas_cost_last + timedelta(days=1)) if gas_cost_last is not None and gas_cost_last  < yesterday else None
    elec_cost_fill = (elec_cost_last + timedelta(days=1)) if elec_cost_last is not None and elec_cost_last < yesterday else None

    if gas_kwh_fill is None and gas_cost_fill is None and elec_cost_fill is None:
        return  # All series current

    # Months needed across all active fill windows
    fill_starts = [f for f in (gas_kwh_fill, gas_cost_fill, elec_cost_fill) if f is not None]
    overall_start = min(fill_starts)
    months_needed: set[tuple[int, int]] = set()
    d = overall_start
    while d <= yesterday:
        months_needed.add((d.year, d.month))
        d += timedelta(days=1)

    # buckets: start_dt → delta value, collected only for dates in each series' window
    gas_kwh_pts:  dict[datetime, float] = {}
    gas_cost_pts: dict[datetime, float] = {}
    elec_cost_pts: dict[datetime, float] = {}

    for year, month in sorted(months_needed):
        try:
            chart = await api.get_chart(premises_fid, year, month)
        except FuseError:
            _LOGGER.warning("FuseEnergy backfill: chart %d-%02d failed", year, month)
            continue

        for supply_info in chart.get("supplies") or []:
            sfid = supply_info.get("supply_fid")
            is_gas = sfid == gas_fid
            is_elec = sfid == elec_fid

            if not is_gas and not is_elec:
                continue

            for wrapper in supply_info.get("bars") or []:
                bar = wrapper.get("bar") or {}
                bar_type = bar.get("type", "")
                if bar_type == "FORECAST" or not bar_type:
                    continue
                idx = bar.get("index") or {}
                bar_date = _index_to_date(idx)
                if bar_date is None or bar_date > yesterday:
                    continue
                start_dt = _bar_start(idx)
                if start_dt is None:
                    continue
                kwh  = _safe_float(bar.get("kWh"))
                cost = _safe_float((bar.get("money") or {}).get("amount"))

                if is_gas:
                    if gas_kwh_fill and bar_date >= gas_kwh_fill:
                        if kwh is not None and kwh > 0:
                            gas_kwh_pts[start_dt] = kwh
                    if gas_cost_fill and bar_date >= gas_cost_fill:
                        if cost is not None and cost >= 0:
                            gas_cost_pts[start_dt] = cost
                elif is_elec:
                    if elec_cost_fill and bar_date >= elec_cost_fill:
                        if cost is not None and cost >= 0:
                            elec_cost_pts[start_dt] = cost

    injected: list[str] = []

    def _inject(pts: dict, baseline: float, unit: str, stat_id: str, label: str) -> None:
        if not pts:
            return
        cum = baseline
        data: list[StatisticData] = []
        for start_dt, delta in sorted(pts.items()):
            cum += delta
            data.append(StatisticData(start=start_dt, sum=round(cum, 6), state=round(delta, 6)))
        async_add_external_statistics(hass, _make_metadata(stat_id, unit), data)
        injected.append(f"{label}={len(data)}d")

    _inject(gas_kwh_pts,   gas_kwh_base,   "kWh", STAT_GAS_KWH,          "gas_kwh")
    if gas_cost_base > 0:
        _inject(gas_cost_pts,  gas_cost_base,  "GBP", STAT_GAS_COST,     "gas_cost")
    if elec_cost_base > 0:
        _inject(elec_cost_pts, elec_cost_base, "GBP", STAT_ELECTRICITY_COST, "elec_cost")

    if injected:
        _LOGGER.info("FuseEnergy: daily backfill injected — %s", ", ".join(injected))


async def async_inject_today(
    hass: HomeAssistant,
    api: FuseEnergyAPI,
    premises_fid: str,
    supplies: list,
) -> None:
    """Inject today's completed hourly electricity bars on each coordinator poll.

    Keeps the Energy Dashboard's grid consumption current without a manual
    import_history call. Gas is skipped — smart meter 1-day lag means today is
    always 0. Skips silently if no historical baseline exists (import not yet run).
    """
    if not premises_fid:
        return

    elec_fid = next(
        (s.supply_fid for s in supplies if SUPPLY_ELECTRICITY in s.supply_type), None
    )
    if not elec_fid:
        return

    today = date.today()
    try:
        chart = await api.get_chart(premises_fid, today.year, today.month, today.day)
    except FuseError:
        return

    kwh_pts: dict[datetime, float] = {}
    cost_pts: dict[datetime, float] = {}

    for supply_info in chart.get("supplies") or []:
        if supply_info.get("supply_fid") != elec_fid:
            continue
        for wrapper in supply_info.get("bars") or []:
            bar = wrapper.get("bar") or {}
            if bar.get("type") != "REALISED":
                continue
            idx = bar.get("index") or {}
            start_dt = _bar_start(idx)
            if start_dt is None:
                continue
            kwh = _safe_float(bar.get("kWh"))
            cost = _safe_float((bar.get("money") or {}).get("amount"))
            if kwh and kwh > 0:
                kwh_pts[start_dt] = kwh
            if cost is not None:
                cost_pts[start_dt] = cost

    if not kwh_pts:
        return

    # Find yesterday's last cumulative sum — the baseline for today's running total.
    # Query the 2-hour window ending at today's midnight (BST) to handle DST safely.
    today_midnight_utc = datetime(
        today.year, today.month, today.day, 0, 0, tzinfo=_TZ_LONDON
    ).astimezone(_UTC)
    window_start = today_midnight_utc - timedelta(hours=2)

    today_midnight_ts = today_midnight_utc.timestamp()

    def _last_sum_before_today(stat_id: str) -> float:
        """Return the cumulative sum at the end of yesterday.

        Primary: statistics_during_period in a 2-hour window ending at midnight.
        Fallback: get_last_statistics — most recent bar with start before midnight.
        This covers monetary stats where statistics_during_period may return empty.
        """
        rows = statistics_during_period(
            hass, window_start, today_midnight_utc, {stat_id}, "hour", None, {"sum"}
        )
        entries = rows.get(stat_id) or []
        if entries:
            return float(entries[-1]["sum"])

        # Fallback: get_last_statistics fetches the N most-recent rows regardless
        # of time. Filter to entries whose start is strictly before today's midnight.
        last = get_last_statistics(hass, 5, stat_id, True, {"sum"})
        for row in last.get(stat_id) or []:
            start = row.get("start")
            if start is None:
                continue
            start_ts = start.timestamp() if isinstance(start, datetime) else float(start)
            if start_ts < today_midnight_ts:
                return float(row.get("sum") or 0.0)
        return 0.0

    recorder = _get_recorder(hass)
    baseline_kwh = await recorder.async_add_executor_job(_last_sum_before_today, STAT_ELECTRICITY_KWH)

    # Only inject if historical import has been run (baseline > 0).
    # Without a baseline, injecting with sum starting at 0 would break the
    # cumulative series and make historical totals look wrong.
    if baseline_kwh == 0.0:
        return

    baseline_cost = await recorder.async_add_executor_job(_last_sum_before_today, STAT_ELECTRICITY_COST)

    def _build(pts: dict, baseline: float, unit: str, stat_id: str) -> None:
        cum = baseline
        data: list[StatisticData] = []
        for start_dt, delta in sorted(pts.items()):
            cum += delta
            data.append(StatisticData(start=start_dt, sum=round(cum, 6), state=round(delta, 6)))
        async_add_external_statistics(hass, _make_metadata(stat_id, unit), data)

    _build(kwh_pts, baseline_kwh, "kWh", STAT_ELECTRICITY_KWH)
    if cost_pts and baseline_cost > 0.0:
        _build(cost_pts, baseline_cost, "GBP", STAT_ELECTRICITY_COST)

    _LOGGER.debug(
        "FuseEnergy: injected %d today electricity bars (baseline=%.3f kWh)",
        len(kwh_pts), baseline_kwh,
    )


def _next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


async def async_run_import(
    hass: HomeAssistant,
    api: FuseEnergyAPI,
    premises_fid: str,
    supplies: list,
    *,
    start_date: date | None,
    end_date: date | None,
    include_electricity: bool,
    include_gas: bool,
    include_cost: bool,
    granularity: str,
    dry_run: bool,
) -> dict:
    """Fetch history from Fuse API and optionally inject HA long-term statistics.

    granularity values:
      "auto"   — electricity=hourly (daily chart endpoint), gas=daily (monthly chart)
      "hourly" — electricity=hourly, gas=daily (gas API has no sub-daily resolution)
      "daily"  — both=daily (monthly chart endpoint, ~9 API calls for full backfill)

    Returns a summary dict that is also written to the HA log.
    Only REALISED bars (not FORECAST) are imported.
    """
    if not premises_fid:
        _LOGGER.error("FuseEnergy import_history: premises_fid not set — integration not fully loaded")
        return {"error": "premises_fid missing"}

    if start_date is None:
        start_date = SWITCH_IN_DATE
    if end_date is None:
        end_date = date.today() - timedelta(days=1)

    if end_date < start_date:
        _LOGGER.warning("FuseEnergy import_history: end_date %s before start_date %s", end_date, start_date)
        return {"error": "end_date before start_date"}

    # Map supply_fid → supply_type, and find elec/gas fids
    supply_type_map: dict[str, str] = {}
    elec_fid: str | None = None
    gas_fid: str | None = None
    for s in supplies:
        supply_type_map[s.supply_fid] = s.supply_type
        if SUPPLY_ELECTRICITY in s.supply_type:
            elec_fid = s.supply_fid
        elif SUPPLY_GAS in s.supply_type:
            gas_fid = s.supply_fid

    if include_electricity and not elec_fid:
        _LOGGER.warning("FuseEnergy import_history: no electricity supply found")
    if include_gas and not gas_fid:
        _LOGGER.warning("FuseEnergy import_history: no gas supply found")

    # buckets[stat_id][start_dt] = delta_value
    buckets: dict[str, dict[datetime, float]] = {
        STAT_ELECTRICITY_KWH: {},
        STAT_ELECTRICITY_COST: {},
        STAT_GAS_KWH: {},
        STAT_GAS_COST: {},
    }
    api_calls = 0

    # ---------------------------------------------------------------
    # Step 1: Monthly chart → daily bars (electricity + gas)
    # One API call per calendar month. Returns both supplies in one response.
    # ---------------------------------------------------------------
    cur_month = date(start_date.year, start_date.month, 1)
    end_month = date(end_date.year, end_date.month, 1)
    while cur_month <= end_month:
        try:
            chart = await api.get_chart(premises_fid, cur_month.year, cur_month.month)
            api_calls += 1
        except FuseError as exc:
            _LOGGER.warning(
                "FuseEnergy import: monthly chart %d-%02d failed: %s",
                cur_month.year, cur_month.month, exc,
            )
            cur_month = _next_month(cur_month)
            await asyncio.sleep(1)
            continue

        for supply_info in chart.get("supplies") or []:
            sfid = supply_info.get("supply_fid") or ""
            stype = supply_type_map.get(sfid, "")
            is_elec = SUPPLY_ELECTRICITY in stype
            is_gas = SUPPLY_GAS in stype

            if is_elec and not include_electricity:
                continue
            if is_gas and not include_gas:
                continue

            for wrapper in supply_info.get("bars") or []:
                bar = wrapper.get("bar") or {}
                bar_type = bar.get("type", "")
                # Electricity: require REALISED only.
                # Gas: accept any confirmed type (PROVISIONAL, ESTIMATED, etc.) —
                # recent gas bars may not settle to REALISED for a day or two.
                if is_elec and bar_type != "REALISED":
                    continue
                if is_gas and (bar_type == "FORECAST" or not bar_type):
                    continue
                idx = bar.get("index") or {}
                bar_date = _index_to_date(idx)
                if bar_date is None or bar_date < start_date or bar_date > end_date:
                    continue

                kwh = _safe_float(bar.get("kWh"))
                cost = _safe_float((bar.get("money") or {}).get("amount"))
                start_dt = _bar_start(idx)
                if start_dt is None:
                    continue

                if is_elec:
                    if kwh is not None and kwh > 0:
                        buckets[STAT_ELECTRICITY_KWH][start_dt] = kwh
                    if include_cost and cost is not None and cost >= 0:
                        buckets[STAT_ELECTRICITY_COST][start_dt] = cost
                elif is_gas:
                    if kwh is not None and kwh > 0:
                        buckets[STAT_GAS_KWH][start_dt] = kwh
                    if include_cost and cost is not None and cost >= 0:
                        buckets[STAT_GAS_COST][start_dt] = cost

        cur_month = _next_month(cur_month)
        await asyncio.sleep(0.5)

    # ---------------------------------------------------------------
    # Step 2: Hourly electricity (daily chart endpoint, one call/day)
    # Only for auto/hourly granularity. Gas stays at daily resolution.
    # ---------------------------------------------------------------
    use_hourly_elec = granularity in ("hourly", "auto") and include_electricity and elec_fid
    if use_hourly_elec:
        _LOGGER.info(
            "FuseEnergy import: fetching hourly electricity from %s to %s (%d days)",
            start_date, end_date, (end_date - start_date).days + 1,
        )
        # Clear daily electricity — will be replaced by per-hour data
        buckets[STAT_ELECTRICITY_KWH].clear()
        buckets[STAT_ELECTRICITY_COST].clear()

        day = start_date
        while day <= end_date:
            try:
                chart = await api.get_chart(premises_fid, day.year, day.month, day.day)
                api_calls += 1
            except FuseError as exc:
                _LOGGER.debug("FuseEnergy import: hourly chart %s failed: %s", day, exc)
                day += timedelta(days=1)
                await asyncio.sleep(0.2)
                continue

            for supply_info in chart.get("supplies") or []:
                sfid = supply_info.get("supply_fid") or ""
                if sfid != elec_fid:
                    continue
                for wrapper in supply_info.get("bars") or []:
                    bar = wrapper.get("bar") or {}
                    if bar.get("type") != "REALISED":
                        continue
                    idx = bar.get("index") or {}
                    start_dt = _bar_start(idx)
                    if start_dt is None:
                        continue
                    kwh = _safe_float(bar.get("kWh"))
                    cost = _safe_float((bar.get("money") or {}).get("amount"))
                    if kwh is not None and kwh > 0:
                        buckets[STAT_ELECTRICITY_KWH][start_dt] = kwh
                    if include_cost and cost is not None and cost >= 0:
                        buckets[STAT_ELECTRICITY_COST][start_dt] = cost

            day += timedelta(days=1)
            await asyncio.sleep(0.3)

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------
    all_times = [dt for pts in buckets.values() for dt in pts]
    summary: dict = {
        "dry_run": dry_run,
        "api_calls": api_calls,
        "granularity": "hourly" if use_hourly_elec else "daily",
        "date_range": f"{start_date} to {end_date}",
        "points": {sid: len(pts) for sid, pts in buckets.items()},
        "statistic_ids": list(_STAT_NAMES.keys()),
    }
    if all_times:
        summary["earliest"] = str(min(all_times))
        summary["latest"] = str(max(all_times))

    _LOGGER.info(
        "FuseEnergy import_history (dry_run=%s): %d API calls | "
        "elec_kwh=%d elec_cost=%d gas_kwh=%d gas_cost=%d | "
        "range=%s..%s | granularity=%s",
        dry_run,
        api_calls,
        len(buckets[STAT_ELECTRICITY_KWH]),
        len(buckets[STAT_ELECTRICITY_COST]),
        len(buckets[STAT_GAS_KWH]),
        len(buckets[STAT_GAS_COST]),
        summary.get("earliest", "none"),
        summary.get("latest", "none"),
        summary["granularity"],
    )

    if dry_run:
        return summary

    # ---------------------------------------------------------------
    # Step 3: Inject statistics
    # async_add_external_statistics upserts — safe to re-run (idempotent).
    # sum is cumulative from the first data point in the series.
    # ---------------------------------------------------------------
    _stat_units = {
        STAT_ELECTRICITY_KWH: "kWh",
        STAT_ELECTRICITY_COST: "GBP",
        STAT_GAS_KWH: "kWh",
        STAT_GAS_COST: "GBP",
    }
    injected: dict[str, int] = {}

    for stat_id, pts in buckets.items():
        if not pts:
            continue
        sorted_pts = sorted(pts.items())
        cum = 0.0
        stat_data: list[StatisticData] = []
        for start_dt, delta in sorted_pts:
            cum += delta
            stat_data.append(StatisticData(start=start_dt, sum=round(cum, 6), state=round(delta, 6)))

        meta = _make_metadata(stat_id, _stat_units[stat_id])
        async_add_external_statistics(hass, meta, stat_data)
        injected[stat_id] = len(stat_data)
        _LOGGER.info("FuseEnergy: injected %d statistics for %s", len(stat_data), stat_id)

    summary["injected"] = injected
    return summary
