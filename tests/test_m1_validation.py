"""M1 gate tests (spec §7, §16.5-8) and the §3.2 put-call cross-check.

The point of most of these is not that the code runs, but that the checks can
actually *fail* when they should. A detector that has never been shown a defect
is not a detector.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spx_gex.capture import capture  # noqa: E402
from spx_gex.config import Config, repo_path  # noqa: E402
from spx_gex.expiry import (  # noqa: E402
    dte_calendar,
    expiry_bucket,
    is_am_settled_monthly,
    third_friday,
    time_to_expiry_years,
)
from spx_gex.greeks import gamma  # noqa: E402
from spx_gex.normalize import normalize_capture  # noqa: E402
from spx_gex.providers import get_provider  # noqa: E402
from spx_gex.validation import (  # noqa: E402
    build_sample,
    implied_drift_scan,
    moneyness_class,
    path_a,
    put_call_iv_consistency,
    systematic_tilt,
)

# The synthetic provider prices with these. Nothing in the validation path is
# told them — recovering them is the test.
TRUE_R, TRUE_Q = 0.045, 0.013
TRUE_DRIFT = TRUE_R - TRUE_Q

FIXTURE_DATE = "1999-01-04"

# §7.3, matched to 1e-9
GAMMA_FIXTURES = [
    (100.0, 100.0, 0.00, 0.000, 0.20, 1.0, 0.0198476274),
    (100.0, 110.0, 0.00, 0.000, 0.20, 1.0, 0.0185819222),
    (7400.0, 7400.0, 0.045, 0.013, 0.15, 0.25, 0.0007090754),
    (7400.0, 7400.0, 0.045, 0.013, 0.15, 1.0 / 365.0, 0.0068654434),
    (7400.0, 7000.0, 0.045, 0.013, 0.18, 0.5, 0.0003459537),
]


@pytest.fixture
def config() -> Config:
    return Config.load()


@pytest.fixture
def clean_fixture_day():
    raw = repo_path("data", "raw_chain", FIXTURE_DATE)
    norm = repo_path("data", "normalized", FIXTURE_DATE)
    for path in (raw, norm):
        shutil.rmtree(path, ignore_errors=True)
    yield raw
    for path in (raw, norm):
        shutil.rmtree(path, ignore_errors=True)


def _normalized(config: Config, clean_fixture_day, label: str = "0945") -> pd.DataFrame:
    provider = get_provider(config, "synthetic")
    capture(config, provider, FIXTURE_DATE, label=label, kind="surface")
    frame, _ = normalize_capture(config, FIXTURE_DATE, label)
    return frame


# --- §7.3 fixtures ------------------------------------------------------------


@pytest.mark.parametrize("S,K,r,q,sigma,T,expected", GAMMA_FIXTURES)
def test_gamma_matches_spec_fixtures(S, K, r, q, sigma, T, expected):
    assert abs(gamma(S, K, r, q, sigma, T) - expected) < 1e-9


def test_gamma_returns_none_rather_than_zero_when_undefined():
    """A silent 0.0 would sum into net GEX as a measurement of nothing."""
    assert gamma(7400, 7400, 0.045, 0.013, 0.15, 0.0) is None
    assert gamma(7400, 7400, 0.045, 0.013, 0.0, 0.25) is None


# --- §6.2 expiry --------------------------------------------------------------


def test_am_settled_monthly_detection():
    assert third_friday(2026, 8) == pd.Timestamp("2026-08-21").date()
    assert is_am_settled_monthly(pd.Timestamp("2026-08-21").date(), "SPX")
    assert not is_am_settled_monthly(pd.Timestamp("2026-08-21").date(), "SPXW")
    assert not is_am_settled_monthly(pd.Timestamp("2026-08-20").date(), "SPX")


def test_am_settlement_shortens_t_by_the_documented_amount(config):
    """§6.2: from Thursday 15:45 an AM-settled monthly has ~17.75h left, not
    ~24.25h. Getting this wrong is a ~17% gamma error once a month."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from spx_gex.expiry import expiration_timestamp

    tz = ZoneInfo("America/New_York")
    friday = third_friday(2026, 8)
    thursday_1545 = datetime(2026, 8, 20, 15, 45, tzinfo=tz)

    am_hours = time_to_expiry_years(
        expiration_timestamp(friday, "SPX", tz), thursday_1545
    ) * 365 * 24
    pm_hours = time_to_expiry_years(
        expiration_timestamp(friday, "SPXW", tz), thursday_1545
    ) * 365 * 24

    assert am_hours == pytest.approx(17.75, abs=0.01)
    assert pm_hours == pytest.approx(24.25, abs=0.01)
    # ~27% error in T -> ~17% in gamma, since gamma ~ 1/sqrt(T)
    assert (1 - (am_hours / pm_hours) ** 0.5) == pytest.approx(0.144, abs=0.01)


