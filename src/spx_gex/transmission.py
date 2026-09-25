"""Mixed-provenance transmission spots and the implied-delay estimate.

`data/manual_input/transmission_spot.csv` began as a hand-exported input and is
now appended by the scheduled Yahoo fetcher. Its `source` column preserves the
row-level distinction:

    date,time_label,spot,source_timestamp
    2026-07-27,0945,7411.98,2026-07-27T09:45:03-04:00

Two uses. M2 reads it as the spot series for the frozen-map recompute points.
Here it serves a narrower purpose: measuring the provider delay instead of
assuming it.

The measurement is a nearest-price match, and it is worth being precise about
what that can and cannot resolve. We hold one underlying price per capture and
compare it against a point-in-time series. So the estimate is
bounded by the spacing of that series — it can tell a 15-minute lag from a
two-hour one, and it cannot tell 15 minutes from 20. It is a drift alarm, not a
calibration instrument, and `resolution_minutes` on every result says so.

It is also unidentifiable outright on a flat day: if spot sits at 7400 at both
09:45 and 14:00, no price match can say which one the snapshot came from.
That case reports `identifiable: false` rather than a confident wrong answer.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import manual_input_dir

TRANSMISSION_FILE = "transmission_spot.csv"


class TransmissionRow:
    __slots__ = ("date", "time_label", "spot", "timestamp")

    def __init__(self, date: str, time_label: str, spot: float, timestamp: datetime):
        self.date = date
        self.time_label = time_label
        self.spot = spot
        self.timestamp = timestamp

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TransmissionRow({self.time_label} {self.spot} @ {self.timestamp})"


def read_transmission_spot(date_str: str, path: Path | None = None) -> list[TransmissionRow]:
    """Rows for one date, sorted by timestamp. Missing input is not an error."""
    source = path or (manual_input_dir() / TRANSMISSION_FILE)
    if not source.exists():
        return []

    rows: list[TransmissionRow] = []
    with source.open(newline="") as fh:
        for raw in csv.DictReader(fh):
            if (raw.get("date") or "").strip() != date_str:
                continue
            try:
                spot = float(raw["spot"])
                stamp = datetime.fromisoformat((raw["source_timestamp"] or "").strip())
            except (KeyError, TypeError, ValueError):
                continue
            if stamp.tzinfo is None:
                continue  # a naive stamp cannot be compared to a UTC request time
            rows.append(
                TransmissionRow(
                    date=raw["date"].strip(),
                    time_label=(raw.get("time_label") or "").strip(),
                    spot=spot,
                    timestamp=stamp.astimezone(timezone.utc),
                )
            )
    rows.sort(key=lambda r: r.timestamp)
    return rows


def _neighbour_gap_minutes(rows: list[TransmissionRow], index: int) -> float | None:
    """Smallest gap to an adjacent point — the finest lag the grid can resolve."""
    gaps = []
    if index > 0:
        gaps.append((rows[index].timestamp - rows[index - 1].timestamp).total_seconds())
    if index < len(rows) - 1:
        gaps.append((rows[index + 1].timestamp - rows[index].timestamp).total_seconds())
    return min(gaps) / 60.0 if gaps else None


def estimate_implied_delay(
    snapshot_spot: float | None,
    request_time: datetime,
    rows: list[TransmissionRow],
    configured_minutes: float,
    warn_threshold_minutes: float = 5.0,
    ambiguity_margin_points: float = 2.0,
) -> dict[str, Any]:
    """Match the snapshot's underlying price to the transmission series.

    Returns the implied lag plus everything needed to judge whether to believe
    it. `implied_delay_minutes` is present but meaningless when
    `identifiable` is false.
    """
    result: dict[str, Any] = {
        "implied_delay_minutes": None,
        "identifiable": False,
        "reason": None,
        "matched_time_label": None,
        "matched_timestamp_utc": None,
        "matched_spot": None,
        "snapshot_spot": snapshot_spot,
        "spot_abs_diff": None,
        "runner_up_margin_points": None,
        "resolution_minutes": None,
        "configured_delay_minutes": configured_minutes,
        "deviation_minutes": None,
        "warn": False,
    }

    if snapshot_spot is None:
        result["reason"] = "snapshot returned no underlying price"
        return result
    if not rows:
        result["reason"] = (
            "no transmission_spot.csv rows for this date — the scheduled fetch "
            "may not have run, or the historical minute bars may be unavailable"
        )
        return result

    diffs = sorted(
        ((abs(snapshot_spot - row.spot), idx) for idx, row in enumerate(rows)),
        key=lambda pair: pair[0],
    )
    best_diff, best_idx = diffs[0]
    best = rows[best_idx]

    result.update(
        {
            "matched_time_label": best.time_label,
            "matched_timestamp_utc": best.timestamp.isoformat(),
            "matched_spot": best.spot,
            "spot_abs_diff": round(best_diff, 4),
            "resolution_minutes": _neighbour_gap_minutes(rows, best_idx),
        }
    )

    if len(diffs) > 1:
        margin = diffs[1][0] - best_diff
        result["runner_up_margin_points"] = round(margin, 4)
        if margin < ambiguity_margin_points:
            result["reason"] = (
                f"runner-up transmission point is only {margin:.2f} pts further away "
                f"(threshold {ambiguity_margin_points}); spot is too flat across the "
                "series to identify a lag"
            )
            return result

    implied = (
        request_time.astimezone(timezone.utc) - best.timestamp
    ).total_seconds() / 60.0
    deviation = implied - configured_minutes

    result.update(
        {
            "implied_delay_minutes": round(implied, 2),
            "identifiable": True,
            "deviation_minutes": round(deviation, 2),
            "warn": abs(deviation) > warn_threshold_minutes,
        }
    )
    if result["warn"]:
        resolution = result["resolution_minutes"]
        result["reason"] = (
            f"implied lag {implied:.1f} min vs configured {configured_minutes:.0f} min "
            f"(off by {deviation:+.1f}). Transmission grid resolves to about "
            f"{resolution:.0f} min, so treat this as a drift alarm, not a new value — "
            "do not edit provider.delay_minutes off one reading."
            if resolution
            else f"implied lag {implied:.1f} min vs configured {configured_minutes:.0f} min"
        )
    return result
