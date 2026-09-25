#!/usr/bin/env python3
"""Greek validation — the M1 gate (spec §7).

    python3 scripts/validate_greeks.py --date 2026-07-27 --time 0945

Runs, in order:

  1. §7.3 fixture check      our Black-Scholes against five deterministic values
  2. §7   sample build       ~24 contracts, ITM/ATM/OTM x call/put x 3 buckets,
                             at least two AM-settled SPX
  3. §7.1 Path A             self-consistency of our BS  [DIAGNOSTIC, not a gate]
  4. §7.2 declared rates and their sensitivity
  5. §3.2 put-call IV        the cross-check that replaces Path B

Path B is N/A on a quotes-free tier. That is recorded, not scored as a failure.

Exit codes: 0 Path A passed, 1 Path A failed or a stop condition fired,
2 the validation could not run at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from spx_gex.config import Config, ConfigError, repo_path  # noqa: E402
from spx_gex.greeks import gamma  # noqa: E402
from spx_gex.validation import (  # noqa: E402
    ValidationError,
    build_sample,
    implied_drift_scan,
    fit_massive_conventions,
    massive_implied_t,
    path_a,
    path_b_not_applicable,
    put_call_iv_consistency,
    systematic_tilt,
)

# §7.3 — deterministic, q-adjusted, matched to 1e-9
FIXTURES = [
    (100.0, 100.0, 0.00, 0.000, 0.20, 1.0, 0.0198476274),
    (100.0, 110.0, 0.00, 0.000, 0.20, 1.0, 0.0185819222),
    (7400.0, 7400.0, 0.045, 0.013, 0.15, 0.25, 0.0007090754),
    (7400.0, 7400.0, 0.045, 0.013, 0.15, 1.0 / 365.0, 0.0068654434),
    (7400.0, 7000.0, 0.045, 0.013, 0.18, 0.5, 0.0003459537),
]


def check_fixtures() -> tuple[bool, list[str]]:
    lines, ok = [], True
    for S, K, r, q, sigma, T, expected in FIXTURES:
        got = gamma(S, K, r, q, sigma, T)
        diff = abs(got - expected)
        passed = diff < 1e-9
        ok = ok and passed
        lines.append(
            f"  [{'OK ' if passed else 'FAIL'}] S={S:<7g} K={K:<7g} T={T:<12.8f} "
            f"got={got:.12f} spec={expected:.10f} diff={diff:.2e}"
        )
    return ok, lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--time", default="0945", help="capture label to validate")
    ap.add_argument("--config", help="path to experiment.yaml")
    ap.add_argument("--json-out", help="also write the full record here")
    args = ap.parse_args()

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    print("=" * 74)
    print(f"Greek validation — {args.date} {args.time}")
    print("=" * 74)

    # --- 1. fixtures ---------------------------------------------------------
    print("\n1. §7.3 fixture check (our Black-Scholes, matched to 1e-9)")
    fixtures_ok, fixture_lines = check_fixtures()
    print("\n".join(fixture_lines))
    if not fixtures_ok:
        print("\nSTOP: our own formula does not reproduce the spec fixtures. "
              "Nothing downstream is meaningful until this passes.")
        return 1

    # --- 2. sample -----------------------------------------------------------
    normalized = repo_path("data", "normalized", args.date, f"{args.time}.parquet")
    if not normalized.exists():
        print(
            f"\nFAILED: {normalized} not found. Run normalize_chain.py first.",
            file=sys.stderr,
        )
        return 2
    frame = pd.read_parquet(normalized)

    try:
        sample, coverage = build_sample(
            frame,
            target_size=int(config.get("validation.sample_size", 24)),
            min_am_settled=int(config.get("validation.min_am_settled", 2)),
        )
    except ValidationError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 2

    print(f"\n2. §7 validation sample — {coverage['size']} contracts")
    print(f"   by bucket    : {coverage['by_bucket']}")
    print(f"   by moneyness : {coverage['by_moneyness']}")
    print(f"   by type      : {coverage['by_type']}")
    print(
        f"   AM-settled   : {coverage['am_settled_in_sample']} "
        f"({'meets §16.7' if coverage['meets_am_settled_rule'] else 'BELOW THE RULE'})"
    )
    if coverage["empty_cells"]:
        print(f"   !! empty cells: {coverage['empty_cells']}")
        print("      (deep ITM often has no Massive greeks — expected, but it "
              "shapes what the sample can test)")

    # --- 3. Path A -----------------------------------------------------------
    gate = float(config.get("validation.path_a_max_median_abs_pct_error", 1.0))
    r_cfg, q_cfg = config.get("rates.r", None), config.get("rates.q", None)

    print("\n3. §7.1 Path A — is OUR Black-Scholes self-consistent?  [DIAGNOSTIC]")
    print("   Not a gate, and it cannot reach 1%: Massive prices on a CRR binomial")
    print("   tree with finite-difference vega, so this is a cross-model comparison")
    print("   with an irreducible residual (METHODOLOGY §3.4).")
    primary = path_a(sample, float(r_cfg), float(q_cfg), gate)
    _print_path_a(primary, gate)
    print(f"   {'bucket':>10} {'n':>4} {'median%':>9} {'p90%':>9} {'signed%':>9}")
    for bucket, row in sorted(primary.by_bucket.items()):
        print(f"   {bucket:>10} {row['n']:>4} {row['median_abs_pct_error']:>9.3f} "
              f"{row['p90_abs_pct_error']:>9.3f} {row['median_signed_pct_error']:>+9.3f}")

    # --- 4. §7.2 -------------------------------------------------------------
    # The candidate-rate grid search is RETIRED. The study declares r and q and
    # reports their sensitivity instead of solving for the Massive's.
    print("\n4. §7.2 — rates are declared, not solved for")
    print(f"   r = {config.require('rates.r')} ({config.get('rates.r_source', '?')}), "
          f"q = {config.require('rates.q')} ({config.get('rates.q_source', '?')}), "
          f"{config.get('rates.compounding', '?')}")
    if config.get("rates.provisional", False):
        print("   !! PROVISIONAL — replace with the day's actual quotes; every "
              "output row\n      carries rates_provisional until you do")
    scan = implied_drift_scan(sample)
    declared = float(config.require("rates.r")) - float(config.require("rates.q"))
    print(f"   diagnostic: the r-q that would best fit Massive is "
          f"{scan['implied_r_minus_q']:+.4f} vs our declared {declared:+.4f}"
          " — reported, not adopted")

    # --- 4b. §7.2 Massive spot and expiry conventions -------------------------
    offset_cfg = config.get("validation.massive_expiry_offset_hours", None)
    implied_t = massive_implied_t(frame)
    print("\n4b. §7.2 Massive time-to-expiry — from their own vega/gamma = S^2*sigma*T")
    if not implied_t["available"]:
        print(f"   unavailable: {implied_t['reason']}")
    else:
        print(f"   {'expiry':>12} {'our T(h)':>10} {'Massive T(h)':>12} {'dT(h)':>8}")
        for row in implied_t["by_expiry"][:6]:
            print(f"   {row['expiration_date']:>12} {row['our_t_hours']:>10.2f} "
                  f"{row['massive_t_hours']:>12.2f} {row['delta_t_hours']:>+8.2f}")
        print(f"\n   median dT : {implied_t['median_delta_t_hours']:+.2f} h  "
              f"range {implied_t['delta_t_range_hours']}")
        print(f"   -> {implied_t['reading']}")
        print("   (gamma alone cannot separate sigma from T; vega can. A gamma-only "
              "fit on\n    this data produced a spurious tenor trend.)")

    fit = fit_massive_conventions(frame)
    print("\n4c. cross-check — joint (S, T) fit to the gamma curve")
    if not fit["available"]:
        print(f"   unavailable: {fit['reason']}")
    else:
        print(f"   {'expiry':>12} {'n':>4} {'our S':>9} {'fitted S':>9} {'dS':>7} "
              f"{'our T(h)':>9} {'fit T(h)':>9} {'dT(h)':>7} {'rms%':>6}")
        for row in fit["fits"]:
            print(f"   {row['expiration_date']:>12} {row['n_strikes']:>4} "
                  f"{row['our_spot']:>9.2f} {row['fitted_spot']:>9.2f} "
                  f"{row['delta_spot_pts']:>+7.2f} {row['our_t_hours']:>9.2f} "
                  f"{row['fitted_t_hours']:>9.2f} {row['delta_t_hours']:>+7.2f} "
                  f"{row['rms_pct']:>6.2f}")
        print(f"\n   median dT   : {fit['median_delta_t_hours']:+.2f} h "
              f"(spread {fit['delta_t_spread_hours']:.2f} h across expiries)")
        print(f"   median dS   : {fit['median_delta_spot_pts']:+.2f} pts — "
              "a level, not a lag; a delayed spot would\n"
              "                 change sign with the market and this does not")
        print(f"   median rms  : {fit['median_rms_pct']:.2f}%  — once their S and T "
              "are used, our formula\n                 reproduces their gamma curve "
              "to about this much")
        print(f"   config value: {offset_cfg if offset_cfg is not None else 'null (unresolved)'}")
        if offset_cfg is None:
            print("   Leave it null until dT is stable across several days. A value "
                  "fitted to one\n   day is the §19.4 error of picking the closest fit.")

    # --- 5. §3.2 put-call IV consistency -------------------------------------
    print("\n5. §3.2 put-call IV consistency (replaces Path B)")
    consistency = put_call_iv_consistency(
        frame,
        min_pairs=int(config.get("validation.put_call_iv_consistency.min_pairs_per_bucket", 20)),
        max_abs_intercept=float(
            config.get("validation.put_call_iv_consistency.max_abs_intercept", 0.005)
        ),
        max_abs_slope=float(
            config.get("validation.put_call_iv_consistency.max_abs_slope", 0.02)
        ),
        min_vega_fraction=float(
            config.get("validation.put_call_iv_consistency.min_vega_fraction", 0.05)
        ),
    )
    if not consistency["available"]:
        print(f"   unavailable: {consistency['reason']}")
    else:
        print(
            f"   {consistency['total_pairs']} call/put pairs "
            f"({consistency['excluded_low_vega']} excluded below the "
            f"{consistency['min_vega_fraction']:.0%} vega floor)"
        )
        for bucket, entry in sorted(consistency["by_bucket"].items()):
            if not entry.get("sufficient"):
                print(f"   {bucket:<10} n={entry['n_pairs']:<5} {entry.get('reason')}")
                continue
            r2 = entry["r_squared"]
            print(
                f"   {bucket:<10} n={entry['n_pairs']:<5} "
                f"intercept={entry['intercept']:+.5f}  slope={entry['slope']:+.5f}  "
                f"R²={'n/a' if r2 is None else f'{r2:.3f}'}"
            )
            fwd = entry.get("implied_forward_error_pts")
            if fwd is not None:
                drift = entry.get("implied_r_minus_q_error")
                print(
                    f"              implied ΔF={fwd:+.2f} pts "
                    f"(std {entry['implied_forward_error_std_pts']:.2f}, "
                    f"constant={entry.get('forward_error_is_constant')})"
                    + (f"  ->  Δ(r-q)={drift:+.5f}" if drift else "")
                )
            print(f"              -> {entry['reading']}")

    # --- 6. Path B -----------------------------------------------------------
    path_b = path_b_not_applicable(
        config.get(
            "validation.path_b_reason",
            "tier returns no quotes; there is no mid to invert IV from",
        )
    )
    print("\n6. §7.1 Path B — N/A")
    print(f"   {path_b['reason'].strip()}")
    print("   This is a recorded fact about the tier, not a failed gate.")

    # --- verdict -------------------------------------------------------------
    record = {
        "date": args.date,
        "time_label": args.time,
        "fixtures_passed": fixtures_ok,
        "sample_coverage": coverage,
        "path_a_configured": primary.as_dict() if primary else None,
        "implied_drift_scan": scan,
        "massive_implied_t": implied_t,
        "massive_conventions_fit": fit,
        "put_call_iv_consistency": consistency,
        "path_b": path_b,
    }

    print("\n" + "=" * 74)
    stops: list[str] = []
    blocks_m2 = bool(config.get("validation.path_a_blocks_m2", False))
    if primary is not None:
        if not primary.passed:
            stops.append(
                f"Path A median {primary.median_abs_pct_error:.3f}% >= {gate}% gate"
            )
        if systematic_tilt(primary.bias_by_moneyness):
            stops.append("systematic residual tilt across moneyness")
        if systematic_tilt(primary.bias_by_bucket):
            stops.append("systematic residual tilt across DTE")

    if not coverage["meets_am_settled_rule"]:
        stops.append("fewer than two AM-settled contracts in the sample (§16.7)")

    # §7.1's warning made operational: passing the gate is not the same as
    # having found the Massive's convention. If the free scan fits far better
    # than the best named candidate, the named candidate is simply wrong and
    # squeaking under 1% — exactly the "5% T error passes a 10% gate" failure,
    # one order of magnitude down.
    scored = primary.as_dict() if primary is not None else None
    if scored and scan["median_abs_pct_error_at_best"] > 0:
        ratio = scored["median_abs_pct_error"] / scan["median_abs_pct_error_at_best"]
        if ratio > 5.0 and abs(scored["r_minus_q"] - scan["implied_r_minus_q"]) > 0.002:
            stops.append(
                f"the chosen convention (r-q={scored['r_minus_q']:+.4f}, "
                f"{scored['median_abs_pct_error']:.4f}%) fits {ratio:.0f}x worse than "
                f"the free scan (r-q={scan['implied_r_minus_q']:+.4f}, "
                f"{scan['median_abs_pct_error_at_best']:.4f}%) — passing 1% here "
                "would lock in the wrong convention for all 15 days"
            )

    # §3.2 evidence, cross-read against the scan.
    if consistency.get("available"):
        for bucket, entry in consistency["by_bucket"].items():
            if entry.get("sufficient") and entry.get("implied_r_minus_q_error"):
                if entry.get("forward_error_is_constant"):
                    stops.append(
                        f"put-call IV in {bucket} implies the Massive's r-q differs "
                        f"from ours by {entry['implied_r_minus_q_error']:+.4f} "
                        "(constant forward error) — independent of Path A, so this "
                        "is corroboration, not noise"
                    )

    if stops and blocks_m2:
        print("VERDICT: DO NOT PROCEED TO M2")
        for stop in stops:
            print(f"  - {stop}")
        print("\n§19.4: report rather than picking the closest fit.")
    elif stops:
        print("VERDICT: reported, not blocking. Path A is a diagnostic on our own")
        print("Black-Scholes; it cannot reach 1% against a CRR tree with")
        print("finite-difference vega (METHODOLOGY §3.4), and M2 does not wait on it.")
        for stop in stops:
            print(f"  - {stop}")
    else:
        print("VERDICT: Path A passed. M2 may begin.")
        print(
            f"  Convention to record: r-q = {scan['implied_r_minus_q']:+.5f} "
            f"(median|err| {scan['median_abs_pct_error_at_best']:.4f}%)."
        )
        print("  Write it into config/experiment.yaml AND docs/METHODOLOGY.md, "
              "with the residual bias tables.")
    print("=" * 74)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(record, indent=2, default=str))
        print(f"\nfull record -> {args.json_out}")

    return 1 if stops else 0


def _print_path_a(result, gate: float) -> None:
    print(f"   n={result.n}  median|err|={result.median_abs_pct_error:.4f}%  "
          f"p90={result.p90_abs_pct_error:.4f}%   (reference {gate}%, not a gate)")
    print(f"   bias by moneyness : {result.bias_by_moneyness}")
    print(f"   bias by DTE       : {result.bias_by_bucket}")
    print(f"   bias by settlement: {result.bias_by_settlement}")


if __name__ == "__main__":
    raise SystemExit(main())
