# Handoff — state at end of the study (T4, 2026-07-31)

Read this first in a fresh session. It exists so decisions already made are not
re-litigated and findings already established are not re-derived.

**The manual phase is over.** Conclusions: `docs/FINAL_REPORT.md`. Design and
the reasons behind it: `docs/FRAMEWORK.md`. Q4 is a manual field left for the
operator to fill retroactively. Capture keeps running — six agents, unlimited
calls, and every milestone above capture recomputes from stored Parquet.

---

## Two different outside companies — check which one before reading on

| | **Massive Options** | **optioncharts.io** |
|---|---|---|
| Role | data source | third-party reference |
| Gives | raw chain: IV, greeks, OI | published netGEX, flip, walls |
| Publishes GEX? | **no, never has** | yes — that is what it is for |
| Named in code as | `massive_iv`, `massive_gamma`, `massive_vega` | `optioncharts_reference.csv`, `reference_net_gex` |
| Feeds | Path A, the expiry offset, put-call IV | Q1 only |

Path A and the expiry offset measure us against **Massive**. Q1 measures us
against **optioncharts**. Nothing measured against one carries over to the other
without an argument. This produced one wrong conclusion — T1 §6 — before the
names were split.

**The word "vendor" no longer appears anywhere in the code, config or tests, and
should not be reintroduced.** Three prefixes replaced it on T1: `massive_*` for
the data source, `reference_*` for optioncharts, and `provider_*` for the
plumbing that is genuinely provider-agnostic (synthetic included). The day
summaries and `SPEC.md` keep the old names because they are dated records.

## The study ends Friday 2026-07-31, at 5 days not 15

Decided T2. Reasoning in `docs/WIND_DOWN.md`; `config/experiment.yaml` carries
`target_valid_days: 5` with `target_valid_days_original: 15` beside it. In
short: Q1 and Q3 are answered and would not become truer with more days, and the
two things still open are **mechanism** questions that a larger sample cannot
settle. Friday is not a round number — it carries the largest weekly roll-off in
the window, the condition the frozen map has never run across.

**Capture does not stop.** All six agents keep firing; unlimited calls, ~1 MB a
day, and every milestone above M0 recomputes from stored Parquet. What stops is
the manual download and the daily analysis pass. Same rule as day one: code can
be written later, a chain cannot be captured later.

**Double-download stop condition — never met, closed unresolved.** It was to run
until three consecutive days confirmed the 14:00 export carried 14:00 data. T2
discriminated strongly, T3 weakly, **T4 not at all** — its two expiries pointed
opposite ways. So optioncharts' true as-of stays an **inference, not an
observation**, and every Q1 number carries an as-of uncertainty of up to fifteen
minutes. That is written into the report's premises rather than left implicit.

**Deliverables, done:** `docs/FINAL_REPORT.md` (Q1–Q3 and the retractions; Q4 is
the operator's, to be filled retroactively) and `docs/FRAMEWORK.md` (design and
the reasoning behind it).

## Purpose (this framing decides several things below)

The study produces an **observation framework**, not numbers meant to replace
optioncharts'. Consequences, all deliberate:

- Q1 is **structural validation / discrepancy diagnosis** — **provisional
  naming, decided T1.** It was "a smoke test" while it compared four headline
  numbers off a screenshot. The exposure-by-strike export made it 244 strikes,
  a full exposure curve and a verified convention check, which is more than a
  smoke test. It is **not** a calibration and must not be called one:
  optioncharts' spot, upstream data source and outlier handling are all
  undisclosed. Settle the final name after the study ends, on the evidence.
