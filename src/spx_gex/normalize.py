"""Raw provider payload -> normalized contract table (spec §6).

The provider layer preserved the provider's column names verbatim. This is the
only place they are renamed, and it is the only place a provider switch has to be
re-taught.

Every filter counts what it removed and writes a line to the day's
data-quality record (§6.4). Nothing is dropped silently.

---
A spec ambiguity resolved here, deliberately and visibly.

§6.4 drops `open_interest == 0`, and then declares a severe flag at ">5% of
expected contracts dropped". Taken literally those two rules contradict each
other: on a real SPX chain most listed strikes carry no open interest, so the
zero-OI filter alone removes far more than 5% and would invalidate every single
day.

The resolution: a zero-OI contract contributes *exactly* zero to net GEX
(`Γ · 0 · mult · S² · 0.01 = 0`). Removing it loses no information — it is a
definitional narrowing, not a data-quality loss. So drops are accounted in two
separate ledgers:

  definitional  zero open interest              -> reported, NOT gated
  quality       duplicates, unusable fields,    -> gated at 5%
                already-settled contracts

and "expected contracts" for both the §6.4 severe flag and the §1.2 validity
rule means contracts with open interest, which is the population the study is
actually about. Both counts appear in every data-quality record, so anyone who
disagrees with this reading can recompute the other one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .capture import root_of
from .config import Config, raw_chain_dir, repo_path
from .transmission import read_transmission_spot
from .expiry import (
    dte_calendar,
    expiration_timestamp,
    expiry_bucket,
    is_am_settled_monthly,
    time_to_expiry_years,
)

# provider column -> our name. The only mapping table in the codebase.
COLUMN_MAP = {
    "details.ticker": "symbol",
    "details.contract_type": "option_type",
    "details.strike_price": "strike",
    "details.expiration_date": "expiration_date",
    "details.shares_per_contract": "multiplier",
    "open_interest": "open_interest",
    "day.volume": "volume",
    "implied_volatility": "massive_iv",
    "greeks.gamma": "massive_gamma",
    "greeks.delta": "massive_delta",
    "greeks.vega": "massive_vega",
    "greeks.theta": "massive_theta",
    "underlying_asset.price": "underlying_spot",
}

STALE_QUOTE_GRACE_MINUTES = 5.0


class NormalizeError(RuntimeError):
    pass


@dataclass
class FilterLedger:
    """Every filter's effect, split into the two ledgers described above."""

    definitional: dict[str, int] = field(default_factory=dict)
    quality: dict[str, int] = field(default_factory=dict)
    flags: dict[str, int] = field(default_factory=dict)
    not_applicable: dict[str, str] = field(default_factory=dict)

    def drop_definitional(self, name: str, count: int) -> None:
        self.definitional[name] = self.definitional.get(name, 0) + int(count)

    def drop_quality(self, name: str, count: int) -> None:
        self.quality[name] = self.quality.get(name, 0) + int(count)

    def flag(self, name: str, count: int) -> None:
        self.flags[name] = self.flags.get(name, 0) + int(count)

    @property
    def total_quality_drops(self) -> int:
        return sum(self.quality.values())

    @property
    def total_definitional_drops(self) -> int:
        return sum(self.definitional.values())


def _read_meta(date_str: str, label: str) -> dict[str, Any]:
    path = raw_chain_dir(date_str) / f"{label}.meta.json"
    if not path.exists():
        raise NormalizeError(f"no capture meta at {path}")
    return json.loads(path.read_text())


def _read_raw(date_str: str, label: str) -> pd.DataFrame:
    path = raw_chain_dir(date_str) / f"{label}.parquet"
    if not path.exists():
        raise NormalizeError(f"no raw capture at {path}")
    return pd.read_parquet(path)


def normalized_dir(date_str: str) -> Path:
    return repo_path("data", "normalized", date_str)


def _transmission_spot_for(
    date_str: str, asof: datetime, max_gap_minutes: float = 5.0
) -> dict[str, Any] | None:
    """The hand-recorded spot for the instant this surface actually carries.

    Matched on the resolved as-of, not on the capture label. The label is what
    the capture was *aiming* at; the as-of is what it *got*, and when a download
    slips they are different. On the first live day the 09:45 capture resolved
    to 09:52 while spot was moving 22 points in 15 minutes — pairing it with the
    09:45 price would have put a surface and a spot from seven minutes apart
    into the same gamma.

    Still no interpolation: the nearest recorded instant or nothing. A gap wider
    than `max_gap_minutes` returns None rather than a plausible-looking guess,
    and the gap itself is reported so it can never be silently absorbed.
    """
    rows = read_transmission_spot(date_str)
    if not rows:
        return None
    nearest = min(rows, key=lambda r: abs((r.timestamp - asof).total_seconds()))
    gap_minutes = abs((nearest.timestamp - asof).total_seconds()) / 60.0
    if gap_minutes > max_gap_minutes:
        return {
            "spot": None,
            "time_label": nearest.time_label,
            "gap_minutes": round(gap_minutes, 2),
            "too_far": True,
        }
    return {
        "spot": nearest.spot,
        "time_label": nearest.time_label,
        "gap_minutes": round(gap_minutes, 2),
        "too_far": False,
    }


