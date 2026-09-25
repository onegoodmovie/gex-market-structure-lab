# SPX self-computed GEX — final report

**Window:** 2026-07-27 to 2026-07-31, five trading days (T0–T4).
**Planned:** fifteen. Why it changed is in §0 and `docs/WIND_DOWN.md`.
**Design of the framework this produced:** `docs/FRAMEWORK.md`. This document
carries conclusions only.

---

## 0. Why five days and not fifteen

Decided on T2, before the outcome was known, and recorded in three places
including `config/experiment.yaml`. Changing a stopping rule mid-study is
legitimate; changing it silently is not.

Q1 and Q3 were answered and would not have become truer with more days — Q3
already spanned three market directions. The two things still open were
**mechanism** questions, which a larger sample cannot settle. Friday was kept
because it carries the weekly roll-off, the one condition the framework had
never run across; it earned its place by breaking two things (§5).

**Capture did not stop.** Six LaunchAgents keep firing. Every milestone above
capture recomputes from stored Parquet, so a question asked in a month is a
re-run over 30 or 60 days, not a new study.

---

## 1. Q1 — structural validation against optioncharts.io

Not a calibration, and the name should stay provisional: optioncharts' spot,
upstream source and outlier handling are all undisclosed.

### Premises, including one that stayed open

- **Arithmetic conventions verified**, not assumed: same open interest, the same
  100 multiplier, and the same GEX definition (dollar gamma per 1% move).
  Backing their gamma out of their published per-strike exposure with our own OI
  lands at ~1.00 against Massive's gamma.
- **Our 15-minute delay is verified** on evidence internal to our own data, and
  independently stated by Massive's own subscription page.
- **Their as-of is UNRESOLVED.** Their page is labelled 15-minute delayed; T2
  and T3 pairings implied it is effectively live; T4's three-download test did
  not discriminate — its two expiries pointed opposite ways. The double download
  was to run until three consecutive confirmations and got one. **Every Q1
  number below therefore carries an as-of uncertainty of up to fifteen minutes**,
  which on the flip is worth several points (§3).

### Result

| | n | median | IQR | range |
|---|---|---|---|---|
| **1DTE flip error, ordinary days** | 3 | **+0.00** | **0.36** | 0.73 |
| 1DTE flip error, incl. roll-off day | 4+ | +0.09 | 1.17 | 4.13 |
| 0DTE flip error | 5 | +5.09 | **13.89** | 27.94 |

**On 1DTE, two independently constructed exposure curves agree to under a point
— on one day exactly, to two decimals — with curve correlation 0.993–0.9998.**
That is the study's cleanest result and it is a real one: nothing was tuned to
produce it.

**On 0DTE there is no agreement to report.** The IQR is 13.89 points against a
median of 5.09. §3 explains why, and it is not a defect in either side.

**The roll-off day degrades 1DTE too**, from sub-point to +1.97…+3.58. That
day's "1DTE" is three calendar days out across a weekend. One observation, so a
candidate rather than a finding — but a specific reason not to pool Friday's
rows with the rest of the week.

---

## 2. Q2 — frozen-map descriptive usefulness

Descriptive only. The spec forbids significance claims at this n and nothing
here is a test.

| bucket | raw crossings | /day | **episodes** | **/day** | **days to 40 episodes** |
|---|---|---|---|---|---|
| 0DTE | 32 | 6.4 | **6** | **1.2** | **33** |
| 1-7DTE | 17 | 3.4 | 2 | 0.4 | 100 |
| 8-30DTE | **0** | 0 | 0 | 0 | n/a |
| aggregate | 19 | 3.8 | 4 | 0.8 | 50 |

**Roughly 80% of crossings reverse within thirty minutes** (81% 0DTE, 88%
1-7DTE, 79% aggregate).

That one line changes the answer to the spec's own question. Raw counts are the
wrong denominator: forty raw crossings is not forty independent observations
when four in five are the same excursion counted again. On episodes — crossings
separated by more than the recross window — **0DTE would need about 33 trading
days to reach n = 40, not six.** Roughly a month and a half of sessions, in one
volatility regime, extrapolated from five days.

**8-30DTE recorded zero crossings in five days.** Its frozen flip sat near 7500
while spot ranged 7332–7505. In this window that bucket's flip was
descriptively inert — a fact about the window, but a blank row that should be
stated rather than left blank.

---

## 3. The 0DTE flip is not a well-defined quantity

This is one conclusion reached from four independent directions. It was
originally written up as three separate puzzles, which is why it took five days.

**Across sources.** Our 0DTE flip against optioncharts' over five days:
+10.98, +7.44, +5.09, −16.96, −6.45. Mean +1.64, sd 12.63. No stable sign, no
stable magnitude.

**Within a single source.** optioncharts' own two 0DTE exports fifteen minutes
apart on T3 put the curve zero at **7395.89 and 7419.23 — 23 points — while spot
moved 1.18.** The instability is not an artefact of comparing two vendors.