- **Q2 is answered** (`scripts/run_q2_crossings.py`, five days). It had appeared
  in no day summary and would have been an empty section in the report:

  | bucket | raw n | /day | days to 40 | **episodes** | **days to 40 episodes** | recross <30m |
  |---|---|---|---|---|---|---|
  | 0DTE | 32 | 6.4 | 6 | **6** | **33** | 81% |
  | 1-7DTE | 17 | 3.4 | 12 | 2 | 100 | 88% |
  | 8-30DTE | **0** | 0 | n/a | 0 | n/a | — |
  | aggregate | 19 | 3.8 | 11 | 4 | 50 | 79% |

  **~80% of crossings reverse within 30 minutes**, so raw counts are the wrong
  denominator: 0DTE needs about **33 trading days** to reach 40 independent
  episodes, not six. 8-30DTE recorded zero crossings in five days.
- Path A is a **diagnostic**, not a gate
- Massive's and optioncharts' conventions are **documented, not matched**
- The two months of prior OptionCharts records are **not** being continued

## Built

| Milestone | State |
|---|---|
| M0 — capture | done, running on schedule |
| M1 — normalize / greeks / validation | done; Path A is a diagnostic |
| M2 — frozen map | done, runs on live data |
| M3 — attribution | **done**; §12 identity gate passes on every day |
| M4 — reporting | `FINAL_REPORT.md` + `FRAMEWORK.md` written; no `build_daily_report.py` |

96 tests pass. `python3 -m pytest tests -q`.

**Five study days captured, T0 2026-07-27 … T4 2026-07-31 — the window is
complete.** Per-day findings in `docs/T0_SUMMARY_2026-07-27.md` …
`T4_SUMMARY_2026-07-31.md`.

## Automated (six LaunchAgents, Mon–Fri)

| Agent | Fires | Does |
|---|---|---|
| `com.spxgex.oi_base` | 09:25 | OI base capture |
| `com.spxgex.surface0945` | 10:00 | surface, target as-of 09:45 |
| `com.spxgex.surface1030` | 10:45 | surface, target as-of 10:30 — **added T1** |
| `com.spxgex.surface1400` | 14:15 | surface, target as-of 14:00 |
| `com.spxgex.surface1545` | 16:00 | surface, target as-of 15:45 |
| `com.spxgex.spot` | 16:05 | Yahoo 1-min spots into `transmission_spot.csv` |

**A capture's label is its as-of, never its download time.** `surface1400`
fires at **14:15** and the data it carries is as-of **14:00**; the label is the
instant, the fire time is the instant plus `provider.delay_minutes`. Verified in
every `.meta.json`: T2's 1400 capture has `request_started_utc` 14:15:02 ET and
`asof_utc` 14:00:13 ET. This trips people up when pairing against an outside
source — pair on the as-of, not on the clock, or you compare two different
minutes (T2 §4d: fifteen minutes is worth ~8 flip points).

cron **cannot** run this (no login-keychain access), and the agents exec
python3 directly because TCC blocks `/bin/bash` from `~/Documents`. Both
verified empirically; see README.

## The daily manual step — ENDED 2026-07-31

Kept for reference in case the study is resumed. Nothing below is running now;
the six capture agents are, and need nothing by hand.

1. **The optioncharts exposure-by-strike CSV — two expiries, 0DTE and 1DTE.**
   Found on T1. 244 strikes with `call_gex`, `put_gex`, and `gex_profile`,
   which is the full exposure curve, not a summary. Curve correlation against
   our own runs 0.993–0.9998. Take 1DTE as well as 0DTE: 0DTE is the noisiest
   slice and 1DTE is the control. **T2 added a third: take the export at two
   download times 15 minutes apart** — that is what pinned the as-of on their
   side (§4b), and T3 confirmed it: paired against their 14:00 export our 1DTE
   flip matched to **−0.00 pts**. **Record the spot at
   download time** — the file carries no spot, no expiry and no timestamp, and
   rename it with date / time / expiry on save. (An unlabelled file is still
   recoverable: back gamma out with `call_gex / (OI · 100 · S² · 0.01)` and the
   right expiry scores ~1.00 against Massive's gamma while every other scores
   0.56–6.28.)
