"""2×2 counterfactual attribution (spec §12).

For baseline `t0` and each `t1`, four states with OI fixed throughout:

```
        IV from t0   IV from t1
t0        A            C          <- C never existed in the market
t1        B            D
```

```
total_change            = D - A
mechanical_component    = B - A          (spot/time first)
surface_component       = D - B
mechanical_contribution = 0.5·((B-A) + (D-C))    order-independent (Shapley)
surface_contribution    = 0.5·((C-A) + (D-B))
```

Both pairs are reported. The first is path-dependent — it answers "spot and
time moved first, then the surface" — and the second removes that choice.

**This is a counterfactual attribution under a declared model. It is not a
causal decomposition**, and the two components are not additive in any deeper
sense (§12, language discipline).

Two things this module gets right that are easy to get wrong:

**One contract set, fixed across all four states.** The usable set differs
between captures — on T1, 331 contracts flagged implausible at 09:45 had good
greeks by 14:00 — so taking each state's own usable rows would put a membership
change inside `D - A` and report it as surface repricing. The intersection is
used, and how many contracts that costs is reported.

**One bucket policy across all four states.** The 0DTE aggregate rules in
§9.1 are keyed on capture label, so a t1 of 15:45 would drop 0DTE from D's
aggregate while A kept it, and the identity would fail for a reason that has
nothing to do with attribution. `t1`'s policy is applied to all four.

One consequence worth knowing before reading a flip row: **the flip is
independent of the observation spot** — under sticky strike the observed S never
enters the exposure curve, it only sets the solver's grid window. So for the
flip metric specifically, `mechanical_component` is pure time decay. Spot's
contribution to it is exactly zero, by construction rather than by measurement.
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

METRICS = ["net_gex", "flip", "call_gex_peak", "put_gex_peak"]
IDENTITY_TOLERANCE = 1e-6


def attribute(a: float, b: float, c: float, d: float) -> dict[str, float]:
    """The §12.1 arithmetic, isolated so it can be tested without a day of data.

    Both decompositions are returned. The path-dependent pair answers "spot and
    time moved first"; the Shapley pair removes that ordering choice and is the
    one that satisfies the identity by construction.
    """
    return {
        "total_change": d - a,
        "mechanical_component": b - a,
        "surface_component": d - b,
        "mechanical_contribution": 0.5 * ((b - a) + (d - c)),
        "surface_contribution": 0.5 * ((c - a) + (d - b)),
    }


def build_common_inputs(
    config: Config,
    date_str: str,
    t0: str,
    t1: str,
    normalized_root: Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Two frames over one contract set: same rows, t0's IV and t1's IV.

    Returns (frame_iv_t0, frame_iv_t1, coverage).
    """
    oi = _normalized(date_str, "oi_base", normalized_root)
    s0 = usable_for_gex(_normalized(date_str, t0, normalized_root))
    s1 = usable_for_gex(_normalized(date_str, t1, normalized_root))

    common = s0.merge(
        s1[JOIN_KEYS + ["massive_iv"]],
        on=JOIN_KEYS,
        how="inner",
        suffixes=("", "_t1"),
    )
    common = common.merge(
        oi[JOIN_KEYS + ["open_interest"]],
        on=JOIN_KEYS,
        how="inner",
        suffixes=("_surface", "_oibase"),
    )
    if common.empty:
        raise FrozenMapError(
            f"{t0}, {t1} and the OI base share no contracts on {date_str}"
        )
    common["open_interest"] = common["open_interest_oibase"]
    common = common.drop(columns=["open_interest_surface", "open_interest_oibase"])

    frame_t0 = common.drop(columns=["massive_iv_t1"]).copy()
    frame_t1 = common.drop(columns=["massive_iv"]).rename(
        columns={"massive_iv_t1": "massive_iv"}
    ).copy()

    coverage = {
        "usable_at_t0": int(len(s0)),
        "usable_at_t1": int(len(s1)),
        "common_with_oi_base": int(len(common)),
        "dropped_vs_t0": int(len(s0) - len(common)),
        "dropped_vs_t1": int(len(s1) - len(common)),
        "iv_changed_contracts": int(
            (common["massive_iv"] != common["massive_iv_t1"]).sum()
        ),
    }
    return frame_t0, frame_t1, coverage


def _state_rows(
    frame: pd.DataFrame,
    asof: datetime,
    spot: float,
    r: float,
    q: float,
    config: Config,
    policy_label: str,
) -> dict[str, dict[str, Any]]:
    """One recomputation, keyed by expiry bucket."""
    rows = recompute_at(frame, asof, spot, r, q, config, policy_label)
    return {row["expiry_bucket"]: row for row in rows}


