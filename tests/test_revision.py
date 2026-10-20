"""Regression tests for the gas/cost revision pass.

Fuse publishes provisional "FORECAST" bars for recent days and settles them
later.  A day written early therefore holds a value that can be wrong, and the
fill-window logic never revisits it.  These tests pin the rewriting behaviour:
the corrected day and every later row must be re-emitted with sums rechained
onto the stored baseline.

Run standalone (no pytest required):

    python3 tests/test_revision.py

Inside the Home Assistant container:

    docker exec homeassistant python3 /config/tests/test_revision.py
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components"))

from fuse_energy import statistics as S  # noqa: E402

TZ = S._TZ_LONDON


def _row_start(d: date) -> datetime:
    """Local midnight for a day, as the UTC datetime HA stores."""
    return datetime(d.year, d.month, d.day, 0, 0, tzinfo=TZ).astimezone(S._UTC)


def _install_rows(rows: dict[date, tuple[float, float]]) -> None:
    """Point the module's statistics reader at a canned daily series."""

    def fake_statistics_during_period(hass, start, end, ids, period, units, types):
        stat_id = next(iter(ids))
        return {
            stat_id: [
                {"start": _row_start(d), "state": st, "sum": su}
                for d, (st, su) in sorted(rows.items())
            ]
        }

    S.statistics_during_period = fake_statistics_during_period


def test_revision_rewrites_changed_day_and_tail() -> None:
    # Stored: 20 kWh before the suspect days, then 42/48/46 provisional, then 5.
    rows: dict[date, tuple[float, float]] = {
        date(2026, 10, 15): (3.0, 100.0),
        date(2026, 10, 16): (5.0, 105.0),
        date(2026, 10, 17): (42.4, 147.4),
        date(2026, 10, 18): (47.6, 195.0),
        date(2026, 10, 19): (46.0, 241.0),
    }
    _install_rows(rows)

    settled = {
        date(2026, 10, 17): 4.0,
        date(2026, 10, 18): 4.2,
        date(2026, 10, 19): 4.1,
    }
    plan = S._plan_revision(None, S.STAT_GAS_KWH, settled, date(2026, 10, 19))

    assert plan is not None, "expected a revision plan"
    data, changes = plan

    # Three days rewritten; 15 and 16 October are untouched.
    assert len(data) == 3, len(data)
    assert [r["state"] for r in data] == [4.0, 4.2, 4.1]
    # Sums are rechained from the stored 16 October total (105.0), not the old ones.
    assert [round(r["sum"], 3) for r in data] == [109.0, 113.2, 117.3]
    assert len(changes) == 3, changes
    print("ok  test_revision_rewrites_changed_day_and_tail")


def test_revision_extends_past_last_changed_day() -> None:
    # Only 17 October was wrong; 18 and 19 keep their stored states but must
    # still have their sums recomputed, so all three rows come back.
    rows = {
        date(2026, 10, 16): (5.0, 105.0),
        date(2026, 10, 17): (42.4, 147.4),
        date(2026, 10, 18): (6.0, 153.4),
        date(2026, 10, 19): (7.0, 160.4),
    }
    _install_rows(rows)

    plan = S._plan_revision(
        None, S.STAT_GAS_KWH, {date(2026, 10, 17): 4.0}, date(2026, 10, 19)
    )
    assert plan is not None
    data, changes = plan
    assert [r["state"] for r in data] == [4.0, 6.0, 7.0]
    assert [round(r["sum"], 3) for r in data] == [109.0, 115.0, 122.0]
    assert len(changes) == 1, changes
    print("ok  test_revision_extends_past_last_changed_day")


def test_no_change_is_a_noop() -> None:
    rows = {
        date(2026, 10, 16): (5.0, 105.0),
        date(2026, 10, 17): (4.0, 109.0),
    }
    _install_rows(rows)

    plan = S._plan_revision(
        None, S.STAT_GAS_KWH, {date(2026, 10, 17): 4.0}, date(2026, 10, 17)
    )
    assert plan is None, "a settled value equal to the stored one must not rewrite"
    print("ok  test_no_change_is_a_noop")


def test_missing_baseline_refuses_to_rewrite() -> None:
    # Nothing stored before the first settled day: rewriting would invent a sum.
    rows = {date(2026, 10, 17): (42.4, 147.4)}
    _install_rows(rows)

    plan = S._plan_revision(
        None, S.STAT_GAS_KWH, {date(2026, 10, 17): 4.0}, date(2026, 10, 17)
    )
    assert plan is None
    print("ok  test_missing_baseline_refuses_to_rewrite")


def test_bar_type_filter() -> None:
    assert S._bar_is_settled({"type": "REALISED"})
    assert S._bar_is_settled({"type": "realised"})
    assert not S._bar_is_settled({"type": "FORECAST"})
    assert not S._bar_is_settled({})
    print("ok  test_bar_type_filter")


def test_unit_class_always_supplied() -> None:
    meta_kwh = S._make_metadata(S.STAT_GAS_KWH, "kWh")
    meta_gbp = S._make_metadata(S.STAT_GAS_COST, "GBP")
    assert meta_kwh["unit_class"] == "energy"
    assert "unit_class" in meta_gbp and meta_gbp["unit_class"] is None
    print("ok  test_unit_class_always_supplied")


if __name__ == "__main__":
    test_revision_rewrites_changed_day_and_tail()
    test_revision_extends_past_last_changed_day()
    test_no_change_is_a_noop()
    test_missing_baseline_refuses_to_rewrite()
    test_bar_type_filter()
    test_unit_class_always_supplied()
    print("\nall revision tests passed")