2. **Take each export twice — at 14:00 and at 14:15.** This is not redundancy.
   It is the only thing that pins the as-of on their side, and T2 showed a
   15-minute mispairing is worth ~8 flip points, the same size as the effect
   under investigation. Note the dashboard's headline flip at each download too;
   it equals the curve zero and confirms which snapshot you got.

Then, per expiry:

```bash
python3 scripts/compare_exposure_by_strike.py --date YYYY-MM-DD --time 1400 \
    --expiry YYYY-MM-DD --spot <spot at download> --downloaded HH:MM --csv <path>
python3 scripts/run_q2_crossings.py        # re-run after each day's frozen map
```

## Settled decisions — do not reopen without a reason

- **r = 0.036906, q = 0.013903**, continuous, ACT/365. Converted from SOFR
  3.64% (ACT/360 simple → ×365/360) and a 1.40% simple dividend yield
  (→ ln(1+q)). ±50bp moves the flip **under half a point**, which is why the
  values are declared rather than solved for.
- **`massive_expiry_offset_hours` stays null; investigation closed.** The offset
  is real but the UTC-end-of-day explanation was refuted: AM-settled contracts
  show +3.33 h where that theory predicts +10.5 h. It cancels in Q3's
  difference, so it does not matter. **T1 narrowed the shape**: dT is ~3.5 h at
  0–2 DTE and reproduces across days to within 6 minutes there, then decays
  with tenor and is not reproducible past a week. The all-expiry median (3.38 /
  3.33 / 2.80 across three readings) moves mainly with which expiries the chain
  carries, so read it per bucket, never as one scalar.
- **Never backfill or repair an anomalous contract to make the two sides
  agree.** The reported number stays the one the filter actually produced.
  Measure what the filter costs and report it *beside* the number, never inside
  it. `compare_exposure_by_strike.py` §6 does this and is labelled DIAGNOSTIC in
  its own output. T1 showed exactly why: restoring the filtered Jul 29 puts
  improved the curve fit (median |diff| 0.494B → 0.336B) and **worsened** the
  flip (gap +0.18 → +1.47 pts). A repair chosen to fix one would have quietly
  broken the other. The stand-in IV is the same strike's opposite leg — put-call
  parity — deliberately **not** backed out of optioncharts, which would launder
  the reference into the quantity being tested against it, and which is
  two-valued anyway because gamma is not monotone in σ.
- **Net GEX is mechanical; the flip is not** — M3's headline, on **four days ×
  two windows × four buckets = 32 rows**, across a fall, a rise, a fall and a
  rise:

  | net GEX, mechanical share | |
  |---|---|
  | min | 62% |
  | **median** | **117%** |
  | max | 201% |
  | surface **opposes** the move | **27 of 32 rows** |

  Net GEX moves on spot and time; surface repricing damps it. The flip is the
  mirror — its mechanical term is *pure time decay*, because the flip does not
  depend on spot at all.

  **Where the damping is exact and where it is absent** (T3 sharpened this):

  | bucket | surface opposes |
  |---|---|
  | 0DTE | **8 / 8** |
  | 1-7DTE | **8 / 8** |
  | 8-30DTE | **4 / 8** |
  | aggregate | 7 / 8 |

  The damping is exact where the surface term is large, and coin-flip in
  8-30DTE where it is small (|surf| 0.23–1.47B against totals of 3.4–13.4B).
  Read that as "the 8-30DTE surface term is near zero plus noise", not as a
  mechanism.
