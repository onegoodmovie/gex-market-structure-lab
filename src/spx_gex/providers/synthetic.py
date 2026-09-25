"""Deterministic offline provider that mimics the Massive snapshot payload.

Purpose: run M0 end-to-end — fetch, schema check, Parquet write, meta write —
without a key, without market hours, and without spending quota. It is also
the §17 synthetic-fixture run.

Deliberate design decision: the Black-Scholes block below is a *duplicate* of
the one that will live in `greeks.py`, and that duplication is the point. This
module plays the provider. If it shared code with our own validator, §7 Path A
would be comparing a formula against itself and would pass no matter what.

What it reproduces on purpose:
  * both roots — SPXW weeklies (PM) and SPX third-Friday monthlies (AM)
  * dotted-nested payload shape, so the flattener is exercised for real
  * missing greeks on deep ITM/OTM contracts (§7 sample construction)
  * the chosen tier's shape: `emulate_plan: starter` omits last_quote
    entirely, so the §5.3 as-of fallback ladder runs in tests
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from .base import FetchMeta, FetchResult, ProviderError, flatten_records

# A real zone, not a fixed offset: a hardcoded -04:00 silently expires the 0DTE
# leg on winter dates, which is precisely the §6.2 failure mode.
_ET = ZoneInfo("America/New_York")


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs(s: float, k: float, r: float, q: float, sigma: float, t: float, is_call: bool):
    """Provider-side Black-Scholes. See module docstring on why this is duplicated."""
    if t <= 0 or sigma <= 0:
        return None
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    disc_q = math.exp(-q * t)
    disc_r = math.exp(-r * t)
    gamma = disc_q * _norm_pdf(d1) / (s * sigma * math.sqrt(t))
    vega = s * disc_q * _norm_pdf(d1) * math.sqrt(t) / 100.0
    if is_call:
        delta = disc_q * _norm_cdf(d1)
        price = s * disc_q * _norm_cdf(d1) - k * disc_r * _norm_cdf(d2)
        theta = (
            -s * disc_q * _norm_pdf(d1) * sigma / (2 * math.sqrt(t))
            - r * k * disc_r * _norm_cdf(d2)
            + q * s * disc_q * _norm_cdf(d1)
        ) / 365.0
    else:
        delta = -disc_q * _norm_cdf(-d1)
        price = k * disc_r * _norm_cdf(-d2) - s * disc_q * _norm_cdf(-d1)
        theta = (
            -s * disc_q * _norm_pdf(d1) * sigma / (2 * math.sqrt(t))
            + r * k * disc_r * _norm_cdf(-d2)
            - q * s * disc_q * _norm_cdf(-d1)
        ) / 365.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "price": price}


def _iv_with_forward_bias(
    s: float, k: float, r: float, q: float, sigma: float, t: float,
    is_call: bool, bias: float,
) -> float:
    """First-order IV shift from inverting with a forward that is off by `bias`.

        sigma_call - sigma_true ~ -e^{-rT}*N(d2)*dF / vega
        sigma_put  - sigma_true ~ +e^{-rT}*N(-d2)*dF / vega

    Since N(d2) + N(-d2) = 1, the call/put spread comes out as
    -e^{-rT}*dF/vega — a level shaped like 1/vega, which is exactly the
    signature §3.2 has to detect.
    """
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    vega = s * math.exp(-q * t) * _norm_pdf(d1) * math.sqrt(t)
    if vega <= 1e-12:
        return sigma
    forward_error = s * (math.exp((r - q + bias) * t) - math.exp((r - q) * t))
    disc = math.exp(-r * t)
    shift = (
        -disc * _norm_cdf(d2) * forward_error / vega
        if is_call
        else disc * _norm_cdf(-d2) * forward_error / vega
    )
    return max(0.01, sigma + shift)


def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    fridays = 0
    while True:
        if d.weekday() == 4:
            fridays += 1
            if fridays == 3:
                return d
        d += timedelta(days=1)


def _expirations(asof: date, max_dte: int) -> list[date]:
    out: list[date] = []
    for offset in range(0, max_dte + 1):
        d = asof + timedelta(days=offset)
        if d.weekday() >= 5:
            continue
        if offset <= 7 or d.weekday() == 4:
            out.append(d)
    for month_offset in (0, 1, 2):
        month = asof.month + month_offset
        year = asof.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        tf = _third_friday(year, month)
        if asof <= tf <= asof + timedelta(days=max_dte) and tf not in out:
            out.append(tf)
    return sorted(set(out))


def _occ_ticker(root: str, exp: date, is_call: bool, strike: float) -> str:
    return (
        f"O:{root}{exp:%y%m%d}{'C' if is_call else 'P'}"
        f"{int(round(strike * 1000)):08d}"
    )


class SyntheticProvider:
    """Offline stand-in. Never used for study data — only for wiring tests."""

    def __init__(self, config, api_key: str | None = None):
        self._cfg = config
        self._max_dte = int(config.require("capture.expiry_scope_max_dte"))
        self._page_limit = int(config.get("provider.page_limit", 250))
        self._spot = float(config.get("provider.synthetic.spot", 7400.0))
        self._seed = int(config.get("provider.synthetic.seed", 20260101))
        self._timeframe = str(config.get("provider.synthetic.timeframe", "REAL-TIME"))
        self._strike_span_pct = float(config.get("provider.synthetic.strike_span_pct", 5.0))
        # `starter` omits the whole last_quote block, matching the chosen tier, so
        # the §5.3 fallback ladder is exercised by the fixture instead of being
        # dead code that only runs against the live API.
        self._emulate_plan = str(config.get("provider.synthetic.emulate_plan", "starter"))
        # snapshot -> a real per-capture instant (the good case)
        # daily    -> the same instant all day, i.e. a daily-granularity stamp
        #             wearing a timestamp's clothes; the §5.3 collision check
        #             exists solely to catch this
        # none     -> no timestamp fields at all, forcing the level-4 inference
        self._stamp_mode = str(config.get("provider.synthetic.stamp_mode", "snapshot"))
        # The fixture fires its request `delay` after the data's asof, exactly as
        # production does. Without this the implied-delay measurement would be
        # comparing simulated data against a real wall clock and measuring noise.
        self._delay_minutes = float(config.get("provider.delay_minutes", 0.0))
        # Injects a forward misspecification into the *reported* IVs, as if the
        # provider priced with the true r-q but inverted with a wrong one. Without
        # this the fixture gives calls and puts identical IVs and the §3.2
        # detector is trivially satisfied — i.e. never actually tested.
        self._forward_bias = float(
            config.get("provider.synthetic.forward_bias_r_minus_q", 0.0)
        )

    def provider_name(self) -> str:
        return "synthetic"

    def endpoint(self, underlying: str) -> str:
        return f"synthetic://snapshot/options/{underlying}"

    def fetch_chain(self, underlying: str, asof: datetime):
        return self.fetch_chain_with_meta(underlying, asof).frame

    def fetch_chain_with_meta(self, underlying: str, asof: datetime) -> FetchResult:
        rng = random.Random(self._seed + asof.toordinal())

        asof_utc = asof.astimezone(timezone.utc) if asof.tzinfo else asof.replace(tzinfo=timezone.utc)
        if self._stamp_mode == "daily":
            midnight = datetime.combine(
                asof_utc.astimezone(_ET).date(), dtime(0, 0), tzinfo=_ET
            )
            quote_ns = int(midnight.timestamp() * 1e9)
        else:
            quote_ns = int(asof_utc.timestamp() * 1e9)

        # A deterministic intraday path so that snapshots taken at different
        # labels genuinely differ in spot and in IV level. Without both moving,
        # the §12 mechanical/surface split has nothing to separate.
        et_hour = asof_utc.astimezone(_ET).hour + asof_utc.astimezone(_ET).minute / 60.0
        session_frac = min(max((et_hour - 9.5) / 6.5, 0.0), 1.0)
        day_drift_pct = rng.gauss(0.0, 0.55)
        day_iv_shift = rng.gauss(0.0, 0.012)
        spot = self._spot * (
            1.0
            + day_drift_pct / 100.0 * session_frac
            + 0.0012 * math.sin(session_frac * 5.0)
        )
        iv_shift = day_iv_shift * session_frac

        # The strike ladder and OI are anchored to the *base* spot, not the
        # drifted one: listed strikes do not move intraday, and OI is frozen
        # until the next overnight OCC publication (§13).
        anchor = self._spot
        half_width = int(anchor * self._strike_span_pct / 100.0 / 25.0)
        strikes = [
            round(anchor / 25.0) * 25 + step * 25
            for step in range(-half_width, half_width + 1)
        ]

        records: list[dict[str, Any]] = []
        for exp in _expirations(asof_utc.date(), self._max_dte):
            is_monthly = exp == _third_friday(exp.year, exp.month)
            root = "SPX" if is_monthly else "SPXW"
            settle_time = dtime(9, 30) if root == "SPX" else dtime(16, 0)
            exp_dt = datetime.combine(exp, settle_time, tzinfo=_ET)
            t_years = max((exp_dt - asof_utc).total_seconds(), 0.0) / (365.0 * 86400.0)
            if t_years <= 0:
                continue
            for strike in strikes:
                moneyness = math.log(strike / spot)
                for is_call in (True, False):
                    sigma = max(
                        0.06,
                        0.14 + iv_shift + 0.9 * abs(moneyness) - 0.6 * moneyness,
                    ) * (1.0 + 0.10 * math.exp(-t_years * 40))
                    greeks = _bs(spot, strike, 0.045, 0.013, sigma, t_years, is_call)
                    if greeks is None:
                        continue
                    reported_iv, report_r, report_q = sigma, 0.045, 0.013
                    if self._forward_bias:
                        # A provider whose forward is off does not report an
                        # inconsistent pair: it inverts IV with its own wrong
                        # r-q and then computes greeks from *that* IV with the
                        # same wrong r-q. Its numbers are internally coherent,
                        # which is exactly why Path A cannot see the error and
                        # the put-call spread can.
                        reported_iv = _iv_with_forward_bias(
                            spot, strike, 0.045, 0.013, sigma, t_years,
                            is_call, self._forward_bias,
                        )
                        report_q = 0.013 - self._forward_bias
                        biased = _bs(
                            spot, strike, report_r, report_q, reported_iv,
                            t_years, is_call,
                        )
                        if biased is not None:
                            greeks = biased
                    price = max(greeks["price"], 0.05)
                    spread = max(0.10, price * 0.02)
                    bid = round(max(price - spread / 2, 0.0), 2)
                    ask = round(price + spread / 2, 2)
                    otm_depth = abs(strike - spot) / spot
                    anchor_depth = abs(strike - anchor) / anchor
                    oi = int(max(0, rng.gauss(2500 * math.exp(-anchor_depth * 25), 600)))
                    volume = int(max(0, rng.gauss(oi * 0.35, oi * 0.2)))

                    record: dict[str, Any] = {
                        "break_even_price": round(
                            strike + price if is_call else strike - price, 2
                        ),
                        "day": {
                            "close": round(price, 2),
                            "high": round(price * 1.08, 2),
                            "low": round(price * 0.92, 2),
                            "open": round(price * 1.01, 2),
                            "previous_close": round(price * 0.97, 2),
                            "volume": volume,
                            "vwap": round(price * 1.002, 4),
                        },
                        "details": {
                            "contract_type": "call" if is_call else "put",
                            "exercise_style": "european",
                            "expiration_date": exp.isoformat(),
                            "shares_per_contract": 100,
                            "strike_price": float(strike),
                            "ticker": _occ_ticker(root, exp, is_call, strike),
                        },
                        "implied_volatility": round(reported_iv, 6),
                        "open_interest": oi,
                        "underlying_asset": {
                            "price": spot,
                            "ticker": underlying,
                            "timeframe": self._timeframe,
                        },
                    }
                    if self._stamp_mode != "none":
                        record["day"]["last_updated"] = quote_ns
                        record["underlying_asset"]["last_updated"] = quote_ns
                    if self._emulate_plan != "starter":
                        record["last_quote"] = {
                            "ask": ask,
                            "ask_size": rng.randint(1, 60),
                            "bid": bid,
                            "bid_size": rng.randint(1, 60),
                            "last_updated": quote_ns,
                            "midpoint": round((bid + ask) / 2, 4),
                            "timeframe": self._timeframe,
                        }
                    # Vendors routinely drop greeks on deep ITM. Reproduce it so the
                    # §7 sample builder has to cope with the real shape of the data.
                    if otm_depth <= 0.15:
                        record["greeks"] = {
                            "delta": round(greeks["delta"], 6),
                            "gamma": round(greeks["gamma"], 9),
                            "theta": round(greeks["theta"], 6),
                            "vega": round(greeks["vega"], 6),
                        }
                    records.append(record)

        if not records:
            raise ProviderError("synthetic provider produced no contracts")

        frame = flatten_records(records)
        started = asof_utc + timedelta(minutes=self._delay_minutes)
        finished = started
        pages = max(1, math.ceil(len(records) / self._page_limit))
        iso = datetime.fromtimestamp(quote_ns / 1e9, tz=timezone.utc).isoformat()

        if self._stamp_mode == "none":
            ts_field = None
        elif self._emulate_plan != "starter":
            ts_field = "last_quote.last_updated"
        else:
            ts_field = "underlying_asset.last_updated"
        meta = FetchMeta(
            provider=self.provider_name(),
            endpoint=self.endpoint(underlying),
            request_started_utc=started.isoformat(),
            request_finished_utc=finished.isoformat(),
            pages=pages,
            row_count=len(frame),
            request_params={
                "expiration_date.lte": (
                    asof_utc.date() + timedelta(days=self._max_dte)
                ).isoformat(),
                "limit": self._page_limit,
                "synthetic_seed": self._seed,
            },
            provider_timestamp_field=ts_field,
            provider_timestamp_min_utc=iso if ts_field else None,
            provider_timestamp_max_utc=iso if ts_field else None,
            quote_timeframes=(
                {} if self._emulate_plan == "starter"
                else {self._timeframe: len(frame)}
            ),
            underlying_asset={
                "price": spot,
                "ticker": underlying,
                "timeframe": self._timeframe,
            },
            notes=["SYNTHETIC DATA — not a study capture, never valid for §1.2"],
        )
        return FetchResult(frame=frame, meta=meta)