def normalize_capture(
    config: Config,
    date_str: str,
    label: str,
    raw: pd.DataFrame | None = None,
    meta: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Normalize one capture. Returns (frame, data_quality_record)."""
    meta = meta if meta is not None else _read_meta(date_str, label)
    raw = raw if raw is not None else _read_raw(date_str, label)
    tz = ZoneInfo(config.get("capture.timezone", "America/New_York"))
    capture_date = date.fromisoformat(date_str)
    ledger = FilterLedger()

    # §5.3 — one resolved instant for the whole snapshot. Starter has no
    # per-contract stamp, so T is anchored to the capture's asof and the level
    # it came from travels with every row.
    asof_utc_str = meta.get("asof_utc")
    if not asof_utc_str:
        raise NormalizeError(
            f"{date_str}/{label} has no resolved asof. T cannot be computed from "
            "wall-clock time (§5.3); re-run the capture or fix the meta."
        )
    asof = datetime.fromisoformat(asof_utc_str)

    raw_rows = len(raw)
    missing = [c for c in ("details.ticker", "details.strike_price") if c not in raw.columns]
    if missing:
        raise NormalizeError(f"raw capture is missing {missing}; cannot normalize")

    frame = pd.DataFrame(index=raw.index)
    for provider_col, our_col in COLUMN_MAP.items():
        frame[our_col] = raw[provider_col] if provider_col in raw.columns else pd.NA

    frame["root"] = frame["symbol"].map(root_of)
    frame["spot_source"] = "massive_underlying_asset"
    frame["option_type"] = frame["option_type"].astype("string").str.lower()
    for numeric in (
        "strike",
        "multiplier",
        "open_interest",
        "volume",
        "massive_iv",
        "massive_gamma",
        "massive_delta",
        "massive_vega",
        "massive_theta",
        "underlying_spot",
    ):
        frame[numeric] = pd.to_numeric(frame[numeric], errors="coerce")

    # §5.4 — the manual transmission drop is the designated spot source, so if
    # Massive omits `underlying_asset.price` this is a fallback, not a
    # failure. Which source was used is stamped on every row: a spot from a
    # different clock than the surface is a real caveat, not a detail.
    spot_match: dict[str, Any] | None = None
    if frame["underlying_spot"].isna().all() and label != "oi_base":
        spot_match = _transmission_spot_for(
            date_str,
            asof,
            float(config.get("normalization.spot_match_max_gap_minutes", 5.0)),
        )
        if spot_match and not spot_match["too_far"]:
            frame["underlying_spot"] = spot_match["spot"]
            frame["spot_source"] = (
                f"transmission_spot.csv[{spot_match['time_label']}]"
                f" +{spot_match['gap_minutes']:.1f}min from asof"
            )
            ledger.flag("underlying_spot_from_transmission", len(frame))

    # --- §6.4 filters, in order, each one counted ---------------------------

    unknown_root = frame["root"] == "?"
    if unknown_root.any():
        ledger.drop_quality("unknown_root", int(unknown_root.sum()))
        frame = frame[~unknown_root]

    bad_type = ~frame["option_type"].isin(["call", "put"])
    if bad_type.any():
        ledger.drop_quality("unparseable_option_type", int(bad_type.sum()))
        frame = frame[~bad_type]

    bad_strike = frame["strike"].isna() | (frame["strike"] <= 0)
    if bad_strike.any():
        ledger.drop_quality("unparseable_strike", int(bad_strike.sum()))
        frame = frame[~bad_strike]

    parsed_expiry = pd.to_datetime(frame["expiration_date"], errors="coerce")
    bad_expiry = parsed_expiry.isna()
    if bad_expiry.any():
        ledger.drop_quality("unparseable_expiration_date", int(bad_expiry.sum()))
        frame = frame[~bad_expiry]
        parsed_expiry = parsed_expiry[~bad_expiry]
    frame["expiration_date"] = parsed_expiry.dt.date

    duplicated = frame.duplicated(
        subset=["root", "expiration_date", "strike", "option_type"], keep="first"
    )
    if duplicated.any():
        ledger.drop_quality("duplicate_contract", int(duplicated.sum()))
        frame = frame[~duplicated]

    # Definitional, not a quality loss — see the module docstring.
    zero_oi = frame["open_interest"].fillna(0) == 0
    ledger.drop_definitional("zero_open_interest", int(zero_oi.sum()))
    frame = frame[~zero_oi]

    # --- §6.2 / §6.3 derived time -------------------------------------------
    settlement_times = config.get("normalization.settlement_times_et", None)
    frame["expiration_timestamp"] = [
        expiration_timestamp(exp, root, tz, settlement_times)
        for exp, root in zip(frame["expiration_date"], frame["root"])
    ]
    frame["settlement_type"] = frame["root"].map(lambda r: "AM" if r == "SPX" else "PM")
    frame["am_settled_monthly"] = [
        is_am_settled_monthly(exp, root)
        for exp, root in zip(frame["expiration_date"], frame["root"])
    ]
    convention = config.get("normalization.time_convention", "calendar")
    frame["t_years"] = [
        time_to_expiry_years(ts, asof, convention) for ts in frame["expiration_timestamp"]
    ]
    frame["t_minutes"] = frame["t_years"] * 365.0 * 24.0 * 60.0

    settled = frame["t_years"] <= 0
    if settled.any():
        # Not a Massive error: on the third Friday the AM-settled SPX monthly has
        # already settled by 09:45 while still appearing in the chain.
        ledger.drop_quality("already_settled_at_asof", int(settled.sum()))
        frame = frame[~settled]

    frame["dte"] = [
        dte_calendar(exp, asof.astimezone(tz).date()) for exp in frame["expiration_date"]
    ]
    frame["expiry_bucket"] = frame["dte"].map(expiry_bucket)

    # --- flags (retained, not dropped) --------------------------------------
    bad_iv = frame["massive_iv"].isna() | (frame["massive_iv"] <= 0)
    frame["bad_massive_iv"] = bad_iv
    ledger.flag("bad_massive_iv", int(bad_iv.sum()))

    no_gamma = frame["massive_gamma"].isna()
    frame["no_massive_gamma"] = no_gamma
    ledger.flag("no_massive_gamma", int(no_gamma.sum()))

    # Vendor greeks that are not merely missing but *impossible*. Verified
    # against the live chain on 2026-07-27: 5.6% of contracts carried gamma < 0
    # and 6.2% vega < 0, both meaningless for a long option, alongside IVs of
    # 0.03%. They are the residue of IV inversion failing on deep-ITM contracts
    # — 43.8% of strikes more than 15% in the money were affected, against 0%
    # out of the money.
    #
    # A negative gamma summed into net GEX is corruption, not noise, so these
    # are excluded from every computed quantity. They are flagged rather than
    # dropped: the normalized layer stays a faithful account of what Massive
    # said, and `usable_for_gex` carries the decision downstream.
    sanity = config.get("normalization.sanity", {}) or {}
    iv_floor = float(sanity.get("min_massive_iv", 0.01))
    iv_ceiling = float(sanity.get("max_massive_iv", 3.0))

    # Strictly negative, not <= 0. A gamma of exactly 0.0 is Massive
    # rounding an underflowing value (9 decimals on this feed), and it
    # contributes exactly nothing to GEX — harmless in the same way a zero-OI
    # contract is. A gamma below zero is impossible and therefore corrupt.
    # The live chain carries both: 714 negative, 21 exactly zero.
    implausible = (
        (frame["massive_gamma"].notna() & (frame["massive_gamma"] < 0))
        | (frame["massive_vega"].notna() & (frame["massive_vega"] < 0))
        | (frame["massive_iv"].notna() & (frame["massive_iv"] < iv_floor))
        | (frame["massive_iv"].notna() & (frame["massive_iv"] > iv_ceiling))
    )
    frame["implausible_greeks"] = implausible
    ledger.flag("implausible_greeks", int(implausible.sum()))

    frame["usable_for_gex"] = (
        ~implausible
        & frame["massive_gamma"].notna()
        & (frame["massive_gamma"] > 0)
        & ~bad_iv
        & frame["underlying_spot"].notna()
        & (frame["t_years"] > 0)
    )

    # §6.4's stale-quote rule, adjusted for a delayed tier: on a 15-minute feed
    # every quote is 15 minutes old by design, so the threshold is the delay
    # plus the grace period, not the grace period alone.
    delay_min = float(config.get("provider.delay_minutes", 0.0))
    request_time = datetime.fromisoformat(meta["request_finished_utc"])
    asof_age_minutes = (request_time - asof).total_seconds() / 60.0
    stale = asof_age_minutes > (delay_min + STALE_QUOTE_GRACE_MINUTES)
    frame["stale_quote"] = stale
    if stale:
        ledger.flag("stale_quote_all_rows", len(frame))

    # Filters with no data to act on this tier — recorded, not deleted (§6.4).
    for name, status in (config.get("normalization.filters", {}) or {}).items():
        if status == "not_applicable_on_this_plan":
            ledger.not_applicable[name] = config.get(
                "normalization.filters_na_reason", "no data for this filter"
            )

    # --- provenance stamped on every row (§9) --------------------------------
    frame["date"] = date_str
    frame["time_label"] = label
    frame["asof_utc"] = asof.isoformat()
    frame["asof_source_level"] = meta.get("asof_source_level")
    frame["asof_is_inferred"] = bool(meta.get("asof_is_inferred", False))
    frame["oi_asof"] = meta.get("oi_asof_date")
    frame["time_convention"] = convention
    frame["expiration_timestamp_source"] = config.get(
        "normalization.expiration_timestamp_source", "derived"
    )
    frame["gex_definition_version"] = config.get("gex.definition_version", "1.0")
    frame["sign_convention"] = config.get("gex.sign_convention", "")
    frame["expiry_scope_max_dte"] = config.get("capture.expiry_scope_max_dte")
    frame["provider"] = meta.get("provider")
    frame["provider_plan"] = meta.get("provider_plan")

    frame = frame.reset_index(drop=True)
    quality = _build_quality_record(
        config, date_str, label, meta, ledger, raw_rows, frame, spot_match
    )
    return frame, quality


def _build_quality_record(
    config: Config,
    date_str: str,
    label: str,
    meta: dict[str, Any],
    ledger: FilterLedger,
    raw_rows: int,
    frame: pd.DataFrame,
    spot_match: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # "Expected" = contracts with open interest. See the module docstring.
    expected = raw_rows - ledger.total_definitional_drops
    retained = len(frame)
    retention_pct = round(100.0 * retained / expected, 3) if expected else 0.0
    quality_drop_pct = (
        round(100.0 * ledger.total_quality_drops / expected, 3) if expected else 0.0
    )
    threshold = float(config.get("normalization.severe_drop_threshold_pct", 5.0))

    # The count of implausible rows is the wrong thing to gate on: they are ~11%
    # of a real chain and almost all deep ITM, where open interest is thin. What
    # matters for GEX is how much *weight* they carry near the money. Measured
    # on 2026-07-27, OI-weighted and |GEX|-weighted agree closely (1.15% vs
    # 1.27% at 15:45), so OI is a fair and much cheaper proxy.
    #
    # The gate applies to the layers this row actually reports. Vendor greeks
    # degrade sharply as expiry approaches — the 0DTE bucket lost 1.4% of its
    # open interest at 09:45 and 8.8% at 15:45 — and at 15:45 that layer is
    # already held out of the aggregate for the §9.1 reason. Letting an excluded
    # layer's data quality invalidate the layers that *are* reported would fail
    # the 15:45 capture every single day while saying nothing about the numbers
    # the row actually carries.
    ntm_oi_lost_pct = 0.0
    ntm_oi_lost_pct_all = 0.0
    if len(frame) and "implausible_greeks" in frame:
        spot_ref = frame["underlying_spot"].dropna()
        if len(spot_ref):
            spot_value = float(spot_ref.iloc[0])
            near = (frame["strike"] - spot_value).abs() / spot_value <= 0.10
            bad = frame["implausible_greeks"]

            def _lost(mask):
                total = float(frame.loc[mask, "open_interest"].sum())
                lost = float(frame.loc[mask & bad, "open_interest"].sum())
                return round(100.0 * lost / total, 4) if total else 0.0

            ntm_oi_lost_pct_all = _lost(near)
            excluded = list(
                config.get("gex.zero_dte_excluded_from_aggregate_times", []) or []
            )
            reported = near
            if label in excluded:
                reported = near & (frame["expiry_bucket"] != "0DTE")
            ntm_oi_lost_pct = _lost(reported)

    spot_series = frame["underlying_spot"].dropna() if "underlying_spot" in frame else []
    spot_missing = len(spot_series) == 0

    severe: list[str] = []
    if quality_drop_pct > threshold:
        severe.append(
            f"quality drops {quality_drop_pct:.2f}% exceed the {threshold}% threshold"
        )
    # The OI base is captured pre-market for open interest alone; no surface and
    # no spot are computed from it, so a missing spot there is not severe.
    ntm_threshold = float(
        config.get("normalization.sanity.max_ntm_oi_lost_pct", 1.0)
    )
    if ntm_oi_lost_pct > ntm_threshold:
        severe.append(
            f"implausible Massive greeks carry {ntm_oi_lost_pct:.2f}% of the "
            f"open interest within 10% of spot in this row's reported layers "
            f"(threshold {ntm_threshold}%) — that is GEX weight the study "
            "cannot compute"
        )
    if spot_missing and label != "oi_base":
        detail = ""
        if spot_match and spot_match.get("too_far"):
            detail = (
                f"; nearest recorded spot is {spot_match['time_label']}, "
                f"{spot_match['gap_minutes']:.1f} min from the resolved as-of "
                "— too far to use"
            )
        severe.append(
            "underlying spot missing from both the Massive payload and "
            f"transmission_spot.csv (§5.4){detail}"
        )

    return {
        "date": date_str,
        "time_label": label,
        "raw_rows": raw_rows,
        "expected_contracts": expected,
        "expected_definition": "raw rows minus zero-open-interest contracts",
        "retained": retained,
        "retention_pct": retention_pct,
        "definitional_drops": ledger.definitional,
        "definitional_drops_total": ledger.total_definitional_drops,
        "quality_drops": ledger.quality,
        "quality_drops_total": ledger.total_quality_drops,
        "quality_drop_pct": quality_drop_pct,
        "severe_drop_threshold_pct": threshold,
        "flags": ledger.flags,
        "filters_not_applicable": ledger.not_applicable,
        # 31-45 DTE is inside the capture scope but is not one of §6.5's reported
        # layers. Naming it rather than leaving a NaN key keeps the record
        # readable and stops it being mistaken for missing data.
        "contracts_by_bucket": (
            {
                ("out_of_scope_31-45DTE" if pd.isna(k) else str(k)): int(v)
                for k, v in frame["expiry_bucket"]
                .value_counts(dropna=False)
                .items()
            }
            if len(frame)
            else {}
        ),
        "am_settled_monthly_contracts": (
            int(frame["am_settled_monthly"].sum()) if len(frame) else 0
        ),
        "implausible_greeks": int(frame["implausible_greeks"].sum()) if len(frame) else 0,
        "usable_for_gex": int(frame["usable_for_gex"].sum()) if len(frame) else 0,
        "ntm_oi_lost_to_implausible_pct": ntm_oi_lost_pct,
        "ntm_oi_lost_to_implausible_pct_all_buckets": ntm_oi_lost_pct_all,
        "ntm_gate_scope": (
            "excludes the 0DTE layer, which this row holds out of the aggregate"
            if label in (config.get("gex.zero_dte_excluded_from_aggregate_times", []) or [])
            else "all reported buckets"
        ),
        "spot_source": (
            str(frame["spot_source"].iloc[0]) if len(frame) else None
        ),
        "spot_match": spot_match,
        "asof_utc": meta.get("asof_utc"),
        "asof_source_level": meta.get("asof_source_level"),
        "asof_is_inferred": meta.get("asof_is_inferred"),
        "severe_flags": severe,
        # §1.2: a valid day needs >=95% retention and no unresolved severe flag
        "meets_retention_rule": retention_pct >= 95.0,
        "passed": (not severe) and retention_pct >= 95.0,
    }


def write_normalized(
    frame: pd.DataFrame,
    quality: dict[str, Any],
    date_str: str,
    label: str,
    output_root: Path | None = None,
) -> tuple[Path, Path]:
    out_dir = (
        Path(output_root) / date_str
        if output_root is not None
        else normalized_dir(date_str)
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = out_dir / f"{label}.parquet"
    quality_path = out_dir / f"{label}.quality.json"

    stored = frame.copy()
    # Parquet wants a single tz for a datetime column; store UTC and convert for
    # display, per §6.1.
    stored["expiration_timestamp"] = pd.to_datetime(
        stored["expiration_timestamp"], utc=True
    )
    stored["expiration_date"] = stored["expiration_date"].astype(str)
    stored.to_parquet(parquet_path, index=False)
    quality_path.write_text(json.dumps(quality, indent=2, sort_keys=True, default=str))
    return parquet_path, quality_path
