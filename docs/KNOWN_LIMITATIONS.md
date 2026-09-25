# Known limitations

What this measure cannot see. None of these are bugs, and none of them are
fixable inside the study design — they bound what the results can be used to
say.

---

## 1. Open interest is frozen intraday

OI is published by OCC overnight. Every intraday number in this study is
computed against the **previous** session's confirmed OI. New 0DTE opens,
closes, rolls, spreads, and dealer inventory changes are all invisible until
the next overnight publication.

So the computed quantity is precisely:

> a same-day exposure proxy conditioned on the previous session's confirmed OI.

Contract volume is captured in every snapshot as the only observable proxy for
this blind spot. It is unsigned: **the direction of new dealer inventory can
never be inferred from it.** Volume-based flags are diagnostic only.

---

## 2. The dealer sign convention is an assumption

Dealers are assumed long calls and short puts relative to customers. That is
the standard naive convention and it is what makes "net GEX" a number at all.
It is not a measurement of anyone's book, and no part of this study can
validate it. Every output row carries `sign_convention` so no downstream reader
can forget.

---

## 3. The data is 15 minutes delayed, always

Massive Options Starter is a delayed tier. This is a fixed property of the
data, not a condition that sometimes applies.

The study handles it by **shifting the download clock**, not by tolerating
stale numbers: each snapshot is downloaded `delay` after the instant it is
meant to carry (09:45 → 10:00, 14:00 → 14:15, 15:45 → 16:00). The captures
therefore carry on-target data, and the residual risk moves from "the data is
old" to "the delay is not what we think it is."

That residual is measured, not assumed. Every capture matches its underlying
price against `transmission_spot.csv` and reports the implied lag. See §3c for
what that measurement can actually resolve.

**The 15:45 0DTE layer is no longer excluded for delay reasons.** It is kept,
computed, and flagged `not_cross_day_comparable` because of §9.1's `1/√T`
instability — a property of the maths, not of the feed — and held out of the
`aggregate` bucket so a non-comparable number cannot contaminate a comparable
one. The 16:00 row has no 0DTE layer at all, because at settlement those
contracts do not exist.

## 3a. No quotes, and what that costs

Starter returns no `last_quote` and no `last_trade`. Three consequences, none
of them silent:

- **No bid, ask, or mid.** Spec §19.3 (which mid definition to use) is void —
  there is nothing to choose between.
- **Two §6.4 filters cannot run.** Crossed-market rejection (`bid > ask`) and
  the zero-bid flag have no data to act on. They are recorded as
  `not_applicable_on_this_plan` in every day's data-quality record rather than
  removed, so the absence is a visible decision.
- **§7 Path B is impossible.** IV cannot be inverted from a mid that does not
  exist. Path B is marked N/A with its reason; this is a recorded fact, not a
  failed gate.

The last one is the one that actually costs something. Path A compares our
gamma against the vendor's gamma using *the vendor's own IV* as the input to
both — it is a consistency check, and it cannot see a systematic error in that
IV. Path B was the only check in the design fed by an independent input.

The replacement is the put-call IV consistency check (METHODOLOGY §3.2):
regress `iv_call - iv_put` on `ln(K/S)` and look for a systematic tilt. It uses
no external data — only a relation the vendor's own numbers must satisfy — so
it is genuinely independent of Path A, and it gives the §7.2 grid search a
second criterion instead of leaving it as an unconstrained fit.

It is narrower than Path B was. It can detect a misspecified `r - q`; it cannot
detect an IV surface that is internally consistent and simply wrong.

## 3a-bis. Verified against the live API, 2026-07-27

A full-chain probe (12,778 contracts, 52 pages, 28 expiries to Sep 4) settled
several things that were assumptions until then.

**Confirmed working.** Endpoint, field names, nesting and pagination all match
the parser. Both roots present: 1,074 SPX and 11,704 SPXW, so §16.7's
AM-settled requirement is comfortably satisfiable (964 AM-settled monthly
contracts survive normalization on a single capture).

**`underlying_asset` carries only `ticker`.** No `price`, no `last_updated`.
So `transmission_spot.csv` is not a fallback for this study — it is the *only*
spot source, and a surface capture whose target time has no hand-recorded row
cannot be normalized at all.

