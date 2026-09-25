"""Peak proxies (spec §10.4).

**The word "wall" appears nowhere in self-computed output, deliberately.** The
optioncharts' wall definition is undisclosed, so four candidate proxies are stored
as separate columns and, after the study, each is compared against the optioncharts'
number to report which is closest. Collapsing them into one column named
`wall` would be asserting an equivalence the study exists to test.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def _argmax_strike(table: pd.DataFrame, column: str, use_abs: bool = True) -> float | None:
    if table.empty or column not in table.columns:
        return None
    series = table[column].abs() if use_abs else table[column]
    series = series.dropna()
    if series.empty or float(series.max()) == 0.0:
        return None
    return float(table.loc[series.idxmax(), "strike"])


def peak_proxies(by_strike: pd.DataFrame) -> dict[str, Any]:
    """Four proxies, four columns, no aggregation between them."""
    return {
        "call_gex_peak": _argmax_strike(by_strike, "call_gex"),
        "put_gex_peak": _argmax_strike(by_strike, "put_gex"),
        "call_oi_peak": _argmax_strike(by_strike, "call_oi", use_abs=False),
        "put_oi_peak": _argmax_strike(by_strike, "put_oi", use_abs=False),
    }