**Structurally.** On the roll-off day our own 0DTE curve crosses zero **three
times inside 27 points**, and the negative excursion between two of them is
narrower than the solver's grid. "The flip" is then a choice among roots, not a
measurement. This appeared in 2 of 24 frozen-map 0DTE rows, both at 15:45 —
i.e. it emerges as expiry approaches.

**Temporally.** 81% of 0DTE crossings reverse within thirty minutes (§2).

None of these is a fault in the data, in Massive, in optioncharts, or in the
formula. Near expiry the exposure curve flattens and develops multiple shallow
crossings; a level defined as "where the curve crosses zero" inherits that. The
spec said from the start that 0DTE gamma could not be validated on this tier.
**That was right, and three coincident signs plus one day's slope were enough to
talk the study out of it for two days.**

**Consequence for the framework:** report the 0DTE flip with its root count, or
do not report it as a number.

---

## 4. Q3 — mechanical versus surface repricing

The question the study exists for. 40 attribution rows: five days × two windows
× four expiry buckets. The §12 identity holds on every row.

| net GEX, mechanical share | |
|---|---|
| min | 62% |
| **median** | **114%** |
| max | 201% |
| **surface opposes the move** | **33 / 40** |

| bucket | surface opposes | median mechanical share |
|---|---|---|
| **0DTE** | **10 / 10** | 120% |
| **1-7DTE** | **10 / 10** | 114% |
| 8-30DTE | 4 / 10 | 95% |
| aggregate | 9 / 10 | 112% |

**Intraday net GEX moves on spot and time. Surface repricing damps that move
rather than adding to it** — ten times out of ten in both short buckets, across
a fall, a rise, a fall, a rise and a rise, including the roll-off day. A
mechanical share above 100% is the arithmetic of the surface term opposing.

**8-30DTE is not an exception, it is a near-zero term.** Its surface
contribution runs 0.23–1.52B against totals of 3.4–13.4B and its sign is
coin-flip. Read it as noise around zero, not as different behaviour. (This is
one of the three retractions — §5.)

**The flip is the mirror image.** Across all 40 rows the median |mechanical|
contribution is **1.08 points** against a median |surface| of **5.76**, max
17.95. Net GEX moves for mechanical reasons; the flip moves for surface reasons.

And the flip's mechanical term is *pure time decay*, structurally: under sticky
strike the observation spot never enters the exposure curve, so spot contributes
exactly zero to it. Verified, not estimated.

**Language, per §12:** this is a counterfactual attribution under a declared
model. State C never existed in the market. It is not a causal decomposition.

---

## 5. Three retractions

Recorded as a section because the pattern matters more than any of them.

| claimed | on | evidence | broke on |
|---|---|---|---|
| The flip gap is a σ(K) skew difference between the two sources | T2 | 4 of 4 negative slopes, one day | T3: two positive, two negative |
| The 0DTE flip gap is a systematic positive bias | T0–T2 | three same-sign readings | T3: −16.96 |
| 8-30DTE is a systematic attribution exception | T2 | 4 of 4 rows | T3–T4: 4 of 10 |

All three were drawn from three or four observations within a single condition.
All three were reported with the same confidence as the main line, which was
built on 24 rows across three market directions and never moved.

**The σ(K) retraction carries an extra warning.** It was measured on the day
whose paired capture failed its data-quality gate and whose implied spot was 12
points off, and it did not reproduce on the day when all five captures were
clean. The tidy result came from the bad day and the mess from the good one.
When a finding appears, check the quality of the specific input it came from
before writing it down.

**Rule adopted:** state the number of observations in the same sentence as the
claim, and do not promote anything to a settled decision until it has survived a
day whose data quality differs from the day it was found on.

---

## 6. Q4 — research value

*Per spec, a manual field. Left for the operator, to be filled **retroactively**
and marked as such — the daily judgement was not recorded at the time, and
reconstructing it now is a different thing from having made it then.*

| day | `added_value` | one line |
|---|---|---|
| T0 2026-07-27 | | |
| T1 2026-07-28 | | |
| T2 2026-07-29 | | |
| T3 2026-07-30 | | |
| T4 2026-07-31 | | |

**Overall:**

---

## 7. What the archive supports without a new study

Five days of raw chains are on disk and capture continues. Every milestone
above capture recomputes retroactively, so these are re-runs rather than new
experiments:

- **The weekend/roll-off effect on Q1** (§1) — testable on any Friday.
- **Whether the 8-30DTE surface term is genuinely zero** — needs the sample
  size Q2's episode arithmetic describes, not a new design.
- **Whether Massive's expiry offset changes across a quarterly roll** — the
  offset is documented as ~3.5 h at 0–2 DTE, decaying with tenor, and stays
  unwritten to config.
- **optioncharts' true as-of** (§1) — only resolvable if they publish a
  timestamp, or on a day when the surface moves enough for the double download
  to discriminate. T2 was such a day; T3 and T4 were not.