**Impossible vendor greeks, concentrated deep ITM.** 5.6% of contracts carry
`gamma < 0`, 6.2% carry `vega < 0`, and 11.0% carry an IV below 1% — all
meaningless for a long option, all residue of IV inversion failing far in the
money. By moneyness: 43.8% bad among strikes more than 15% ITM, 1.4% near the
money, 0% out of the money.

They are flagged `implausible_greeks` and excluded from every computed quantity
via `usable_for_gex`, never dropped from the record. The gate is OI-weighted
near the money rather than a raw count, because these rows are ~11% of
contracts but carried only **0.036%** of the open interest within 10% of spot.
A count-based threshold would fail every day while measuring nothing.

A `gamma` of exactly 0.0 is treated differently from a negative one: it is the
vendor rounding an underflow at 9 decimal places, contributes exactly zero to
GEX, and is as harmless as a zero-OI contract.

## 3b. The as-of timestamp may itself be an inference

T is computed from a resolved as-of instant, walking a four-level ladder
(§5.3). Level 4 is `request_time - 900s` — arithmetic, not vendor data. It is
reached when no usable vendor stamp exists, **or** when the stamp turns out to
be daily-granularity: identical at 09:45 and 14:00, which every same-day pair
of captures checks for.

**On this tier level 4 is not a contingency, it is the permanent state.**
Verified 2026-07-27: level 1 and level 2 fields do not exist in the payload at
all, and level 3 (`day.last_updated`) holds 138 distinct values spanning back
to 2025-07-24 — it is each contract's last *trade* date, not the snapshot's
instant. Taking `max()` of it would hand a months-old T to any quiet contract,
so the resolver now rejects any candidate whose values span more than an hour
across contracts, and falls through to inference.

Three consequences follow, and they compound:

1. **Every T in the study is inferred** as `request_time − delay_minutes`.
   `asof_is_inferred = true` on every row.
2. **The delay assumption is now load-bearing**, not a caveat. The
   implied-delay check against `transmission_spot.csv` stops being a nice extra
   and becomes the only instrument that can catch the assumption drifting.
3. **Punctuality of the download now propagates straight into T.** If the 15:45
   capture fires at 16:03 instead of 16:00, the inferred as-of is 15:48, and a
   0DTE contract with 15 minutes of life left has its T wrong by 20% — roughly
   10% in gamma. `download_drift_minutes` is recorded on every capture for
   exactly this reason. Late is not "close enough" here.

## 3b-bis. Short-dated gamma cannot be validated on this tier

Measured 2026-07-27 on the first live capture. A 10-point uncertainty in the
spot used to compute gamma produces, for an ATM contract at 15% vol:

| Remaining life | σ√T | gamma error from a 10-pt spot error |
|---|---|---|
| 0DTE, 6 h | 0.0039 | **6.0%** |
| 0DTE, 15 min | 0.0008 | **75.5%** |
| 3 days | 0.0136 | 0.87% |
| 18 days | 0.0333 | 0.47% |

Now stack that against what this tier gives us. Massive returns **no spot at
all**, so the value we compare against is hand-recorded at 15-minute
granularity from a different source, and the snapshot's as-of is a level-4
inference (§3b) with a few minutes of slack. A few minutes of a normal morning
is easily 10–20 points: on the first live day spot moved 22.7 points between
09:45 and 10:00.

The consequence is structural, not a bad day:

> **§7 Path A can validate the 8–30DTE layer and, more weakly, 1–7DTE. It
> cannot validate 0DTE gamma on this tier at all**, because doing so would
> require knowing the vendor's spot to within a point or two and the vendor
> does not publish it.

This showed up immediately. On the first live capture the residual by bucket
was 8-30DTE −1.5%, 1-7DTE +3.5%, 0DTE −5.2%, and no combination of `r − q`,
assumed spot, or expiration offset collapsed all three at once — while 8-30DTE
alone reconciled to −0.16% and stayed stable across a ±40-point spot range.

**What this does not license.** Reading the 8–30DTE reconciliation as blanket
validation would be exactly the §19.4 error of picking the closest fit. What it
supports is narrower: the *conventions* (r, q, day count, settlement times) are
shared across tenors, so validating them on the tenor where they are observable
is evidence about the conventions — not evidence that any particular 0DTE gamma
is right.

**It also bears on §9.2.** The 15:45 row's 0DTE layer sits at 15 minutes to
expiry, where a 10-point spot error is a 75% gamma error. Marking it
`not_cross_day_comparable` for the §9.1 singularity is still correct, but it
understates the problem: that number is not merely incomparable across days, it
carries an error bar wider than the quantity itself unless the spot is known to
within about a point. Recording the 15:45 spot precisely, at the actual as-of
rather than the label, is the only thing that narrows it.

