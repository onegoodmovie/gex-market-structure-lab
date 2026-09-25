#!/usr/bin/env python3
"""Build the frozen-surface mechanical map for one day (spec §10).

    python3 scripts/run_frozen_map.py --date 2026-07-27

Frozen OI (from the OI base), frozen IV (from the 09:45 surface), with S and T
both recomputed at every point in `frozen_map.recompute_times`. Writes
data/derived/YYYY-MM-DD/frozen_map.parquet.

Re-runnable against any past date purely from stored Parquet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from spx_gex.config import Config, ConfigError  # noqa: E402
from spx_gex.exposure import rate_sensitivity, usable_for_gex  # noqa: E402
from spx_gex.frozen_map import (  # noqa: E402
    FrozenMapError,
    build_frozen_map,
    write_frozen_map,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--config", help="path to experiment.yaml")
    ap.add_argument("--sensitivity", action="store_true",
                    help="also report the +-50bp r/q sensitivity")
    ap.add_argument("--base", default=None,
                    help="surface base to freeze (default: frozen_map.surface_base_time)")
    ap.add_argument("--compare-base", default=None, metavar="LABEL",
                    help="also build from LABEL and report the difference; "
                         "the written map stays the primary base")
    args = ap.parse_args()

    try:
        config = Config.load(args.config)
        table, meta = build_frozen_map(config, args.date, args.base)
    except (ConfigError, FrozenMapError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    if table.empty:
        print("FAILED: no rows produced", file=sys.stderr)
        return 2

    parquet_path, _ = write_frozen_map(table, meta, args.date)
    print(f"frozen map — {args.date}   r={meta['r']} q={meta['q']}"
          + ("  [PROVISIONAL RATES]" if meta["rates_provisional"] else ""))
    print(f"  {meta['contracts_in_frozen_input']:,} contracts, "
          f"{meta['rows']} rows -> {parquet_path.name}")
    if meta["recompute_times_missing_spot"]:
        print(f"  !! no transmission spot for {meta['recompute_times_missing_spot']} "
              "— those rows are absent, never interpolated")
    print()

    show = table[["time_label", "expiry_bucket", "spot", "t_min_remaining",
                  "net_gex", "flip", "n_roots", "call_gex_peak", "put_gex_peak",
                  "n_contracts"]].copy()
    show["net_gex"] = (show["net_gex"] / 1e9).round(3)
    show = show.rename(columns={"net_gex": "net_gex_B"})
    print(show.to_string(index=False))

    flagged = table[table["not_cross_day_comparable"]]
    if len(flagged):
        print(f"\n  {len(flagged)} row(s) flagged not_cross_day_comparable "
              "(§9.1) and held out of the aggregate")

    # --- how much does the choice of surface base matter? --------------------
    # Not a switch to be argued about: build both and measure. 09:45 is the
    # capture most exposed to the provider's unsettled open (T1 §3), and every
    # frozen-map row is built on it.
    if args.compare_base:
        try:
            other, other_meta = build_frozen_map(config, args.date, args.compare_base)
        except FrozenMapError as exc:
            print(f"\nbase comparison unavailable: {exc}")
            print("  (the alternative base has no capture on this date — expected "
                  "for days before it was scheduled)")
        else:
            keys = ["time_label", "expiry_bucket"]
            merged = table[keys + ["net_gex", "flip", "n_contracts"]].merge(
                other[keys + ["net_gex", "flip", "n_contracts"]],
                on=keys, suffixes=("_a", "_b"),
            )
            print(f"\nsurface base {meta['surface_base']} vs "
                  f"{other_meta['surface_base']}  —  "
                  f"{other_meta['contracts_in_frozen_input']:,} contracts in the "
                  f"alternative base vs {meta['contracts_in_frozen_input']:,}")
            print(f"  {'time':>6}{'bucket':>11}{'net A B':>10}{'net B B':>10}"
                  f"{'d net B':>9}{'d net %':>9}{'flip A':>10}{'flip B':>10}{'d flip':>8}")
            for _, row in merged.iterrows():
                dn = row.net_gex_b - row.net_gex_a
                pct = 100 * dn / abs(row.net_gex_a) if row.net_gex_a else float("nan")
                print(f"  {row.time_label:>6}{row.expiry_bucket:>11}"
                      f"{row.net_gex_a/1e9:>+10.2f}{row.net_gex_b/1e9:>+10.2f}"
                      f"{dn/1e9:>+9.2f}{pct:>+8.1f}%"
                      f"{row.flip_a:>10.2f}{row.flip_b:>10.2f}"
                      f"{row.flip_b - row.flip_a:>+8.2f}")
            print(f"\n  Reported, not decided. The written map uses "
                  f"{meta['surface_base']}; changing that is a config change, "
                  "made\n  on several days of this table rather than on one.")

    if args.sensitivity:
        from spx_gex.frozen_map import build_frozen_inputs
        frame = build_frozen_inputs(config, args.date, args.base)
        spot = float(table["spot"].iloc[0])
        sens = rate_sensitivity(
            frame, spot, meta["r"], meta["q"],
            bump=float(config.get("rates.sensitivity_bump", 0.005)),
        )
        print(f"\n±{sens['bump']*1e4:.0f}bp sensitivity at S={spot:.2f}")
        print(f"  base: net GEX {sens['base_net_gex']/1e9:+.3f}B   "
              f"flip {sens['base_flip']:.2f}")
        print(f"  {'':<8} {'net GEX':>10} {'change':>10} {'change%':>9} "
              f"{'flip':>10} {'d flip pts':>11}")
        for key in ("r_up", "r_down", "q_up", "q_down"):
            row = sens[key]
            print(f"  {key:<8} {row['net_gex']/1e9:>+9.3f}B "
                  f"{row['net_gex_abs_change']/1e9:>+9.4f}B "
                  f"{row['net_gex_pct_change']:>+8.3f}% "
                  f"{row['flip']:>10.2f} {row['flip_change_pts']:>+11.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
