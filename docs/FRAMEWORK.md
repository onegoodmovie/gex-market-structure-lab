# The observation framework

What the study was built to produce. `docs/FINAL_REPORT.md` carries the
conclusions; this carries the design, and the reasons behind the choices that
turned out to be load-bearing.

Written so that someone re-running this in a month, on a different provider or a
longer window, does not have to rediscover why anything is the way it is.

---

## 1. What the framework computes

A transparent SPX/SPXW gamma-exposure proxy, and a split of its intraday
movement into a **spot/time mechanical** component and a **surface repricing**
component.

It is not a vendor replacement, not a signal, and not a claim about dealer
inventory. It is *a same-day exposure proxy conditioned on the previous
session's confirmed open interest* — intraday OI is frozen, so new opens,
closes and rolls are invisible until the next overnight OCC publication.

## 2. The layer that must never be clever

Capture stores the provider's own column names, flattened, untouched. No
renaming, no filtering, no derived columns. Every later layer recomputes from
it.

This is what made five days of retractions cheap: when the `vendor_*` prefix was
split into `massive_*` / `reference_*` / `provider_*` on T1, nothing had to be
migrated — the normalized and derived layers were regenerated and every
downstream number reproduced bit-for-bit.

**Code can be written later. A chain cannot be captured later.** That rule
decided the schedule, the storage layout, and the wind-down.

## 3. Timing, which is where the errors live

**A capture's label is its as-of, never its download time.** `surface1400` fires
at 14:15 and carries 14:00 data; the label is the instant, the fire time is the
instant plus the provider delay. Every `.meta.json` records both.

**Pair on the as-of, not on the clock.** Fifteen minutes of surface movement is
worth roughly eight flip points. A comparison against an outside source whose
as-of is not pinned is not a measurement.

**Verify the delay rather than assuming it.** The joint (S, T) fit recovers an
implied spot from the provider's own gamma curve. On a day when spot changes
direction more than once this discriminates sharply: 15-minute total error 18.0
points against 79.2 for 30 minutes and 56.4 for zero. Run it on any such day.

**When the outside source's as-of is unknown, take the export twice.** Fifteen
minutes apart. It only discriminates when the surface actually moves in that
window — which is why it worked once in three attempts.

## 4. Attribution: two invariants that are not optional

Four states, OI fixed: A = (t0 spot/time, t0 surface), B = (t1, t0), C = (t0,
t1), D = (t1, t1). Both the path-dependent and the order-independent (Shapley)
decompositions are reported, and their identity is a gate on every row.

**One contract set across all four states.** The usable set moves between
captures — hundreds of contracts cross the greek-plausibility boundary intraday
as moneyness migrates with spot. Letting each state use its own rows puts a
membership change inside `D − A` and reports it as surface repricing. Measured
once at **−304% of a row's total change**: the artefact was three times the
signal.

**One bucket policy across all four states.** The 0DTE aggregate rules are keyed
on capture label, so a t1 of 15:45 would drop 0DTE from D while A kept it and
the identity would fail on membership rather than on anything attributed.

Both are tested, and the cost of the first is reported beside every result
rather than assumed small — it ranged from −5% to −304% across the window.

## 5. Filtering: report the cost, never repair the number

Contracts whose greeks are impossible are flagged and excluded from every
computed quantity, and never dropped from the record.

**Never backfill or repair an anomalous contract to make two sides agree.** What
the filter costs is measured and reported *beside* the number, never inside it.
The stand-in IV for that measurement is the same strike's opposite leg —
put-call parity — deliberately not backed out of the reference, which would
launder the thing being tested into the test.

The case for this is empirical, not aesthetic: restoring the filtered contracts
once improved the curve fit and **worsened** the flip. A repair chosen to fix
one would have quietly broken the other, and a repair chosen because it made the
two sides agree would have been fitted to the reference rather than measured
against it.

## 6. Reading rules that cost time before they were written down

- **The median hides the thing that matters.** Read p10 and p90. A median of
  1.00 sat on top of a p10 of 0.79 for two days. Always per bucket, median *and*
  p90.
- **Check both legs.** The convention check computed calls only, over a band
  narrower than the tail it was supposed to catch.
- **A gamma-only fit cannot separate σ from T.** Near the money gamma depends on
  them only through σ√T. Use `vega / gamma = S²·σ·T`.
- **The gamma peak is not the spot.** The 1/σ factor plus a downward smile puts
  it 1–4 points above S.
- **The flip carries no information about spot handling.** Under sticky strike
  the observation spot only sets the solver's grid window, so the root is
  invariant — exactly. A flip comparison is a T test, never an S test.
- **Both outside sources can be transiently wrong** — the API feed as well as
  the screenshot. Diff the flag set against a later capture before reading a
  quality failure as degradation.
- **Net GEX is a difference of two large nearly-equal numbers.** Report the legs
  beside it or an 8% leg disagreement reads as 61%.
- **State the number of observations in the same sentence as the claim** (§5 of
  the report). Three of five days' findings were retracted; all three came from
  three or four observations inside one condition.

## 7. Known limits of the construction

- Every T is inferred from the download clock. Levels 1–3 of the as-of ladder
  are unusable on this tier; the delay is verified but the mechanism is still an
  inference from request time.
- Intraday OI is frozen. Volume is captured as the only observable proxy for
  what that misses, and the direction of new inventory is never inferred from
  unsigned volume.
- The frozen map's surface base is a declared assumption, not a neutral choice.
  Its size is measurable daily by building both candidate bases: roughly 0.3
  flip points per point of spot movement between them.
- Near expiry the exposure curve develops multiple shallow zero crossings. Report
  the 0DTE flip with its root count or not as a number at all.
- Path A is a cross-model comparison — our closed form against a CRR tree with
  finite-difference vega — so its residual is irreducible and it is a diagnostic,
  never a gate.

## 8. What to keep if this is rebuilt elsewhere

In order of how much they earned their place:

1. The raw layer that stores provider column names untouched.
2. The identity gate on the attribution, and the two invariants behind it.
3. Reporting the cost of every filter and every fixed choice beside the number.
4. Capture continuing after analysis stops.
5. The per-day write-up, including the retractions. The three that broke are
   worth more than most of what held, because they are the only record of how
   confident to be next time.