def test_expiry_buckets_and_month_boundary():
    assert expiry_bucket(0) == "0DTE"
    assert expiry_bucket(7) == "1-7DTE"
    assert expiry_bucket(8) == "8-30DTE"
    assert expiry_bucket(31) is None       # captured, not a reported layer
    assert expiry_bucket(-1) is None
    # §16.8 — buckets stay correct across a month change
    assert dte_calendar(pd.Timestamp("2026-09-02").date(),
                        pd.Timestamp("2026-08-28").date()) == 5


# --- §6 normalization ---------------------------------------------------------


def test_normalization_separates_definitional_from_quality_drops(config, clean_fixture_day):
    """Zero-OI contracts contribute exactly 0 to GEX, so dropping them is a
    definitional narrowing. Counting them against the 5% severe threshold would
    invalidate every real day."""
    provider = get_provider(config, "synthetic")
    capture(config, provider, FIXTURE_DATE, label="0945", kind="surface")
    frame, quality = normalize_capture(config, FIXTURE_DATE, "0945")

    assert quality["definitional_drops"]["zero_open_interest"] > 0
    assert quality["quality_drops_total"] == 0
    assert quality["quality_drop_pct"] == 0.0
    assert quality["passed"] is True
    # the definitional drops are excluded from the denominator, not hidden
    assert (
        quality["expected_contracts"]
        == quality["raw_rows"] - quality["definitional_drops_total"]
    )
    assert (frame["open_interest"] > 0).all()


def test_normalization_stamps_provenance_on_every_row(config, clean_fixture_day):
    frame = _normalized(config, clean_fixture_day)
    for column in (
        "asof_utc",
        "asof_source_level",
        "asof_is_inferred",
        "time_convention",
        "sign_convention",
        "gex_definition_version",
        "settlement_type",
    ):
        assert column in frame.columns
        assert frame[column].notna().all()


# --- §7 sample ----------------------------------------------------------------


def test_moneyness_class_is_type_aware():
    assert moneyness_class("call", 7000, 7400) == "ITM"
    assert moneyness_class("put", 7000, 7400) == "OTM"
    assert moneyness_class("call", 7800, 7400) == "OTM"
    assert moneyness_class("put", 7800, 7400) == "ITM"
    assert moneyness_class("call", 7400, 7400) == "ATM"


def test_sample_meets_the_spec_shape(config, clean_fixture_day):
    frame = _normalized(config, clean_fixture_day)
    sample, coverage = build_sample(frame, target_size=24, min_am_settled=2)

    assert coverage["size"] == 24
    assert coverage["meets_am_settled_rule"]           # §16.7
    assert set(coverage["by_type"]) == {"call", "put"}
    assert set(coverage["by_bucket"]) <= {"0DTE", "1-7DTE", "8-30DTE"}
    assert set(coverage["by_moneyness"]) == {"ITM", "ATM", "OTM"}


