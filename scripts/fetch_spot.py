#!/usr/bin/env python3
"""Fetch the SPX 1-minute closes the study needs, from Yahoo (spec §5.4).

    python3 scripts/fetch_spot.py --date 2026-07-27
    python3 scripts/fetch_spot.py --date 2026-07-27 --dry-run
    python3 scripts/fetch_spot.py --date 2026-07-27 --with-capture-asofs

**Deviation from §5.4, stated plainly.** The spec calls `transmission_spot.csv`
a hand-dropped export. The first live day contains hand-recorded rows; current
scheduled production and recent backfills append Yahoo rows. The file therefore
has mixed provenance and carries a `source` column: a value typed by a person
and a value pulled from Yahoo are not interchangeable.

**Validated before being trusted.** Against the seven values hand-recorded on
2026-07-27, Yahoo's `^GSPC` 1-minute close at label `HH:MM` matched to within
0.01 at every intraday point — the same bar convention, not an approximation of
it. The 16:00 point differed by 0.04, which is the closing auction minute and is
expected to disagree slightly.

**Limits worth knowing.** Yahoo serves 1-minute bars for roughly the last 30
days only, so this backfills recent days and cannot reconstruct the whole study
later. A minute with no trade comes back null and is reported as missing rather
than filled from a neighbour.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests  # noqa: E402

from spx_gex.capture import scheduled_download_et, target_asof_et  # noqa: E402
from spx_gex.config import Config, ConfigError, manual_input_dir, raw_chain_dir  # noqa: E402

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
SOURCE_TAG = "yahoo:^GSPC:1m"
MANUAL_TAG = "manual"
FIELDNAMES = ["date", "time_label", "spot", "source_timestamp", "source", "note"]


def required_labels(config: Config, day: date) -> list[str]:
    """Every minute the study reads, derived from config — never a hardcoded list.

    Three sets, unioned: the target as-ofs (what each capture is for), the
    derived download times (so the implied-delay check resolves at 15 minutes
    instead of 105), and the frozen-map recomputation points.
    """
    labels: set[str] = set()
    for target in config.require("capture.target_asof_times"):
        labels.add(target)
        labels.add(f"{scheduled_download_et(config, day, target):%H%M}")
    labels.update(config.require("frozen_map.recompute_times"))
    return sorted(labels)


def capture_asof_labels(config: Config, date_str: str) -> dict[str, str]:
    """The instant each capture actually resolved to, as HHMM -> capture label.

    Worth having: `normalize.py` matches spot to the resolved as-of, and a
    download that slipped resolves somewhere between the labelled minutes.
    """
    tz = ZoneInfo(config.get("capture.timezone", "America/New_York"))
    out: dict[str, str] = {}
    day_dir = raw_chain_dir(date_str)
    if not day_dir.exists():
        return out
    for meta_path in sorted(day_dir.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        asof = meta.get("asof_utc")
        if not asof or meta.get("capture_kind") != "surface":
            continue
        local = datetime.fromisoformat(asof).astimezone(tz)
        out[f"{local:%H%M}"] = str(meta.get("time_label"))
    return out


def fetch_bars(day: date, tz: ZoneInfo) -> dict[str, float]:
    """1-minute closes for one session, keyed HHMM in exchange local time."""
    start = int(datetime.combine(day, time(0, 0), tzinfo=tz).timestamp())
    response = requests.get(
        YAHOO_URL,
        params={"interval": "1m", "period1": start, "period2": start + 86400},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json().get("chart", {})
    if payload.get("error"):
        raise RuntimeError(f"Yahoo error: {payload['error']}")
    results = payload.get("result")
    if not results:
        raise RuntimeError("Yahoo returned no result block")

    result = results[0]
    stamps = result.get("timestamp") or []
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close") or []
    return {
        f"{datetime.fromtimestamp(t, tz):%H%M}": float(c)
        for t, c in zip(stamps, closes)
        if c is not None
    }


def read_existing(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    """Write a stable, repository-safe CSV using LF on every platform."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD; defaults to today in ET")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--with-capture-asofs",
        action="store_true",
        help="also fetch the minute each capture actually resolved to",
    )
    ap.add_argument("--config")
    args = ap.parse_args()

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    tz = ZoneInfo(config.get("capture.timezone", "America/New_York"))
    date_str = args.date or datetime.now(tz).date().isoformat()
    args.date = date_str
    day = date.fromisoformat(date_str)
    if day.weekday() >= 5:
        print(f"REFUSED: {args.date} is a weekend", file=sys.stderr)
        return 2
    if (date.today() - day).days > 29:
        print(
            f"REFUSED: {args.date} is more than 29 days back; Yahoo serves 1-minute "
            "bars for roughly 30 days only and this cannot be backfilled later.",
            file=sys.stderr,
        )
        return 2

    wanted = required_labels(config, day)
    extra = capture_asof_labels(config, args.date) if args.with_capture_asofs else {}
    for label in extra:
        if label not in wanted:
            wanted.append(label)
    wanted.sort()

    try:
        bars = fetch_bars(day, tz)
    except (requests.RequestException, RuntimeError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    print(f"Yahoo ^GSPC 1m — {args.date}: {len(bars)} bars\n")

    path = manual_input_dir() / "transmission_spot.csv"
    existing = read_existing(path)
    have = {(row["date"], row["time_label"]) for row in existing}

    added: list[dict[str, str]] = []
    missing: list[str] = []
    kept: list[str] = []
    for label in wanted:
        if (args.date, label) in have:
            kept.append(label)
            continue
        close = bars.get(label)
        if close is None:
            missing.append(label)
            continue
        stamp = datetime.combine(
            day, time(int(label[:2]), int(label[2:])), tzinfo=tz
        )
        added.append(
            {
                "date": args.date,
                "time_label": label,
                "spot": f"{close:.2f}",
                "source_timestamp": stamp.isoformat(),
                "source": SOURCE_TAG,
                "note": f"capture asof for {extra[label]}" if label in extra else "",
            }
        )

    for row in added:
        print(f"  + {row['time_label']}  {row['spot']:>10}  {row['note']}")
    if kept:
        print(f"\n  kept {len(kept)} existing row(s) untouched: {kept}")
        print("    a hand-typed value and a fetched one are not interchangeable;")
        print("    delete the row by hand if you want it refetched")
    if missing:
        print(f"\n  !! no bar for {missing} — reported, never filled from a neighbour")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0
    if not added:
        print("\nnothing to add.")
        return 0

    rows = [
        {**{k: row.get(k, "") for k in FIELDNAMES},
         "source": row.get("source") or MANUAL_TAG}
        for row in existing
    ] + added
    rows.sort(key=lambda r: (r["date"], r["time_label"]))
    write_rows(path, rows)
    print(f"\nwrote {len(added)} row(s) -> {path.name}  ({len(rows)} total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
