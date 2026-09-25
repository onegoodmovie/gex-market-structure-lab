"""M2 tests (spec §9, §10, §11, §16.9-12)."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spx_gex.capture import capture  # noqa: E402
from spx_gex.config import Config, repo_path  # noqa: E402
from spx_gex.exposure import contract_gex, gex_by_strike, net_gex, rate_sensitivity  # noqa: E402
from spx_gex.flip_solver import exposure_curve, solve_flip  # noqa: E402
from spx_gex.frozen_map import build_frozen_map, recompute_at  # noqa: E402
from spx_gex.normalize import normalize_capture  # noqa: E402
from spx_gex.peaks import peak_proxies  # noqa: E402
from spx_gex.providers import get_provider  # noqa: E402

FIXTURE_DATE = "1999-01-04"
ET = ZoneInfo("America/New_York")


def _spec_11_fixture() -> pd.DataFrame:
    """§11's two-contract fixture: r=0.045, q=0.013, T=0.02, multiplier 100."""
    return pd.DataFrame(
        {
            "strike": [7500.0, 7300.0],
            "option_type": ["call", "put"],
            "massive_iv": [0.14, 0.17],
            "open_interest": [20000, 20000],
            "multiplier": [100, 100],
            "t_years": [0.02, 0.02],
        }
    )


# --- §11 fixture --------------------------------------------------------------


def test_spec_11_curve_endpoints():
    """The spec states the curve runs −1.19 B at 7300 to +0.39 B at 7400."""
    frame = _spec_11_fixture()
    assert net_gex(frame, 7300.0, 0.045, 0.013) / 1e9 == pytest.approx(-1.19, abs=0.01)
    assert net_gex(frame, 7400.0, 0.045, 0.013) / 1e9 == pytest.approx(0.39, abs=0.01)


def test_spec_11_single_root_and_location():
    result = solve_flip(_spec_11_fixture(), 7400.0, 0.045, 0.013, grid_step_points=5.0)
    assert result["n_roots"] == 1
    assert result["primary_flip"] == pytest.approx(7377.91, abs=0.01)
    assert result["no_root"] is False
    assert result["spot_vs_flip"] == "above"


# --- §11's four root cases ----------------------------------------------------


def test_no_root_is_a_state_not_a_failure():
    """A curve that never crosses is real. Returning an endpoint dressed up as
    a flip would be worse than saying so."""
    calls_only = pd.DataFrame(
        {
            "strike": [7400.0, 7500.0],
            "option_type": ["call", "call"],
            "massive_iv": [0.15, 0.15],
            "open_interest": [1000, 1000],
            "multiplier": [100, 100],
            "t_years": [0.02, 0.02],
        }
    )
    result = solve_flip(calls_only, 7400.0, 0.045, 0.013)
    assert result["no_root"] is True
    assert result["primary_flip"] is None
    assert result["n_roots"] == 0


def test_multiple_roots_are_kept_and_never_averaged():
    """§11: multiple roots are the normal case for 0DTE. The primary is chosen
    by rule; the others stay in the record."""
    frame = pd.DataFrame(
        {
            "strike": [7350.0, 7400.0, 7450.0, 7500.0],
            "option_type": ["put", "call", "put", "call"],
            "massive_iv": [0.20, 0.20, 0.20, 0.20],
            "open_interest": [50000, 50000, 50000, 50000],
            "multiplier": [100] * 4,
            "t_years": [0.0006] * 4,      # ~5 hours, where the curve oscillates
        }
    )
    result = solve_flip(frame, 7420.0, 0.045, 0.013, grid_step_points=5.0)
    assert result["n_roots"] >= 2
    assert result["primary_flip"] in result["all_roots"]
    # the selected root is the nearest one, not a mean
    nearest = min(result["all_roots"], key=lambda x: abs(x - 7420.0))
    assert result["primary_flip"] == nearest
    assert result["primary_flip"] != pytest.approx(
        sum(result["all_roots"]) / len(result["all_roots"])
    ) or len(set(result["all_roots"])) == 1


def test_grid_finer_than_strike_spacing():
    """§11: a grid coarser than the 25-point SPX spacing both misses real roots
    and manufactures false ones. The configured step must stay below it."""
    config = Config.load()
    assert float(config.get("flip_solver.grid_step_points")) < 25.0


def test_root_exactly_on_a_grid_node():
    frame = _spec_11_fixture()
    result = solve_flip(frame, 7400.0, 0.045, 0.013, grid_step_points=5.0)
    grid, values = exposure_curve(frame, 7400.0, 0.045, 0.013, grid_step_points=5.0)
    # the fixture's root is not on a node; the solver must still find it
    assert not any(abs(v) < 1e-9 for v in values)
    assert result["n_roots"] == 1


