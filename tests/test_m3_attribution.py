"""M3 tests (spec §12) — actual-surface refresh and the 2×2 attribution."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spx_gex.actual_map import build_actual_map  # noqa: E402
from spx_gex.attribution import (  # noqa: E402
    METRICS,
    attribute,
    build_attribution,
    build_common_inputs,
)
from spx_gex.capture import capture  # noqa: E402
from spx_gex.config import Config, repo_path  # noqa: E402
from spx_gex.normalize import normalize_capture, write_normalized  # noqa: E402
from spx_gex.providers import get_provider  # noqa: E402

FIXTURE_DATE = "1999-01-04"
SURFACES = ("0945", "1030", "1400", "1545")


@pytest.fixture
def config() -> Config:
    return Config.load()


@pytest.fixture
def fixture_day(config: Config):
    """A synthetic day carrying an OI base and three surfaces."""
    raw = repo_path("data", "raw_chain", FIXTURE_DATE)
    norm = repo_path("data", "normalized", FIXTURE_DATE)
    derived = repo_path("data", "derived", FIXTURE_DATE)
    for path in (raw, norm, derived):
        shutil.rmtree(path, ignore_errors=True)

    transmission = repo_path("data", "manual_input", "transmission_spot.csv")
    original = transmission.read_text() if transmission.exists() else None
    provider = get_provider(config, "synthetic")
    try:
        rows = ["date,time_label,spot,source_timestamp"]
        # Spot must move, or the mechanical component is trivially zero.
        for label, spot in (("0945", 7400.00), ("1030", 7408.00), ("1130", 7415.00),
                            ("1400", 7430.00), ("1545", 7425.00), ("1600", 7422.00)):
            rows.append(
                f"{FIXTURE_DATE},{label},{spot:.2f},"
                f"{FIXTURE_DATE}T{label[:2]}:{label[2:]}:00-05:00"
            )
        transmission.write_text("\n".join(rows) + "\n")

        capture(config, provider, FIXTURE_DATE, label="oi_base", kind="oi_base")
        for label in SURFACES:
            capture(config, provider, FIXTURE_DATE, label=label, kind="surface")
        for label in ("oi_base", *SURFACES):
            frame, quality = normalize_capture(config, FIXTURE_DATE, label)
            write_normalized(frame, quality, FIXTURE_DATE, label)
        yield config
    finally:
        if original is not None:
            transmission.write_text(original)
        for path in (raw, norm, derived):
            shutil.rmtree(path, ignore_errors=True)


# --- §12.1 arithmetic ---------------------------------------------------------


def test_identity_holds_for_arbitrary_states():
    """mechanical_contribution + surface_contribution == total_change."""
    for a, b, c, d in [(1.0, 2.0, 3.0, 4.0), (-5.0, 12.5, -0.25, 7.0),
                       (0.0, 0.0, 0.0, 0.0), (1e9, -3e9, 2.5e9, -7e9)]:
        parts = attribute(a, b, c, d)
        assert parts["mechanical_contribution"] + parts["surface_contribution"] == (
            pytest.approx(parts["total_change"], rel=1e-12, abs=1e-9)
        )


def test_unchanged_surface_attributes_everything_to_mechanical():
    """If the surface never moved, C == A and D == B, so surface is exactly 0."""
    a, b = 10.0, 25.0
    parts = attribute(a, b, c=a, d=b)
    assert parts["surface_contribution"] == 0.0
    assert parts["surface_component"] == 0.0
    assert parts["mechanical_contribution"] == pytest.approx(parts["total_change"])


def test_unchanged_spot_and_time_attributes_everything_to_surface():
    """The mirror case: B == A and D == C."""
    a, c = 10.0, 4.0
    parts = attribute(a, b=a, c=c, d=c)
    assert parts["mechanical_contribution"] == 0.0
    assert parts["mechanical_component"] == 0.0
    assert parts["surface_contribution"] == pytest.approx(parts["total_change"])


def test_shapley_is_order_independent():
    """The path-dependent pair depends on ordering; the Shapley pair does not.

    Swapping which factor moves first swaps `mechanical_component` and
    `surface_component` but must leave the contributions unchanged.
    """
    a, b, c, d = 3.0, 11.0, -2.0, 6.0
    forward = attribute(a, b, c, d)
    # same four states, factors relabelled: B and C swap roles
    reverse = attribute(a, c, b, d)
    assert forward["mechanical_contribution"] == pytest.approx(
        reverse["surface_contribution"]
    )
    assert forward["surface_contribution"] == pytest.approx(
        reverse["mechanical_contribution"]
    )


# --- §12 on real machinery ----------------------------------------------------


def test_actual_map_uses_each_captures_own_surface(fixture_day):
    table, meta = build_actual_map(fixture_day, FIXTURE_DATE)
    assert not table.empty
    assert set(table["surface_source"]) == set(SURFACES)
    assert set(table["map_kind"]) == {"actual"}
    assert meta["complete"]


def test_attribution_identity_gate_passes(fixture_day):
    table, meta = build_attribution(fixture_day, FIXTURE_DATE, "0945", "1400")
    assert not table.empty
    assert meta["identity_gate_passed"], meta["identity_failures"]
    assert (table["identity_residual"].abs() < 1e-6).all()


def test_every_metric_is_either_attributed_or_recorded_as_skipped(fixture_day):
    """A metric undefined in one of the four states (a flip with no root) is
    skipped — but never silently. Every METRIC is accounted for either way."""
    table, meta = build_attribution(fixture_day, FIXTURE_DATE, "0945", "1400")
    for bucket in table["expiry_bucket"].unique():
        present = set(table[table.expiry_bucket == bucket]["metric"])
        noted = {s.split("/")[1].split(":")[0] for s in meta["metrics_skipped"]
                 if s.startswith(f"{bucket}/")}
        assert present | noted == set(METRICS), f"{bucket}: {present} | {noted}"


def test_contract_set_is_identical_across_the_four_states(fixture_day):
    """§12 fixes OI and membership; a moving contract set would enter D - A."""
    f0, f1, coverage = build_common_inputs(fixture_day, FIXTURE_DATE, "0945", "1400")
    assert len(f0) == len(f1) == coverage["common_with_oi_base"]
    keys = ["root", "expiration_date", "strike", "option_type"]
    assert f0[keys].equals(f1[keys])
    # and the two frames differ only in the IV column
    assert "massive_iv" in f0.columns and "massive_iv" in f1.columns
    table, _ = build_attribution(fixture_day, FIXTURE_DATE, "0945", "1400")
    assert table["n_contracts"].nunique() == 1


def test_one_bucket_policy_across_states(fixture_day):
    """t1's §9.1 policy is applied to all four, or the identity fails on
    membership rather than on anything being attributed."""
    table, meta = build_attribution(fixture_day, FIXTURE_DATE, "0945", "1545")
    assert meta["bucket_policy_label"] == "1545"
    assert set(table["bucket_policy_label"]) == {"1545"}
    assert meta["identity_gate_passed"], meta["identity_failures"]


def test_iv_alignment_stamped_on_every_row(fixture_day):
    """§16 checklist item 14."""
    table, _ = build_attribution(fixture_day, FIXTURE_DATE, "0945", "1400")
    assert (table["iv_alignment"] == "sticky_strike").all()
    assert table["attribution_is_counterfactual"].all()


def test_membership_effect_is_reported_not_corrected(fixture_day):
    """The cost of fixing the contract set is measured beside the number."""
    table, _ = build_attribution(fixture_day, FIXTURE_DATE, "0945", "1400")
    assert "membership_effect" in table.columns
    assert "state_D_unconstrained" in table.columns
    # state_D itself must come from the fixed set, never from t1's own rows
    assert not table["state_D"].equals(table["state_D_unconstrained"]) or (
        table["membership_effect"].abs().max() == 0
    )


def test_missing_transmission_spot_is_refused_not_guessed(fixture_day):
    from spx_gex.frozen_map import FrozenMapError

    with pytest.raises(FrozenMapError, match="no transmission spot"):
        build_attribution(fixture_day, FIXTURE_DATE, "0945", "1234")
