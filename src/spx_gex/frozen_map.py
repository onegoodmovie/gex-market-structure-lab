"""Frozen-surface mechanical map (spec §10).

What is held fixed and what moves:

```
OI            frozen at the day's OI base
IV surface    frozen at the 09:45 actual surface, attached per strike
S             the Transmission spot at each recomputation point
T             recomputed at every point
```

**Both S and T update at every recomputation.** Updating spot alone is a
specification violation, and not a subtle one: for 0DTE, time decay reshapes
the gamma profile more over an afternoon than a typical intraday spot move
does. §10.1 says so explicitly and this module is where it would be easy to
get wrong.

IV alignment is sticky-strike (§10.2): σ_K stays attached to its original
strike. There is no switch, on purpose.

Recomputation points come from `transmission_spot.csv` — 09:45, 11:30, 14:00,
15:45, 16:00 — and a point with no recorded spot is skipped and reported, never
interpolated.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Config, repo_path
from .exposure import gex_by_strike, net_gex, usable_for_gex
from .expiry import SECONDS_PER_YEAR, expiry_bucket
from .flip_solver import solve_flip
from .peaks import peak_proxies
from .transmission import read_transmission_spot

JOIN_KEYS = ["root", "expiration_date", "strike", "option_type"]


class FrozenMapError(RuntimeError):
    pass


def _normalized(
    date_str: str, label: str, normalized_root: Path | None = None
) -> pd.DataFrame:
    path = (
        Path(normalized_root) / date_str / f"{label}.parquet"
        if normalized_root is not None
        else repo_path("data", "normalized", date_str, f"{label}.parquet")
    )
    if not path.exists():
        raise FrozenMapError(f"missing {path}; run normalize_chain.py first")
    return pd.read_parquet(path)


def build_frozen_inputs(
    config: Config,
    date_str: str,
    surface_base: str | None = None,
    normalized_root: Path | None = None,
) -> pd.DataFrame:
    """OI from the day's OI base, IV from the surface base (§10.1).

    Joined rather than taken from one capture: the OI base is the declared
    source of open interest, and the surface base is the declared source of the
    IV surface. Taking both from one file would work today — OI does not move
    intraday — and would quietly stop matching the spec the day that changes.

    `surface_base` overrides the configured base. It exists because 09:45 is the
    capture most exposed to the provider's unsettled open (T1 §3: 91% of the OI
    flagged implausible at 09:45 was computable again by 14:00), and the whole
    frozen map is built on it. Rather than choosing between 09:45 and a later
    base on argument, both are captured and the difference is measured.
    """
    surface_label = str(
        surface_base
        if surface_base is not None
        else config.get("frozen_map.surface_base_time", "0945")
    )

    # The usability filter applies to the *surface* only. It requires a spot and
    # a usable gamma, and the OI base has neither by design — it is captured
    # pre-market and exists to supply open interest, nothing else. Filtering it
    # the same way discards every row.
    oi = _normalized(date_str, "oi_base", normalized_root)
    surface = usable_for_gex(_normalized(date_str, surface_label, normalized_root))

    merged = surface.merge(
        oi[JOIN_KEYS + ["open_interest"]],
        on=JOIN_KEYS,
        how="inner",
        suffixes=("_surface", "_oibase"),
    )
    merged["open_interest"] = merged["open_interest_oibase"]
    merged = merged.drop(columns=["open_interest_surface", "open_interest_oibase"])
    if merged.empty:
        raise FrozenMapError("OI base and surface base share no contracts")
    return merged


def recompute_at(
    frame: pd.DataFrame,
    asof: datetime,
    spot: float,
    r: float,
    q: float,
    config: Config,
    label: str,
    policy_label: str | None = None,
) -> list[dict[str, Any]]:
    """One row per expiry bucket at one recomputation point.

    `policy_label` decouples *which* §9.1 0DTE rules apply from *when* the row
    is stamped. It exists for §12: the four attribution states sit at two
    different times but must share one bucket policy, or the identity fails on
    a membership change rather than on anything being attributed. Defaults to
    `label`, so M2 is unaffected.
    """
    tz = ZoneInfo(config.get("capture.timezone", "America/New_York"))
    policy = policy_label or label
    work = frame.copy()

    # T is recomputed here — not carried over from the capture. This is the
    # line §10.1 is about.
    expiry_ts = pd.to_datetime(work["expiration_timestamp"], utc=True)
    work["t_years"] = (expiry_ts - pd.Timestamp(asof)).dt.total_seconds() / SECONDS_PER_YEAR
    work = work[work["t_years"] > 0]

    asof_date = asof.astimezone(tz).date()
    work["dte"] = [
        (pd.Timestamp(e).date() - asof_date).days for e in work["expiration_date"]
    ]
    work["expiry_bucket"] = work["dte"].map(expiry_bucket)

    not_comparable = list(config.get("gex.zero_dte_not_cross_day_comparable_times", []))
    no_aggregate = list(config.get("gex.zero_dte_excluded_from_aggregate_times", []))
    absent = list(config.get("gex.zero_dte_absent_times", []))

    buckets = [b for b in ("0DTE", "1-7DTE", "8-30DTE") if b in set(work["expiry_bucket"])]
    rows: list[dict[str, Any]] = []

    for bucket in buckets + ["aggregate"]:
        if bucket == "aggregate":
            leg = work[work["expiry_bucket"].notna()]
            if policy in no_aggregate:
                leg = leg[leg["expiry_bucket"] != "0DTE"]
        else:
            if bucket == "0DTE" and policy in absent:
                continue
            leg = work[work["expiry_bucket"] == bucket]
        if leg.empty:
            continue

        flip = solve_flip(
            leg,
            spot,
            r,
            q,
            grid_min_pct=float(config.get("flip_solver.grid_min_pct", -5.0)),
            grid_max_pct=float(config.get("flip_solver.grid_max_pct", 5.0)),
            grid_step_points=float(config.get("flip_solver.grid_step_points", 5.0)),
            root_selection_rule=str(
                config.get("flip_solver.root_selection_rule", "nearest_to_spot")
            ),
        )
        by_strike = gex_by_strike(leg, spot, r, q)

        rows.append(
            {
                "date": asof.astimezone(tz).date().isoformat(),
                "time_label": label,
                "expiry_bucket": bucket,
                "spot": spot,
                "spot_timestamp": asof.isoformat(),
                "t_min_remaining": round(
                    float(leg["t_years"].min()) * SECONDS_PER_YEAR / 60.0, 4
                ),
                "net_gex": net_gex(leg, spot, r, q),
                "flip": flip["primary_flip"],
                "all_roots": json.dumps(flip["all_roots"]),
                "n_roots": flip["n_roots"],
                "no_root": flip["no_root"],
                "root_selection_rule": flip["root_selection_rule"],
                "spot_vs_flip": flip["spot_vs_flip"],
                "dist_to_flip_pct": flip["dist_to_flip_pct"],
                **peak_proxies(by_strike),
                "n_contracts": int(len(leg)),
                # §9 provenance
                "gex_definition_version": config.get("gex.definition_version", "1.0"),
                "sign_convention": config.get("gex.sign_convention", ""),
                "expiry_scope": config.get("capture.expiry_scope_max_dte"),
                "iv_alignment": config.get("frozen_map.iv_alignment", "sticky_strike"),
                "oi_asof": frame["oi_asof"].iloc[0] if "oi_asof" in frame else None,
                "surface_timestamp": frame["asof_utc"].iloc[0]
                if "asof_utc" in frame
                else None,
                "time_convention": config.get("normalization.time_convention"),
                "t_floored": False,
                "zero_dte_excluded": bucket == "0DTE" and policy in absent,
                "not_cross_day_comparable": bucket == "0DTE"
                and policy in not_comparable,
                "excluded_from_aggregate": bucket == "0DTE" and policy in no_aggregate,
                "r": r,
                "q": q,
                "rates_provisional": bool(config.get("rates.provisional", False)),
                "asof_is_inferred": bool(frame["asof_is_inferred"].iloc[0])
                if "asof_is_inferred" in frame
                else None,
            }
        )
    return rows


def build_frozen_map(
    config: Config,
    date_str: str,
    surface_base: str | None = None,
    normalized_root: Path | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """The full recompute-times × bucket map for one day."""
    frame = build_frozen_inputs(config, date_str, surface_base, normalized_root)
    tz = ZoneInfo(config.get("capture.timezone", "America/New_York"))
    r = float(config.require("rates.r"))
    q = float(config.require("rates.q"))

    spots = {row.time_label: row for row in read_transmission_spot(date_str)}
    wanted = list(config.require("frozen_map.recompute_times"))
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []

    for label in wanted:
        record = spots.get(label)
        if record is None:
            skipped.append(label)
            continue
        asof = datetime.combine(
            date.fromisoformat(date_str),
            datetime.strptime(label, "%H%M").time(),
            tzinfo=tz,
        )
        rows.extend(recompute_at(frame, asof, record.spot, r, q, config, label))

    base_used = str(
        surface_base
        if surface_base is not None
        else config.get("frozen_map.surface_base_time", "0945")
    )
    for row in rows:
        row["surface_base"] = base_used
    table = pd.DataFrame(rows)
    meta = {
        "date": date_str,
        "surface_base": base_used,
        "recompute_times_requested": wanted,
        "recompute_times_missing_spot": skipped,
        "rows": len(table),
        "contracts_in_frozen_input": int(len(frame)),
        "r": r,
        "q": q,
        "rates_provisional": bool(config.get("rates.provisional", False)),
        "iv_alignment": config.get("frozen_map.iv_alignment", "sticky_strike"),
        "complete": not skipped,
    }
    return table, meta


def write_frozen_map(
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
    parquet_path = out_dir / "frozen_map.parquet"
    meta_path = out_dir / "frozen_map.meta.json"
    table.to_parquet(parquet_path, index=False)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True, default=str))
    return parquet_path, meta_path
