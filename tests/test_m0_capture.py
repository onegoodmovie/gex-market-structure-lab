"""M0 acceptance tests (spec §16.1-4, §16.15)."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spx_gex.capture import (  # noqa: E402
    CaptureRefused,
    capture,
    previous_business_day,
    root_of,
    run_checks,
)
from spx_gex.config import Config, ConfigError, repo_path  # noqa: E402
from spx_gex.providers import get_provider  # noqa: E402
from spx_gex.providers.base import flatten_record, flatten_records  # noqa: E402

# A date that can never be a study day, so a stray file is obvious.
FIXTURE_DATE = "1999-01-04"


@pytest.fixture
def config() -> Config:
    return Config.load()


@pytest.fixture
def clean_fixture_day():
    target = repo_path("data", "raw_chain", FIXTURE_DATE)
    shutil.rmtree(target, ignore_errors=True)
    yield target
    shutil.rmtree(target, ignore_errors=True)


# --- flattening ---------------------------------------------------------------


def test_flatten_preserves_provider_names():
    record = {
        "open_interest": 12,
        "details": {"strike_price": 7400.0, "ticker": "O:SPXW260724C07400000"},
        "greeks": {"gamma": 0.0007},
    }
    flat = flatten_record(record)
    assert flat == {
        "open_interest": 12,
        "details.strike_price": 7400.0,
        "details.ticker": "O:SPXW260724C07400000",
        "greeks.gamma": 0.0007,
    }


def test_flatten_records_column_order_is_first_seen():
    frame = flatten_records([{"b": 1, "a": 2}, {"c": 3, "a": 4}])
    assert list(frame.columns) == ["b", "a", "c"]
    assert pd.isna(frame.loc[1, "b"])


def test_flatten_json_encodes_lists():
    assert flatten_record({"x": [1, 2]}) == {"x": "[1, 2]"}


# --- small pure helpers -------------------------------------------------------


@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("O:SPXW260724C07400000", "SPXW"),
        ("O:SPX260821P07000000", "SPX"),
        ("SPXW260724C07400000", "SPXW"),
        ("O:XSP260724C00740000", "?"),
        (None, "?"),
    ],
)
def test_root_of(ticker, expected):
    assert root_of(ticker) == expected


def test_previous_business_day_skips_the_weekend():
    assert previous_business_day(date(2026, 7, 27)) == date(2026, 7, 24)  # Mon -> Fri
    assert previous_business_day(date(2026, 7, 24)) == date(2026, 7, 23)


# --- §16.15 path containment --------------------------------------------------


def test_repo_path_refuses_to_escape():
    with pytest.raises(ConfigError):
        repo_path("..", "somewhere_else")


# --- §16.1-4 capture ----------------------------------------------------------


def test_capture_writes_raw_and_meta(config, clean_fixture_day):
    provider = get_provider(config, "synthetic")
    meta = capture(config, provider, FIXTURE_DATE, label="oi_base", kind="oi_base")

    parquet = clean_fixture_day / "oi_base.parquet"
    meta_file = clean_fixture_day / "oi_base.meta.json"
    assert parquet.exists() and meta_file.exists()

    # §16.1 — provider column names survive the round trip unmodified
    frame = pd.read_parquet(parquet)
    for column in ("details.strike_price", "greeks.gamma", "open_interest"):
        assert column in frame.columns
    assert len(frame) == meta["row_count"]

    # §16.2 — required provenance
    stored = json.loads(meta_file.read_text())
    for key in (
        "provider",
        "endpoint",
        "request_started_utc",
        "provider_timestamp_max_utc",
        "row_count",
        "parquet_sha256",
    ):
        assert stored[key] is not None, key

    # §16.3 — both roots
    assert stored["checks"]["missing_roots"] == []
    assert stored["checks"]["roots"]["SPX"] > 0
    assert stored["checks"]["roots"]["SPXW"] > 0

    # §5.1 — OI is as-of the prior session. The rule is weekday-only: 1999-01-01
    # was a Friday *and* a market holiday, and the value still names it. That is
    # the documented limitation, asserted here so it cannot regress unnoticed.
    assert stored["oi_asof_date"] == "1999-01-01"
    assert "No exchange holiday calendar applied" in stored["oi_asof_rule"]


def test_capture_never_overwrites(config, clean_fixture_day):
    provider = get_provider(config, "synthetic")
    capture(config, provider, FIXTURE_DATE, label="0945", kind="surface")
    before = (clean_fixture_day / "0945.parquet").read_bytes()

    with pytest.raises(CaptureRefused):
        capture(config, provider, FIXTURE_DATE, label="0945", kind="surface")

    assert (clean_fixture_day / "0945.parquet").read_bytes() == before


def test_capture_refuses_a_bad_date(config, clean_fixture_day):
    provider = get_provider(config, "synthetic")
    with pytest.raises(CaptureRefused):
        capture(config, provider, "04/01/1999", label="0945", kind="surface")


def test_oi_is_frozen_across_intraday_snapshots(config, clean_fixture_day):
    """§13: OI must not move between snapshots of the same day, but the
    surface must. A fixture where neither moves would make M3 vacuous."""
    provider = get_provider(config, "synthetic")
    frames = {}
    for label in ("0945", "1545"):
        capture(config, provider, FIXTURE_DATE, label=label, kind="surface")
        frames[label] = pd.read_parquet(clean_fixture_day / f"{label}.parquet")

    key = ["details.ticker"]
    merged = frames["0945"].merge(frames["1545"], on=key, suffixes=("_a", "_b"))
    assert len(merged) == len(frames["0945"])
    assert (merged["open_interest_a"] == merged["open_interest_b"]).all()
    assert merged["underlying_asset.price_a"].iloc[0] != merged["underlying_asset.price_b"].iloc[0]
    assert not (merged["implied_volatility_a"] == merged["implied_volatility_b"]).all()


# --- checks block -------------------------------------------------------------


def test_run_checks_flags_a_missing_critical_field():
    frame = pd.DataFrame(
        {
            "details.ticker": ["O:SPXW260724C07400000", "O:SPX260821P07000000"],
            "details.strike_price": [7400.0, 7000.0],
            "open_interest": [10, 20],
        }
    )
    checks = run_checks(frame)
    assert "greeks.gamma" in checks["missing_critical_fields"]
    assert checks["passed"] is False
    assert checks["roots"] == {"SPXW": 1, "SPX": 1}


def test_greeks_gate_is_near_the_money_not_chain_wide(config, clean_fixture_day):
    """§7 says providers drop greeks on deep ITM. A chain-wide 95% gate would
    therefore reject good captures; the gate applies inside ±10% of spot."""
    provider = get_provider(config, "synthetic")
    meta = capture(config, provider, FIXTURE_DATE, label="0945", kind="surface")
    checks = meta["checks"]

    assert checks["field_coverage_pct"]["greeks.gamma"] < 95.0  # would fail a naive gate
    assert checks["near_the_money_coverage_pct"]["greeks.gamma"] >= 95.0
    assert "greeks.gamma" not in checks["missing_critical_fields"]
    assert checks["passed"] is True


def test_missing_greeks_near_the_money_does_fail(config):
    frame = pd.DataFrame(
        {
            "details.ticker": ["O:SPXW260724C07400000", "O:SPX260821P07000000"],
            "details.strike_price": [7400.0, 7000.0],
            "details.expiration_date": ["2026-07-24", "2026-08-21"],
            "details.contract_type": ["call", "put"],
            "details.shares_per_contract": [100, 100],
            "open_interest": [10, 20],
            "underlying_asset.price": [7400.0, 7400.0],
            "implied_volatility": [0.15, 0.18],
            "greeks.gamma": [None, 0.0004],  # the ATM one is the one that matters
        }
    )
    checks = run_checks(frame)
    assert "greeks.gamma" in checks["missing_critical_fields"]
    assert checks["passed"] is False


# --- §5.3 as-of resolution ladder --------------------------------------------


def test_asof_uses_level_2_when_the_plan_has_no_quotes(config, clean_fixture_day):
    """Starter returns no last_quote, so level 1 is skipped, not failed."""
    provider = get_provider(config, "synthetic")
    meta = capture(config, provider, FIXTURE_DATE, label="0945", kind="surface")
    assert meta["asof_source_level"] == 2
    assert meta["asof_source_field"] == "underlying_asset.last_updated"
    assert meta["asof_is_inferred"] is False


def test_asof_falls_back_to_inference_when_no_stamp_exists(config, clean_fixture_day):
    cfg = config.override("provider.synthetic.stamp_mode", "none")
    provider = get_provider(cfg, "synthetic")
    meta = capture(cfg, provider, FIXTURE_DATE, label="0945", kind="surface")

    assert meta["asof_source_level"] == 4
    assert meta["asof_source_field"] is None
    assert meta["asof_is_inferred"] is True
    assert "request_time - 900s" in meta["asof_note"]

    # The inferred instant is exactly the delay behind the request
    requested = datetime.fromisoformat(meta["request_finished_utc"])
    resolved = datetime.fromisoformat(meta["asof_utc"])
    assert abs((requested - resolved).total_seconds() - 900) < 1.0


def test_identical_stamps_across_captures_degrade_to_inference(config, clean_fixture_day):
    """§5.3: a field that reads the same at 09:45 and 14:00 is a daily stamp.

    It resolves cleanly at level 2 on the first capture — there is nothing to
    compare against yet — and only the second capture can expose it. That is
    exactly why the check is mandatory rather than nice to have.
    """
    cfg = config.override("provider.synthetic.stamp_mode", "daily")
    provider = get_provider(cfg, "synthetic")

    first = capture(cfg, provider, FIXTURE_DATE, label="0945", kind="surface")
    assert first["asof_source_level"] == 2
    assert first["asof_intraday_collision"] is None

    second = capture(cfg, provider, FIXTURE_DATE, label="1400", kind="surface")
    assert second["asof_intraday_collision"]["collided_with"] == "0945"
    assert second["asof_source_level"] == 4
    assert second["asof_is_inferred"] is True
    assert second["asof_degraded_from"]["level"] == 2
    assert second["asof_degraded_from"]["field"] == "underlying_asset.last_updated"


def test_distinct_stamps_do_not_degrade(config, clean_fixture_day):
    provider = get_provider(config, "synthetic")
    capture(config, provider, FIXTURE_DATE, label="0945", kind="surface")
    second = capture(config, provider, FIXTURE_DATE, label="1400", kind="surface")
    assert second["asof_intraday_collision"] is None
    assert second["asof_source_level"] == 2
    assert second["asof_is_inferred"] is False


# --- §9.2 revised: 0DTE at 15:45 is kept, marked, and held out of aggregate ---


@pytest.mark.parametrize(
    "label,not_comparable",
    [("0945", False), ("1400", False), ("1545", True)],
)
def test_zero_dte_is_captured_and_marked_not_excluded(
    config, clean_fixture_day, label, not_comparable
):
    """Shifting the download clock removed the delay argument for dropping the
    15:45 0DTE layer. What remains is §9.1's 1/√T instability, which is a
    reason to mark the number, not to discard it."""
    provider = get_provider(config, "synthetic")
    meta = capture(config, provider, FIXTURE_DATE, label=label, kind="surface")

    assert meta["zero_dte_not_cross_day_comparable"] is not_comparable
    assert meta["zero_dte_excluded_from_aggregate"] is not_comparable
    assert meta["zero_dte_layer_absent"] is False

    # Kept in every case — the whole point of the revision
    assert meta["zero_dte_contracts_returned"] > 0
    if not_comparable:
        assert "1/√T" in meta["zero_dte_note"]
        assert "not of the delay" in meta["zero_dte_note"]


def test_zero_dte_marking_does_not_depend_on_the_delay_flag(config, clean_fixture_day):
    """The §9.1 justification is numerical, so it must survive is_delayed=False.
    A flag that moved with the tier would be the old delay rule in disguise."""
    cfg = config.override("provider.is_delayed", False)
    provider = get_provider(cfg, "synthetic")
    meta = capture(cfg, provider, FIXTURE_DATE, label="1545", kind="surface")
    assert meta["zero_dte_not_cross_day_comparable"] is True


def test_implied_delay_is_not_measured_premarket(config, clean_fixture_day):
    """The OI base runs before the open and transmission spot starts at 09:45.
    Matching them would compare a pre-open snapshot against post-open prices
    and report a confident negative lag."""
    provider = get_provider(config, "synthetic")
    meta = capture(config, provider, FIXTURE_DATE, label="oi_base", kind="oi_base")
    assert meta["implied_delay"]["identifiable"] is False
    assert meta["implied_delay"]["warn"] is False
    assert "pre-market" in meta["implied_delay"]["reason"]


def test_dte_buckets_answer_the_monday_question(config, clean_fixture_day):
    """Whether the provider still returns same-day PM-settled contracts in a late
    snapshot is recorded on every capture, so the answer lands in the data."""
    provider = get_provider(config, "synthetic")
    meta = capture(config, provider, FIXTURE_DATE, label="1545", kind="surface")
    buckets = meta["contracts_by_dte"]
    assert set(buckets) == {"0DTE", "1-7DTE", "8-30DTE", "31+DTE", "expired"}
    assert buckets["expired"] == 0
    assert meta["zero_dte_contracts_returned"] == buckets["0DTE"]


# --- plan-absent fields -------------------------------------------------------


def test_quote_fields_are_na_not_missing(config, clean_fixture_day):
    """§6.4: the two quote-dependent filters have no data to act on. That is
    recorded as N/A; it must never look like a failed capture."""
    provider = get_provider(config, "synthetic")
    meta = capture(config, provider, FIXTURE_DATE, label="0945", kind="surface")
    na = meta["checks"]["not_applicable_on_this_plan"]

    for field in ("last_quote.bid", "last_quote.ask", "last_quote.midpoint"):
        assert field in na
        assert field not in meta["checks"]["missing_critical_fields"]
    assert meta["checks"]["passed"] is True
    assert meta["checks"]["unexpectedly_present"] == []


def test_unexpected_quote_fields_are_surfaced(config):
    """If the tier ever returns more than assumed, say so rather than ignore it."""
    frame = pd.DataFrame(
        {
            "details.ticker": ["O:SPXW260724C07400000", "O:SPX260821P07000000"],
            "details.strike_price": [7400.0, 7000.0],
            "details.expiration_date": ["2026-07-24", "2026-08-21"],
            "details.contract_type": ["call", "put"],
            "details.shares_per_contract": [100, 100],
            "open_interest": [10, 20],
            "underlying_asset.price": [7400.0, 7400.0],
            "implied_volatility": [0.15, 0.18],
            "greeks.gamma": [0.0007, 0.0004],
            "last_quote.bid": [1.2, 3.4],
        }
    )
    checks = run_checks(frame, Config.load())
    assert "last_quote.bid" in checks["unexpectedly_present"]


def test_run_checks_flags_a_missing_root():
    frame = pd.DataFrame({"details.ticker": ["O:SPXW260724C07400000"]})
    assert run_checks(frame)["missing_roots"] == ["SPX"]
