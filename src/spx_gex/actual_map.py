"""Actual-surface refresh (spec §12).

The frozen map answers "what would GEX be now if the surface had not moved".
This answers "what is it, with the surface that actually exists now". Same OI
base, same recomputation machinery, same sticky-strike alignment — the only
thing that changes is which capture supplies σ_K.

Written as a thin parameterisation of `frozen_map.build_frozen_inputs` on
purpose. Two modules that each build their own contract set would eventually
build slightly different ones, and the whole of §12 is a difference between
them; a membership difference would show up as signal.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Config, repo_path
from .exposure import usable_for_gex
from .frozen_map import JOIN_KEYS, FrozenMapError, _normalized, recompute_at
from .transmission import read_transmission_spot


def build_actual_inputs(
    config: Config,
    date_str: str,
    label: str,
    normalized_root: Path | None = None,
) -> pd.DataFrame:
    """OI from the day's OI base, IV from the `label` capture's own surface."""
    oi = _normalized(date_str, "oi_base", normalized_root)
    surface = usable_for_gex(_normalized(date_str, label, normalized_root))

    merged = surface.merge(
        oi[JOIN_KEYS + ["open_interest"]],
        on=JOIN_KEYS,
        how="inner",
        suffixes=("_surface", "_oibase"),
    )
    merged["open_interest"] = merged["open_interest_oibase"]
    merged = merged.drop(columns=["open_interest_surface", "open_interest_oibase"])
    if merged.empty:
        raise FrozenMapError(f"OI base and the {label} surface share no contracts")
    return merged


def build_actual_map(
    config: Config, date_str: str, normalized_root: Path | None = None
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """One block of rows per capture that carries a real surface."""
    tz = ZoneInfo(config.get("capture.timezone", "America/New_York"))
    r = float(config.require("rates.r"))
    q = float(config.require("rates.q"))

    spots = {row.time_label: row for row in read_transmission_spot(date_str)}
    labels = list(config.get("actual_map.surface_times", ["0945", "1400", "1545"]))

    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    counts: dict[str, int] = {}

    for label in labels:
        record = spots.get(label)
        path = (
            Path(normalized_root) / date_str / f"{label}.parquet"
            if normalized_root is not None
            else repo_path("data", "normalized", date_str, f"{label}.parquet")
        )
        if record is None or not path.exists():
            skipped.append(label)
            continue
        frame = build_actual_inputs(config, date_str, label, normalized_root)
        counts[label] = int(len(frame))
        asof = datetime.combine(
            date.fromisoformat(date_str),
            datetime.strptime(label, "%H%M").time(),
            tzinfo=tz,
        )
        block = recompute_at(frame, asof, record.spot, r, q, config, label)
        for row in block:
            row["surface_source"] = label      # what separates this from the frozen map
            row["map_kind"] = "actual"
        rows.extend(block)

    table = pd.DataFrame(rows)
    meta = {
        "date": date_str,
        "surface_times_requested": labels,
        "surface_times_missing": skipped,
        "rows": len(table),
        "contracts_per_surface": counts,
        "r": r,
        "q": q,
        "rates_provisional": bool(config.get("rates.provisional", False)),
        "iv_alignment": config.get("frozen_map.iv_alignment", "sticky_strike"),
        "complete": not skipped,
    }
    return table, meta


def write_actual_map(
    table: pd.DataFrame,
    meta: dict[str, Any],
    date_str: str,
    output_root: Path | None = None,
) -> tuple[Path, Path]:
    out_dir = (
        Path(output_root) / date_str
        if output_root is not None
        else repo_path("data", "derived", date_str)
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = out_dir / "actual_map.parquet"
    meta_path = out_dir / "actual_map.meta.json"
    table.to_parquet(parquet_path, index=False)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True, default=str))
    return parquet_path, meta_path
