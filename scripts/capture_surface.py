#!/usr/bin/env python3
"""Capture one intraday IV-surface snapshot (spec §5.2).

`--time` names the **target as-of** — the instant the snapshot should carry,
not when the request fires:

    0945  surface base for the frozen map      downloaded 10:00 ET
    1400  attribution t1, paired with the      downloaded 14:15 ET
          optioncharts screenshot (§8.1)
    1545  attribution t2                       downloaded 16:00 ET

The download times are target + provider.delay_minutes. They are derived, never
configured — change the tier and they move by themselves.

    python3 scripts/capture_surface.py --date 2026-07-27 --time 0945

Never run pre-market: SPX/SPXW quotes outside regular hours are too thin to
anchor a day (§5.2).

Exit codes: 0 captured and checks passed, 1 captured but checks failed
(the file is still on disk), 2 nothing was captured.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spx_gex.capture import (  # noqa: E402
    CaptureRefused,
    capture,
    summary_lines,
    target_asof_et,
)
from spx_gex.config import Config, ConfigError  # noqa: E402
from spx_gex.providers import ProviderError, get_provider  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--time", required=True, help="target as-of label, e.g. 0945 (not the clock time)"
    )
    ap.add_argument("--date", help="YYYY-MM-DD; defaults to today in ET")
    ap.add_argument("--provider", help="override provider.name (e.g. synthetic)")
    ap.add_argument("--config", help="path to experiment.yaml")
    args = ap.parse_args()

    try:
        config = Config.load(args.config)
        allowed = list(config.require("capture.target_asof_times"))
        if args.time not in allowed:
            print(
                f"REFUSED: --time must be one of {allowed}; got {args.time!r}. "
                "Adding a time changes the study design, not just a filename.",
                file=sys.stderr,
            )
            return 2
        tz = ZoneInfo(config.get("capture.timezone", "America/New_York"))
        now_et = datetime.now(tz)
        date_str = args.date or now_et.date().isoformat()
        provider = get_provider(config, args.provider)

        # The window ends after the close on purpose: the 15:45 target is
        # downloaded at 16:00, and on a delayed tier that request returns
        # pre-close data. The guard is about what the data covers, not about
        # when the button was pressed.
        delay = float(config.require("provider.delay_minutes"))
        latest = (
            datetime.combine(now_et.date(), time(16, 0), tzinfo=tz)
            + timedelta(minutes=delay + 15)
        ).strftime("%H:%M")
        if provider.provider_name() != "synthetic" and not (
            "09:30" <= now_et.strftime("%H:%M") <= latest
        ):
            print(
                f"REFUSED: {now_et:%H:%M} ET is outside the capture window "
                f"(09:30–{latest}). §5.2 forbids capturing an IV surface pre-market.",
                file=sys.stderr,
            )
            return 2

        due = target_asof_et(config, date.fromisoformat(date_str), args.time) + timedelta(
            minutes=delay
        )
        print(
            f"Surface capture — {date_str} target asof {args.time} ET "
            f"(download due {due:%H:%M} ET) via {provider.provider_name()}"
        )
        meta = capture(config, provider, date_str, label=args.time, kind="surface")
    except CaptureRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except (ConfigError, ProviderError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    print("\n".join(summary_lines(meta)))

    drift_limit = float(config.get("capture.download_drift_warn_minutes", 5.0))
    drift = meta.get("download_drift_minutes")
    if drift is not None and abs(drift) > drift_limit:
        print(
            f"  !! download fired {drift:+.1f} min from its derived time. Check cron."
        )
    delta = meta.get("asof_vs_target_minutes")
    if delta is not None and abs(delta) > drift_limit:
        print(
            f"  !! this snapshot carries an asof {delta:+.1f} min from the {args.time} "
            "target. §8.1 pairs the 14:00 row with a optioncharts screenshot taken at that "
            "minute; a wide gap breaks the pairing and the row drops out of Q1."
        )
    return 0 if meta["checks"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
