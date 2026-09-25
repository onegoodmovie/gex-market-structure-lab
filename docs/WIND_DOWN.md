# Stopping at 5 days instead of 15 — decided T2, 2026-07-29

The spec set `target_valid_days: 15`. The study will instead end **Friday
2026-07-31**, at 5 captured days (T0 Mon 27th through T4 Fri 31st).

Changing a stopping rule partway through is legitimate. Changing it silently is
not — which is why it is recorded here, in `config/experiment.yaml`, in the
handoff, and in the final report, with the reasoning rather than just the
decision.

---

## Why 5 is enough for what this study set out to answer

**Q1 and Q3 are already answered, and more days would not make them truer.**

Q3 — how much of the intraday change is spot/time mechanics and how much is
surface repricing — is the question the study exists for. It now has 24
attribution rows across three days and, critically, three *different* markets:
T0 fell, T1 rose, T2 fell. The mechanical share of the net-GEX change has a
median of 113% with the surface term opposing the move in 19 of 24 rows. Two
more days adds two more rows per bucket to a result that has already held
across the sign of the market.

Q1 — the structural comparison against optioncharts — has gone considerably
further than a smoke test: conventions verified (same OI, same multiplier, same
GEX definition), curve correlation 0.993–0.9998, and on T2 the residual
disagreement was localised to a measured σ(K) skew difference between the two
upstream sources.

## Why the two things still open are not sample-size problems

**The 8-30DTE exception** — the one bucket where surface repricing adds to the
move instead of damping it, four times out of four. Ten more days would
establish that it is real. They would not establish *what it is*, and only the
"what" decides whether it belongs in an observation framework. The obvious
mechanism was tested on T2 and rejected: the bucket's surface contribution does
not follow its own ATM IV change (2 of 6, anti-correlated on two days), while
the same test is exact for 1-7DTE (6 of 6). The next candidate is smile shape
rather than level, which is a modelling question, not a counting one.

**The 0DTE flip sign count** — three same-sign readings, and a plan to keep
counting toward p = 6.25% at five. T2's σ(K) measurement makes the count
redundant: the sign is predicted by a measured skew difference that also
predicts the tenor dependence (0DTE +5.09, 1DTE −0.55). Counting coin flips is
only worth doing while the coin is unexplained.

## Why Friday specifically, and why it cannot be cut

T4 is a Friday, which carries the largest weekly roll-off of the study window.
That is the condition under which the original methodological risk lives — the
expiry-basis question for a PM-settled snapshot — and **the frozen map has never
run across it.** The fifth day is not a rounding-up to a nicer number; it is the
one remaining condition the first four days do not cover.

## What does not stop

**Capture keeps running.** All six LaunchAgents stay installed and firing. The
Starter tier has unlimited calls, storage is about 1 MB per day, and every
milestone above M0 recomputes retroactively from stored Parquet.

What stops on Friday is the **manual** work — the daily optioncharts download
and the daily analysis pass.

This follows the rule the study has run on since day one: *code can be written
later, a chain cannot be captured later.* If a new question arrives in a month
— what the 8-30DTE exception is, whether the expiry offset changes across a
quarterly roll — the raw material will be on disk and the answer is a re-run
over 30 or 60 days rather than a new study.

## Stop condition for the double download

The 14:00 + 14:15 double download exists to pin optioncharts' as-of (T2 §4b).
**Once three consecutive days show the 14:00 export carrying 14:00 data, drop
the 14:15 download.** Do not run it for all remaining days out of habit — it was
a measurement, and it stops when it has measured.

## Deliverable

A final report after Friday's close, structured Q1–Q4, stating explicitly why
the study ran 5 days rather than 15.
