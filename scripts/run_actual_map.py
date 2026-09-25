#!/usr/bin/env python3
"""Actual-surface refresh and the §12 2×2 attribution (M3).

    python3 scripts/run_actual_map.py --date 2026-07-28
    python3 scripts/run_actual_map.py --date 2026-07-28 --attribution-only

Writes `actual_map.parquet` and one `attribution_<t0>_<t1>.parquet` per t1.

Exit codes: 0 everything ran and the identity gate passed, 1 the identity gate
failed, 2 nothing could be computed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from spx_gex.actual_map import build_actual_map, write_actual_map  # noqa: E402
from spx_gex.attribution import build_attribution, write_attribution  # noqa: E402
from spx_gex.config import Config, ConfigError  # noqa: E402
from spx_gex.frozen_map import FrozenMapError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--config", help="path to experiment.yaml")
    ap.add_argument("--t0", default="0945", help="attribution baseline")
    ap.add_argument("--t1", nargs="*", default=["1400", "1545"])
    ap.add_argument("--attribution-only", action="store_true")
    args = ap.parse_args()

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    r, q = config.require("rates.r"), config.require("rates.q")
    print(f"M3 — {args.date}   r={r} q={q}")

    # --- actual map ---------------------------------------------------------
    if not args.attribution_only:
        try:
            table, meta = build_actual_map(config, args.date)
        except FrozenMapError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 2
        if table.empty:
            print("FAILED: no surfaces produced rows", file=sys.stderr)
            return 2
        path, _ = write_actual_map(table, meta, args.date)
        print(f"\nactual map — {meta['rows']} rows -> {path.name}")
        if meta["surface_times_missing"]:
            print(f"  !! no surface for {meta['surface_times_missing']}")
        cols = ["time_label", "expiry_bucket", "spot", "t_min_remaining",
                "net_gex", "flip", "call_gex_peak", "put_gex_peak", "n_contracts"]
        show = table[cols].copy()
        show["net_gex_B"] = (show.pop("net_gex") / 1e9).round(3)
        print(show.to_string(index=False))

    # --- attribution --------------------------------------------------------
    failures: list[str] = []
    for t1 in args.t1:
        try:
            table, meta = build_attribution(config, args.date, args.t0, t1)
        except FrozenMapError as exc:
            print(f"\nattribution {args.t0}->{t1}: SKIPPED — {exc}")
            continue
        if table.empty:
            print(f"\nattribution {args.t0}->{t1}: no rows")
            continue
        path, _ = write_attribution(table, meta, args.date, args.t0, t1)

        cov = meta["coverage"]
        print(f"\n{'='*78}\nattribution  {args.t0} -> {t1}   "
              f"spot {meta['spot_t0']} -> {meta['spot_t1']}   -> {path.name}")
        print(f"  contract set: {cov['common_with_oi_base']:,} common "
              f"(dropped {cov['dropped_vs_t0']:,} vs {args.t0}, "
              f"{cov['dropped_vs_t1']:,} vs {t1}); "
              f"{cov['iv_changed_contracts']:,} contracts changed IV")
        print(f"  bucket policy: {meta['bucket_policy_label']} applied to all four states")

        for bucket in meta["buckets"]:
            sub = table[table.expiry_bucket == bucket]
            print(f"\n  [{bucket}]")
            print(f"    {'metric':>14}{'A':>12}{'D':>12}{'total':>12}"
                  f"{'mech':>12}{'surf':>12}{'mech%':>8}")
            for _, row in sub.iterrows():
                scale = 1e9 if row.metric == "net_gex" else 1.0
                unit = "B" if row.metric == "net_gex" else ""
                tot = row.total_change
                pct = (100 * row.mechanical_contribution / tot) if tot else float("nan")
                print(f"    {row.metric + unit:>14}"
                      f"{row.state_A/scale:>12.3f}{row.state_D/scale:>12.3f}"
                      f"{tot/scale:>+12.3f}"
                      f"{row.mechanical_contribution/scale:>+12.3f}"
                      f"{row.surface_contribution/scale:>+12.3f}"
                      f"{pct:>7.0f}%")

        worst = table.reindex(
            table.membership_effect_pct_of_total.abs().sort_values(ascending=False).index
        ).head(1)
        if not worst.empty and pd.notna(worst.iloc[0].membership_effect_pct_of_total):
            w = worst.iloc[0]
            scale = 1e9 if w.metric == "net_gex" else 1.0
            print(f"\n  cost of holding the contract set fixed [DIAGNOSTIC]: largest is "
                  f"{w.expiry_bucket}/{w.metric}, "
                  f"D={w.state_D/scale:.3f} vs {w.state_D_unconstrained/scale:.3f} "
                  f"on t1's own rows — {w.membership_effect_pct_of_total:+.0f}% of that "
                  f"row's total change.\n  That is the artifact avoided, not an error.")

        if meta.get("metrics_skipped"):
            print("\n  metrics with no attribution (undefined in at least one state):")
            for s in meta["metrics_skipped"]:
                print(f"    - {s}")

        if meta["identity_gate_passed"]:
            print("\n  identity gate: PASSED "
                  "(mechanical + surface == total, all rows)")
        else:
            failures.extend(meta["identity_failures"])
            print("\n  identity gate: FAILED")
            for f in meta["identity_failures"]:
                print(f"    - {f}")

    print(f"\n{'='*78}")
    if failures:
        print("VERDICT: identity gate failed — the attribution is not usable.")
        return 1
    print("VERDICT: M3 ran. Attribution is counterfactual under a declared model,")
    print("not a causal decomposition (§12).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
