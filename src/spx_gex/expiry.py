"""Expiration timestamps, time to expiry, and expiry buckets (spec §6.2, §6.3, §6.5).

The spec calls §6.2 the highest-risk field in the study, and the reason is the
AM/PM settlement split:

    SPXW (PM-settled):  expiration_date @ 16:00:00 America/New_York
    SPX  (AM-settled):  expiration_date @ 09:30:00 America/New_York

AM-settled SPX monthlies (third Friday) stop trading at Thursday's close and
settle Friday morning against SET. From Thursday 15:45 the true remaining time
is ≈17.75 h, not ≈24.25 h — a ~27% error in T and ~17% in gamma, recurring once
a month. Getting this wrong does not fail loudly; it quietly biases one day in
twenty.

Zones are real IANA zones, never fixed offsets. A hardcoded -04:00 expires the
0DTE leg on winter dates.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

SECONDS_PER_YEAR = 365.0 * 86400.0

DEFAULT_SETTLEMENT_ET = {
    "SPX": "09:30:00",   # AM-settled, against SET
    "SPXW": "16:00:00",  # PM-settled
}


class ExpiryError(ValueError):
    pass


def settlement_time(root: str, settlement_times: dict[str, str] | None = None) -> dtime:
    table = settlement_times or DEFAULT_SETTLEMENT_ET
    if root not in table:
        raise ExpiryError(
            f"no settlement time configured for root {root!r}. Refusing to guess — "
            "a wrong settlement time is a silent ~17% gamma error (§6.2)."
        )
    hh, mm, ss = (int(part) for part in table[root].split(":"))
    return dtime(hh, mm, ss)


def expiration_timestamp(
    expiration_date: date,
    root: str,
    tz: ZoneInfo,
    settlement_times: dict[str, str] | None = None,
) -> datetime:
    """Derived expiration instant, tz-aware (§6.2)."""
    return datetime.combine(
        expiration_date, settlement_time(root, settlement_times), tzinfo=tz
    )


def time_to_expiry_years(
    expiration_ts: datetime,
    asof: datetime,
    convention: str = "calendar",
) -> float:
    """T in years. Negative when the contract has already settled.

    Negative is returned rather than clamped: a caller that silently floors to
    zero turns an expired contract into an infinitely-gammaed one, and a caller
    that clamps to a small positive number invents a contract that no longer
    exists. The decision belongs upstream, where it can be recorded.
    """
    if convention != "calendar":
        raise ExpiryError(
            f"time_convention {convention!r} is not implemented. Only 'calendar' "
            "exists; 'trading' would need a session calendar the study does not "
            "have. §7.2 decides which Massive uses — if it turns out to be "
            "trading time, that is a stop-and-report, not a silent substitution."
        )
    return (expiration_ts - asof).total_seconds() / SECONDS_PER_YEAR


def dte_calendar(expiration_date: date, asof_date: date) -> int:
    return (expiration_date - asof_date).days


def expiry_bucket(dte: int) -> str | None:
    """§6.5 buckets. `None` for anything outside the study's scope."""
    if dte < 0:
        return None
    if dte == 0:
        return "0DTE"
    if dte <= 7:
        return "1-7DTE"
    if dte <= 30:
        return "8-30DTE"
    return None  # 31-45 DTE is captured but not a reported layer


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    fridays = 0
    while True:
        if d.weekday() == 4:
            fridays += 1
            if fridays == 3:
                return d
        d += timedelta(days=1)


def is_am_settled_monthly(expiration_date: date, root: str) -> bool:
    """SPX root on a third Friday — the once-a-month case §6.2 warns about."""
    return root == "SPX" and expiration_date == third_friday(
        expiration_date.year, expiration_date.month
    )


def crosses_month_boundary(dates: list[date]) -> bool:
    """Acceptance criterion §16.8 needs a sample that spans a month change."""
    months = {(d.year, d.month) for d in dates}
    return len(months) > 1


def describe(expiration_date: date, root: str, asof: datetime, tz: ZoneInfo) -> dict[str, Any]:
    """Everything derived for one contract's expiry, for auditing one row."""
    ts = expiration_timestamp(expiration_date, root, tz)
    return {
        "expiration_timestamp": ts.isoformat(),
        "settlement_type": "AM" if root == "SPX" else "PM",
        "am_settled_monthly": is_am_settled_monthly(expiration_date, root),
        "t_years": time_to_expiry_years(ts, asof),
        "t_minutes": time_to_expiry_years(ts, asof) * SECONDS_PER_YEAR / 60.0,
        "dte_calendar": dte_calendar(expiration_date, asof.astimezone(tz).date()),
    }