- **M3's two non-negotiable invariants.** One contract set across all four
  states (the usable set moves between captures — on T1 the unconstrained state
  D differed by −304% of a row's total change), and one bucket policy across
  all four (§9.1's 0DTE rules are label-keyed and would fail the identity on
  membership). Both are tested.
- **optioncharts' *arithmetic* conventions are verified** (T1 §6b): same open
  interest, multiplier 100, same GEX definition (dollar gamma per 1% move).
  A σ(K) skew difference was claimed on T2 and **retracted on T3** (item 5) —
  it did not reproduce, and the day it appeared was the day the paired capture
  was degraded. `compare_exposure_by_strike.py` §3 reports both
  legs with p10/p90 and the slope against `ln(K/S)`; it computed calls only,
  over too narrow a band, until T2. Never read that section's median alone.
- **Path A cannot reach 1%** — Massive prices on a CRR tree with
  finite-difference vega; this is a cross-model comparison.
- **0DTE gamma cannot be validated on this tier at all.** A 10-point spot
  uncertainty is 6% gamma error at 6h to expiry and 75% at 15 minutes, and
  Massive publishes no spot.
- **Every T in the study is inferred** (`request_time − 15 min`). Levels 1–3 of
  the as-of ladder are all unusable on this tier. **But the 15 minutes is now
  verified (T2 §4a)**, on evidence internal to our own data: the §7.2 joint
  (S, T) fit recovers an implied spot from Massive's gamma curve, and on a day
  with three spot direction changes the assumed delay gives a total absolute
  error of 18.0 points against 79.2 for 30 minutes and 56.4 for zero. The
  ladder is still level 4; the number it uses is no longer unchecked. Re-run
  this on any day the spot reverses more than once. **Massive's own
  subscription page states "15-minute Delayed Data" for Options Starter**, so
  this leg now has two independent confirmations: the provider's own statement
  and our internal fit.
- **optioncharts' exposure page is effectively live** despite its "15 min
  delay" label (T2 §4b) — inferred, not observed, so re-tested daily by taking
  the export at both 14:00 and 14:15. Pair our 14:00 capture with their **14:00**
  download. Their dashboard headline flip is exactly the `gex_profile` zero, so
  headline readings and curve readings are one series.

## Traps that already cost time — do not fall in again

- **The median hides the thing that matters.** A correcting expiry offset moves
  Path A's median 1.68% → 1.84% while p90 collapses 78.8% → 6.4%. Long-dated
  contracts dominate the median. Always read per bucket, median *and* p90.
- **A gamma-only fit cannot separate σ from T.** Near the money gamma depends on
  them only through `σ√T`. Fitting T against gamma produced a convincing,
  reproducible, entirely spurious "day-count effect". Use
  `vega / gamma = S²·σ·T`, which breaks the degeneracy.
- **The gamma peak is not the spot.** The 1/σ factor plus a downward smile puts
  the apparent peak 1–4 points above S.
- **The flip carries no information about spot handling, and agreement on it is
  not agreement on S.** Under sticky strike the observation spot only sets the
  solver's grid window, so the root is invariant — exactly 0.000 pts per point,
  at every tenor. Any hypothesis that explains a flip discrepancy by a spot
  difference is dead on arrival. It *does* respond to T (0.24–1.15 pts/h,
  non-monotone in tenor), so a flip comparison is a T test, not an S test.
- **Both outside sources can be transiently wrong — Massive's feed as well as
  the optioncharts screenshot.** On T0 the 15:45 optioncharts row was a 100×
  outlier between two consistent readings; it is quarantined, and the
  discipline is a second reading ~20 min later. On T1 *Massive* did it: at
  09:45 a 152,767-OI put
  reported IV 0.17% and negative gamma, sane on all five surrounding captures.
  91% of the OI flagged implausible at 09:45 is fine again by 14:00. Before
  reading a normalization failure as degradation, diff the flag set against a
  later capture.
- **Net GEX is a difference of two large nearly-equal numbers.** On T1 the call
  leg matched optioncharts to 8% and the net to 61%. Always report the legs
  beside the net, or a good result reads as a bad one.

## Open

1. `q` quoted range is 1.09–1.40%; 1.40% is used. 31bp, inside the sensitivity.
2. Expiry offset unexplained (closed by choice, not by answer).
3. ~2% uniform residual in Path A after the offset — a scale effect.
4. Vendor row basis: use the **single-expiry row**, never the header aggregate.
   Settled in practice on T1; keep doing it.
5. **The 0DTE flip gap — NOT a bias. Retracted T3.** Four same-instant
   readings: **+10.98, +7.44, +5.09, −16.96** — mean +1.64, sd 12.63, range
   27.94. Three same-sign readings looked like a systematic effect at p = 12.5%;
   the fourth dissolved it. There is no bias to explain.

   **The σ(K) skew explanation is retracted with it.** T2 measured the gamma
   ratio's slope against `ln(K/S)` as negative on all four leg×tenor
   combinations and read that as a convention difference. T3 gives **+4.52 /
   −1.78 (call) and −1.82 / +3.75 (put)** — two positive, two negative. A
   convention does not change overnight, so the T2 slope was not one.

   **And the direction of the evidence is the wrong way round**: T2's paired
   1400 capture *failed* the NTM gate and had an implied spot 12 points off,
   while T3's five captures were all clean — the tidy result came from the bad
   day and the mess from the good one. Most likely T2's slope was an artifact
   of that capture's moneyness-dependent greek degradation.

   **What is actually established, and it is the stronger result:** the **1DTE**
   gap is **+0.18, −0.55, −0.00** across three days and three measurement
   routes — sd 0.38, range 0.73, and T3 is an exact match to two decimals. Two
   independent constructions of the same quantity agree to under a point
   wherever the quantity is stable.

   0DTE is not stable. optioncharts' own two exports fifteen minutes apart on
   T3 put the 0DTE curve zero at **7395.89 and 7419.23 — 23 points — while spot
   moved 1.18**. The spec said from the start that 0DTE gamma cannot be
   validated on this tier. That was right, and three days of coincident signs
   plus one day's slope were enough to talk me out of it. **Do not reopen.**
6. **Frozen-map base — REFRAMED (T2 §2, §3).** The question was "is 09:45
   safe", on the theory that it is uniquely exposed to the unsettled open. **It
   is not.** T1 degraded at the open and healed; T2 was clean at the open and
   degraded through the session — 411 contracts clean at 09:45 were flagged by
   14:00, the exact mirror of T1. No capture is safe; a different base
   relocates the problem rather than avoiding it. The useful question is how
   large the assumption is, and the first comparison answers it: **a base 45
   minutes later moves the flip 14 to 18 points.**

   ```bash
   python3 scripts/run_frozen_map.py --date YYYY-MM-DD --compare-base 1030
   ```

   Keep 09:45 as the written base. Report the assumption; do not try to remove
   it. And do not read the difference as the transient — spot fell 48 points
   between the two bases on T2, so most of it is genuine surface movement.
7. **Does optioncharts publish an as-of timestamp anywhere?** The export
   carries none — no date, no expiry, no spot, no time — and the page shows
   only a delay label, so the study's as-of for it is inferred exactly the way
   ours is. If a real timestamp exists, it converts item 5 from unanswerable to
   answerable. Nothing else on the horizon does.
8. **CLOSED (T2 §5c-ii): the degradation is not wandering, it is deterministic
   moneyness migration.** The 411 contracts newly flagged between 09:45 and
   14:00 sit at median |ln(K/S)| = 0.070, strictly between the always-clean core
   (0.040) and the always-bad deep wing (0.185), and **99% are in-the-money**.
   Spot moved 54 points intraday; contracts crossed a fixed failure boundary in
   the order their moneyness predicts. The NTM gate fires on days spot moves.
   That is a property of the chain, not a defect in the gate — do not loosen it.
9. **The 8-30DTE "exception" — DISSOLVED T3.** Claimed on T2 at 4 of 4 rows
   where the surface *added* to the move instead of damping it. T3's two rows
   both damp, making it **4 of 8** — a coin flip. There is no exception, and the
   vol-drift mechanism tested and rejected on T2 was a mechanism for something
   that is not there. What survives is sharper: the damping is **8/8 in both
   0DTE and 1-7DTE**, and absent in 8-30DTE where the surface term is small and
   sign-random.