def build_attribution(
    config: Config,
    date_str: str,
    t0: str,
    t1: str,
    normalized_root: Path | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    tz = ZoneInfo(config.get("capture.timezone", "America/New_York"))
    r = float(config.require("rates.r"))
    q = float(config.require("rates.q"))

    spots = {row.time_label: row for row in read_transmission_spot(date_str)}
    for label in (t0, t1):
        if label not in spots:
            raise FrozenMapError(f"no transmission spot recorded for {label} on {date_str}")

    frame_t0, frame_t1, coverage = build_common_inputs(
        config, date_str, t0, t1, normalized_root
    )

    def asof_of(label: str) -> datetime:
        return datetime.combine(
            date.fromisoformat(date_str),
            datetime.strptime(label, "%H%M").time(),
            tzinfo=tz,
        )

    a0, a1 = asof_of(t0), asof_of(t1)
    s0, s1 = spots[t0].spot, spots[t1].spot

    # t1's bucket policy for all four — see the module docstring.
    A = _state_rows(frame_t0, a0, s0, r, q, config, t1)
    B = _state_rows(frame_t0, a1, s1, r, q, config, t1)
    C = _state_rows(frame_t1, a0, s0, r, q, config, t1)
    D = _state_rows(frame_t1, a1, s1, r, q, config, t1)

    # What holding membership fixed actually costs. State D uses the
    # intersection; the actual map at t1 uses t1's own usable rows. The gap is
    # the size of the artifact that would have entered `D - A` as "surface
    # repricing" had the contract set been allowed to move. Reported, not
    # corrected — fixing the set is the correct choice and this says what it cost.
    from .actual_map import build_actual_inputs  # local: avoids a circular import

    D_own = _state_rows(
        build_actual_inputs(config, date_str, t1, normalized_root),
        a1,
        s1,
        r,
        q,
        config,
        t1,
    )

    buckets = [b for b in A if b in B and b in C and b in D]
    rows: list[dict[str, Any]] = []
    identity_failures: list[str] = []
    skipped_metrics: list[str] = []

    for bucket in buckets:
        for metric in METRICS:
            a, b, c, d = (state[bucket].get(metric) for state in (A, B, C, D))
            if any(v is None for v in (a, b, c, d)):
                # A flip that has no root in one of the four states leaves the
                # difference undefined. Recorded, not quietly dropped — a
                # missing row and a row that was never attempted look identical
                # downstream otherwise.
                absent = [n for n, v in zip("ABCD", (a, b, c, d)) if v is None]
                skipped_metrics.append(
                    f"{bucket}/{metric}: undefined in state(s) {','.join(absent)}"
                )
                continue
            a, b, c, d = float(a), float(b), float(c), float(d)
            raw_own = D_own.get(bucket, {}).get(metric)
            d_own = None if raw_own is None else float(raw_own)
            parts = attribute(a, b, c, d)
            total = parts["total_change"]
            mech_contrib = parts["mechanical_contribution"]
            surf_contrib = parts["surface_contribution"]
            residual = (mech_contrib + surf_contrib) - total
            scale = max(abs(total), abs(a), 1.0)
            if abs(residual) > IDENTITY_TOLERANCE * scale:
                identity_failures.append(f"{bucket}/{metric}: residual {residual:.6g}")
            rows.append(
                {
                    "date": date_str,
                    "t0": t0,
                    "t1": t1,
                    "expiry_bucket": bucket,
                    "metric": metric,
                    "state_A": a, "state_B": b, "state_C": c, "state_D": d,
                    **parts,
                    "identity_residual": residual,
                    "state_D_unconstrained": d_own,
                    "membership_effect": None if d_own is None else d_own - d,
                    "membership_effect_pct_of_total": (
                        None if (d_own is None or not total)
                        else 100.0 * (d_own - d) / total
                    ),
                    "spot_t0": s0, "spot_t1": s1,
                    "n_contracts": int(len(frame_t0)),
                    "iv_alignment": config.get("frozen_map.iv_alignment", "sticky_strike"),
                    "bucket_policy_label": t1,
                    "attribution_is_counterfactual": True,
                    "r": r, "q": q,
                    "rates_provisional": bool(config.get("rates.provisional", False)),
                }
            )

    table = pd.DataFrame(rows)
    meta = {
        "date": date_str, "t0": t0, "t1": t1,
        "spot_t0": s0, "spot_t1": s1,
        "coverage": coverage,
        "buckets": buckets,
        "metrics": METRICS,
        "rows": len(table),
        "identity_gate_passed": not identity_failures,
        "identity_failures": identity_failures,
        "metrics_skipped": skipped_metrics,
        "identity_tolerance_relative": IDENTITY_TOLERANCE,
        "bucket_policy_label": t1,
        "r": r, "q": q,
        "note": "counterfactual attribution under a declared model; not a causal "
                "decomposition (§12)",
    }
    return table, meta


def write_attribution(
    table: pd.DataFrame,
    meta: dict[str, Any],
    date_str: str,
    t0: str,
    t1: str,
    output_root: Path | None = None,
) -> tuple[Path, Path]:
    out_dir = (
        Path(output_root) / date_str
        if output_root is not None
        else repo_path("data", "derived", date_str)
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = out_dir / f"attribution_{t0}_{t1}.parquet"
    meta_path = out_dir / f"attribution_{t0}_{t1}.meta.json"
    table.to_parquet(parquet_path, index=False)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True, default=str))
    return parquet_path, meta_path
