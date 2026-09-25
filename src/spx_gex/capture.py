"""M0 capture core (spec §5, §16.1-4).

Shared by capture_oi_base.py and capture_surface.py. Deliberately dumb: fetch,
persist raw, then check. Raw option chains cannot be reconstructed after the
fact, so the write happens *before* any validation verdict — a capture that
fails a schema check still lands on disk and still exits non-zero.

Acceptance criteria enforced here:
  1. raw Parquet holds the provider's unmodified column names
  2. one .meta.json per capture with provider, endpoint, request time,
     provider timestamp, row count
  3. both SPX and SPXW present
  4. re-running a capture never overwrites an existing file
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Config, raw_chain_dir
from .transmission import estimate_implied_delay, read_transmission_spot

# Chain-wide required fields: every contract carries these or the provider is
# returning something the study cannot use.
CRITICAL_FIELDS = {
    "open_interest": "the GEX weight (§9)",
    "details.strike_price": "strike",
    "details.expiration_date": "expiry bucketing (§6.5)",
    "details.contract_type": "call/put sign (§9)",
    "details.ticker": "root, SPX vs SPXW (§6.2)",
    "details.shares_per_contract": "multiplier (§9)",
}

# Required *near the money* only. §7 states outright that providers drop greeks on
# deep-ITM contracts, and a full SPX chain to 45 DTE runs thousands of points
# wide — so a chain-wide 95% gate on these would fail almost every real capture
# while telling us nothing. Gamma weight and the frozen surface both live near
# the money; that is where coverage has to hold.
NTM_CRITICAL_FIELDS = {
    "greeks.gamma": "§7 Path A reference",
    "implied_volatility": "frozen surface input (§10.1)",
}

ADVISORY_FIELDS = {
    "day.volume": "§13 blind-spot diagnostic",
    "underlying_asset.price": "§5.2 underlying_spot",
}

# Fields the chosen tier is known not to return. Reported as N/A rather than as
# 0% coverage, so the record shows a deliberate absence instead of looking like
# a broken capture. Keyed by the config flag that declares them absent.
PLAN_ABSENT_FIELDS = {
    "last_quote.bid": ("provider.has_quotes", "§7 Path B only — no quotes on Starter"),
    "last_quote.ask": ("provider.has_quotes", "§7 Path B only — no quotes on Starter"),
    "last_quote.midpoint": ("provider.has_quotes", "§7 Path B only — no quotes on Starter"),
    "last_quote.timeframe": ("provider.has_quotes", "no quotes; delay is a config fact"),
    "last_trade.sip_timestamp": ("provider.has_trades", "no trades on Starter"),
}

COVERAGE_THRESHOLD_PCT = 95.0
NTM_BAND_PCT = 10.0  # |K - S| / S, the band the gate applies to

# §5.3 as-of resolution ladder. Level 4 is inference, not a field.
DEFAULT_ASOF_PRIORITY = [
    "last_quote.last_updated",
    "underlying_asset.last_updated",
    "day.last_updated",
]
INFERRED_ASOF_LEVEL = 4


class CaptureError(RuntimeError):
    pass


class CaptureRefused(CaptureError):
    """Raised before any network call — nothing was fetched, nothing spent."""


def _et(config: Config) -> ZoneInfo:
    return ZoneInfo(config.get("capture.timezone", "America/New_York"))


def delay_minutes(config: Config) -> float:
    """The one delay number. Everything time-shaped derives from it."""
    return float(config.require("provider.delay_minutes"))


def target_asof_et(config: Config, capture_date: date, label: str) -> datetime:
    """The instant a capture is *meant* to carry, in ET."""
    tz = _et(config)
    hour, minute = int(label[:2]), int(label[2:])
    return datetime.combine(capture_date, dtime(hour, minute), tzinfo=tz)


def scheduled_download_et(config: Config, capture_date: date, label: str) -> datetime:
    """When to actually fire the request: target asof + the provider delay.

    Derived, never listed in config. On a 15-minute tier the 09:45 target is
    downloaded at 10:00 — and if the tier ever changes, this moves by itself
    instead of disagreeing with a hardcoded second copy.
    """
    return target_asof_et(config, capture_date, label) + timedelta(
        minutes=delay_minutes(config)
    )


def dte_bucket_counts(frame: pd.DataFrame, capture_date: date) -> dict[str, int]:
    """Contracts by calendar DTE at the raw layer.

    Cheap, and it answers a question that is otherwise invisible: whether the
    provider still returns same-day PM-settled contracts in a late snapshot.
    """
    if "details.expiration_date" not in frame.columns:
        return {}
    expiries = pd.to_datetime(frame["details.expiration_date"], errors="coerce")
    dte = (expiries.dt.date - capture_date).map(
        lambda d: d.days if pd.notna(d) else None
    )
    buckets = {"0DTE": 0, "1-7DTE": 0, "8-30DTE": 0, "31+DTE": 0, "expired": 0}
    for value in dte:
        if value is None:
            continue
        if value < 0:
            buckets["expired"] += 1
        elif value == 0:
            buckets["0DTE"] += 1
        elif value <= 7:
            buckets["1-7DTE"] += 1
        elif value <= 30:
            buckets["8-30DTE"] += 1
        else:
            buckets["31+DTE"] += 1
    return buckets


def previous_business_day(d: date) -> date:
    """OI is as-of the prior session's close (§5.1).

    Weekday arithmetic only — no exchange holiday calendar is applied, so on the
    day after a market holiday `oi_asof_date` names a non-session day. The rule
    is recorded alongside the value in meta.json so it is never mistaken for a
    verified session date.
    """
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


def root_of(ticker: Any) -> str:
    """SPXW vs SPX from an OCC ticker such as `O:SPXW260727C07400000`."""
    if not isinstance(ticker, str) or not ticker:
        return "?"
    body = ticker[2:] if ticker.startswith("O:") else ticker
    if body.startswith("SPXW"):
        return "SPXW"
    if body.startswith("SPX"):
        return "SPX"
    return "?"


def write_parquet_atomic(frame: pd.DataFrame, path: Path) -> None:
    """Write via a temp file in the same directory, then rename into place.

    An interrupted capture must never leave a half-written Parquet that later
    reads as a short day.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        try:
            frame.to_parquet(tmp, index=False)
        except Exception:
            # Provider payloads occasionally mix types within one JSON field. Cast
            # the offenders to string rather than dropping them; the raw layer is
            # lossless by contract and normalize.py can parse strings later.
            coerced = frame.copy()
            for column in coerced.columns:
                if coerced[column].dtype == object:
                    coerced[column] = coerced[column].map(
                        lambda v: v if v is None or isinstance(v, str) else json.dumps(v)
                    )
            coerced.to_parquet(tmp, index=False)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ns_to_iso(ns: Any) -> str | None:
    """Provider epoch-nanoseconds to a tz-aware UTC ISO string (§6.1)."""
    try:
        seconds = float(ns) / 1e9
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def resolve_asof(
    frame: pd.DataFrame,
    request_time: datetime,
    priority: list[str],
    delay_seconds: int,
    max_span_minutes: float = 60.0,
) -> dict[str, Any]:
    """Pick the as-of instant for a snapshot, walking the §5.3 ladder.

    Returns the resolved instant plus the level it came from. A level-4 result
    is inferred arithmetic, not provider data, and `asof_is_inferred` says so —
    T computed from it inherits the whole uncertainty of the delay assumption.
    """
    rejected: list[dict[str, Any]] = []
    for level, field in enumerate(priority, start=1):
        if field not in frame.columns:
            continue
        values = pd.to_numeric(frame[field], errors="coerce").dropna()
        values = values[values > 0]
        if values.empty:
            continue

        # A snapshot stamp describes one instant, so its values must cluster.
        # Massive's `day.last_updated` spans a full year across contracts — it
        # is each contract's last *trade* date, not the snapshot's as-of, and
        # taking max() of it would silently hand a months-old T to any quiet
        # contract. Dispersion is the test that tells the two apart, and it
        # runs per capture rather than waiting for the cross-capture collision
        # check to notice on the second capture of the day.
        span_minutes = float(values.max() - values.min()) / 6e10
        if span_minutes > max_span_minutes:
            rejected.append(
                {
                    "field": field,
                    "level": level,
                    "span_minutes": round(span_minutes, 1),
                    "reason": (
                        f"values span {span_minutes / 1440:.1f} days across "
                        "contracts — a per-contract activity date, not a "
                        "snapshot instant"
                    ),
                }
            )
            continue

        return {
            "asof_utc": _ns_to_iso(values.max()),
            "asof_min_utc": _ns_to_iso(values.min()),
            "asof_source_field": field,
            "asof_source_level": level,
            "asof_is_inferred": False,
            "asof_span_minutes": round(span_minutes, 2),
            "asof_rejected_levels": rejected,
            "asof_note": None,
        }

    inferred = request_time.astimezone(timezone.utc) - timedelta(seconds=delay_seconds)
    return {
        "asof_utc": inferred.isoformat(),
        "asof_min_utc": None,
        "asof_source_field": None,
        "asof_source_level": INFERRED_ASOF_LEVEL,
        "asof_is_inferred": True,
        "asof_span_minutes": None,
        "asof_rejected_levels": rejected,
        "asof_note": (
            f"no usable provider timestamp in {priority}; inferred as "
            f"request_time - {delay_seconds}s. T from this value carries the full "
            "uncertainty of the assumed delay (§5.3)."
        ),
    }