def test_sample_is_deterministic(config, clean_fixture_day):
    """§18: re-running any analysis on stored data must be byte-identical."""
    frame = _normalized(config, clean_fixture_day)
    first, _ = build_sample(frame)
    second, _ = build_sample(frame)
    assert first["symbol"].tolist() == second["symbol"].tolist()


# --- §7.1 Path A --------------------------------------------------------------


def test_path_a_passes_at_the_true_convention(config, clean_fixture_day):
    frame = _normalized(config, clean_fixture_day)
    sample, _ = build_sample(frame)
    result = path_a(sample, r=TRUE_R, q=TRUE_Q)
    assert result.passed
    assert result.median_abs_pct_error < 0.01     # far under the 1% gate


def test_path_a_fails_on_a_wrong_convention(config, clean_fixture_day):
    """The gate has to be able to fail, or it is decoration."""
    frame = _normalized(config, clean_fixture_day)
    sample, _ = build_sample(frame)
    result = path_a(sample, r=0.30, q=0.0)
    assert not result.passed


def test_a_wrong_convention_leaves_a_systematic_tilt(config, clean_fixture_day):
    """§7.2: an unexplained tilt by moneyness is a stop condition even when the
    median passes. Check the tilt detector responds to a real distortion."""
    frame = _normalized(config, clean_fixture_day)
    sample, _ = build_sample(frame)
    assert not systematic_tilt(path_a(sample, TRUE_R, TRUE_Q).bias_by_moneyness)
    assert systematic_tilt(path_a(sample, 0.30, 0.0).bias_by_moneyness)


def test_drift_scan_recovers_the_providers_convention(config, clean_fixture_day):
    """The scan is told nothing about r or q; it has to find r-q itself."""
    frame = _normalized(config, clean_fixture_day)
    sample, _ = build_sample(frame)
    scan = implied_drift_scan(sample)
    assert scan["implied_r_minus_q"] == pytest.approx(TRUE_DRIFT, abs=0.002)
    assert scan["median_abs_pct_error_at_best"] < 0.05


def test_gate_alone_does_not_prove_the_convention_is_right(config, clean_fixture_day):
    """§7.1's warning, as a test: r=q=0 squeaks under the 1% gate while being
    dozens of times worse than the truth. Passing is not the same as correct,
    which is why the verdict compares against the free scan."""
    frame = _normalized(config, clean_fixture_day)
    sample, _ = build_sample(frame)

    wrong = path_a(sample, r=0.0, q=0.0)
    scan = implied_drift_scan(sample)

    assert wrong.passed                                    # under 1%
    assert wrong.median_abs_pct_error > 20 * scan["median_abs_pct_error_at_best"]


# --- §3.2 put-call IV consistency --------------------------------------------


def test_consistency_clean_when_the_forward_is_self_consistent(config, clean_fixture_day):
    frame = _normalized(config, clean_fixture_day)
    result = put_call_iv_consistency(frame, min_pairs=10)
    assert result["available"]
    for entry in result["by_bucket"].values():
        if entry.get("sufficient"):
            assert entry["intercept_within_tolerance"]
            assert entry["slope_within_tolerance"]


def test_consistency_detects_an_injected_forward_error(config, clean_fixture_day):
    """The detector must recover a misspecification of known size, not merely
    run. 1.3 percentage points of r-q error is injected into the provider's
    reported IVs; the check has to see it and quantify it."""
    bias = 0.013
    cfg = config.override("provider.synthetic.forward_bias_r_minus_q", bias)
    provider = get_provider(cfg, "synthetic")
    capture(cfg, provider, FIXTURE_DATE, label="1400", kind="surface")
    frame, _ = normalize_capture(cfg, FIXTURE_DATE, "1400")

    result = put_call_iv_consistency(frame, min_pairs=10)
    checked = [e for e in result["by_bucket"].values() if e.get("sufficient")]
    assert checked

    # The level, not the slope, is what carries a forward error.
    assert any(not e["intercept_within_tolerance"] for e in checked)

    # And it is quantified: recovered drift error close to what was injected.
    longer_dated = result["by_bucket"].get("8-30DTE")
    assert longer_dated is not None and longer_dated["sufficient"]
    assert longer_dated["forward_error_is_constant"]
    assert longer_dated["implied_r_minus_q_error"] == pytest.approx(bias, rel=0.25)
    assert "misspecified forward" in longer_dated["reading"]


