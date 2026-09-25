#!/usr/bin/env python3
"""Q2 — frozen-map descriptive usefulness (spec §Q2).

    python3 scripts/run_q2_crossings.py

15 days was never powered to test whether flip crossings predict anything, and
5 days is less so. The answerable question is accrual rate and coherence, so
this produces a descriptive table and an estimate of how many trading days
would be needed to reach n = 40 crossings. **No significance claims** — the
spec says so and nothing here is a test.

Per crossing: the 30-minute move after, the move to the close, realized range
in the 30 minutes before against the 30 after, and whether spot crossed back
within 30 minutes.

Two definitional choices, both load-bearing:

**The flip is the frozen map's**, held piecewise constant between recompute
points. Q2 asks whether the *frozen* map is descriptively useful, so the actual
map's flip would answer a different question.

**A crossing is spot moving across a level, never the level moving across
spot.** The frozen flip steps at each recompute point, and at 15:45 the
aggregate drops 0DTE per §9.1, which can jump the level by tens of points. Sign
changes at a segment boundary are therefore not counted — only minute-to-minute
sign changes evaluated against one constant level.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from spx_gex.config import Config, repo_path  # noqa: E402

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
WINDOW_MIN = 30


def session_bars(day: date, tz: ZoneInfo) -> pd.DataFrame:
    """1-minute high/low/close for one session. Highs and lows are needed for
    realized range; `fetch_spot.fetch_bars` returns closes only and is left
    alone because the scheduled agent depends on it."""
    start = int(datetime.combine(day, time(0, 0), tzinfo=tz).timestamp())
    r = requests.get(YAHOO, params={"interval": "1m", "period1": start,
                                    "period2": start + 86400},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = []
    for t, c, h, lo in zip(res["timestamp"], q["close"], q["high"], q["low"]):
        if c is None:
            continue
        stamp = datetime.fromtimestamp(t, tz)
        rows.append({"label": f"{stamp:%H%M}", "minute": stamp.hour * 60 + stamp.minute,
                     "close": float(c), "high": float(h) if h else float(c),
                     "low": float(lo) if lo else float(c)})
    return pd.DataFrame(rows).sort_values("minute").reset_index(drop=True)


def flip_segments(frozen: pd.DataFrame, bucket: str) -> list[tuple[int, int, float]]:
    """(start_minute, end_minute, flip) — piecewise constant, in clock order."""
    sub = frozen[frozen.expiry_bucket == bucket].copy()
    sub["minute"] = [int(t[:2]) * 60 + int(t[2:]) for t in sub.time_label]
    sub = sub.sort_values("minute")
    segs, rows = [], list(sub.itertuples())
    for i, row in enumerate(rows):
        end = rows[i + 1].minute if i + 1 < len(rows) else 16 * 60
        if row.minute < end and pd.notna(row.flip):
            segs.append((row.minute, end, float(row.flip)))
    return segs


def crossings_for(bars: pd.DataFrame, segs: list[tuple[int, int, float]]) -> list[dict]:
    """Sign changes of (close - flip) inside one segment. Boundaries excluded."""
    out = []
    close_min = int(bars.minute.max())
    for lo, hi, flip in segs:
        seg = bars[(bars.minute >= lo) & (bars.minute < hi)].reset_index(drop=True)
        if len(seg) < 2:
            continue
        d = seg.close - flip
        for i in range(1, len(seg)):
            if d[i - 1] == 0 or (d[i - 1] > 0) == (d[i] > 0):
                continue
            m = int(seg.minute[i])
            after = bars[(bars.minute > m) & (bars.minute <= m + WINDOW_MIN)]
            before = bars[(bars.minute >= m - WINDOW_MIN) & (bars.minute <= m)]
            spot = float(seg.close[i])
            recross = False
            if not after.empty:
                side = (spot - flip) > 0
                recross = bool((((after.close - flip) > 0) != side).any())
            out.append({
                "minute": m,
                "time": f"{m//60:02d}:{m%60:02d}",
                "direction": "up" if d[i] > 0 else "down",
                "flip": flip,
                "spot": spot,
                "post_cross_move_30m": (float(after.close.iloc[-1]) - spot)
                                       if not after.empty else None,
                "post_cross_truncated_by_close": bool(m + WINDOW_MIN > close_min),
                "post_cross_move_to_close": float(bars.close.iloc[-1]) - spot,
                "range_30m_before": (float(before.high.max() - before.low.min())
                                     if len(before) > 1 else None),
                "range_30m_after": (float(after.high.max() - after.low.min())
                                    if len(after) > 1 else None),
                "recross_within_30m": recross,
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--buckets", nargs="*",
                    default=["0DTE", "1-7DTE", "8-30DTE", "aggregate"])
    ap.add_argument("--target", type=int, default=40, help="crossings needed (spec: 40)")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    config = Config.load()
    tz = ZoneInfo(config.get("capture.timezone", "America/New_York"))
    days = sorted(p.parent.name for p in
                  repo_path("data", "derived").glob("*/frozen_map.parquet"))
    if not days:
        print("no frozen maps found; run run_frozen_map.py first", file=sys.stderr)
        return 2

    print("Q2 — frozen-map descriptive usefulness")
    print(f"  {len(days)} day(s): {', '.join(days)}")
    print("  flip = frozen map, piecewise constant; a crossing is spot moving across")
    print("  a held level, never the level stepping across spot. No significance claims.\n")

    everything: dict[str, list[dict]] = {b: [] for b in args.buckets}
    per_day: list[dict] = []
    for d in days:
        frozen = pd.read_parquet(repo_path("data", "derived", d, "frozen_map.parquet"))
        bars = session_bars(date.fromisoformat(d), tz)
        row = {"date": d, "bars": len(bars)}
        for b in args.buckets:
            cs = crossings_for(bars, flip_segments(frozen, b))
            for c in cs:
                c["date"] = d
            everything[b].extend(cs)
            row[b] = len(cs)
        per_day.append(row)

    print(f"  {'date':>12}" + "".join(f"{b:>11}" for b in args.buckets))
    for row in per_day:
        print(f"  {row['date']:>12}" + "".join(f"{row[b]:>11}" for b in args.buckets))
    print(f"  {'TOTAL':>12}" + "".join(f"{len(everything[b]):>11}" for b in args.buckets))

    # An episode is a run of crossings separated by less than the recross window.
    # With 75-90% recrossing inside 30 minutes, raw crossings are the same
    # excursion counted several times, and n=40 raw is nowhere near 40
    # independent observations. The spec asks for days-to-40; both bases are
    # reported because only one of them answers "when would this be powered".
    def episodes(cs: list[dict]) -> int:
        if not cs:
            return 0
        ordered = sorted(cs, key=lambda c: (c["date"], c["minute"]))
        n = 1
        for a, b in zip(ordered, ordered[1:]):
            if b["date"] != a["date"] or b["minute"] - a["minute"] > WINDOW_MIN:
                n += 1
        return n

    print(f"\n  {'bucket':>10}{'n':>5}{'per day':>9}{'days to n=' + str(args.target):>14}"
          f"{'recross<30m':>13}{'median |30m|':>14}{'range after/before':>20}")
    summary = {}
    n_days = len(days)
    for b in args.buckets:
        cs = everything[b]
        n = len(cs)
        rate = n / n_days
        need = (args.target / rate) if rate else float("inf")
        f = pd.DataFrame(cs)
        rec = f.recross_within_30m.mean() if n else float("nan")
        mv = f.post_cross_move_30m.abs().median() if n and f.post_cross_move_30m.notna().any() else float("nan")
        rr = ((f.range_30m_after / f.range_30m_before).median()
              if n and f.range_30m_before.notna().any() else float("nan"))
        ep = episodes(cs)
        ep_rate = ep / n_days
        summary[b] = {"n": n, "episodes": ep, "per_day": rate,
                      "episodes_per_day": ep_rate,
                      "days_to_target_episodes": None if ep_rate == 0 else args.target / ep_rate,
                      "days_to_target": None if rate == 0 else need,
                      "recross_rate": None if n == 0 else float(rec),
                      "median_abs_move_30m": None if n == 0 else float(mv),
                      "median_range_ratio_after_before": None if n == 0 else float(rr)}
        need_s = "n/a" if rate == 0 else f"{need:.0f}"
        print(f"  {b:>10}{n:>5}{rate:>9.1f}{need_s:>14}"
              f"{'' if n == 0 else f'{rec:>12.0%}'}"
              f"{'' if n == 0 else f'{mv:>13.1f}'}"
              f"{'' if n == 0 else f'{rr:>19.2f}'}")

    print(f"\n  {'bucket':>10}{'raw n':>7}{'episodes':>10}{'ep/day':>8}"
          f"{'days to ' + str(args.target) + ' episodes':>24}")
    for b in args.buckets:
        s_ = summary[b]
        d = s_["days_to_target_episodes"]
        print(f"  {b:>10}{s_['n']:>7}{s_['episodes']:>10}{s_['episodes_per_day']:>8.1f}"
              f"{('n/a' if d is None else f'{d:.0f}'):>24}")

    print("\n  Read: `days to n=40` is what the spec asks for, and the raw count is the")
    print("  wrong denominator for it. Most crossings recross inside 30 minutes, so they")
    print("  are one excursion counted repeatedly; the episode column is the basis on")
    print("  which the study would actually be powered. Both are extrapolations from a")
    print("  handful of sessions in one volatility regime, not forecasts.")

    record = {"days": days, "n_days": n_days, "target": args.target,
              "per_day": per_day, "summary": summary,
              "crossings": {b: everything[b] for b in args.buckets},
              "note": "descriptive only; spec §Q2 forbids significance claims"}
    out = Path(args.json_out) if args.json_out else repo_path("output", "summary", "q2_crossings.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, default=str))
    print(f"\n  record -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
