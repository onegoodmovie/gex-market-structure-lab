#!/usr/bin/env python3
"""Capability probe for the chosen tier (spec §19.1, §5.3).

Answers two questions the config cannot answer on its own:

  1. does this plan return the fields the study needs?
  2. which level of the §5.3 as-of ladder actually hits, and is that
     timestamp a real snapshot stamp or a daily one wearing a disguise?

Computes nothing, stores nothing, writes nothing.

    python3 scripts/probe_provider.py --pages 4 --recheck-after 180

The credential comes from the macOS Keychain at request time (see
config/experiment.yaml -> provider.credential); nothing has to be exported.

Run it *intraday*. After the close every timestamp is overnight-stale and the
variation check cannot distinguish a frozen stamp from a quiet market.

The delay is NOT probed — on Massive Options Starter it is a known tier
property (15 min) held in config.provider.is_delayed, and the field that would
have reported it (`last_quote.timeframe`) does not exist on this plan.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from spx_gex.capture import (  # noqa: E402
    CRITICAL_FIELDS,
    DEFAULT_ASOF_PRIORITY,
    NTM_CRITICAL_FIELDS,
    resolve_asof,
    run_checks,
)
from spx_gex.config import Config, ConfigError  # noqa: E402
from spx_gex.providers import ProviderError, get_provider  # noqa: E402


def _fetch(provider, config):
    return provider.fetch_chain_with_meta(
        config.require("provider.underlying_ticker"), datetime.now(timezone.utc)
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pages", type=int, default=4, help="page cap; probe, not capture")
    ap.add_argument(
        "--recheck-after",
        type=int,
        default=0,
        metavar="SECONDS",
        help="re-fetch after N seconds and check the as-of stamp actually moved. "
        "This is the §5.3 intraday-variation test, run before day 1 instead of "
        "discovering it on the second capture. 180+ recommended.",
    )
    ap.add_argument("--provider", help="override provider.name")
    ap.add_argument("--config", help="path to experiment.yaml")
    ap.add_argument("--show-raw", action="store_true", help="print one raw contract")
    args = ap.parse_args()

    try:
        config = Config.load(args.config)
        config = config.override("provider.max_pages", args.pages)
        config = config.override("provider.allow_truncated_pagination", True)
        provider = get_provider(config, args.provider)
        underlying = config.require("provider.underlying_ticker")
        print(
            f"Probing {provider.provider_name()} "
            f"({config.get('provider.plan', 'unknown')} tier) for {underlying}, "
            f"DTE <= {config.require('capture.expiry_scope_max_dte')}, "
            f"up to {args.pages} page(s)\n"
        )
        result = _fetch(provider, config)
    except (ConfigError, ProviderError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    frame, meta = result.frame, result.meta
    checks = run_checks(frame, config)
    n = len(frame)

    print(f"Contracts returned : {n:,}  (across {meta.pages} page(s))")
    print(f"Roots              : {checks['roots']}")
    if checks["missing_roots"]:
        print(f"  !! missing root(s): {checks['missing_roots']} — AM-settled SPX "
              "monthlies matter for T (§6.2)")
    print(
        f"Distinct expiries  : {checks['distinct_expiries']}   "
        f"nearest={checks['nearest_expiry']}  furthest={checks['furthest_expiry']}"
    )

    # --- field coverage ------------------------------------------------------
    print("\nField coverage (share of contracts with a non-null value)")
    print("-" * 70)
    ntm_cov = checks["near_the_money_coverage_pct"]
    for field, pct in checks["field_coverage_pct"].items():
        if field in NTM_CRITICAL_FIELDS:
            band = ntm_cov[field]
            mark = "OK " if band > 95 else ("LOW" if band > 0 else "GONE")
            print(
                f"  [{mark}] +{pct:6.1f}%  {field:<30} {NTM_CRITICAL_FIELDS[field]}\n"
                f"              {band:6.1f}% within ±{checks['near_the_money_band_pct']:.0f}%"
                f" of spot  <- this is the gated number"
            )
            continue
        mark = "OK " if pct > 95 else ("LOW" if pct > 0 else "GONE")
        why = CRITICAL_FIELDS.get(field) or "advisory"
        critical = " *" if field in CRITICAL_FIELDS else "  "
        print(f"  [{mark}]{critical}{pct:6.1f}%  {field:<30} {why}")
    for field, reason in checks["not_applicable_on_this_plan"].items():
        print(f"  [N/A]  {'':6}   {field:<30} {reason}")
    print(
        "  (* chain-wide gate at 95%;  + gated near the money only — providers "
        "legitimately\n   drop greeks on deep ITM, see §7)"
    )
    if checks["unexpectedly_present"]:
        print(
            f"\n  !! this plan returned fields it was not expected to: "
            f"{checks['unexpectedly_present']}\n"
            "     Good news, but revisit the tier assumptions in config before "
            "relying on them."
        )

    # --- §5.3 as-of ladder ---------------------------------------------------
    delay_seconds = int(float(config.require("provider.delay_minutes")) * 60)
    priority = list(config.get("asof_resolution.priority", DEFAULT_ASOF_PRIORITY))
    max_span = float(config.get("asof_resolution.max_span_minutes", 60.0))
    asof = resolve_asof(
        frame, datetime.now(timezone.utc), priority, delay_seconds, max_span
    )

    print("\nAs-of resolution (§5.3)")
    print("-" * 70)
    for level, field in enumerate(priority, start=1):
        present = field in frame.columns and frame[field].notna().any()
        hit = "<-- USED" if asof["asof_source_level"] == level else ""
        rejected = next(
            (r for r in asof.get("asof_rejected_levels", []) if r["level"] == level), None
        )
        state = "REJECTED" if rejected else ("present" if present else "absent ")
        print(f"  level {level}  {state:8} {field:<34}{hit}")
        if rejected:
            print(f"            -> {rejected['reason']}")
    inferred_mark = "<-- USED" if asof["asof_is_inferred"] else ""
    print(f"  level 4  fallback  request_time - {delay_seconds}s{'':17}{inferred_mark}")
    print(f"\n  resolved asof    : {asof['asof_utc']}")
    if asof["asof_is_inferred"]:
        print("  !! INFERRED, not provider data. Every T computed from it carries the")
        print("     full uncertainty of the assumed 15-minute delay.")
    else:
        age = (
            datetime.now(timezone.utc) - datetime.fromisoformat(asof["asof_utc"])
        ).total_seconds() / 60.0
        print(f"  age right now    : {age:.1f} min")
        print(f"  expected on this tier: ~{delay_seconds / 60:.0f} min")

    delayed = config.require("provider.is_delayed")
    print(f"\n  provider.is_delayed: {delayed}  (config fact, not probed)")

    # --- the check that separates a timestamp from a usable timestamp --------
    if args.recheck_after > 0:
        print(f"\n  re-fetching in {args.recheck_after}s to test intraday variation ...")
        time.sleep(args.recheck_after)
        try:
            second = _fetch(provider, config)
        except ProviderError as exc:
            print(f"  recheck FAILED: {exc}", file=sys.stderr)
            return 2
        asof2 = resolve_asof(
            second.frame, datetime.now(timezone.utc), priority, delay_seconds, max_span
        )
        print(f"  first  : {asof['asof_utc']}")
        print(f"  second : {asof2['asof_utc']}")
        if asof["asof_is_inferred"] or asof2["asof_is_inferred"]:
            print("  -> already on the inferred level; nothing to compare.")
        elif asof["asof_utc"] == asof2["asof_utc"]:
            print(
                f"  !! IDENTICAL after {args.recheck_after}s. "
                f"`{asof['asof_source_field']}` is a daily-granularity stamp and is\n"
                "     meaningless for a snapshot. Captures will degrade themselves to\n"
                "     level 4 automatically, but know that going in: T becomes an\n"
                "     assumption everywhere, not a measurement."
            )
        else:
            moved = (
                datetime.fromisoformat(asof2["asof_utc"])
                - datetime.fromisoformat(asof["asof_utc"])
            ).total_seconds()
            print(f"  -> moved {moved:.0f}s. Usable snapshot timestamp.")
    else:
        print(
            "\n  intraday variation NOT tested. Pass --recheck-after 180 to settle it\n"
            "  now; otherwise the first same-day pair of captures will."
        )

    spot = (meta.underlying_asset or {}).get("price")
    if spot:
        print(f"\nUnderlying price   : {spot}")
        if "details.strike_price" in frame.columns and "greeks.gamma" in frame.columns:
            strikes = pd.to_numeric(frame["details.strike_price"], errors="coerce")
            deep = frame[(strikes - spot).abs() / spot > 0.10]
            if len(deep):
                have = int(deep["greeks.gamma"].notna().sum())
                print(
                    f"Deep (>10% from S) : {have}/{len(deep)} have gamma "
                    f"({100.0 * have / len(deep):.0f}%)"
                )
                print("  (Vendors drop greeks on deep ITM — this shapes the §7 "
                      "24-contract sample.)")
    else:
        print("\nUnderlying price   : NOT RETURNED — you have transmission_spot.csv "
              "anyway (§5.4)")

    for note in meta.notes:
        print(f"\nnote: {note}")

    print("\n" + "=" * 70)
    if checks["passed"]:
        print("VERDICT: all critical fields present. This tier can run the experiment.")
        print("NOTE: no quotes on Starter -> §7 Path B is N/A by tier, not a failure.")
        print("      Path A (massive_iv -> our gamma) is unaffected and is the hard gate.")
    else:
        print("VERDICT: NOT SUFFICIENT on this plan.")
        for field in checks["missing_critical_fields"]:
            why = CRITICAL_FIELDS.get(field) or NTM_CRITICAL_FIELDS.get(field, "")
            print(f"  missing/sparse -> {field}  ({why})")
        for root in checks["missing_roots"]:
            print(f"  root absent    -> {root}")
    print("=" * 70)

    if args.show_raw and n:
        print("\n--- one contract, flattened, verbatim ---")
        print(json.dumps(frame.iloc[0].to_dict(), indent=2, default=str)[:2000])

    return 0 if checks["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
