#!/usr/bin/env python3
"""Rebuild normalized and derived SPX GEX data from preserved inputs.

Safe default: OUTPUT_ROOT must be a new or empty directory.  The command never
downloads data and never writes data/normalized or data/derived.  A canonical
replacement is a separate, explicit promotion decision after verification.

    python3 scripts/rebuild_derived.py --output-root /private/tmp/spx-gex-rebuild
    python3 scripts/rebuild_derived.py --output-root /tmp/one-day --date 2026-07-31

Inputs are data/raw_chain, data/manual_input/transmission_spot.csv and the YAML
configuration.  reproduction_manifest.json records their hashes, the code
commit, package versions, the exact configuration snapshot and every output.
No API credential is accessed: this is an offline rebuild.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from spx_gex.actual_map import build_actual_map, write_actual_map  # noqa: E402
from spx_gex.attribution import build_attribution, write_attribution  # noqa: E402
from spx_gex.config import Config, ConfigError  # noqa: E402
from spx_gex.frozen_map import (  # noqa: E402
    FrozenMapError,
    build_frozen_map,
    write_frozen_map,
)
from spx_gex.normalize import (  # noqa: E402
    NormalizeError,
    normalize_capture,
    write_normalized,
)

MANIFEST_NAME = "reproduction_manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_record(path: Path, base: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(base).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": sha256(path),
    }


def compare_to_current(output_root: Path) -> dict[str, Any]:
    """Byte-compare staging outputs with the current local data tree."""
    exact: list[str] = []
    changed: list[str] = []
    new: list[str] = []
    absent_from_rebuild: list[str] = []
    for layer in ("normalized", "derived"):
        staged_root = output_root / layer
        current_root = REPO_ROOT / "data" / layer
        staged = {
            p.relative_to(staged_root).as_posix(): p
            for p in staged_root.rglob("*")
            if p.is_file()
        }
        current = {
            p.relative_to(current_root).as_posix(): p
            for p in current_root.rglob("*")
            if p.is_file()
        }
        for relative, path in staged.items():
            item = f"{layer}/{relative}"
            if relative not in current:
                new.append(item)
            elif sha256(path) == sha256(current[relative]):
                exact.append(item)
            else:
                changed.append(item)
        absent_from_rebuild.extend(
            f"{layer}/{relative}" for relative in current.keys() - staged.keys()
        )
    return {
        "comparison": "SHA-256 byte comparison against current local data tree",
        "exact_count": len(exact),
        "changed_count": len(changed),
        "new_count": len(new),
        "absent_from_rebuild_count": len(absent_from_rebuild),
        "changed": sorted(changed),
        "new": sorted(new),
        "absent_from_rebuild": sorted(absent_from_rebuild),
    }


def git_value(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def discover_dates(raw_root: Path) -> list[str]:
    return sorted(
        p.name
        for p in raw_root.iterdir()
        if p.is_dir() and len(p.name) == 10 and list(p.glob("*.parquet"))
    )


def require_empty_output(path: Path) -> None:
    resolved = path.expanduser().resolve()
    canonical_data = (REPO_ROOT / "data").resolve()
    if resolved == canonical_data or canonical_data in resolved.parents:
        raise ValueError(
            "output root may not be data/ or a child of data/; rebuild into a "
            "separate directory and promote only after verification"
        )
    if resolved.exists() and any(resolved.iterdir()):
        raise ValueError(f"output root is not empty: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)


def rebuild_date(
    config: Config,
    date_str: str,
    raw_root: Path,
    normalized_root: Path,
    derived_root: Path,
) -> dict[str, Any]:
    day = raw_root / date_str
    labels = sorted(p.stem for p in day.glob("*.parquet"))
    if not labels:
        raise NormalizeError(f"no raw Parquet in {day}")

    quality_failures: list[str] = []
    for label in labels:
        frame, quality = normalize_capture(config, date_str, label)
        write_normalized(
            frame, quality, date_str, label, output_root=normalized_root
        )
        if not quality["passed"]:
            quality_failures.append(label)

    frozen, frozen_meta = build_frozen_map(
        config, date_str, normalized_root=normalized_root
    )
    if frozen.empty:
        raise FrozenMapError(f"{date_str}: frozen map produced no rows")
    write_frozen_map(frozen, frozen_meta, date_str, output_root=derived_root)

    actual, actual_meta = build_actual_map(
        config, date_str, normalized_root=normalized_root
    )
    if actual.empty:
        raise FrozenMapError(f"{date_str}: actual map produced no rows")
    write_actual_map(actual, actual_meta, date_str, output_root=derived_root)

    t0 = str(config.get("attribution.baseline_time", "0945"))
    t1_times = [
        str(v) for v in config.get("attribution.t1_times", ["1400", "1545"])
    ]
    identity_failures: list[str] = []
    attribution_rows: dict[str, int] = {}
    for t1 in t1_times:
        table, meta = build_attribution(
            config, date_str, t0, t1, normalized_root=normalized_root
        )
        if table.empty:
            raise FrozenMapError(f"{date_str}: attribution {t0}->{t1} is empty")
        write_attribution(table, meta, date_str, t0, t1, output_root=derived_root)
        attribution_rows[f"{t0}_{t1}"] = len(table)
        identity_failures.extend(meta.get("identity_failures", []))

    if identity_failures:
        raise FrozenMapError(
            f"{date_str}: attribution identity gate failed: {identity_failures}"
        )
    return {
        "date": date_str,
        "raw_labels": labels,
        "normalization_quality_failures": quality_failures,
        "frozen_rows": len(frozen),
        "actual_rows": len(actual),
        "attribution_rows": attribution_rows,
    }


def build_manifest(
    output_root: Path,
    config_path: Path,
    dates: list[str],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_root = REPO_ROOT / "data" / "raw_chain"
    manual = REPO_ROOT / "data" / "manual_input" / "transmission_spot.csv"
    input_paths = [
        p
        for date_str in dates
        for p in sorted((raw_root / date_str).glob("*"))
        if p.is_file()
    ] + [manual, config_path]
    output_paths = sorted(
        p
        for p in output_root.rglob("*")
        if p.is_file() and p.name != MANIFEST_NAME
    )
    packages = {}
    for name in ("pandas", "pyarrow", "PyYAML", "scipy"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    dirty = (git_value("status", "--short") or "").splitlines()
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "offline_rebuild": True,
        "raw_cutoff_date": max(dates),
        "dates": dates,
        "code": {
            "repository": str(REPO_ROOT),
            "git_commit": git_value("rev-parse", "HEAD"),
            "git_status_at_rebuild": dirty,
        },
        "runtime": {
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "platform": platform.platform(),
            "packages": packages,
        },
        "configuration": {
            "source": str(config_path),
            "snapshot": "config_snapshot.yaml",
            "sha256": sha256(config_path),
        },
        "inputs": [file_record(p, REPO_ROOT) for p in input_paths],
        "results": results,
        "outputs": [file_record(p, output_root) for p in output_paths],
        "verification_against_current": compare_to_current(output_root),
        "promotion": {
            "performed": False,
            "note": "verified staging output; production data was not overwritten",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument(
        "--date", action="append", dest="dates", help="YYYY-MM-DD; repeatable"
    )
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "config" / "experiment.yaml"
    )
    args = parser.parse_args()

    output_root = args.output_root.expanduser().resolve()
    config_path = args.config.expanduser().resolve()
    raw_root = REPO_ROOT / "data" / "raw_chain"
    try:
        require_empty_output(output_root)
        config = Config.load(config_path)
        dates = sorted(set(args.dates or discover_dates(raw_root)))
        if not dates:
            raise ValueError("no raw dates found")
        absent = [d for d in dates if not (raw_root / d).is_dir()]
        if absent:
            raise ValueError(f"raw dates do not exist: {absent}")

        snapshot = output_root / "config_snapshot.yaml"
        shutil.copy2(config_path, snapshot)
        normalized_root = output_root / "normalized"
        derived_root = output_root / "derived"
        results = []
        for date_str in dates:
            print(f"rebuilding {date_str} ...", flush=True)
            result = rebuild_date(
                config, date_str, raw_root, normalized_root, derived_root
            )
            results.append(result)
            print(
                f"  normalized {len(result['raw_labels'])} captures; "
                f"derived {result['frozen_rows']} frozen + "
                f"{result['actual_rows']} actual rows",
                flush=True,
            )
        manifest = build_manifest(output_root, config_path, dates, results)
        manifest_path = output_root / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    except (ConfigError, NormalizeError, FrozenMapError, OSError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    quality_failures = sum(
        len(r["normalization_quality_failures"]) for r in results
    )
    print(
        f"DONE: {len(dates)} dates, {len(manifest['inputs'])} inputs, "
        f"{len(manifest['outputs'])} outputs -> {output_root}"
    )
    print(f"manifest: {manifest_path}")
    comparison = manifest["verification_against_current"]
    print(
        "current-copy comparison: "
        f"{comparison['exact_count']} exact, {comparison['changed_count']} changed, "
        f"{comparison['new_count']} new, "
        f"{comparison['absent_from_rebuild_count']} absent from rebuild"
    )
    if quality_failures:
        print(
            f"WARNING: {quality_failures} normalized captures retained but failed "
            "their recorded quality gate"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
