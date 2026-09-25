#!/usr/bin/env python3
"""Capture the day's open-interest base (spec §5.1). Run before 09:30 ET.

OI is published by OCC overnight and does not change intraday, so one fetch a
day is enough — but it must happen, because it cannot be reconstructed later.

    python3 scripts/capture_oi_base.py --date 2026-07-27
    python3 scripts/capture_oi_base.py --date 2026-07-27 --provider synthetic

Exit codes: 0 captured and checks passed, 1 captured but checks failed
(the file is still on disk), 2 nothing was captured.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spx_gex.capture import CaptureRefused, capture, summary_lines  # noqa: E402
from spx_gex.config import Config, ConfigError  # noqa: E402
from spx_gex.providers import ProviderError, get_provider  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD; defaults to today in ET")
    ap.add_argument("--provider", help="override provider.name (e.g. synthetic)")
    ap.add_argument("--config", help="path to experiment.yaml")
    args = ap.parse_args()

    try:
        config = Config.load(args.config)
        tz = ZoneInfo(config.get("capture.timezone", "America/New_York"))
        date_str = args.date or datetime.now(tz).date().isoformat()
        provider = get_provider(config, args.provider)
        print(f"OI base capture — {date_str} via {provider.provider_name()}")
        meta = capture(config, provider, date_str, label="oi_base", kind="oi_base")
    except CaptureRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except (ConfigError, ProviderError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    print("\n".join(summary_lines(meta)))
    if not meta["oi_base_before_deadline"]:
        print(
            "  note: captured after the 09:30 ET deadline. OI itself is unchanged "
            "intraday, so the value is still valid; the deadline is a discipline check."
        )
    return 0 if meta["checks"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
