#!/usr/bin/env python3
"""Run the complete deterministic GEX pipeline without network access."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEMO_DATE = "1999-01-04"
GENERATED_DIRS = (
    ROOT / "data" / "raw_chain" / DEMO_DATE,
    ROOT / "data" / "normalized" / DEMO_DATE,
    ROOT / "data" / "derived" / DEMO_DATE,
)


def run(*args: str) -> None:
    command = [sys.executable, *args]
    print(f"\n$ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clean",
        action="store_true",
        help="replace only the generated 1999-01-04 demo directories",
    )
    args = parser.parse_args()

    existing = [path for path in GENERATED_DIRS if path.exists()]
    if existing and not args.clean:
        print("Demo output already exists:", file=sys.stderr)
        for path in existing:
            print(f"  {path.relative_to(ROOT)}", file=sys.stderr)
        print("Re-run with --clean to replace this synthetic demo only.", file=sys.stderr)
        return 2
    if args.clean:
        for path in GENERATED_DIRS:
            shutil.rmtree(path, ignore_errors=True)

    run("scripts/capture_oi_base.py", "--date", DEMO_DATE, "--provider", "synthetic")
    for label in ("0945", "1030", "1400", "1545"):
        run(
            "scripts/capture_surface.py",
            "--date",
            DEMO_DATE,
            "--time",
            label,
            "--provider",
            "synthetic",
        )
    run("scripts/normalize_chain.py", "--date", DEMO_DATE)
    run("scripts/run_frozen_map.py", "--date", DEMO_DATE)
    run("scripts/run_actual_map.py", "--date", DEMO_DATE)

    print("\nSynthetic demo complete. No network or credentials were used.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