def check_intraday_variation(
    day_dir: Path, label: str, asof_utc: str | None
) -> dict[str, Any] | None:
    """§5.3: two captures on the same day must not resolve to the same instant.

    A provider field that returns an identical value at 09:45 and 14:00 is a
    daily-granularity stamp. It looks like a timestamp and is useless as one —
    this is the only way to tell those two apart. On a collision the caller
    degrades the capture to the inferred level.
    """
    if not asof_utc:
        return None
    for sibling in sorted(day_dir.glob("*.meta.json")):
        try:
            other = json.loads(sibling.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if other.get("time_label") == label:
            continue
        if other.get("asof_is_inferred"):
            continue
        if other.get("asof_utc") == asof_utc:
            return {
                "collided_with": other.get("time_label"),
                "collided_file": sibling.name,
                "shared_asof_utc": asof_utc,
            }
    return None


def _coverage_pct(series: pd.Series) -> float:
    return round(100.0 * series.notna().sum() / len(series), 2) if len(series) else 0.0


def _ntm_mask(frame: pd.DataFrame) -> pd.Series | None:
    """Rows within NTM_BAND_PCT of spot, or None if spot is unavailable."""
    if "details.strike_price" not in frame.columns:
        return None
    spot = None
    if "underlying_asset.price" in frame.columns:
        prices = pd.to_numeric(frame["underlying_asset.price"], errors="coerce").dropna()
        if not prices.empty:
            spot = float(prices.iloc[0])
    if not spot:
        return None
    strikes = pd.to_numeric(frame["details.strike_price"], errors="coerce")
    return ((strikes - spot).abs() / spot * 100.0) <= NTM_BAND_PCT


def run_checks(frame: pd.DataFrame, config: Config | None = None) -> dict[str, Any]:
    """Field coverage and root presence. Returns a block for meta.json."""
    n = len(frame)
    all_fields = list(CRITICAL_FIELDS) + list(NTM_CRITICAL_FIELDS) + list(ADVISORY_FIELDS)
    coverage: dict[str, float] = {}
    missing_critical: list[str] = []
    for field in all_fields:
        column = frame[field] if field in frame.columns else pd.Series([None] * n)
        coverage[field] = _coverage_pct(column)
        if field in CRITICAL_FIELDS and coverage[field] < COVERAGE_THRESHOLD_PCT:
            missing_critical.append(field)

    # Fields the tier is declared not to return. Absent is expected; *present*
    # is the interesting case, because it means the plan gives more than assumed.
    plan_absent: dict[str, str] = {}
    unexpectedly_present: list[str] = []
    for field, (flag, reason) in PLAN_ABSENT_FIELDS.items():
        declared_available = bool(config.get(flag, True)) if config else True
        if declared_available:
            continue
        plan_absent[field] = reason
        if field in frame.columns and frame[field].notna().any():
            unexpectedly_present.append(field)

    ntm = _ntm_mask(frame)
    ntm_coverage: dict[str, float] = {}
    for field in NTM_CRITICAL_FIELDS:
        if field not in frame.columns:
            ntm_coverage[field] = 0.0
        elif ntm is None:
            # No spot in the payload, so the band cannot be drawn. Fall back to
            # chain-wide coverage rather than silently passing the gate.
            ntm_coverage[field] = coverage[field]
        else:
            ntm_coverage[field] = _coverage_pct(frame.loc[ntm, field])
        if ntm_coverage[field] < COVERAGE_THRESHOLD_PCT:
            missing_critical.append(field)

    roots: dict[str, int] = {}
    if "details.ticker" in frame.columns:
        roots = {
            str(k): int(v)
            for k, v in frame["details.ticker"].map(root_of).value_counts().items()
        }
    missing_roots = [r for r in ("SPX", "SPXW") if roots.get(r, 0) == 0]

    expiries: list[str] = []
    if "details.expiration_date" in frame.columns:
        expiries = sorted(
            {str(v) for v in frame["details.expiration_date"].dropna().unique()}
        )

    return {
        "field_coverage_pct": coverage,
        "not_applicable_on_this_plan": plan_absent,
        "unexpectedly_present": unexpectedly_present,
        "near_the_money_coverage_pct": ntm_coverage,
        "near_the_money_band_pct": NTM_BAND_PCT,
        "near_the_money_contracts": int(ntm.sum()) if ntm is not None else None,
        "coverage_threshold_pct": COVERAGE_THRESHOLD_PCT,
        "missing_critical_fields": missing_critical,
        "roots": roots,
        "missing_roots": missing_roots,
        "distinct_expiries": len(expiries),
        "nearest_expiry": expiries[0] if expiries else None,
        "furthest_expiry": expiries[-1] if expiries else None,
        "passed": not missing_critical and not missing_roots,
    }


def capture(
    config: Config,
    provider,
    date_str: str,
    label: str,
    kind: str,
) -> dict[str, Any]:
    """Fetch one snapshot and persist it. `label` is the filename stem.

    Returns the meta dict. Raises CaptureRefused *before* the network call when
    the target file already exists (§16.4).
    """
    if kind not in {"oi_base", "surface"}:
        raise ValueError(f"kind must be oi_base or surface, got {kind!r}")

    try:
        capture_date = date.fromisoformat(date_str)
    except ValueError as exc:
        raise CaptureRefused(f"--date must be YYYY-MM-DD, got {date_str!r}") from exc

    tz = _et(config)
    now_et = datetime.now(tz)
    is_synthetic = provider.provider_name() == "synthetic"

    if not is_synthetic and capture_date != now_et.date():
        raise CaptureRefused(
            f"snapshot endpoints return live state only; cannot capture {date_str} "
            f"on {now_et.date()}. Historical backfill is not possible — this is why "
            "§1.1 says start the cron before building anything else."
        )

    out_dir = raw_chain_dir(date_str)
    parquet_path = out_dir / f"{label}.parquet"
    meta_path = out_dir / f"{label}.meta.json"
    for existing in (parquet_path, meta_path):
        if existing.exists():
            raise CaptureRefused(
                f"{existing} already exists; refusing to overwrite (§16.4). "
                "Delete it by hand if the existing capture is known bad."
            )
    out_dir.mkdir(parents=True, exist_ok=True)

    # Two distinct instants, kept apart deliberately. `asof` is what the data
    # should be as-of; `fired_at` is when the request goes out. In production
    # they differ by the provider delay, and conflating them was what made the old
    # label-offset warning fire on every fixture run.
    asof = now_et
    fired_at = now_et
    if is_synthetic:
        # Anchor the fixture to the label, not to wall clock: three snapshots that
        # all carry the same timestamp would make the §12 attribution identically
        # zero and prove nothing.
        anchor = (
            datetime.strptime(label, "%H%M").time()
            if kind == "surface"
            else datetime.strptime("0830", "%H%M").time()
        )
        asof = datetime.combine(capture_date, anchor, tzinfo=tz)
        fired_at = asof + timedelta(minutes=delay_minutes(config))

    result = provider.fetch_chain_with_meta(
        config.require("provider.underlying_ticker"), asof
    )

    # --- persist first, judge second ----------------------------------------
    write_parquet_atomic(result.frame, parquet_path)

    meta: dict[str, Any] = asdict(result.meta)
    meta.update(
        {
            "capture_kind": kind,
            "capture_date": date_str,
            "time_label": label,
            "capture_wall_clock_et": fired_at.isoformat(),
            "parquet_file": parquet_path.name,
            "parquet_sha256": _sha256(parquet_path),
            "parquet_bytes": parquet_path.stat().st_size,
            "columns": list(result.frame.columns),
            "spec_version": config.get("experiment.spec_version", "1.0"),
            "expiry_scope_max_dte": config.require("capture.expiry_scope_max_dte"),
            "provider_plan": config.get("provider.plan", None),
            "provider_is_delayed": config.require("provider.is_delayed"),
            "provider_delay_minutes": delay_minutes(config),
            "contracts_by_dte": dte_bucket_counts(result.frame, capture_date),
            "checks": run_checks(result.frame, config),
        }
    )

    # --- §5.3 as-of resolution ----------------------------------------------
    delay_seconds = int(delay_minutes(config) * 60)
    asof_info = resolve_asof(
        result.frame,
        request_time=datetime.fromisoformat(meta["request_finished_utc"]),
        priority=list(config.get("asof_resolution.priority", DEFAULT_ASOF_PRIORITY)),
        delay_seconds=delay_seconds,
        max_span_minutes=float(
            config.get("asof_resolution.max_span_minutes", 60.0)
        ),
    )
    meta.update(asof_info)

    if config.get("asof_resolution.require_intraday_variation", True):
        collision = check_intraday_variation(out_dir, label, meta.get("asof_utc"))
        meta["asof_intraday_collision"] = collision
        if collision:
            # Identical instants at two different capture times means the field
            # carries daily granularity. Degrade rather than trust it.
            inferred = datetime.fromisoformat(
                meta["request_finished_utc"]
            ).astimezone(timezone.utc) - timedelta(seconds=delay_seconds)
            meta["asof_degraded_from"] = {
                "field": meta["asof_source_field"],
                "level": meta["asof_source_level"],
                "value": meta["asof_utc"],
            }
            meta.update(
                {
                    "asof_utc": inferred.isoformat(),
                    "asof_source_field": None,
                    "asof_source_level": INFERRED_ASOF_LEVEL,
                    "asof_is_inferred": True,
                    "asof_note": (
                        f"resolved instant matched the {collision['collided_with']} "
                        "capture exactly — that field is a daily stamp, not a "
                        f"snapshot stamp. Degraded to level {INFERRED_ASOF_LEVEL} "
                        f"(request_time - {delay_seconds}s)."
                    ),
                }
            )

    if kind == "oi_base":
        meta["oi_asof_date"] = previous_business_day(capture_date).isoformat()
        meta["oi_asof_rule"] = (
            "prior weekday; OCC publishes OI overnight, so the morning fetch is "
            "as-of the previous session's close (§5.1). No exchange holiday "
            "calendar applied — verify by hand around holidays."
        )
        deadline = config.get("capture.oi_base_deadline_et", "09:30")
        meta["oi_base_before_deadline"] = now_et.strftime("%H:%M") < deadline

    if kind == "surface":
        # The label names the asof this capture is *for*, not when it ran. Two
        # separate questions follow, and conflating them hid a real failure mode:
        #   download_drift  — did cron fire on time?
        #   asof_vs_target  — did we actually get 14:00 data?
        # Only the second one is what §8.1 pairing depends on.
        target = target_asof_et(config, capture_date, label)
        scheduled = scheduled_download_et(config, capture_date, label)
        meta["target_asof_et"] = target.isoformat()
        meta["scheduled_download_et"] = scheduled.isoformat()
        meta["download_drift_minutes"] = round(
            (fired_at.astimezone(tz) - scheduled).total_seconds() / 60.0, 2
        )

    # --- continuous measurement of the delay assumption ----------------------
    # Surface captures only. The OI base runs pre-market, and transmission_spot
    # starts at 09:45, so matching one against the other compares a pre-open
    # snapshot to post-open prices and manufactures a negative lag.
    if kind != "surface":
        meta["implied_delay"] = {
            "implied_delay_minutes": None,
            "identifiable": False,
            "reason": "not measured for pre-market captures — no transmission "
            "spot exists before the open (§5.4)",
            "warn": False,
        }
    elif config.get("provider.implied_delay.enabled", True):
        spot = None
        if "underlying_asset.price" in result.frame.columns:
            prices = pd.to_numeric(
                result.frame["underlying_asset.price"], errors="coerce"
            ).dropna()
            if not prices.empty:
                spot = float(prices.iloc[0])
        meta["implied_delay"] = estimate_implied_delay(
            snapshot_spot=spot,
            request_time=datetime.fromisoformat(meta["request_finished_utc"]),
            rows=read_transmission_spot(date_str),
            configured_minutes=delay_minutes(config),
            warn_threshold_minutes=float(
                config.get("provider.implied_delay.warn_threshold_minutes", 5.0)
            ),
            ambiguity_margin_points=float(
                config.get("provider.implied_delay.ambiguity_margin_points", 2.0)
            ),
        )
        if meta["implied_delay"]["warn"]:
            meta.setdefault("notes", []).append(
                "IMPLIED DELAY DRIFT: " + str(meta["implied_delay"]["reason"])
            )

    # --- did we hit the asof we were aiming at? ------------------------------
    if kind == "surface" and meta.get("asof_utc"):
        target_utc = target_asof_et(config, capture_date, label).astimezone(timezone.utc)
        resolved = datetime.fromisoformat(meta["asof_utc"])
        meta["asof_vs_target_minutes"] = round(
            (resolved - target_utc).total_seconds() / 60.0, 2
        )

    # --- §9.2, revised ------------------------------------------------------
    # The delay argument for dropping 0DTE at 15:45 is gone: the download clock
    # is shifted, so the 15:45 row carries real 15:45 data. What remains is
    # §9.1's 1/√T singularity, which is a property of the maths and not of the
    # feed. So the layer is kept and marked, not discarded.
    not_comparable = list(config.get("gex.zero_dte_not_cross_day_comparable_times", []))
    no_aggregate = list(config.get("gex.zero_dte_excluded_from_aggregate_times", []))
    absent = list(config.get("gex.zero_dte_absent_times", []))

    meta["zero_dte_not_cross_day_comparable"] = label in not_comparable
    meta["zero_dte_excluded_from_aggregate"] = label in no_aggregate
    meta["zero_dte_layer_absent"] = label in absent
    if meta["zero_dte_not_cross_day_comparable"]:
        meta["zero_dte_note"] = (
            f"0DTE at {label} is captured and reported, but flagged not comparable "
            "across days: §9.1's 1/√T singularity swings the number ~5x on time "
            "decay alone near expiry. This is a property of the maths, not of the "
            "delay. Held out of the aggregate so a non-comparable quantity does "
            "not contaminate a comparable one."
        )

    # Monday's open question (§19): does the provider still return same-day
    # PM-settled contracts in a late snapshot? Recorded on every capture so the
    # answer is in the data rather than in someone's memory.
    zero_dte_count = meta["contracts_by_dte"].get("0DTE", 0)
    meta["zero_dte_contracts_returned"] = zero_dte_count
    if kind == "surface" and label in not_comparable and zero_dte_count == 0:
        meta.setdefault("notes", []).append(
            f"NO 0DTE contracts in the {label} snapshot. If this repeats, the "
            "provider drops same-day contracts once they are near settlement and "
            "the §9.2 'capture and mark' decision is moot — there is nothing to "
            "capture. Record it; do not work around it."
        )

    tmp_meta = meta_path.with_suffix(".json.tmp")
    tmp_meta.write_text(json.dumps(meta, indent=2, sort_keys=True, default=str))
    os.replace(tmp_meta, meta_path)
    return meta


def summary_lines(meta: dict[str, Any]) -> list[str]:
    """Human-readable capture verdict for the terminal and the cron log."""
    checks = meta["checks"]
    lines = [
        f"provider        : {meta['provider']}",
        f"endpoint        : {meta['endpoint']}",
        f"file            : {meta['parquet_file']}  ({meta['parquet_bytes']:,} bytes)",
        f"contracts       : {meta['row_count']:,} across {meta['pages']} page(s)",
        f"roots           : {checks['roots']}",
        f"expiries        : {checks['distinct_expiries']}  "
        f"({checks['nearest_expiry']} .. {checks['furthest_expiry']})",
        f"asof (§5.3)     : {meta.get('asof_utc')}",
        f"  source        : level {meta.get('asof_source_level')} "
        f"{meta.get('asof_source_field') or '(inferred)'}"
        + ("  <- INFERRED, not provider data" if meta.get("asof_is_inferred") else ""),
        f"delayed         : {meta.get('provider_is_delayed')} "
        f"({meta.get('provider_delay_minutes')} min, {meta.get('provider_plan')} tier)",
        f"contracts/DTE   : {meta.get('contracts_by_dte')}",
    ]
    if meta.get("target_asof_et"):
        lines.append(
            f"target asof     : {meta['target_asof_et'][11:16]} ET  "
            f"(download due {meta['scheduled_download_et'][11:16]} ET, "
            f"drift {meta.get('download_drift_minutes'):+.1f} min)"
        )
    if meta.get("asof_vs_target_minutes") is not None:
        delta = meta["asof_vs_target_minutes"]
        flag = "  <- §8.1 pairing at risk" if abs(delta) > 5 else ""
        lines.append(f"asof vs target  : {delta:+.1f} min{flag}")
    implied = meta.get("implied_delay") or {}
    if implied.get("identifiable"):
        lines.append(
            f"implied delay   : {implied['implied_delay_minutes']:.1f} min "
            f"(configured {implied['configured_delay_minutes']:.0f}, "
            f"dev {implied['deviation_minutes']:+.1f}, "
            f"grid resolves ~{implied['resolution_minutes']:.0f} min)"
        )
    elif implied:
        lines.append(f"implied delay   : not identifiable — {implied.get('reason')}")
    if meta.get("asof_degraded_from"):
        lines.append(
            f"  !! degraded from level {meta['asof_degraded_from']['level']} "
            f"({meta['asof_degraded_from']['field']}): same instant as the "
            f"{meta['asof_intraday_collision']['collided_with']} capture"
        )
    if meta.get("zero_dte_not_cross_day_comparable"):
        lines.append(
            f"  0DTE at {meta['time_label']}: captured and reported, flagged "
            "not_cross_day_comparable (§9.1), held out of aggregate"
        )
    if meta.get("zero_dte_contracts_returned") == 0 and meta["capture_kind"] == "surface":
        lines.append("  !! zero 0DTE contracts in this snapshot — see notes")
    if checks.get("unexpectedly_present"):
        lines.append(
            f"  note: fields present that this plan was not expected to return: "
            f"{checks['unexpectedly_present']} — revisit the tier assumptions"
        )
    if meta.get("oi_asof_date"):
        lines.append(f"oi_asof         : {meta['oi_asof_date']} (rule: prior weekday)")
    if meta.get("capture_label_offset_minutes") is not None:
        lines.append(
            f"label offset    : {meta['capture_label_offset_minutes']:+.1f} min from "
            f"the {meta['time_label']} label"
        )
    for field in checks["missing_critical_fields"]:
        lines.append(f"  !! critical field missing/sparse: {field}")
    for root in checks["missing_roots"]:
        lines.append(f"  !! root absent: {root}")
    for note in meta.get("notes") or []:
        lines.append(f"  note: {note}")
    lines.append("VERDICT: " + ("OK" if checks["passed"] else "CHECKS FAILED"))
    return lines


# Runtime delay detection was removed with the tier decision: Starter returns no
# `last_quote` block, so there is no `timeframe` field to read. The delay is a
# known property of the plan and lives in config.provider.is_delayed.
