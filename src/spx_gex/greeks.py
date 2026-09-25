"""Black-Scholes greeks, q-adjusted (spec §9).

Our own implementation. It is deliberately *not* shared with
`providers/synthetic.py`, which plays the provider — if the two shared code,
§7 Path A would be comparing a formula against itself and would pass
unconditionally.

```
d₁ = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
Γ  = e^{-qT} · φ(d₁) / (S·σ·√T)
```

Pure functions, no config, no I/O, no pandas. Everything that varies by
convention — r, q, the time basis, where the expiration timestamp came from —
is decided upstream and passed in, so a §7.2 grid search can call this with
different assumptions without touching it.

`math.erf` rather than scipy: deterministic, dependency-free in the hot path,
and accurate to ~1e-15, which the §7.3 fixtures need at 1e-9.
"""

from __future__ import annotations

import math

SQRT_2PI = math.sqrt(2.0 * math.pi)


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def d1(S: float, K: float, r: float, q: float, sigma: float, T: float) -> float:
    return (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def d2(S: float, K: float, r: float, q: float, sigma: float, T: float) -> float:
    return d1(S, K, r, q, sigma, T) - sigma * math.sqrt(T)


def _valid(S: float, K: float, sigma: float, T: float) -> bool:
    return (
        S is not None
        and K is not None
        and sigma is not None
        and T is not None
        and S > 0
        and K > 0
        and sigma > 0
        and T > 0
    )


def gamma(
    S: float, K: float, r: float, q: float, sigma: float, T: float
) -> float | None:
    """Γ = e^{-qT}·φ(d₁)/(S·σ·√T). None when the inputs cannot support it.

    Returning None rather than raising or coercing to zero: an expired or
    quote-less contract has no gamma, and a silent 0.0 would sum into net GEX
    as though it were a real measurement of nothing.
    """
    if not _valid(S, K, sigma, T):
        return None
    return (
        math.exp(-q * T)
        * norm_pdf(d1(S, K, r, q, sigma, T))
        / (S * sigma * math.sqrt(T))
    )


def delta(
    S: float, K: float, r: float, q: float, sigma: float, T: float, is_call: bool
) -> float | None:
    if not _valid(S, K, sigma, T):
        return None
    disc_q = math.exp(-q * T)
    value = d1(S, K, r, q, sigma, T)
    return disc_q * norm_cdf(value) if is_call else -disc_q * norm_cdf(-value)


def vega(S: float, K: float, r: float, q: float, sigma: float, T: float) -> float | None:
    """Per 1.00 of vol (not per vol point). Callers scale as they need."""
    if not _valid(S, K, sigma, T):
        return None
    return S * math.exp(-q * T) * norm_pdf(d1(S, K, r, q, sigma, T)) * math.sqrt(T)


def price(
    S: float, K: float, r: float, q: float, sigma: float, T: float, is_call: bool
) -> float | None:
    if not _valid(S, K, sigma, T):
        return None
    disc_q, disc_r = math.exp(-q * T), math.exp(-r * T)
    a, b = d1(S, K, r, q, sigma, T), d2(S, K, r, q, sigma, T)
    if is_call:
        return S * disc_q * norm_cdf(a) - K * disc_r * norm_cdf(b)
    return K * disc_r * norm_cdf(-b) - S * disc_q * norm_cdf(-a)


def gex_contract(
    gamma_value: float,
    open_interest: float,
    multiplier: float,
    spot: float,
) -> float:
    """Dollars of gamma exposure per 1% move (§9).

    `GEX = Γ · OI · multiplier · S² · 0.01`
    """
    return gamma_value * open_interest * multiplier * spot * spot * 0.01
