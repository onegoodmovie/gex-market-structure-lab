"""Gamma exposure from a normalized chain (spec §9).

```
GEX_contract = Γ · OI · multiplier · S² · 0.01        # dollars per 1% move
Net GEX      = Σ_calls GEX_contract - Σ_puts GEX_contract
```

The sign convention — dealers long calls, short puts relative to customers —
is the standard naive one. It is an assumption, not a measurement, and it is
stamped on every row rather than left implicit.

Everything here is a pure function of a frame plus (r, q). Nothing reads
config or disk, so the flip solver can call it thousands of times at trial
spots without re-plumbing anything.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .greeks import gamma as bs_gamma

DOLLARS_PER_ONE_PERCENT = 0.01


def contract_gex(
    frame: pd.DataFrame,
    spot: float,
    r: float,
    q: float,
    t_years: pd.Series | np.ndarray | None = None,
    iv: pd.Series | np.ndarray | None = None,
) -> pd.Series:
    """Signed dollar gamma per 1% move, one value per contract.

    `t_years` and `iv` override the frame's columns so the same function serves
    the frozen map (frozen IV, recomputed T) and the actual map (refreshed IV)
    without either needing its own copy of the formula.

    Contracts whose gamma is undefined return NaN, never 0.0 — a silent zero
    would sum into net GEX as though it were a measurement of nothing.
    """
    t = frame["t_years"] if t_years is None else t_years
    sigma = frame["massive_iv"] if iv is None else iv

    gammas = np.array(
        [
            bs_gamma(spot, k, r, q, s, tt) if (s is not None and tt is not None) else None
            for k, s, tt in zip(frame["strike"], sigma, t)
        ],
        dtype=object,
    )
    gammas = pd.Series(
        [np.nan if g is None else float(g) for g in gammas], index=frame.index
    )

    magnitude = (
        gammas
        * frame["open_interest"]
        * frame["multiplier"]
        * spot
        * spot
        * DOLLARS_PER_ONE_PERCENT
    )
    sign = np.where(frame["option_type"].to_numpy() == "call", 1.0, -1.0)
    return magnitude * sign


def net_gex(
    frame: pd.DataFrame,
    spot: float,
    r: float,
    q: float,
    t_years: pd.Series | np.ndarray | None = None,
    iv: pd.Series | np.ndarray | None = None,
) -> float:
    """Σ calls − Σ puts, in dollars per 1% move."""
    return float(contract_gex(frame, spot, r, q, t_years, iv).sum(skipna=True))


def gex_by_strike(
    frame: pd.DataFrame,
    spot: float,
    r: float,
    q: float,
    t_years: pd.Series | np.ndarray | None = None,
    iv: pd.Series | np.ndarray | None = None,
) -> pd.DataFrame:
    """Per-strike call, put and net exposure — the input to §10.4's peaks."""
    work = frame.copy()
    work["gex"] = contract_gex(frame, spot, r, q, t_years, iv)
    calls = work[work["option_type"] == "call"]
    puts = work[work["option_type"] == "put"]

    table = pd.DataFrame({"strike": sorted(work["strike"].unique())}).set_index("strike")
    table["call_gex"] = calls.groupby("strike")["gex"].sum()
    table["put_gex"] = puts.groupby("strike")["gex"].sum()
    table["call_oi"] = calls.groupby("strike")["open_interest"].sum()
    table["put_oi"] = puts.groupby("strike")["open_interest"].sum()
    return table.fillna(0.0).reset_index()


def usable_for_gex(frame: pd.DataFrame) -> pd.DataFrame:
    """Rows the exposure calculation may use.

    `normalize.py` computed this column; re-deriving it here would be a second
    definition waiting to disagree with the first.
    """
    if "usable_for_gex" not in frame.columns:
        raise KeyError(
            "frame has no usable_for_gex column — normalize it first rather than "
            "guessing which rows are safe"
        )
    return frame[frame["usable_for_gex"]]


def rate_sensitivity(
    frame: pd.DataFrame,
    spot: float,
    r: float,
    q: float,
    bump: float = 0.005,
    include_flip: bool = True,
) -> dict[str, Any]:
    """What ±bump on r and on q does to net GEX and to the flip (§3.3).

    The study declares r and q rather than solving for them, so the honest
    question is not "are they right" but "how much would being wrong cost".

    Report the flip in **points**, not percent. It is the quantity Q1 and Q2 are
    about, its natural unit is index points, and a percentage of 7400 hides the
    thing being asked.
    """
    from .flip_solver import solve_flip

    base_gex = net_gex(frame, spot, r, q)
    base_flip = solve_flip(frame, spot, r, q)["primary_flip"] if include_flip else None
    out: dict[str, Any] = {
        "base_net_gex": base_gex,
        "base_flip": base_flip,
        "bump": bump,
        "spot": spot,
    }
    for name, dr, dq in (
        ("r_up", bump, 0.0),
        ("r_down", -bump, 0.0),
        ("q_up", 0.0, bump),
        ("q_down", 0.0, -bump),
    ):
        bumped_gex = net_gex(frame, spot, r + dr, q + dq)
        entry: dict[str, Any] = {
            "net_gex": bumped_gex,
            "net_gex_abs_change": bumped_gex - base_gex,
            "net_gex_pct_change": (
                round(100.0 * (bumped_gex - base_gex) / abs(base_gex), 4)
                if base_gex
                else None
            ),
        }
        if include_flip:
            bumped_flip = solve_flip(frame, spot, r + dr, q + dq)["primary_flip"]
            entry["flip"] = bumped_flip
            entry["flip_change_pts"] = (
                round(bumped_flip - base_flip, 4)
                if (bumped_flip is not None and base_flip is not None)
                else None
            )
        out[name] = entry
    return out
