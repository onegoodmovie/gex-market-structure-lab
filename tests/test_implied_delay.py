"""Implied-delay measurement (§5.3 continuous check on provider.delay_minutes).

The estimator's job is as much to refuse as to answer: a confident wrong lag
would be worse than no lag at all, because it would look like evidence.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spx_gex.transmission import (  # noqa: E402
    TransmissionRow,
    estimate_implied_delay,
    read_transmission_spot,
)

ET = timezone(timedelta(hours=-4))


def _rows(*pairs):
    """(time_label, HH:MM, spot) -> transmission rows on 2026-07-27."""
    out = []
    for label, hhmm, spot in pairs:
        hour, minute = (int(x) for x in hhmm.split(":"))
        out.append(
            TransmissionRow(
                date="2026-07-27",
                time_label=label,
                spot=spot,
                timestamp=datetime(2026, 7, 27, hour, minute, tzinfo=ET).astimezone(
                    timezone.utc
                ),
            )
        )
    return out


def _request_at(hhmm: str) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 7, 27, hour, minute, tzinfo=ET).astimezone(timezone.utc)


# A day with real intraday movement — the case where a lag is identifiable.
MOVING_DAY = _rows(
    ("0945", "09:45", 7400.0),
    ("1130", "11:30", 7440.0),
    ("1400", "14:00", 7380.0),
    ("1545", "15:45", 7455.0),
    ("1600", "16:00", 7460.0),
)


def test_recovers_the_configured_delay():
    """Download at 10:00 returning the 09:45 price implies a 15-minute lag."""
    result = estimate_implied_delay(
        snapshot_spot=7400.0,
        request_time=_request_at("10:00"),
        rows=MOVING_DAY,
        configured_minutes=15.0,
    )
    assert result["identifiable"] is True
    assert result["matched_time_label"] == "0945"
    assert result["implied_delay_minutes"] == pytest.approx(15.0)
    assert result["deviation_minutes"] == pytest.approx(0.0)
    assert result["warn"] is False


def test_warns_when_the_delay_has_drifted():
    """A 14:15 download returning the 11:30 price is not a 15-minute feed."""
    result = estimate_implied_delay(
        snapshot_spot=7440.0,
        request_time=_request_at("14:15"),
        rows=MOVING_DAY,
        configured_minutes=15.0,
    )
    assert result["identifiable"] is True
    assert result["matched_time_label"] == "1130"
    assert result["implied_delay_minutes"] == pytest.approx(165.0)
    assert result["warn"] is True
    assert "drift alarm, not a new value" in result["reason"]


def test_flat_day_is_not_identifiable():
    """Spot within noise at several times carries no lag information at all.

    This is the case that matters most: the estimator must say so rather than
    return whichever point happened to be a hundredth of a point closer.
    """
    flat = _rows(
        ("0945", "09:45", 7400.0),
        ("1130", "11:30", 7400.5),
        ("1400", "14:00", 7400.2),
        ("1545", "15:45", 7401.0),
    )
    result = estimate_implied_delay(
        snapshot_spot=7400.1,
        request_time=_request_at("14:15"),
        rows=flat,
        configured_minutes=15.0,
    )
    assert result["identifiable"] is False
    assert result["implied_delay_minutes"] is None
    assert "too flat" in result["reason"]


def test_reports_the_resolution_of_the_transmission_grid():
    """Five hand-kept points a day cannot resolve minutes. Say so explicitly."""
    result = estimate_implied_delay(
        snapshot_spot=7380.0,
        request_time=_request_at("14:15"),
        rows=MOVING_DAY,
        configured_minutes=15.0,
    )
    # nearest neighbours of the 14:00 point are 11:30 and 15:45
    assert result["resolution_minutes"] == pytest.approx(105.0)


def test_missing_transmission_file_is_not_an_error():
    result = estimate_implied_delay(
        snapshot_spot=7400.0,
        request_time=_request_at("10:00"),
        rows=[],
        configured_minutes=15.0,
    )
    assert result["identifiable"] is False
    assert result["implied_delay_minutes"] is None
    assert "scheduled fetch" in result["reason"]


def test_missing_snapshot_spot_is_not_an_error():
    result = estimate_implied_delay(
        snapshot_spot=None,
        request_time=_request_at("10:00"),
        rows=MOVING_DAY,
        configured_minutes=15.0,
    )
    assert result["identifiable"] is False
    assert "no underlying price" in result["reason"]


# --- CSV parsing --------------------------------------------------------------


def test_reads_only_the_requested_date(tmp_path):
    csv = tmp_path / "transmission_spot.csv"
    csv.write_text(
        "date,time_label,spot,source_timestamp\n"
        "2026-07-27,0945,7411.98,2026-07-27T09:45:03-04:00\n"
        "2026-07-27,1400,7402.10,2026-07-27T14:00:01-04:00\n"
        "2026-07-28,0945,7420.00,2026-07-28T09:45:02-04:00\n"
    )
    rows = read_transmission_spot("2026-07-27", path=csv)
    assert [r.time_label for r in rows] == ["0945", "1400"]
    assert rows[0].spot == 7411.98
    assert rows[0].timestamp.tzinfo is not None


def test_skips_rows_without_a_usable_timestamp(tmp_path):
    """A naive timestamp cannot be compared to a UTC request time; dropping it
    is safer than assuming a zone on the study's behalf."""
    csv = tmp_path / "transmission_spot.csv"
    csv.write_text(
        "date,time_label,spot,source_timestamp\n"
        "2026-07-27,0945,7411.98,2026-07-27T09:45:03\n"        # naive
        "2026-07-27,1130,not_a_number,2026-07-27T11:30:00-04:00\n"
        "2026-07-27,1400,7402.10,2026-07-27T14:00:01-04:00\n"
    )
    rows = read_transmission_spot("2026-07-27", path=csv)
    assert [r.time_label for r in rows] == ["1400"]


def test_missing_file_returns_empty(tmp_path):
    assert read_transmission_spot("2026-07-27", path=tmp_path / "nope.csv") == []