## 3c. The implied-delay check is a coarse alarm, not a calibration

Each capture reports the lag that best explains its underlying price, matched
against the hand-kept transmission series. Two hard limits:

**Resolution.** The estimate cannot be finer than the spacing of that series.
With the spec's five points a day the grid resolves to roughly 105 minutes. It
will catch a delay that has silently become an hour. It cannot tell 15 minutes
from 20. `resolution_minutes` is reported on every result so the number is
never read as more precise than it is.

**Identifiability.** On a quiet day, spot sits within a couple of points at
several times and no price match can say which one a snapshot came from. Those
captures report `identifiable: false` with a reason instead of returning
whichever point was fractionally closer — a confident wrong lag would be worse
than none, because it would look like evidence. Expect this to happen on real
days, not just in principle.

Adding transmission points at the download times as well as the target times
(09:45 *and* 10:00, and so on) would bring the resolution to 15 minutes and put
the discrimination exactly at the interval in question. That is a change to the
manual export process, so it is a recommendation rather than something the code
assumes.

The measured value never feeds back into `provider.delay_minutes`. Drift is
reported; the config is changed by a person, on evidence, not by the pipeline.

---

## 4. 0DTE gamma is numerically unstable near expiry

Gamma scales as `1/√T`. With spot completely unchanged, one ATM strike holding
1,000 contracts contributes:

| Time | Time left | $GEX per 1k OI |
|---|---|---|
| 09:45 | 6.25 h | 0.32 B |
| 14:00 | 2.0 h | 0.56 B |
| 15:30 | 0.5 h | 1.13 B |
| 15:45 | 0.25 h | 1.59 B |

A 5× swing from time decay alone, against a total net GEX that typically runs
−1.5 B to −3 B. This is a real ATM-gamma singularity — it is the mechanism
behind pinning — but the resulting number is **not comparable across days**.

The 16:00 row never contains a 0DTE layer: for PM-settled SPXW, 16:00 *is* the
settlement instant, `T = 0`, and the contracts no longer exist.

---

## 5. One vendor, unvalidated externally

A single data provider. Its greeks, its IV, its timestamps. §7 was designed to
check self-consistency (our formula against the vendor's own IV) *and*
invertibility (our IV from mid against the vendor's gamma). On this tier only
the first is available — see §3a. Neither was ever an independent check against
a second source, and a systematic vendor bias would pass what remains.

Deep-ITM contracts frequently arrive without greeks, which shapes which
contracts can appear in the §7 validation sample.

---

## 6. 15 days is not powered to test anything

The window is descriptive by construction. Q2 asks about accrual rate and
coherence — how many crossings occur, how large the moves around them are, how
many trading days would be needed to reach n = 40 — not whether crossings
predict anything.

**No p-values appear anywhere in this study.** A stable non-zero median error
against the vendor is a calibration finding, not a failure; a wide or drifting
IQR is a failure.

---

## 7. The vendor comparison is partly undefinable

The vendor's "wall" definition is undisclosed. Self-computed output therefore
never uses the word — four peak proxies are stored as separate columns, and
after 15 days each is compared against the vendor's wall to report which is
closest.

Disagreement is classified as `definition_gap`, `numerical_error`, or
`unknown_methodology`. Parameters are never quietly tuned to close a gap.

---

## 8. Attribution is counterfactual, not causal

The 2×2 decomposition evaluates a state (state C: the t1 surface at the t0
spot) that never existed in the market. Mechanical and surface components are
an allocation under a declared model. They are **not** a causal decomposition
and are not additive in any deeper sense. Both the ordered and the
order-independent (Shapley) split are reported so the ordering assumption stays
visible.

---

## 9. Operational

- **No exchange holiday calendar.** `oi_asof` skips weekends only, so the
  session after a holiday names a non-session day. Cron fires on holidays and
  those runs fail loudly rather than writing a fake day.
- **Snapshot endpoints are live-only.** A missed capture cannot be backfilled,
  ever. This is why capture ships before any analysis code.
- **Timestamp drift.** A capture is labelled `1400` by intent; the actual
  offset from the label is recorded, and a wide offset breaks the §8.1 pairing
  that Q1 depends on.