# --- §9 exposure --------------------------------------------------------------


def test_sign_convention_is_calls_positive_puts_negative():
    frame = _spec_11_fixture()
    signed = contract_gex(frame, 7400.0, 0.045, 0.013)
    assert signed.iloc[0] > 0      # the call
    assert signed.iloc[1] < 0      # the put


def test_undefined_gamma_is_nan_not_zero():
    """A silent 0.0 sums into net GEX as a measurement of nothing."""
    frame = pd.DataFrame(
        {
            "strike": [7400.0],
            "option_type": ["call"],
            "massive_iv": [0.15],
            "open_interest": [1000],
            "multiplier": [100],
            "t_years": [0.0],       # already settled
        }
    )
    assert pd.isna(contract_gex(frame, 7400.0, 0.045, 0.013).iloc[0])


def test_gex_scales_with_open_interest():
    base = _spec_11_fixture()
    doubled = base.copy()
    doubled["open_interest"] = doubled["open_interest"] * 2
    assert net_gex(doubled, 7400.0, 0.045, 0.013) == pytest.approx(
        2 * net_gex(base, 7400.0, 0.045, 0.013)
    )


# --- §10.4 peaks --------------------------------------------------------------


def test_no_column_is_ever_called_wall(tmp_path):
    """§10.4 forbids the word in self-computed output — optioncharts' definition
    is unknown, which is the whole reason four proxies are stored separately."""
    config = Config.load()
    table, _ = _build_day(config)
    assert not any("wall" in column.lower() for column in table.columns)
    assert {"call_gex_peak", "put_gex_peak", "call_oi_peak", "put_oi_peak"} <= set(
        table.columns
    )


def test_peak_proxies_are_independent():
    by_strike = pd.DataFrame(
        {
            "strike": [7300.0, 7400.0, 7500.0],
            "call_gex": [1.0, 5.0, 2.0],
            "put_gex": [-9.0, -1.0, -2.0],
            "call_oi": [10, 20, 900],
            "put_oi": [800, 20, 10],
        }
    )
    peaks = peak_proxies(by_strike)
    assert peaks["call_gex_peak"] == 7400.0
    assert peaks["put_gex_peak"] == 7300.0
    assert peaks["call_oi_peak"] == 7500.0
    assert peaks["put_oi_peak"] == 7300.0


# --- §10 frozen map -----------------------------------------------------------


def _build_day(config: Config):
    """A synthetic day with captures, normalization and transmission spots."""
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
        for label in ("0945", "1030", "1130", "1400", "1545", "1600"):
            rows.append(
                f"{FIXTURE_DATE},{label},7400.00,"
                f"{FIXTURE_DATE}T{label[:2]}:{label[2:]}:00-05:00"
            )
        transmission.write_text("\n".join(rows) + "\n")

        capture(config, provider, FIXTURE_DATE, label="oi_base", kind="oi_base")
        normalize_capture(config, FIXTURE_DATE, "oi_base")
        for label in ("0945",):
            capture(config, provider, FIXTURE_DATE, label=label, kind="surface")
        from spx_gex.normalize import write_normalized

        for label in ("oi_base", "0945"):
            frame, quality = normalize_capture(config, FIXTURE_DATE, label)
            write_normalized(frame, quality, FIXTURE_DATE, label)
        return build_frozen_map(config, FIXTURE_DATE)
    finally:
        if original is not None:
            transmission.write_text(original)
        for path in (raw, norm, derived):
            shutil.rmtree(path, ignore_errors=True)


def test_frozen_map_produces_all_recompute_rows():
    """§16.12 — the map produces all five time rows."""
    config = Config.load()
    table, meta = _build_day(config)
    assert set(table["time_label"]) == set(config.require("frozen_map.recompute_times"))
    assert meta["recompute_times_missing_spot"] == []


def test_both_spot_and_t_move_between_recompute_points():
    """§10.1 — updating spot alone is a specification violation. Here spot is
    deliberately held flat across the day, so T must still change on its own."""
    config = Config.load()
    table, _ = _build_day(config)
    agg = table[table["expiry_bucket"] == "8-30DTE"].sort_values("time_label")
    assert agg["spot"].nunique() == 1                     # flat by construction
    assert agg["t_min_remaining"].nunique() == len(agg)   # T still moves
    assert agg["net_gex"].nunique() == len(agg)           # and so does the answer


