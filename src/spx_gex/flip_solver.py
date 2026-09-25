"""Gamma flip — the zero of the aggregated exposure curve (spec §11).

The flip is **not** a level and not a linear field. It is where `net_gex(S')`
crosses zero when every contract's gamma is recomputed at the trial spot, with
σ_K, T and OI held fixed:

```
for S' in grid:
    recompute Γ at S' for every contract   (d₁ changes; σ_K, T, OI fixed)
    net_gex(S') = Σ signed contributions
find all sign changes; Brent-solve inside each bracketing interval
```

**Multiple roots are the normal case for 0DTE, not an edge case.** Near expiry
the exposure curve oscillates sharply between strikes. `n_roots > 1` is not an
error, the roots are never averaged, and a grid coarser than the 25-point SPX
strike spacing would both miss real roots and manufacture false ones — which is
why the step is 5 points and why the config comment says so.

Sticky-strike throughout (§10.2): σ_K stays attached to its original strike
when the trial spot moves. Sticky-delta is out of scope and deliberately has no
switch, because a switch is a thing that can be flipped by accident.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import brentq

from .exposure import net_gex


class FlipSolverError(RuntimeError):
    pass


def exposure_curve(
    frame: pd.DataFrame,
    spot: float,
    r: float,
    q: float,
    grid_min_pct: float = -5.0,
    grid_max_pct: float = 5.0,
    grid_step_points: float = 5.0,
) -> tuple[np.ndarray, np.ndarray]:
    """net_gex evaluated across the trial-spot grid."""
    if grid_step_points <= 0:
        raise FlipSolverError("grid_step_points must be positive")
    low = spot * (1.0 + grid_min_pct / 100.0)
    high = spot * (1.0 + grid_max_pct / 100.0)
    grid = np.arange(low, high + grid_step_points, grid_step_points)
    values = np.array([net_gex(frame, float(s), r, q) for s in grid])
    return grid, values


def solve_flip(
    frame: pd.DataFrame,
    spot: float,
    r: float,
    q: float,
    grid_min_pct: float = -5.0,
    grid_max_pct: float = 5.0,
    grid_step_points: float = 5.0,
    root_selection_rule: str = "nearest_to_spot",
) -> dict[str, Any]:
    """All roots, the selected one, and enough context to audit the choice."""
    grid, values = exposure_curve(
        frame, spot, r, q, grid_min_pct, grid_max_pct, grid_step_points
    )

    def curve(s: float) -> float:
        return net_gex(frame, float(s), r, q)

    roots: list[float] = []
    for i in range(len(grid) - 1):
        left, right = values[i], values[i + 1]
        if not np.isfinite(left) or not np.isfinite(right):
            continue
        if left == 0.0:
            roots.append(float(grid[i]))
            continue
        if left * right < 0:
            try:
                roots.append(float(brentq(curve, grid[i], grid[i + 1], xtol=1e-6)))
            except (ValueError, RuntimeError):
                continue
    if len(grid) and values[-1] == 0.0:
        roots.append(float(grid[-1]))

    roots = sorted(set(round(x, 6) for x in roots))

    result: dict[str, Any] = {
        "all_roots": roots,
        "n_roots": len(roots),
        "root_selection_rule": root_selection_rule,
        "grid_min": float(grid[0]),
        "grid_max": float(grid[-1]),
        "grid_step_points": grid_step_points,
        "curve_at_grid_min": float(values[0]),
        "curve_at_grid_max": float(values[-1]),
        "no_root": not roots,
        "primary_flip": None,
        "spot_vs_flip": None,
        "dist_to_flip_pct": None,
    }
    if not roots:
        # A curve that never crosses is a real state, not a failure. Saying so
        # is more useful than returning an endpoint dressed up as a flip.
        return result

    if root_selection_rule == "nearest_to_spot":
        primary = min(roots, key=lambda x: abs(x - spot))
    else:
        raise FlipSolverError(f"unknown root_selection_rule {root_selection_rule!r}")

    result["primary_flip"] = primary
    result["spot_vs_flip"] = "above" if spot > primary else "below"
    result["dist_to_flip_pct"] = round(100.0 * (spot - primary) / primary, 6)
    return result
