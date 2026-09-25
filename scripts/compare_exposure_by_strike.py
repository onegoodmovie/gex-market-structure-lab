#!/usr/bin/env python3
"""Q1 structural validation against an optioncharts.io exposure-by-strike export.

Not a calibration. optioncharts' spot, upstream data source and outlier handling
are all undisclosed, so agreement here is evidence that two independent
constructions of the same quantity behave alike — not a licence to tune ours
onto theirs. Nothing this script computes is ever written back into the study's
numbers; §6 is explicitly a diagnostic.

The export carries no date, no expiry, no spot and no timestamp, so §2
identifies the expiry from the data itself and the operator supplies the rest.

    python3 scripts/compare_exposure_by_strike.py \\
        --date 2026-07-29 --time 1400 --expiry 2026-07-29 \\
        --spot 7440.12 --downloaded 14:00 \\
        --csv ~/Downloads/gex_exposure_by_strike_SPX.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from spx_gex.config import Config  # noqa: E402
from spx_gex.exposure import net_gex, usable_for_gex  # noqa: E402
from spx_gex.flip_solver import solve_flip  # noqa: E402
from spx_gex.transmission import read_transmission_spot  # noqa: E402

REQUIRED = ["strike", "call_gex", "put_gex", "net_gex", "gex_profile"]
DOLLARS_PER_ONE_PERCENT = 0.01
MULTIPLIER = 100.0


def repo(*parts: str) -> Path:
    return Path(__file__).resolve().parent.parent.joinpath(*parts)


def implied_gamma(leg: pd.Series, oi: pd.Series, spot: float) -> pd.Series:
    """Their gamma, backed out of published GEX with open interest we hold.

    GEX = gamma · OI · multiplier · S² · 0.01, so this inverts cleanly — and it
    only lands near 1.0 against another source's gamma if the OI, the
    multiplier and the GEX definition all agree. That is the point of it.
    """
    return leg / (oi * MULTIPLIER * spot * spot * DOLLARS_PER_ONE_PERCENT)


def zero_crossing(strikes: np.ndarray, curve: np.ndarray) -> float | None:
    """Linear-interpolated root of a published exposure curve."""
    for i in range(len(curve) - 1):
        a, b = curve[i], curve[i + 1]
        if (a < 0 <= b) or (a > 0 >= b):
            return float(strikes[i] + (strikes[i + 1] - strikes[i]) * (0 - a) / (b - a))
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="study date, YYYY-MM-DD")
    ap.add_argument("--time", required=True, help="capture label to pair against, e.g. 1400")
    ap.add_argument("--expiry", required=True, help="expiry the export covers, YYYY-MM-DD")
    ap.add_argument("--csv", required=True, help="path to the optioncharts export")
    ap.add_argument("--spot", type=float, required=True,
                    help="spot at the moment of download — the file does not carry it")
    ap.add_argument("--downloaded", default=None, help="download clock time, for the record")
    ap.add_argument("--band", type=float, nargs=2, default=(-2.5, 3.0),
                    help="curve comparison window, %% around spot")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    config = Config.load(str(repo("config", "experiment.yaml")))
    r = float(config.require("rates.r"))
    q = float(config.require("rates.q"))
    S = args.spot

    oc = pd.read_csv(args.csv)
    normalized = repo("data", "normalized", args.date, f"{args.time}.parquet")
    if not normalized.exists():
        print(f"FAILED: {normalized} not found. Run normalize_chain.py first.", file=sys.stderr)
        return 2
    frame = pd.read_parquet(normalized)

    print(f"Q1 structural validation — {args.date} {args.time} vs optioncharts.io")
    print(f"  expiry {args.expiry}   spot {S}   "
          f"downloaded {args.downloaded or 'unrecorded'}   r={r} q={q}")

    # --- 1. file structure --------------------------------------------------
    print("\n1. export structure")
    missing = [c for c in REQUIRED if c not in oc.columns]
    if missing:
        print(f"   FAILED: columns missing: {missing}")
        return 2
    # Cent-rounding tolerance, not a relative one. The export rounds all three
    # columns independently to 2dp, so the residual is bounded by ~1.5 cents
    # regardless of magnitude. A relative test (np.allclose default) fails on
    # far-wing strikes whose net is itself a few cents — it did exactly that on
    # T4's 479-strike roll-off export, where the largest residual was 0.01 and
    # the guard still blocked the whole run.
    residual = (oc["net_gex"] - (oc["call_gex"] + oc["put_gex"])).abs()
    legs_add = bool(residual.max() <= 0.05)
    profile = oc.dropna(subset=["gex_profile"])
    print(f"   {len(oc)} strikes {oc.strike.min():.0f}-{oc.strike.max():.0f}   "
          f"net == call+put: {legs_add} (max resid {residual.max():.3f})   "
          f"gex_profile rows: {len(profile)}")
    if not legs_add:
        print("   !! legs do not sum to net — the export's meaning has changed, stop here")
        return 2

    # --- 2. which expiry is this, on the data's own evidence -----------------
    # The file is unlabelled. Backing gamma out against each candidate expiry
    # scores ~1.0 on the right one and nowhere near it on the others, so a
    # mislabelled download is recoverable rather than silently wrong.
    print("\n2. expiry identification (the export carries no expiry)")
    scores: dict[str, float] = {}
    for candidate in sorted(frame["expiration_date"].astype(str).unique())[:8]:
        sub = frame[frame["expiration_date"].astype(str) == candidate]
        calls = sub[sub.option_type == "call"]
        oi = calls.groupby("strike").open_interest.sum()
        gm = calls.groupby("strike").massive_gamma.mean()
        merged = oc.set_index("strike").join(pd.DataFrame({"oi": oi, "gm": gm}))
        merged = merged[(merged.index >= S * 0.99) & (merged.index <= S * 1.02)]
        merged = merged.dropna(subset=["oi", "gm"])
        merged = merged[(merged.oi > 50) & (merged.gm > 0)]
        if len(merged) >= 5:
            scores[candidate] = float(
                (implied_gamma(merged.call_gex, merged.oi, S) / merged.gm).median()
            )
    best = min(scores, key=lambda k: abs(scores[k] - 1.0)) if scores else None
    for candidate, score in scores.items():
        mark = "  <-- best" if candidate == best else ""
        print(f"   {candidate}  gamma ratio {score:>7.4f}{mark}")
    if best != args.expiry:
        print(f"   !! --expiry says {args.expiry} but the data says {best}. "
              "Check the download before trusting anything below.")

    # --- 3. conventions, and the shape of the disagreement -------------------
    # Both legs, and the slope against moneyness — not a median.
    #
    # This section reported only a median, only for calls, over a +-1-2% band,
    # until T2. That hid the finding: the ratio has a strong monotone slope
    # against ln(K/S) on both legs, which is a sigma(K) skew difference between
    # the two upstream sources. A median near 1.0 with p10 at 0.79 is not noise
    # around 1, it is a one-sided tail, and the study's own T0 trap says so.
    j = frame[frame["expiration_date"].astype(str) == args.expiry]
    if j.empty:
        print(f"\nFAILED: no {args.expiry} contracts in the {args.time} capture", file=sys.stderr)
        return 2
    use = usable_for_gex(j)

    print("\n3. conventions — their gamma, backed out with our open interest")
    print(f"   {'leg':>6}{'n':>5}{'median':>9}{'p10':>8}{'p90':>8}"
          f"{'slope vs ln(K/S)':>19}")
    legs: dict[str, pd.DataFrame] = {}
    for typ, col, sign in (("call", "call_gex", 1.0), ("put", "put_gex", -1.0)):
        sub = use[use.option_type == typ]
        stats = sub.groupby("strike").agg(
            oi=("open_interest", "sum"), gm=("massive_gamma", "mean")
        )
        m = oc.set_index("strike")[[col]].join(stats).dropna()
        m = m[(m.oi > 100) & (m.gm > 1e-5)]
        if m.empty:
            print(f"   {typ:>6}{0:>5}{'-':>9}{'-':>8}{'-':>8}{'-':>19}")
            continue
        m["ratio"] = sign * implied_gamma(m[col], m.oi, S) / m.gm
        m["x"] = np.log(m.index.to_numpy(dtype=float) / S)
        m = m[(m.ratio > 0.2) & (m.ratio < 3.0) & (m.x.abs() < 0.04)]
        legs[typ] = m
        slope = float(np.polyfit(m.x.to_numpy(), m.ratio.to_numpy(), 1)[0]) if len(m) >= 8 else float("nan")
        print(f"   {typ:>6}{len(m):>5}{m.ratio.median():>9.4f}{m.ratio.quantile(0.1):>8.4f}"
              f"{m.ratio.quantile(0.9):>8.4f}{slope:>+19.2f}")

    slopes = {
        t: float(np.polyfit(m.x.to_numpy(), m.ratio.to_numpy(), 1)[0])
        for t, m in legs.items() if len(m) >= 8
    }
    # A slope here is NOT evidence of a convention difference on its own. T2 read
    # four negative slopes as a sigma(K) skew difference; T3 gave two positive and
    # two negative on cleaner captures, and the claim was retracted. A convention
    # does not change overnight. Report the numbers; conclude only across days.
    if slopes and all(v < -0.5 for v in slopes.values()):
        print("   -> all slopes negative today. NOT a convention finding on one day "
              "(retracted\n      once already, T2->T3) — check it reproduces before "
              "reading anything into it.")
    elif slopes and all(abs(v) < 0.5 for v in slopes.values()):
        print("   -> no skew tilt today; the two sigma(K) shapes agree")
    else:
        print(f"   -> mixed slopes {slopes}: no consistent sigma(K) tilt today. Also "
              "check the\n      as-of pairing — the slope is not robust to a "
              "15-minute mismatch.")

    combined = pd.concat(legs.values()) if legs else pd.DataFrame()
    if not combined.empty:
        med = float(combined.ratio.median())
        print(f"   both legs: median {med:.4f} — read the slopes above, not this. "
              "A median\n   near 1.0 can sit on top of a large one-sided tail.")

    # --- 4. legs and net ----------------------------------------------------
    ours_c = float(net_gex(use[use.option_type == "call"], S, r, q))
    ours_p = float(net_gex(use[use.option_type == "put"], S, r, q))
    th_c, th_p = float(oc.call_gex.sum()), float(oc.put_gex.sum())
    print("\n4. legs and net, dollars per 1% move")
    print(f"   {'':14}{'call':>10}{'put':>10}{'net':>10}")
    print(f"   {'ours':14}{ours_c/1e9:>+10.3f}{ours_p/1e9:>+10.3f}{(ours_c+ours_p)/1e9:>+10.3f}")
    print(f"   {'optioncharts':14}{th_c/1e9:>+10.3f}{th_p/1e9:>+10.3f}{(th_c+th_p)/1e9:>+10.3f}")
    print(f"   {'gap %':14}{100*(ours_c-th_c)/abs(th_c):>+10.1f}"
          f"{100*(ours_p-th_p)/abs(th_p):>+10.1f}"
          f"{100*((ours_c+ours_p)-(th_c+th_p))/abs(th_c+th_p):>+10.1f}")
    print("   Read the legs, not the net: net is a difference of two large "
          "nearly-equal\n   numbers and inflates whatever the legs disagree by.")

    # --- 5. the whole curve, and the flip -----------------------------------
    lo, hi = S * (1 + args.band[0] / 100.0), S * (1 + args.band[1] / 100.0)
    band = profile[(profile.strike >= lo) & (profile.strike <= hi)]
    grid = band.strike.to_numpy(dtype=float)
    theirs = band.gex_profile.to_numpy(dtype=float)
    ours = np.array([net_gex(use, float(s), r, q) for s in grid])

    # Our flip is solved at OUR capture's own spot, never at the export's
    # download spot. `solve_flip` centres its grid on the spot it is given, and
    # a different phase can step over a narrow sign excursion and return a
    # different root — on T4's roll-off 0DTE the curve crosses zero three times
    # inside 27 points and the reported flip moved 26 points purely from which
    # spot was passed in. The export's spot belongs to the gamma backout and the
    # comparison band, not to our own solver.
    our_spot = next((row.spot for row in read_transmission_spot(args.date)
                     if row.time_label == args.time), S)
    solved = solve_flip(use, our_spot, r, q)
    our_flip = solved["primary_flip"]
    if solved["n_roots"] > 1:
        print(f"\n   !! our curve has {solved['n_roots']} roots "
              f"{[round(x, 1) for x in solved['all_roots']]} — the flip is not "
              "well defined here;\n      the comparison below uses "
              f"{solved['root_selection_rule']}")
    their_flip = zero_crossing(profile.strike.to_numpy(dtype=float),
                               profile.gex_profile.to_numpy(dtype=float))
    print(f"\n5. exposure curve, {len(grid)} points {lo:.0f}-{hi:.0f}")
    print(f"   correlation {np.corrcoef(ours, theirs)[0, 1]:.5f}   "
          f"median |diff| {np.median(np.abs(ours-theirs))/1e9:.3f}B   "
          f"max |diff| {np.max(np.abs(ours-theirs))/1e9:.3f}B")
    print(f"   flip  ours {our_flip:.2f}   optioncharts "
          f"{'n/a' if their_flip is None else f'{their_flip:.2f}'}   "
          f"gap {'n/a' if their_flip is None else f'{our_flip-their_flip:+.2f} pts'}")

    # --- 6. what our filter costs -------------------------------------------
    # §8.2 discipline: the excluded contracts are NOT repaired and NOT written
    # back. Their marginal effect is measured and reported beside the number,
    # so the reported figure stays the one the filter actually produced.
    #
    # The stand-in IV is the same strike's opposite leg — put-call parity, the
    # same relation §3.2 already leans on. It is deliberately not backed out of
    # optioncharts: that would launder the reference into the quantity being
    # compared against it, and gamma is not monotone in sigma so the inversion
    # is two-valued anyway.
    partner = j[j.usable_for_gex].set_index(["strike", "option_type"]).massive_iv
    opposite = {"call": "put", "put": "call"}
    excluded = j[~j.usable_for_gex].copy()
    excluded["parity_iv"] = [
        partner.get((k, opposite[t])) for k, t in zip(excluded.strike, excluded.option_type)
    ]
    recoverable = excluded[excluded.parity_iv.notna()].copy()
    recoverable["massive_iv"] = recoverable["parity_iv"]
    orphan_oi = int(excluded.open_interest.sum() - recoverable.open_interest.sum())

    print("\n6. what the implausible-greeks filter costs  [DIAGNOSTIC — never written back]")
    print(f"   excluded {len(excluded)} contracts, OI {int(excluded.open_interest.sum()):,}")
    print(f"   of which {len(recoverable)} have a usable opposite leg "
          f"(OI {int(recoverable.open_interest.sum()):,});")
    print(f"   {len(excluded)-len(recoverable)} have none and stay unmeasurable "
          f"(OI {orphan_oi:,})")

    impact: dict[str, float] = {}
    if not recoverable.empty:
        both = pd.concat([use, recoverable])
        n0, n1 = float(net_gex(use, S, r, q)), float(net_gex(both, S, r, q))
        f1 = solve_flip(both, S, r, q)["primary_flip"]
        c1 = np.array([net_gex(both, float(s), r, q) for s in grid])
        impact = {
            "net_reported": n0, "net_if_restored": n1, "net_delta": n1 - n0,
            "flip_reported": our_flip, "flip_if_restored": f1,
            "flip_delta": f1 - our_flip,
            "curve_median_abs_diff_reported": float(np.median(np.abs(ours - theirs))),
            "curve_median_abs_diff_if_restored": float(np.median(np.abs(c1 - theirs))),
            "corr_reported": float(np.corrcoef(ours, theirs)[0, 1]),
            "corr_if_restored": float(np.corrcoef(c1, theirs)[0, 1]),
        }
        print(f"   {'':26}{'as reported':>14}{'if restored':>14}{'delta':>10}")
        print(f"   {'net GEX (B)':26}{n0/1e9:>+14.3f}{n1/1e9:>+14.3f}{(n1-n0)/1e9:>+10.3f}")
        print(f"   {'flip':26}{our_flip:>14.2f}{f1:>14.2f}{f1-our_flip:>+10.2f}")
        if their_flip is not None:
            print(f"   {'flip gap vs optioncharts':26}{our_flip-their_flip:>+14.2f}"
                  f"{f1-their_flip:>+14.2f}{f1-our_flip:>+10.2f}")
        print(f"   {'curve median |diff| (B)':26}"
              f"{np.median(np.abs(ours-theirs))/1e9:>14.3f}"
              f"{np.median(np.abs(c1-theirs))/1e9:>14.3f}"
              f"{(np.median(np.abs(c1-theirs))-np.median(np.abs(ours-theirs)))/1e9:>+10.3f}")
        print("   Restoring may improve one of these and worsen another. That is the "
              "reason\n   for reporting it rather than acting on it.")

    record = {
        "date": args.date, "time_label": args.time, "expiry": args.expiry,
        "spot": S, "downloaded": args.downloaded, "csv": str(args.csv),
        "expiry_identification": scores, "expiry_best_match": best,
        "convention_gamma_ratio_median": float(ratio.median()) if len(ratio) else None,
        "ours": {"call": ours_c, "put": ours_p, "net": ours_c + ours_p},
        "optioncharts": {"call": th_c, "put": th_p, "net": th_c + th_p},
        "curve_correlation": float(np.corrcoef(ours, theirs)[0, 1]),
        "curve_median_abs_diff": float(np.median(np.abs(ours - theirs))),
        "our_flip": float(our_flip), "their_flip": their_flip,
        "flip_gap_pts": None if their_flip is None else float(our_flip - their_flip),
        "filter_excluded_contracts": int(len(excluded)),
        "filter_excluded_oi": int(excluded.open_interest.sum()),
        "filter_marginal_impact": impact,
        "note": "structural validation / discrepancy diagnosis — not a calibration; "
                "§6 is a diagnostic and is never written back into study outputs",
    }
    out = Path(args.json_out) if args.json_out else repo(
        "output", "daily", f"q1_{args.date}_{args.time}_{args.expiry}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, default=str))
    print(f"\nrecord -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