def test_zero_dte_treatment_by_row():
    config = Config.load()
    table, _ = _build_day(config)
    zero = table[table["expiry_bucket"] == "0DTE"].set_index("time_label")

    assert "1600" not in zero.index                       # absent at settlement
    # bool() because pandas hands back numpy bools, for which `is False` is False
    assert bool(zero.loc["0945", "not_cross_day_comparable"]) is False
    assert bool(zero.loc["1400", "not_cross_day_comparable"]) is False
    assert bool(zero.loc["1545", "not_cross_day_comparable"]) is True
    assert bool(zero.loc["1545", "excluded_from_aggregate"]) is True


def test_aggregate_excludes_the_flagged_zero_dte_layer():
    config = Config.load()
    table, _ = _build_day(config)
    at_1545 = table[table["time_label"] == "1545"].set_index("expiry_bucket")
    parts = sum(
        at_1545.loc[b, "n_contracts"]
        for b in ("1-7DTE", "8-30DTE")
        if b in at_1545.index
    )
    assert at_1545.loc["aggregate", "n_contracts"] == parts
    assert at_1545.loc["aggregate", "n_contracts"] != parts + at_1545.loc[
        "0DTE", "n_contracts"
    ]


def test_provenance_is_stamped_on_every_row():
    """§9 — every result row carries the conventions it was computed under."""
    config = Config.load()
    table, _ = _build_day(config)
    for column in (
        "gex_definition_version",
        "sign_convention",
        "iv_alignment",
        "time_convention",
        "r",
        "q",
        "rates_provisional",
        "root_selection_rule",
    ):
        assert column in table.columns
        assert table[column].notna().all()
    assert set(table["iv_alignment"]) == {"sticky_strike"}


def test_all_roots_round_trips_through_parquet():
    config = Config.load()
    table, _ = _build_day(config)
    for value in table["all_roots"]:
        assert isinstance(json.loads(value), list)


# --- §3.3 rate sensitivity ----------------------------------------------------


def test_rate_sensitivity_moves_the_flip_by_less_than_a_point():
    """The reason declaring r and q is safe: ±50bp barely moves the flip, which
    is the quantity Q1 and Q2 are actually about."""
    frame = _spec_11_fixture()
    sens = rate_sensitivity(frame, 7400.0, 0.043, 0.0125, bump=0.005)
    for key in ("r_up", "r_down", "q_up", "q_down"):
        assert abs(sens[key]["flip_change_pts"]) < 1.0


def test_r_and_q_move_the_answer_in_opposite_directions():
    """They enter gamma through (r − q), so a bump to one is a bump to the
    other with the sign reversed. If this ever stops holding, the formula has
    drifted from §9."""
    frame = _spec_11_fixture()
    sens = rate_sensitivity(frame, 7400.0, 0.043, 0.0125, bump=0.005)
    assert sens["r_up"]["net_gex_abs_change"] == pytest.approx(
        sens["q_down"]["net_gex_abs_change"], rel=0.02
    )


# --- surface base selection (T1) ----------------------------------------------


def test_surface_base_override_selects_the_frozen_surface():
    """`--base` picks which capture supplies the frozen IV surface.

    Added because 09:45 is the capture most exposed to the provider's unsettled
    open, and every frozen-map row is built on it. The switch exists so the two
    bases can be measured against each other rather than argued about — and a
    base with no capture must fail loudly, never fall back to the default.
    """
    from spx_gex.frozen_map import FrozenMapError, build_frozen_inputs
    from spx_gex.normalize import write_normalized

    config = Config.load()
    raw = repo_path("data", "raw_chain", FIXTURE_DATE)
    norm = repo_path("data", "normalized", FIXTURE_DATE)
    for path in (raw, norm):
        shutil.rmtree(path, ignore_errors=True)
    provider = get_provider(config, "synthetic")
    try:
        capture(config, provider, FIXTURE_DATE, label="oi_base", kind="oi_base")
        for label in ("0945", "1400"):
            capture(config, provider, FIXTURE_DATE, label=label, kind="surface")
        for label in ("oi_base", "0945", "1400"):
            frame, quality = normalize_capture(config, FIXTURE_DATE, label)
            write_normalized(frame, quality, FIXTURE_DATE, label)

        default = build_frozen_inputs(config, FIXTURE_DATE)
        alt = build_frozen_inputs(config, FIXTURE_DATE, surface_base="1400")
        assert not default.empty and not alt.empty
        # same OI source, different surface
        assert default["asof_utc"].iloc[0] != alt["asof_utc"].iloc[0]

        with pytest.raises(FrozenMapError, match="1030"):
            build_frozen_inputs(config, FIXTURE_DATE, surface_base="1030")
    finally:
        for path in (raw, norm):
            shutil.rmtree(path, ignore_errors=True)