def test_consistency_is_independent_of_path_a(config, clean_fixture_day):
    """The whole reason this check replaces Path B: an IV-level defect that
    Path A cannot see, because Path A feeds that same IV into both sides."""
    cfg = config.override("provider.synthetic.forward_bias_r_minus_q", 0.013)
    provider = get_provider(cfg, "synthetic")
    capture(cfg, provider, FIXTURE_DATE, label="1400", kind="surface")
    frame, _ = normalize_capture(cfg, FIXTURE_DATE, "1400")

    sample, _ = build_sample(frame)

    # The provider's numbers are internally coherent, so Path A reconciles
    # perfectly against the provider's own (wrong) convention and reports nothing
    # amiss. This is the blind spot: Path A can only confirm we reproduced what
    # the provider did, never that what the provider did was right.
    scan = implied_drift_scan(sample)
    reproduced = path_a(sample, r=TRUE_R, q=TRUE_R - scan["implied_r_minus_q"])
    assert reproduced.passed
    assert reproduced.median_abs_pct_error < 0.05

    # And the recovered convention is the biased one, not the true one — Path A
    # would have had us write the provider's error into config as fact.
    assert scan["implied_r_minus_q"] == pytest.approx(TRUE_DRIFT + 0.013, abs=0.003)

    # The put-call check sees the forward error regardless, using no external
    # input and no assumed r or q.
    result = put_call_iv_consistency(frame, min_pairs=10)
    checked = [e for e in result["by_bucket"].values() if e.get("sufficient")]
    assert any(not e["intercept_within_tolerance"] for e in checked)


# --- §5.4 manual spot fallback ------------------------------------------------


def test_spot_falls_back_to_the_manual_transmission_drop(config, clean_fixture_day, tmp_path):
    """If the provider omits underlying_asset.price, §5.4's hand-recorded spot is
    the designated source — a fallback, not a failure. Which source was used is
    stamped on the record, because a spot from a different clock than the
    surface is a caveat worth carrying."""
    import json

    from spx_gex.config import manual_input_dir
    from spx_gex.normalize import normalize_capture

    provider = get_provider(config, "synthetic")
    meta_written = capture(config, provider, FIXTURE_DATE, label="0945", kind="surface")
    raw_dir = repo_path("data", "raw_chain", FIXTURE_DATE)
    raw = pd.read_parquet(raw_dir / "0945.parquet")
    meta = json.loads((raw_dir / "0945.meta.json").read_text())
    true_spot = float(raw["underlying_asset.price"].iloc[0])
    raw["underlying_asset.price"] = None

    transmission = manual_input_dir() / "transmission_spot.csv"
    original = transmission.read_text() if transmission.exists() else None
    try:
        # No hand-recorded row: severe, never a silent zero.
        transmission.write_text("date,time_label,spot,source_timestamp\n")
        _, quality = normalize_capture(config, FIXTURE_DATE, "0945", raw=raw, meta=meta)
        assert quality["passed"] is False
        assert any("spot missing" in flag for flag in quality["severe_flags"])

        # With the row present: usable, and the substitution is on the record.
        transmission.write_text(
            "date,time_label,spot,source_timestamp\n"
            f"{FIXTURE_DATE},0945,{true_spot:.2f},{FIXTURE_DATE}T09:45:03-05:00\n"
        )
        frame, quality = normalize_capture(config, FIXTURE_DATE, "0945", raw=raw, meta=meta)
        assert quality["passed"] is True
        assert quality["spot_source"].startswith("transmission_spot.csv[0945]")
        assert quality["spot_match"]["gap_minutes"] < 1.0
        assert float(frame["underlying_spot"].iloc[0]) == pytest.approx(true_spot, abs=0.01)
    finally:
        if original is not None:
            transmission.write_text(original)


