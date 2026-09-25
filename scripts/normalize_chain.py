#!/usr/bin/env python3
"""Normalize captured chains for one day (spec §6).

    python3 scripts/normalize_chain.py --date 2026-07-27
    python3 scripts/normalize_chain.py --date 2026-07-27 --time 0945

Reads raw Parquet, writes data/normalized/YYYY-MM-DD/. Re-runnable and
idempotent: normalization is a pure function of stored raw data, so re-running
overwrites with identical output. (Raw capture is the opposite — it never
overwrites, because it cannot be regenerated.)

Exit codes: 0 all captures normalized and passed, 1 normalized but a
data-quality rule failed, 2 nothing was normalized.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spx_gex.config import Config, ConfigError, raw_chain_dir  # noqa: E402
from spx_gex.normalize import (  # noqa: E402
    NormalizeError,
    normalize_capture,
    write_normalized,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--time", help="one label; default is every capture present")
    ap.add_argument("--config", help="path to experiment.yaml")
    args = ap.parse_args()

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    day_dir = raw_chain_dir(args.date)
    if not day_dir.exists():
        print(f"FAILED: no captures at {day_dir}", file=sys.stderr)
        return 2

    labels = (
        [args.time]
        if args.time
        else sorted(p.stem for p in day_dir.glob("*.parquet"))
    )
    if not labels:
        print(f"FAILED: no raw Parquet in {day_dir}", file=sys.stderr)
        return 2

    all_passed = True
    for label in labels:
        try:
            frame, quality = normalize_capture(config, args.date, label)
            parquet_path, _ = write_normalized(frame, quality, args.date, label)
        except NormalizeError as exc:
            print(f"{label}: FAILED — {exc}", file=sys.stderr)
            all_passed = False
            continue

        status = "OK" if quality["passed"] else "FAILED"
        print(
            f"{label}: {status}  {quality['retained']:,}/{quality['expected_contracts']:,} "
            f"retained ({quality['retention_pct']:.2f}%)  -> {parquet_path.name}"
        )
        print(
            f"    definitional drops : {quality['definitional_drops']} "
            f"(zero OI contributes exactly 0 to GEX; not gated)"
        )
        print(
            f"    quality drops      : {quality['quality_drops']} "
            f"= {quality['quality_drop_pct']:.2f}% "
            f"(gate {quality['severe_drop_threshold_pct']}%)"
        )
        print(f"    flags              : {quality['flags']}")
        print(f"    buckets            : {quality['contracts_by_bucket']}")
        print(f"    AM-settled monthly : {quality['am_settled_monthly_contracts']}")
        if quality["asof_is_inferred"]:
            print("    !! asof is INFERRED (level 4) — every T here is an assumption")
        for reason in quality["severe_flags"]:
            print(f"    !! SEVERE: {reason}")
        all_passed = all_passed and quality["passed"]

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
