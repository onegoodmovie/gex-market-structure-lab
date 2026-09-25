"""Greek validation (spec §7). The gate M1 exists to pass.

Three independent things live here:

  Path A   massive_iv -> our gamma, compared to massive_gamma. Hard gate at 1%
           median absolute percentage error.
  §7.2     grid search over r, q, time convention and expiration source, to
           find which set of conventions actually reconciles.
  §3.2     put-call IV consistency — the cross-check that replaces Path B on a
           quotes-free tier.

Path B (invert IV from mid, then gamma) is not implemented and cannot be: the
tier returns no quotes. It is reported as N/A with a reason, which is a
recorded fact and not a failed gate.

---
One structural fact about the grid search, worth stating before reading its
output. Gamma depends on r and q through two different routes:

    d₁ contains (r - q)          strong
    the e^{-qT} factor contains q alone   weak

So the surface is a narrow valley running along constant `r - q`, with only
gentle curvature across it. At T = 0.25 and q = 1.3%, the e^{-qT} factor moves
gamma by 0.3% — below the 1% gate. At T = 1.0 it moves it by 1.3%, above.

The practical consequence: `r - q` is well determined by this data and `r` and
`q` separately are not, except through longer-dated contracts. A 2-D grid will
report several (r, q) pairs as near-identical, and that is the truth about what
the data can identify, not a defect in the search. The output reports the
implied `r - q` as the primary finding and flags combos that are
indistinguishable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .expiry import SECONDS_PER_YEAR
from .greeks import gamma as bs_gamma, vega as bs_vega

ATM_BAND = 0.005          # |ln(K/S)| within 0.5% counts as at-the-money
MONEYNESS_CLASSES = ("ITM", "ATM", "OTM")
SAMPLE_BUCKETS = ("0DTE", "1-7DTE", "8-30DTE")


class ValidationError(RuntimeError):
    pass


# --- sample construction (§7) -------------------------------------------------


def moneyness_class(option_type: str, strike: float, spot: float) -> str:
    x = math.log(strike / spot)
    if abs(x) <= ATM_BAND:
        return "ATM"
    if option_type == "call":
        return "ITM" if x < 0 else "OTM"
    return "ITM" if x > 0 else "OTM"


def usable_for_path_a(frame: pd.DataFrame) -> pd.DataFrame:
    """Path A needs both a Massive IV and a Massive gamma to compare against."""
    return frame[
        frame["massive_iv"].notna()
        & (frame["massive_iv"] > 0)
        & frame["massive_gamma"].notna()
        & (frame["massive_gamma"] > 0)
        & frame["underlying_spot"].notna()
        & (frame["t_years"] > 0)
    ].copy()


def build_sample(
    frame: pd.DataFrame,
    target_size: int = 24,
    min_am_settled: int = 2,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """~24 contracts spanning ITM/ATM/OTM × call/put × three DTE buckets,
    with at least two AM-settled SPX contracts (§7, §16.7).

    Deterministic: cells are filled by ranking within each cell, never randomly,
    so the same stored day always yields the same sample.
    """
    usable = usable_for_path_a(frame)
    if usable.empty:
        raise ValidationError(
            "no contracts carry both massive_iv and massive_gamma — Path A cannot run"
        )

    usable["moneyness_class"] = [
        moneyness_class(t, k, s)
        for t, k, s in zip(
            usable["option_type"], usable["strike"], usable["underlying_spot"]
        )
    ]
    usable["abs_log_moneyness"] = (
        np.log(usable["strike"] / usable["underlying_spot"])
    ).abs()

    picked_index: list[Any] = []
    empty_cells: list[str] = []
    for bucket in SAMPLE_BUCKETS:
        for option_type in ("call", "put"):
            for klass in MONEYNESS_CLASSES:
                cell = usable[
                    (usable["expiry_bucket"] == bucket)
                    & (usable["option_type"] == option_type)
                    & (usable["moneyness_class"] == klass)
                ]
                if cell.empty:
                    empty_cells.append(f"{bucket}/{option_type}/{klass}")
                    continue
                # Nearest the money within the cell: where gamma is largest and
                # a convention error shows up most clearly.
                picked_index.append(cell["abs_log_moneyness"].idxmin())

    # §16.7 — AM-settled contracts must be in the sample and reconciling.
    am = usable[usable["am_settled_monthly"]]
    am_in_sample = am.index.intersection(picked_index)
    am_added = 0
    if len(am_in_sample) < min_am_settled:
        need = min_am_settled - len(am_in_sample)
        extra = (
            am.drop(index=am_in_sample, errors="ignore")
            .sort_values("abs_log_moneyness")
            .head(need)
        )
        picked_index.extend(extra.index.tolist())
        am_added = len(extra)

    # Top up towards the target with the next-most-ATM contracts not yet chosen.
    if len(picked_index) < target_size:
        remaining = usable.drop(index=picked_index, errors="ignore")
        top_up = remaining.sort_values("abs_log_moneyness").head(
            target_size - len(picked_index)
        )
        picked_index.extend(top_up.index.tolist())

    sample = usable.loc[picked_index].copy()
    coverage = {
        "size": len(sample),
        "target_size": target_size,
        "empty_cells": empty_cells,
        "am_settled_in_sample": int(sample["am_settled_monthly"].sum()),
        "am_settled_added_deliberately": am_added,
        "by_bucket": sample["expiry_bucket"].value_counts().to_dict(),
        "by_moneyness": sample["moneyness_class"].value_counts().to_dict(),
        "by_type": sample["option_type"].value_counts().to_dict(),
        "meets_am_settled_rule": int(sample["am_settled_monthly"].sum()) >= min_am_settled,
    }
    return sample, coverage


# --- Path A -------------------------------------------------------------------


@dataclass
class PathAResult:
    r: float
    q: float
    n: int
    median_abs_pct_error: float
    mean_pct_error: float
    p90_abs_pct_error: float
    passed: bool
    bias_by_moneyness: dict[str, float]
    bias_by_bucket: dict[str, float]
    bias_by_settlement: dict[str, float]
    by_bucket: dict[str, dict[str, float]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "r": self.r,
            "q": self.q,
            "r_minus_q": round(self.r - self.q, 6),
            "n": self.n,
            "median_abs_pct_error": self.median_abs_pct_error,
            "mean_pct_error": self.mean_pct_error,
            "p90_abs_pct_error": self.p90_abs_pct_error,
            "passed": self.passed,
            "bias_by_moneyness": self.bias_by_moneyness,
            "bias_by_bucket": self.bias_by_bucket,
            "bias_by_settlement": self.bias_by_settlement,
            "by_bucket": self.by_bucket,
        }


def path_a(
    sample: pd.DataFrame,
    r: float,
    q: float,
    gate_pct: float = 1.0,
    massive_expiry_offset_hours: float = 0.0,
) -> PathAResult:
    """Feed Massive IV into our gamma; compare against Massive's gamma.

    Same closed form, same inputs — so any gap is a bug in our T, r, or q, not
    a modelling difference. That is what makes 1% a reasonable hard gate.

    `massive_expiry_offset_hours` shifts T to Massive's expiry convention for
    the duration of the comparison only. It exists because the two are not the
    same thing and conflating them makes the test meaningless: our §6.2
    settlement times are a statement about the market, while Path A is a
    statement about our formula. Comparing our gamma-at-the-true-expiry against
    their gamma-at-their-expiry measures the convention gap, not the formula.
    The study's own GEX always uses our settlement times; this offset never
    leaves the validation path.
    """
    t_adj = sample["t_years"] + massive_expiry_offset_hours * 3600.0 / SECONDS_PER_YEAR
    ours = [
        bs_gamma(s, k, r, q, iv, t)
        for s, k, iv, t in zip(
            sample["underlying_spot"],
            sample["strike"],
            sample["massive_iv"],
            t_adj,
        )
    ]
    work = sample.copy()
    work["our_gamma"] = ours
    work = work[work["our_gamma"].notna()]
    work["pct_error"] = (
        (work["our_gamma"] - work["massive_gamma"]) / work["massive_gamma"] * 100.0
    )
    work["abs_pct_error"] = work["pct_error"].abs()

    def _median_by(column: str) -> dict[str, float]:
        return {
            str(key): round(float(value), 4)
            for key, value in work.groupby(column)["pct_error"].median().items()
        }

    # Per bucket, median AND p90. One number for a whole chain hides the thing
    # that matters: long-dated contracts dominate the median while the short end
    # carries the error. Reporting both, split by tenor, is the revised §7.1.
    by_bucket = {
        str(bucket): {
            "n": int(len(leg)),
            "median_abs_pct_error": round(float(leg["abs_pct_error"].median()), 4),
            "p90_abs_pct_error": round(float(leg["abs_pct_error"].quantile(0.9)), 4),
            "median_signed_pct_error": round(float(leg["pct_error"].median()), 4),
        }
        for bucket, leg in work.groupby("expiry_bucket")
    }

    median_abs = float(work["abs_pct_error"].median())
    return PathAResult(
        r=r,
        q=q,
        n=len(work),
        median_abs_pct_error=round(median_abs, 6),
        mean_pct_error=round(float(work["pct_error"].mean()), 6),
        p90_abs_pct_error=round(float(work["abs_pct_error"].quantile(0.9)), 6),
        passed=median_abs < gate_pct,
        bias_by_moneyness=_median_by("moneyness_class"),
        bias_by_bucket=_median_by("expiry_bucket"),
        bias_by_settlement=_median_by("settlement_type"),
        by_bucket=by_bucket,
    )


def systematic_tilt(bias: dict[str, float], tolerance_pct: float = 0.5) -> bool:
    """A spread across categories wider than tolerance is a §7.2 stop condition
    even when the overall median passes."""
    if len(bias) < 2:
        return False
    return (max(bias.values()) - min(bias.values())) > tolerance_pct


# --- §7.2 grid search ---------------------------------------------------------


def grid_search(
    sample: pd.DataFrame,
    r_candidates: dict[str, float | None],
    q_candidates: dict[str, float | None],
    gate_pct: float = 1.0,
    indistinguishable_pct: float = 0.05,
) -> dict[str, Any]:
    """Score every (r, q) combination and report what the data can identify."""
    skipped = [
        f"r.{name}" for name, value in r_candidates.items() if value is None
    ] + [f"q.{name}" for name, value in q_candidates.items() if value is None]

    combos: list[dict[str, Any]] = []
    for r_name, r_value in r_candidates.items():
        if r_value is None:
            continue
        for q_name, q_value in q_candidates.items():
            if q_value is None:
                continue
            result = path_a(sample, float(r_value), float(q_value), gate_pct)
            row = result.as_dict()
            row.update({"r_name": r_name, "q_name": q_name})
            row["systematic_tilt_moneyness"] = systematic_tilt(result.bias_by_moneyness)
            row["systematic_tilt_dte"] = systematic_tilt(result.bias_by_bucket)
            combos.append(row)

    combos.sort(key=lambda row: row["median_abs_pct_error"])
    best = combos[0] if combos else None
    ties = (
        [
            c
            for c in combos[1:]
            if c["median_abs_pct_error"] - best["median_abs_pct_error"]
            < indistinguishable_pct
        ]
        if best
        else []
    )

    return {
        "combos": combos,
        "best": best,
        "indistinguishable_from_best": ties,
        "indistinguishable_threshold_pct": indistinguishable_pct,
        "skipped_null_candidates": skipped,
        "note": (
            "gamma depends on (r-q) strongly and on q alone only through e^{-qT}; "
            "combos sharing r-q will score almost identically and that is a "
            "property of the data, not of the search"
        ),
    }


def massive_implied_t(
    frame: pd.DataFrame,
    vega_per_vol_point: bool = True,
    band_pct: float = 1.0,
    min_strikes: int = 5,
    max_expiries: int = 10,
) -> dict[str, Any]:
    """Measure Massive's time-to-expiry directly (spec §7.2).

    Uses the identity, from their own greeks and nothing else:

        vega / gamma = S² · σ · T

    **Why this and not a fit to the gamma curve.** Near the money gamma is
    essentially `φ(d₁)/(S·σ√T)` with `d₁ ≈ ln(S/K)/(σ√T)`, so it depends on σ
    and T only through the product `σ√T`. Fitting T against gamma alone cannot
    separate the two, and it silently absorbs any σ-side difference into T. On
    2026-07-27 that artefact was large and convincing: a joint (S, T) fit to the
    gamma curve produced a dT that fell steadily with tenor and crossed into
    negative territory around 4.6 days, which reads like a day-count effect and
    is not one. Vega carries √T outside the ratio, so the degeneracy breaks and
    T comes out on its own.

    The same day, measured this way, dT was flat at +2.8 to +3.9 hours from
    0DTE out to 12 DTE — no trend — which is the signature of a fixed expiry
    *time* convention rather than a delay or a day count.

    `vega_per_vol_point` reflects Massive quoting vega per 1% of vol rather
    than per 1.00; on Massive this is the case and the ratio needs the factor of
    100 back.
    """
    usable = frame[
        frame["massive_vega"].notna()
        & (frame["massive_vega"] > 0)
        & frame["massive_gamma"].notna()
        & (frame["massive_gamma"] > 0)
        & frame["massive_iv"].notna()
        & (frame["massive_iv"] > 0)
        & frame["underlying_spot"].notna()
        & (frame["t_years"] > 0)
    ]
    if usable.empty:
        return {"available": False, "reason": "Massive vega or gamma unusable"}

    spot = float(usable["underlying_spot"].iloc[0])
    scale = 100.0 if vega_per_vol_point else 1.0
    rows: list[dict[str, Any]] = []
    for expiry in sorted(usable["expiration_date"].unique())[:max_expiries]:
        leg = usable[
            (usable["expiration_date"] == expiry)
            & (usable["option_type"] == "call")
            & (usable["strike"] > spot * (1 - band_pct / 100.0))
            & (usable["strike"] < spot * (1 + band_pct / 100.0))
        ]
        if len(leg) < min_strikes:
            continue
        ours = float(leg["t_years"].iloc[0])
        implied = float(
            (
                leg["massive_vega"] / leg["massive_gamma"] * scale
                / (spot * spot * leg["massive_iv"])
            ).median()
        )
        rows.append(
            {
                "expiration_date": str(expiry),
                "n_strikes": int(len(leg)),
                "our_t_hours": round(ours * 8760.0, 3),
                "massive_t_hours": round(implied * 8760.0, 3),
                "delta_t_hours": round((implied - ours) * 8760.0, 3),
            }
        )

    if not rows:
        return {"available": False, "reason": "no expiry had enough usable strikes"}

    deltas = [r["delta_t_hours"] for r in rows]
    tenors = [r["our_t_hours"] for r in rows]
    trend = (
        float(np.polyfit(tenors, deltas, 1)[0]) if len(rows) >= 3 else float("nan")
    )
    return {
        "available": True,
        "by_expiry": rows,
        "median_delta_t_hours": round(float(np.median(deltas)), 3),
        "delta_t_range_hours": [round(min(deltas), 3), round(max(deltas), 3)],
        "trend_hours_per_hour_of_tenor": round(trend, 6),
        "flat_across_tenor": bool(abs(trend) < 0.005),
        "reading": (
            "constant offset across tenor — a fixed expiry-time convention"
            if abs(trend) < 0.005
            else "offset varies with tenor — not a simple expiry-time convention"
        ),
    }


def fit_massive_conventions(
    frame: pd.DataFrame,
    r_minus_q: float = 0.03,
    band_pct: float = 1.5,
    min_strikes: int = 8,
    max_expiries: int = 3,
) -> dict[str, Any]:
    """Recover the spot and the time-to-expiry Massive's gamma implies
    (spec §7.2, the expiration-timestamp and as-of dimensions).

    Fits S and T jointly by least squares against Massive's own gamma curve,
    taking their sigma(K) at every strike as an input. Two reasons that matters:

    * **Skew-robust.** Gamma carries a 1/sigma factor, so a downward-sloping
      smile pushes the apparent gamma peak above S. Reading the peak location
      as "their spot" therefore overstates it — measured at +1 to +4 points on
      2026-07-27. Feeding their smile in removes that bias instead of modelling
      it.
    * **Separates two things a scan cannot.** S moves the curve sideways, T
      changes its width and height, so both are identifiable from one curve.
      A scan over T alone has to assume S, and inherits every error in it.

    This replaced a scan that scored candidate offsets on p90 error. That scan
    was right about one thing worth keeping in mind: **the median is the wrong
    statistic for this**. A chain is dominated by longer-dated contracts, to
    which hours of expiry offset are invisible, so a real offset can move the
    median from 1.68% to 1.84% while p90 collapses from 78.8% to 6.4%.
    Optimising the median concludes "no offset" and is badly wrong. The fit
    sidesteps the issue by working on one expiry at a time, where the signal is.

    `r_minus_q` is weakly identified here and is passed rather than fitted; on
    short-dated contracts it moves the answer far less than S or T do.

    Returns a per-expiry table plus a summary. Agreement of `delta_t_hours`
    across expiries and across captures is the evidence that it is a
    convention rather than noise.
    """
    from scipy.optimize import least_squares

    usable = usable_for_path_a(frame)
    if usable.empty:
        return {"available": False, "reason": "no contracts with both IV and gamma"}

    spot = float(usable["underlying_spot"].iloc[0])
    fits: list[dict[str, Any]] = []
    for expiry in sorted(usable["expiration_date"].unique())[:max_expiries]:
        leg = usable[
            (usable["expiration_date"] == expiry)
            & (usable["option_type"] == "call")
            & (usable["strike"] > spot * (1 - band_pct / 100.0))
            & (usable["strike"] < spot * (1 + band_pct / 100.0))
        ].sort_values("strike")
        if len(leg) < min_strikes:
            continue

        t0 = float(leg["t_years"].iloc[0])
        strikes = leg["strike"].to_numpy(dtype=float)
        ivs = leg["massive_iv"].to_numpy(dtype=float)
        target = leg["massive_gamma"].to_numpy(dtype=float)

        def residual(params):
            s_fit, t_fit = params
            if t_fit <= 0:
                return np.full_like(target, 1e3)
            ours = np.array(
                [bs_gamma(s_fit, k, r_minus_q, 0.0, iv, t_fit) or np.nan
                 for k, iv in zip(strikes, ivs)]
            )
            return (ours - target) / target

        try:
            solution = least_squares(
                residual,
                [spot, t0],
                bounds=([spot * 0.99, t0 * 0.2], [spot * 1.01, t0 * 20]),
            )
        except Exception:  # pragma: no cover - solver refusal is data-dependent
            continue

        s_fit, t_fit = solution.x
        fits.append(
            {
                "expiration_date": str(expiry),
                "n_strikes": int(len(leg)),
                "our_spot": round(spot, 2),
                "fitted_spot": round(float(s_fit), 2),
                "delta_spot_pts": round(float(s_fit) - spot, 2),
                "our_t_hours": round(t0 * 8760.0, 3),
                "fitted_t_hours": round(float(t_fit) * 8760.0, 3),
                "delta_t_hours": round((float(t_fit) - t0) * 8760.0, 3),
                "rms_pct": round(float(np.sqrt(np.mean(solution.fun**2)) * 100), 3),
            }
        )

    if not fits:
        return {"available": False, "reason": "no expiry had enough usable strikes"}

    deltas = [f["delta_t_hours"] for f in fits]
    spots = [f["delta_spot_pts"] for f in fits]
    return {
        "available": True,
        "fits": fits,
        "median_delta_t_hours": round(float(np.median(deltas)), 3),
        "delta_t_spread_hours": round(float(max(deltas) - min(deltas)), 3),
        "median_delta_spot_pts": round(float(np.median(spots)), 2),
        "median_rms_pct": round(float(np.median([f["rms_pct"] for f in fits])), 3),
        "note": (
            "delta_spot is a level, not a lag: a delayed spot would change sign "
            "with the direction of the market and this does not"
        ),
    }


def implied_drift_scan(
    sample: pd.DataFrame,
    q_reference: float = 0.0,
    low: float = -0.05,
    high: float = 0.15,
    steps: int = 401,
    massive_expiry_offset_hours: float = 0.0,
) -> dict[str, Any]:
    """The 'Massive-implied' candidate: scan b = r - q for the best fit.

    This is the one-dimensional direction the data actually constrains. It
    answers "what forward drift reconciles Massive's gamma", which is a
    sharper question than "which of four named rates is it".
    """
    grid = np.linspace(low, high, steps)
    errors = []
    for b in grid:
        result = path_a(
            sample,
            r=float(b) + q_reference,
            q=q_reference,
            massive_expiry_offset_hours=massive_expiry_offset_hours,
        )
        errors.append(result.median_abs_pct_error)

    errors_array = np.asarray(errors)
    best_index = int(errors_array.argmin())
    best_b = float(grid[best_index])
    best_error = float(errors_array[best_index])

    # How wide is the basin that stays under the 1% gate? A wide basin means the
    # data cannot pin the drift down, whatever the argmin says.
    under_gate = grid[errors_array < 1.0]
    return {
        "implied_r_minus_q": round(best_b, 6),
        "median_abs_pct_error_at_best": round(best_error, 6),
        "q_reference": q_reference,
        "basin_under_1pct": (
            [round(float(under_gate.min()), 6), round(float(under_gate.max()), 6)]
            if under_gate.size
            else None
        ),
        "basin_width": (
            round(float(under_gate.max() - under_gate.min()), 6)
            if under_gate.size
            else None
        ),
        "scan_range": [low, high],
        "steps": steps,
    }


# --- §3.2 put-call IV consistency --------------------------------------------


def put_call_iv_consistency(
    frame: pd.DataFrame,
    min_pairs: int = 20,
    max_abs_intercept: float = 0.005,
    max_abs_slope: float = 0.02,
    r_for_discount: float = 0.045,
    min_vega_fraction: float = 0.05,
) -> dict[str, Any]:
    """Call/put IV agreement at matched (root, expiry, strike), per bucket.

    Independent of Path A: no external input, no assumed r or q — only a
    relation Massive's own numbers must satisfy if its forward is
    self-consistent. Without quotes it is the only such evidence available.

    Two readings are produced, and the second is the sharper one.

    **Regression** of `iv_call - iv_put` on `ln(K/S)`. Note what carries the
    signal here: to first order

        σ_call - σ_put ≈ -e^{-rT}·ΔF / vega

    because `N(d₂) + N(-d₂) = 1` exactly — moneyness enters only through vega.
    So a forward error appears as a **non-zero level**, shaped like 1/vega and
    symmetric in d₁, not as a tilt. Any slope the regression reports is a
    by-product of that U-shape being sampled asymmetrically, and it moves with
    the strike distribution. The intercept is the reliable term; the slope is
    reported because it was asked for, and should be read as secondary.

    **Implied forward error**, which is the sharp version:

        ΔF = -(σ_call - σ_put) · vega · e^{rT}

    If Massive's forward is misspecified by a constant, this is constant
    across strikes. Its dispersion says whether the story holds, and its level
    converts straight into a `r - q` error:

        Δ(r-q) ≈ ln(1 + ΔF/S) / T

    That gives §7.2 a number to check its grid search against, rather than a
    yes/no.
    """
    usable = frame[
        frame["massive_iv"].notna()
        & (frame["massive_iv"] > 0)
        & frame["underlying_spot"].notna()
    ].copy()

    calls = usable[usable["option_type"] == "call"]
    puts = usable[usable["option_type"] == "put"]
    keys = ["root", "expiration_date", "strike"]
    pairs = calls.merge(puts, on=keys, suffixes=("_call", "_put"))
    if pairs.empty:
        return {
            "available": False,
            "reason": "no strike carried both a call and a put with usable IV",
        }

    pairs["iv_spread"] = pairs["massive_iv_call"] - pairs["massive_iv_put"]
    pairs["log_moneyness"] = np.log(pairs["strike"] / pairs["underlying_spot_call"])

    # Sharp diagnostic: the forward error each pair implies. Uses our own vega
    # at Massive's IV — an r/q choice enters only through the discount
    # factor, which is a second-order effect on this quantity.
    pairs["vega"] = [
        bs_vega(s, k, r_for_discount, 0.0, iv, t)
        for s, k, iv, t in zip(
            pairs["underlying_spot_call"],
            pairs["strike"],
            pairs["massive_iv_call"],
            pairs["t_years_call"],
        )
    ]
    pairs["implied_forward_error"] = -(
        pairs["iv_spread"] * pairs["vega"] * np.exp(r_for_discount * pairs["t_years_call"])
    )

    # Where vega is tiny, IV is barely determined by price and `iv_call -
    # iv_put` (which goes as 1/vega) explodes. Left in, a handful of 0DTE wing
    # strikes dominate an unweighted regression completely — in testing they
    # drove the fitted intercept to -2.6e8. Restrict to the region where IV
    # actually carries information, per expiry so the cut is scale-free.
    pairs["_max_vega_in_expiry"] = pairs.groupby(
        ["root", "expiration_date"]
    )["vega"].transform("max")
    well_determined = pairs["vega"] >= min_vega_fraction * pairs["_max_vega_in_expiry"]
    excluded_low_vega = int((~well_determined).sum())
    pairs = pairs[well_determined]
    if pairs.empty:
        return {
            "available": False,
            "reason": (
                f"every pair fell below the vega floor "
                f"({min_vega_fraction:.0%} of the ATM vega for its expiry)"
            ),
        }

    results: dict[str, Any] = {}
    for bucket, group in pairs.groupby("expiry_bucket_call"):
        if pd.isna(bucket):
            continue
        entry: dict[str, Any] = {
            "n_pairs": int(len(group)),
            "median_iv_spread": round(float(group["iv_spread"].median()), 6),
        }
        if len(group) < min_pairs:
            entry.update(
                {
                    "sufficient": False,
                    "reason": f"only {len(group)} pairs; need {min_pairs}",
                }
            )
        else:
            slope, intercept = np.polyfit(
                group["log_moneyness"], group["iv_spread"], 1
            )
            fitted = slope * group["log_moneyness"] + intercept
            residual = group["iv_spread"] - fitted
            total = group["iv_spread"] - group["iv_spread"].mean()
            total_ss = float((total**2).sum())
            # An identically flat spread is a perfect result, not an undefined
            # one: R² has no meaning when there is no variance to explain.
            r_squared = (
                round(1.0 - float((residual**2).sum()) / total_ss, 4)
                if total_ss > 1e-18
                else None
            )
            entry.update(
                {
                    "sufficient": True,
                    "slope": round(float(slope), 6),
                    "intercept": round(float(intercept), 6),
                    "r_squared": r_squared,
                    "r_squared_note": (
                        None if r_squared is not None
                        else "spread has no variance — nothing to explain"
                    ),
                    "residual_std": round(float(residual.std()), 6),
                    "slope_within_tolerance": abs(float(slope)) <= max_abs_slope,
                    "intercept_within_tolerance": abs(float(intercept))
                    <= max_abs_intercept,
                }
            )
            entry.update(_forward_error_summary(group))
            entry["reading"] = _read_consistency(entry)
        results[str(bucket)] = entry

    checked = [e for e in results.values() if e.get("sufficient")]
    return {
        "available": True,
        "by_bucket": results,
        "total_pairs": int(len(pairs)),
        "excluded_low_vega": excluded_low_vega,
        "min_vega_fraction": min_vega_fraction,
        "max_abs_slope": max_abs_slope,
        "max_abs_intercept": max_abs_intercept,
        "all_within_tolerance": bool(checked)
        and all(
            e["slope_within_tolerance"] and e["intercept_within_tolerance"]
            for e in checked
        ),
        "note": (
            "diagnostic, not a gate. A systematic slope is §7.2 evidence that "
            "r-q is misspecified and a stop-and-report, never something to tune away"
        ),
    }


def _forward_error_summary(group: pd.DataFrame) -> dict[str, Any]:
    """Median implied forward error and how well the constant-ΔF story holds."""
    valid = group[group["implied_forward_error"].notna()]
    if valid.empty:
        return {"implied_forward_error_pts": None}

    errors = valid["implied_forward_error"]
    median_error = float(errors.median())
    spread_of_errors = float(errors.std())
    spot = float(valid["underlying_spot_call"].median())
    t_years = float(valid["t_years_call"].median())

    drift_error = (
        math.log1p(median_error / spot) / t_years
        if t_years > 0 and (median_error / spot) > -0.99
        else None
    )
    return {
        "implied_forward_error_pts": round(median_error, 4),
        "implied_forward_error_std_pts": round(spread_of_errors, 4),
        # Constant ΔF across strikes is what a genuine forward error looks like.
        # A large dispersion means something else is going on (smoothing, stale
        # legs) and the level should not be read as a rate error.
        "forward_error_is_constant": bool(
            abs(median_error) > 1e-9
            and spread_of_errors < 0.5 * abs(median_error)
        ),
        "implied_r_minus_q_error": round(drift_error, 6) if drift_error else None,
    }


def _read_consistency(entry: dict[str, Any]) -> str:
    """Interpretation. The intercept carries the forward-error signal; the slope
    is a by-product of the 1/vega U-shape and is reported as secondary."""
    drift_error = entry.get("implied_r_minus_q_error")
    constant = entry.get("forward_error_is_constant")

    if entry["intercept_within_tolerance"] and entry["slope_within_tolerance"]:
        return "call and put agree; Massive's forward is self-consistent"

    if not entry["intercept_within_tolerance"] and constant and drift_error:
        direction = "higher" if drift_error > 0 else "lower"
        return (
            f"level offset {entry['intercept']:+.4f} with a near-constant implied "
            f"forward error of {entry['implied_forward_error_pts']:+.1f} pts — the "
            f"signature of a misspecified forward. Vendor's r-q looks {direction} "
            f"than ours by ~{abs(drift_error):.4f}. Independent §7.2 evidence."
        )

    if not entry["intercept_within_tolerance"]:
        return (
            f"level offset {entry['intercept']:+.4f}, but the implied forward "
            f"error is not constant across strikes (std "
            f"{entry.get('implied_forward_error_std_pts')} pts) — more consistent "
            "with Massive smoothing or stale legs than with a rate error"
        )

    return (
        f"tilt without a level offset (slope {entry['slope']:+.4f}). A forward "
        "error would show up as a level, so this is more likely an asymmetric "
        "strike sample than a rate problem — treat as weak evidence"
    )


def q1_smoke_test(
    self_flip: float | None,
    self_net_gex: float | None,
    reference_flip: float | None,
    reference_net_gex: float | None,
    spot: float,
    flip_max_abs_diff_pts: float = 50.0,
    net_gex_max_log10_ratio: float = 1.0,
) -> dict[str, Any]:
    """Q1, as a smoke test rather than a calibration (spec §2 Q1, revised).

    The study is an observation framework, not a replacement for optioncharts'
    numbers, so the question is "are we in the same world" — not "do we agree to
    a tolerance". Three checks, all coarse on purpose:

    * the flip lands on the same side of spot
    * the flips are within `flip_max_abs_diff_pts` of each other
    * net GEX has the same sign and is within an order of magnitude

    Deliberately *not* here: a median error statistic, a calibration constant,
    or any attempt to close the gap. optioncharts' conventions differ from ours
    in ways that are documented and not worth chasing (METHODOLOGY §4a), and a
    tight tolerance on top of that would be measuring the convention gap.
    """
    checks: dict[str, Any] = {"available": True, "failures": []}

    if self_flip is None or reference_flip is None:
        checks["flip"] = {"comparable": False, "reason": "a flip is missing"}
    else:
        same_side = (self_flip > spot) == (reference_flip > spot)
        diff = self_flip - reference_flip
        checks["flip"] = {
            "comparable": True,
            "self": self_flip,
            "reference": reference_flip,
            "diff_pts": round(diff, 4),
            "same_side_of_spot": bool(same_side),
            "within_range": bool(abs(diff) <= flip_max_abs_diff_pts),
        }
        if not same_side:
            checks["failures"].append("flip on the opposite side of spot from optioncharts")
        if abs(diff) > flip_max_abs_diff_pts:
            checks["failures"].append(
                f"flip differs by {diff:+.1f} pts, beyond ±{flip_max_abs_diff_pts:.0f}"
            )

    if not self_net_gex or not reference_net_gex:
        checks["net_gex"] = {"comparable": False, "reason": "a net GEX is missing or zero"}
    else:
        same_sign = (self_net_gex > 0) == (reference_net_gex > 0)
        ratio = abs(self_net_gex) / abs(reference_net_gex)
        log_ratio = abs(math.log10(ratio)) if ratio > 0 else float("inf")
        checks["net_gex"] = {
            "comparable": True,
            "self": self_net_gex,
            "reference": reference_net_gex,
            "ratio": round(ratio, 4),
            "log10_ratio": round(log_ratio, 4),
            "same_sign": bool(same_sign),
            "same_order_of_magnitude": bool(log_ratio <= net_gex_max_log10_ratio),
        }
        if not same_sign:
            checks["failures"].append("net GEX has the opposite sign to optioncharts'")
        if log_ratio > net_gex_max_log10_ratio:
            checks["failures"].append(
                f"net GEX differs by {ratio:.1f}x, beyond one order of magnitude"
            )

    checks["passed"] = not checks["failures"]
    checks["note"] = (
        "smoke test: same world, not same number. A failure here means something "
        "structural is wrong, not that a calibration is off."
    )
    return checks


def path_b_not_applicable(reason: str) -> dict[str, Any]:
    """Path B is impossible without quotes. Recorded, not scored as a failure."""
    return {
        "status": "not_applicable",
        "reason": reason,
        "is_failure": False,
        "spec_reference": "§7.1 Path B; §16.5 reads as 'Path A < 1%, Path B N/A (tier)'",
    }