def test_oi_base_does_not_require_a_spot(config, clean_fixture_day):
    """The OI base is captured pre-market for open interest alone. A missing
    spot there is not a severe flag, and there is no 09:45 transmission row to
    fall back to that early anyway."""
    import json

    from spx_gex.normalize import normalize_capture

    provider = get_provider(config, "synthetic")
    capture(config, provider, FIXTURE_DATE, label="oi_base", kind="oi_base")
    raw_dir = repo_path("data", "raw_chain", FIXTURE_DATE)
    raw = pd.read_parquet(raw_dir / "oi_base.parquet")
    meta = json.loads((raw_dir / "oi_base.meta.json").read_text())
    raw["underlying_asset.price"] = None

    _, quality = normalize_capture(config, FIXTURE_DATE, "oi_base", raw=raw, meta=meta)
    assert quality["severe_flags"] == []
    assert quality["passed"] is True


def test_spot_is_matched_to_the_asof_not_the_label(config, clean_fixture_day):
    """A download that slips carries a different instant than it aimed at. The
    09:45 capture on the first live day resolved to 09:52 while spot moved 22
    points in 15 minutes; pairing it with the 09:45 price would have put a
    surface and a spot seven minutes apart into the same gamma."""
    import json
    from datetime import timedelta

    from spx_gex.config import manual_input_dir
    from spx_gex.normalize import normalize_capture

    provider = get_provider(config, "synthetic")
    capture(config, provider, FIXTURE_DATE, label="0945", kind="surface")
    raw_dir = repo_path("data", "raw_chain", FIXTURE_DATE)
    raw = pd.read_parquet(raw_dir / "0945.parquet")
    meta = json.loads((raw_dir / "0945.meta.json").read_text())
    raw["underlying_asset.price"] = None

    # Pretend the download slipped: the surface actually carries 09:52.
    from datetime import datetime

    asof = datetime.fromisoformat(meta["asof_utc"]) + timedelta(minutes=7)
    meta["asof_utc"] = asof.isoformat()

    transmission = manual_input_dir() / "transmission_spot.csv"
    original = transmission.read_text() if transmission.exists() else None
    try:
        transmission.write_text(
            "date,time_label,spot,source_timestamp\n"
            f"{FIXTURE_DATE},0945,7462.42,{FIXTURE_DATE}T09:45:00-05:00\n"
            f"{FIXTURE_DATE},0952,7462.03,{FIXTURE_DATE}T09:52:00-05:00\n"
            f"{FIXTURE_DATE},1000,7442.85,{FIXTURE_DATE}T10:00:00-05:00\n"
        )
        frame, quality = normalize_capture(config, FIXTURE_DATE, "0945", raw=raw, meta=meta)
        # The label says 0945; the instant says 0952. The instant wins.
        assert quality["spot_match"]["time_label"] == "0952"
        assert float(frame["underlying_spot"].iloc[0]) == pytest.approx(7462.03)

        # Too far from any recorded instant is a severe flag, not a guess.
        transmission.write_text(
            "date,time_label,spot,source_timestamp\n"
            f"{FIXTURE_DATE},1400,7399.96,{FIXTURE_DATE}T14:00:00-05:00\n"
        )
        _, quality = normalize_capture(config, FIXTURE_DATE, "0945", raw=raw, meta=meta)
        assert quality["passed"] is False
        assert quality["spot_match"]["too_far"] is True
        assert "too far to use" in " ".join(quality["severe_flags"])
    finally:
        if original is not None:
            transmission.write_text(original)
