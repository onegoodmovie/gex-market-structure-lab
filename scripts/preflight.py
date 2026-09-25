#!/usr/bin/env python3
"""Readiness check. Run it before the market opens, not at 10:00.

    python3 scripts/preflight.py
    python3 scripts/preflight.py --date 2026-07-28

Everything here is cheap and offline except the optional --probe. The point is
that a missing key, a stale dependency or an un-parseable manual file should
cost you a minute at breakfast, not a capture window that cannot be recaptured.

Exit codes: 0 ready, 1 something needs attention.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spx_gex.capture import scheduled_download_et, target_asof_et  # noqa: E402
from spx_gex.config import Config, ConfigError, raw_chain_dir, repo_path  # noqa: E402
from spx_gex.transmission import read_transmission_spot  # noqa: E402

OK, WARN, BAD = "  ok  ", " warn ", " FAIL "


class Report:
    def __init__(self) -> None:
        self.failed = False

    def line(self, status: str, label: str, detail: str = "") -> None:
        if status == BAD:
            self.failed = True
        print(f"[{status}] {label:<34} {detail}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD; defaults to today in ET")
    ap.add_argument("--config", help="path to experiment.yaml")
    args = ap.parse_args()
    report = Report()

    # --- config --------------------------------------------------------------
    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"[{BAD}] config                             {exc}")
        return 1

    tz = ZoneInfo(config.get("capture.timezone", "America/New_York"))
    now_et = datetime.now(tz)
    date_str = args.date or now_et.date().isoformat()
    capture_date = datetime.fromisoformat(date_str).date()

    print(f"Preflight — {date_str}   (now {now_et:%Y-%m-%d %H:%M %Z})")
    print("=" * 74)

    report.line(OK, "config loaded", str(config.source.name))

    # --- dependencies --------------------------------------------------------
    for module in ("pandas", "pyarrow", "yaml", "requests", "numpy", "scipy"):
        try:
            importlib.import_module(module)
            report.line(OK, f"import {module}")
        except ImportError:
            report.line(BAD, f"import {module}", "pip install -r requirements.txt")

    # --- credentials ---------------------------------------------------------
    # Reads the key to prove it is reachable, then discards it. Only its length
    # is ever reported; the value is never printed, logged, or stored.
    source = str(config.get("provider.credential.source", "env"))
    try:
        key = config.api_key()
        report.line(
            OK,
            f"credential ({source})",
            f"readable, {len(key)} chars"
            + (
                f"  service={config.get('provider.credential.keychain_service', '?')}"
                if source == "keychain"
                else ""
            ),
        )
        del key
    except ConfigError as exc:
        report.line(BAD, f"credential ({source})", str(exc).splitlines()[0])

    report.line(
        OK if str(config.get("provider.auth_scheme", "")) == "bearer" else WARN,
        "auth scheme",
        f"{config.get('provider.auth_scheme', 'unset')} — no credential in the query string",
    )

    # --- timezone ------------------------------------------------------------
    report.line(
        OK if now_et.tzname() in ("EST", "EDT") else WARN,
        "machine timezone",
        f"{datetime.now().astimezone().tzname()} (cron fires on local time)",
    )

    # --- what is already on disk --------------------------------------------
    day_dir = raw_chain_dir(date_str)
    existing = sorted(p.stem for p in day_dir.glob("*.parquet")) if day_dir.exists() else []
    report.line(
        OK if not existing else WARN,
        "captures already present",
        f"{existing or 'none'}" + (" — re-runs will refuse to overwrite" if existing else ""),
    )

    # --- today's schedule ----------------------------------------------------
    print("-" * 74)
    delay = float(config.require("provider.delay_minutes"))
    print(f"Schedule (target as-of + {delay:.0f} min delay):")
    for label in config.require("capture.target_asof_times"):
        target = target_asof_et(config, capture_date, label)
        download = scheduled_download_et(config, capture_date, label)
        done = label in existing
        marker = "captured" if done else (
            "past due" if now_et > download + timedelta(minutes=10) and capture_date == now_et.date()
            else "pending"
        )
        print(
            f"   {label}  asof {target:%H:%M} ET   download {download:%H:%M} ET   {marker}"
        )
    print(f"   oi_base           download before "
          f"{config.get('capture.oi_base_deadline_et', '09:30')} ET")

    # --- manual inputs -------------------------------------------------------
    print("-" * 74)
    rows = read_transmission_spot(date_str)
    wanted = list(config.require("capture.target_asof_times"))
    download_labels = [
        f"{(target_asof_et(config, capture_date, lb) + timedelta(minutes=delay)):%H%M}"
        for lb in wanted
    ]
    present = {r.time_label for r in rows}
    missing_targets = [lb for lb in wanted if lb not in present]
    missing_downloads = [lb for lb in download_labels if lb not in present]

    if not rows:
        report.line(
            WARN,
            "transmission_spot.csv",
            f"no rows for {date_str} yet — needed by normalize if the provider "
            "omits spot",
        )
    else:
        report.line(
            OK if not missing_targets else WARN,
            "transmission_spot.csv",
            f"{len(rows)} rows; target times "
            f"{'all present' if not missing_targets else f'missing {missing_targets}'}",
        )
    if missing_downloads:
        report.line(
            WARN,
            "  download-time spots",
            f"missing {missing_downloads} — optional, but they take the implied-"
            "delay check from ~105 min resolution to 15",
        )

    reference_log = repo_path("data", "manual_input", "optioncharts_reference.csv")
    reference_rows = (
        len(reference_log.read_text().strip().splitlines()) - 1 if reference_log.exists() else -1
    )
    report.line(
        OK if reference_rows > 0 else WARN,
        "optioncharts_reference.csv",
        f"{reference_rows} row(s)" if reference_rows >= 0 else "missing"
        + "  — §8.1 needs a 14:00 screenshot to pair, and it cannot be backfilled",
    )

    # --- unresolved conventions ---------------------------------------------
    print("-" * 74)
    for key in ("rates.r", "rates.q"):
        value = config.get(key, None)
        report.line(
            OK if value is not None else WARN,
            key,
            str(value) if value is not None else "null — validate_greeks will "
            "search for it, then you write the answer in",
        )
    named = [
        name
        for group in ("rates.candidates.r", "rates.candidates.q")
        for name, value in (config.get(group, {}) or {}).items()
        if value is not None
    ]
    report.line(
        OK if len(named) > 2 else WARN,
        "rate candidates filled in",
        f"{named} — add the day's T-bill/SOFR/dividend yield for a real §7.2 grid",
    )

    print("=" * 74)
    if report.failed:
        print("NOT READY — fix the FAIL lines above before the open.")
    else:
        print("Ready. Warnings are things to know, not blockers.")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
