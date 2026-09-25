# Methodology

Every convention this study depends on, and its current status. A convention
marked **UNRESOLVED** is one that §7 has not yet reconciled; code that depends
on it raises rather than picking a plausible default.

**Purpose.** This study produces an **observation framework**, not a set of
numbers meant to replace a vendor's. That framing decides several things below
that would otherwise look like unfinished business: Q1 is a smoke test rather
than a calibration, Path A is a diagnostic rather than a gate, and the vendor's
conventions are documented rather than matched.

Status: M0, M1, M2 built.

---

## 1. What is computed

```
d₁ = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
Γ  = e^{-qT} · φ(d₁) / (S·σ·√T)

GEX_contract = Γ · OI · multiplier · S² · 0.01        # dollars per 1% move
Net GEX      = Σ_calls GEX_contract - Σ_puts GEX_contract
```

**Sign convention:** dealers assumed long calls / short puts relative to
customers. This is the standard naive convention. It is an assumption, not a
measurement, and it is stamped on every output row rather than buried here.

`gex_definition_version: 1.0`.

---

## 1a. Data source

**Massive Options Starter.** Chosen, not provisional. Known tier properties:

| Property | Value | Consequence |
|---|---|---|
| Delay | 15 minutes | §5.3, and the 15:45 0DTE rule below |
| API calls | unlimited | no pagination throttle needed |
| Greeks, IV, open interest | included | §7 Path A is runnable |
| Quotes (`last_quote`) | **not included** | no bid/ask/mid — see §5, §6.4, §7.1 below |
| Trades (`last_trade`) | **not included** | — |

The delay is a **config fact, not a runtime detection**. The field that would
have reported it, `last_quote.timeframe`, does not exist on this plan. Anything
that tried to probe it would find nothing and conclude nothing.

---

## 2. Resolved conventions

| Convention | Value | Where |
|---|---|---|
| Raw storage | provider's own column names, dot-flattened, lossless | `providers/base.py` |
| Timestamps in Parquet | vendor's native form, unmodified | `capture.py` |
| Timestamps in meta | tz-aware UTC ISO-8601 | `capture.py` |
| Expiry scope | DTE ≤ 45 | `capture.expiry_scope_max_dte` |
| Roots | SPX and SPXW captured, never merged before settlement metadata is retained | `capture.py` |
| Multiplier | vendor's `details.shares_per_contract` | raw |
| Mid | **does not exist** — §19.3 is void, there are no quotes to choose between | — |
| IV alignment | sticky-strike, no switch implemented | `frozen_map.iv_alignment` |
| 0DTE late session | excluded, not floored | `gex.zero_dte_late_session` |
| 0DTE at 15:45 and 16:00 | **unconditionally excluded** | `gex.zero_dte_unconditional_exclude_times` |
| Grid step | 5 points, finer than the 25-point SPX strike spacing | `flip_solver.grid_step_points` |

### 2.0 The 15:45 0DTE rule

Shifting the download clock by the delay (§2.4) means the 15:45 row now carries
genuine 15:45 data. The staleness argument for dropping its 0DTE layer is gone.

What remains is a different objection, and it is not about the feed at all:
§9.1's `1/√T` singularity. At 15 minutes to expiry a single ATM strike swings
the number roughly 5× on time decay alone. The value is real — it is the
mechanism behind pinning — but it is **not comparable across days**.

So the layer is kept and marked, not discarded:

| Row | 0DTE treatment |
|---|---|
| 09:45 | normal |
| 14:00 | normal |
| 15:45 | computed and reported, `not_cross_day_comparable: true`, **excluded from `aggregate`** |
| 16:00 | **absent** — for PM-settled SPXW that instant *is* settlement, T = 0, the contracts do not exist |

The 15:45 exclusion from `aggregate` is the point of the marking: a
non-comparable quantity must not be summed into a comparable one. The last row
is a different kind of statement — not a judgement about comparability but a
fact about what exists — and it is not a config choice.

This flag does not depend on `provider.is_delayed`. The justification is
numerical, so a flag that moved with the tier would be the old delay rule
wearing a new name.

### 2.1 Expiration timestamps

Derived, not taken from the vendor, and checked in §7:

```
SPXW (PM-settled):  expiration_date @ 16:00:00 America/New_York
SPX  (AM-settled):  expiration_date @ 09:30:00 America/New_York
```

AM-settled SPX monthlies (third Friday) stop trading at Thursday's close and
settle Friday morning against SET. From Thursday 15:45 the true remaining time
is ≈17.75 h, not ≈24.25 h — a ~27% error in T and ~17% in gamma, recurring once
a month.

Zones are real IANA zones, never fixed UTC offsets. A hardcoded `-04:00`
silently expires the 0DTE leg on winter dates; this was caught by a test on a
January fixture date and is now asserted against.

### 2.2 Time to expiry

```
T_years = (expiration_timestamp - asof) / (365.0 * 86400)
```

**T is always computed from the resolved as-of instant, never from wall-clock
execution time.** On this tier those differ by 15 minutes, and the error scales
as `delay / T` — about 2% at 09:45 on 0DTE, 6% at 14:00, 18% at 15:30.

### 2.3 As-of resolution ladder (§5.3)

Starter has no `last_quote`, so level 1 never hits. The ladder is walked in
order and **the level that hit is recorded on every capture**:

| Level | Source | Status on this tier (verified 2026-07-27) |
|---|---|---|
| 1 | `last_quote.last_updated` | **absent** — no quotes on Starter |
| 2 | `underlying_asset.last_updated` | **absent** — the block holds only `ticker` |
| 3 | `day.last_updated` | **rejected** — 138 values spanning 364 days; a per-contract last-trade date |
| 4 | `request_time - delay` | **this is what the study actually uses** |

A candidate level is rejected when its values span more than
`asof_resolution.max_span_minutes` (60) across contracts. A snapshot stamp
describes one instant; a field spanning a year is describing something else,
and `max()` of it would quietly hand a months-old T to any quiet contract.

A level-4 value is arithmetic, not vendor data. Every T derived from it
inherits the full uncertainty of the assumed delay, and the flag travels with
the row so nothing downstream can mistake it for a measurement.

**The mandatory variation check.** Two captures on the same day must not
resolve to the same instant. A field that reads identically at 09:45 and 14:00
is a daily-granularity stamp — it looks exactly like a timestamp and is useless
as one, and this comparison is the only thing that tells them apart. On a
collision the capture degrades itself to level 4 and records what it degraded
from.

The first capture of a day cannot detect this on its own; it resolves cleanly
because there is nothing to compare against. `probe_provider.py --recheck-after
180` settles the question before day 1 instead.

### 2.4 Capture clock

`provider.delay_minutes` is the **single source of truth** for the delay.
Download times are derived from it and appear nowhere in config:

```
download_time = target_asof + provider.delay_minutes
```

| Target as-of | Download | Purpose |
|---|---|---|
| 09:45 | 10:00 | surface base for the frozen map |
| 14:00 | 14:15 | attribution t1, paired with the vendor screenshot (§8.1) |
| 15:45 | 16:00 | attribution t2 |

The capture label is the **target as-of**, not the clock time, so filenames and
§8.1 pairing keep referring to the instant the study is about. Change the tier
and every download time moves by itself; nothing has to be edited in two places.

Two separate measurements follow, and keeping them apart matters:

- `download_drift_minutes` — did cron fire when it should have? An operational
  question.
- `asof_vs_target_minutes` — does this snapshot actually carry 14:00 data? This
  is the one §8.1 depends on. A capture can fire perfectly on time and still
  miss its target if the vendor's delay is not what config says.

### 2.5 Implied delay — measuring the assumption, not trusting it

Every capture matches its snapshot `underlying_asset.price` against
`transmission_spot.csv` and reports the lag that best explains it. Config is
never updated from this; a drifting value is **reported**, never applied.

What the measurement can and cannot do:

- It is bounded by the spacing of the hand-kept transmission series.
  `resolution_minutes` is reported on every result. With the spec's five points
  a day the grid resolves to roughly 105 minutes — enough to catch a delay that
  has become an hour, useless for telling 15 minutes from 20.
- It is **unidentifiable on a quiet day**. If spot sits within a couple of
  points at several times, no price match can say which one the snapshot came
  from. Those results report `identifiable: false` with a reason rather than
  returning whichever point happened to be fractionally closer. A confident
  wrong lag would be worse than none, because it would look like evidence.

**To make it resolve at 15 minutes instead of 105**, add transmission points at
the download times as well as the target times — 09:45 *and* 10:00, 14:00 *and*
14:15, 15:45 *and* 16:00. The estimator then discriminates at exactly the
interval in question rather than across two-hour gaps. This is a change to the
manual export, so it is a recommendation here rather than an assumption in the
code; the code handles whatever rows it is given.

---

## 3. UNRESOLVED — must be settled before M2

| Convention | Status | Resolved by |
|---|---|---|
| `r` | **UNRESOLVED** (`rates.r: null`) | §7.2 grid search |
| `q` | **UNRESOLVED** (`rates.q: null`) | §7.2 grid search |
| `time_convention` | provisional `calendar` | §7.2 — set to whatever reconciles |
| `expiration_timestamp_source` | provisional `derived` | §7.2 |
| which as-of level hits | unknown until one live probe | `probe_provider.py` |

Resolved since the tier was chosen: `provider.is_delayed` is `true`, a known
property of Massive Options Starter rather than something to detect.

### 3.1 Validation paths

| Path | Status | Gate |
|---|---|---|
| **A** — `massive_iv` → our gamma vs `massive_gamma` | **diagnostic** | none; reported per bucket, median and p90 |
| **B** — invert IV from `mid`, then gamma vs `massive_gamma` | **N/A on this tier** | — |

Path B is impossible on Starter: no quotes, therefore no mid, therefore nothing
to invert. This is recorded as not-applicable with the reason attached — it is
**not** a failed gate and must not be scored as one. Acceptance criterion §16.5
reads as "Path A median gamma error < 1%; Path B N/A (tier)".

Path A is untouched by the tier choice and remains the hard gate. The §7.2
reverse-engineering of `r`, `q`, `time_convention` and the expiration-timestamp
source proceeds exactly as specified.

### 3.2 Put-call IV consistency — the replacement cross-check (M1)

**Why this exists.** Path A feeds the vendor's own IV into our gamma and
compares against the vendor's gamma. Both sides share that IV as an input, so
the test is blind to any error inside it. Path B was the design's only check
with an independent input, and this tier does not have it. Put-call consistency
is the one cross-check that survives, because it uses no external data at
all — only an internal relation the vendor's own numbers must satisfy.

**The relation.** Put-call parity fixes the forward:

```
C - P = S·e^{-qT} - K·e^{-rT}
```

If the vendor inverts call and put prices at the same `(root, expiry, strike)`
using a forward built from its own `r` and `q`, both must come back with the
same implied vol. If its `r - q` is misspecified, call and put absorb the error
with opposite sign. To first order:

```
σ_call - σ_true ≈ -e^{-rT}·N(d₂)·ΔF / vega
σ_put  - σ_true ≈ +e^{-rT}·N(-d₂)·ΔF / vega
```

and since `N(d₂) + N(-d₂) = 1` exactly,

```
σ_call - σ_put ≈ -e^{-rT}·ΔF / vega
```

**Read the intercept, not the slope.** Moneyness enters only through vega, so a
forward error appears as a *level* shaped like `1/vega` — symmetric in `d₁`,
smallest at the money, growing in both wings. It is not a tilt.

Verified numerically. Injecting a 1.3 percentage-point `r - q` error at
T = 0.25, S = 7400:

| ln(K/S) | iv_call − iv_put | spread × vega |
|---|---|---|
| −0.0700 | −0.0345 | −28.4 |
|  0.0000 | −0.0166 | −24.1 |
| +0.0654 | −0.0202 | −22.8 |

OLS gives intercept −0.0205 and slope +0.093. The intercept is the signal; the
slope is a by-product of sampling the U-shape asymmetrically and moves with the
strike distribution, so it is reported but treated as weak evidence.

**The sharp version.** `spread × vega` is nearly constant (mean −24.6, sd 1.69)
and matches the theoretical `−e^{-rT}·ΔF = −24.0`. So invert it:

```
ΔF        = -(σ_call - σ_put) · vega · e^{rT}
Δ(r - q)  ≈ ln(1 + ΔF/S) / T
```

On the numbers above this recovers ΔF ≈ 24.9 points and Δ(r−q) ≈ 0.0135
against an injected 0.0130. The check therefore does not merely detect a
forward error, it **quantifies** it, which is what makes it usable evidence for
the §7.2 grid search rather than a yes/no.

Its dispersion across strikes is the credibility test: a genuine forward error
gives a near-constant ΔF, while a large spread points at vendor smoothing or
stale legs instead.

**Vega floor.** Pairs below 5% of their expiry's ATM vega are excluded. There
IV is barely determined by price, and since the spread goes as `1/vega` those
points explode — in testing, a handful of 0DTE wing strikes dragged the fitted
intercept to −2.6 × 10⁸. The cut is taken per expiry so it is scale-free.

| Result | Reading |
|---|---|
| intercept ≈ 0, slope ≈ 0 | call and put agree; the vendor's forward is self-consistent |
| intercept ≠ 0, ΔF near-constant | misspecified forward; `Δ(r−q)` is quantified and signed |
| intercept ≠ 0, ΔF dispersed | vendor smoothing or stale legs, not a rate error |
| slope ≠ 0 with no level offset | asymmetric strike sample; weak evidence at best |

**How it is used.** As independent evidence in the §7.2 grid search. The grid
search alone is a fit: it finds whichever combination minimises Path A error,
with nothing to say whether that combination is *right*. Put-call consistency
comes from a different relation entirely, so a combination satisfying both is
corroborated, and one satisfying only Path A is a warning.

Why that warning matters is worth stating concretely, because it is the failure
mode this whole section exists for. A vendor whose forward is wrong does not
produce incoherent output — it inverts IV with its own wrong `r − q` and then
computes greeks from *that* IV with the same wrong `r − q`. Its numbers are
internally consistent. **Path A will reconcile perfectly against them**, and
the §7.2 scan will happily recover the vendor's error and recommend writing it
into config as fact. This is tested directly: with a 1.3-point forward error
injected, Path A lands at 0.0001% median error while the put-call check flags
the problem and sizes it.

Path A confirms we reproduced what the vendor did. It can never confirm that
what the vendor did was right. That distinction is the entire value of this
check.

It is a diagnostic, not a gate. A constant-ΔF finding is a §7.2
stop-and-report per spec §19.4, not something to tune away.

`Config.require()` raises on a null value, so a milestone that needs one of
these stops rather than defaulting. Fill them in here *and* in
`config/experiment.yaml` when §7 resolves them, and record the residual bias by
moneyness and by DTE — a systematic tilt in either dimension is a stop
condition even when the median passes.

---

## 4. Field-coverage gates (M0)

Two tiers, because one threshold would be wrong for both.

**Chain-wide, 95%:** `open_interest`, `details.strike_price`,
`details.expiration_date`, `details.contract_type`, `details.ticker`,
`details.shares_per_contract`. Every contract carries these or the payload is
unusable.

**Near the money only, 95% within ±10% of spot:** `greeks.gamma`,
`implied_volatility`.

The second tier exists because §7 states outright that vendors drop greeks on
deep-ITM contracts, and a full SPX chain to 45 DTE runs thousands of points
wide. On the fixture chain, gamma coverage is 74.8% chain-wide and 100% near
the money: a chain-wide gate would have rejected a perfectly usable capture.
Gamma weight and the frozen surface both live near the money, so that is where
coverage has to hold.

Both numbers are recorded in every `.meta.json`; only the second one gates.

**Not applicable on this tier:** `last_quote.bid`, `last_quote.ask`,
`last_quote.midpoint`, `last_quote.timeframe`, `last_trade.sip_timestamp`.
These are reported as N/A rather than as 0% coverage, so an expected absence
never reads as a broken capture. If one of them ever *does* arrive, it is
flagged as `unexpectedly_present` — the tier assumptions would then need
revisiting rather than quietly benefiting.

### 4.1 §6.4 filters

| Filter | Status |
|---|---|
| drop `open_interest == 0` | applied |
| drop crossed markets (`bid > ask`) | **not_applicable_on_this_plan** |
| flag `bid == 0` | **not_applicable_on_this_plan** |
| flag stale quote | applied, against the resolved as-of |
| drop duplicate `(root, expiration_date, strike, option_type)` | applied |
| flag `massive_iv` null or ≤ 0 | applied |

The two quote-dependent filters have no data to act on. They are carried as
explicit N/A entries in the day's data-quality record rather than deleted from
the pipeline, so the record shows a deliberate decision instead of a check that
silently ceased to exist.

---

## 5. Provenance stamped on results

`gex_definition_version, sign_convention, expiry_scope, iv_alignment, oi_asof,
surface_timestamp, spot_timestamp, time_convention, t_floored`.

Capture-level provenance already in `.meta.json`: provider, endpoint, request
start/finish, page count, row count, full column list, Parquet SHA-256, vendor
timestamp field and range, quote timeframes, and the coverage checks.

---

## 4a. §7.2 finding — the vendor's expiry convention (2026-07-27)

The first live day produced a concrete, cross-validated §7.2 result, and the
way it was nearly missed is worth recording alongside it.

**The symptom.** Path A failed on both captures at ~1.7% median, and no value
of `r − q` brought it under 1%. Residuals by bucket on the 14:00 capture:
8-30DTE −1.2%, 1-7DTE +4.9%, **0DTE −78%**.

**Why the median hid it.** The chain is dominated by longer-dated contracts,
for which hours of expiry offset are invisible. The median moved 1.68% → 1.84%
under a correcting offset while p90 collapsed 78.8% → 6.4%. Optimising the
median — which is what §7.1 specifies as the gate — would have concluded "no
offset" and been badly wrong. The expiry scan is therefore scored on **p90**.

**What it is.** The vendor's gamma implies a T that is a constant *additive*
+3 to +4.5 hours longer than ours, stable across expiries, buckets and both
captures. Inverting their ATM gamma for T contract by contract gives +2.1 to
+6.0 h, median ≈ 3.8 h. Our SPXW settlement is 16:00 ET; +4 h would be exactly
20:00 ET, i.e. **00:00 UTC of the following day** — the natural result of
treating `expiration_date` as a date and expiring it at end-of-day UTC.

**Ruling out the alternatives.**

- *Not `time_convention`.* A calendar/trading-time difference is a
  multiplicative factor. Scanning one made 8-30DTE dramatically worse (−39% at
  the factor that fixed 0DTE); the best multiplicative factor was 1.00.
- *Not a stale as-of.* `T = expiry − asof`, so a later expiry and an earlier
  as-of are indistinguishable from Path A alone. They are separable by outside
  evidence: the vendor's gamma peak sits at the money and therefore reveals the
  spot their greeks were computed for. It tracked the *current* spot on both
  captures (peak 7405 vs spot 7399.96 at 14:00; peak 7470 vs 7463.62 at 09:45).
  Four-hour-stale data would have put the 14:00 peak near 7440. The as-of is
  right; the expiry is not.

**Status: CLOSED, null, not under investigation.**

Measured properly — from the vendor's own `vega / gamma = S²·σ·T`, which is the
only estimator here that separates σ from T — the offset is **+3.38 h median,
range +2.84 to +3.90 h, flat across tenor** from 0DTE to 12 DTE.

**The UTC-end-of-day hypothesis is refuted.** If the vendor ignored settlement
type and expired everything at 00:00 UTC of the following day (20:00 ET), the
offset would be +4.0 h for PM-settled SPXW and **+10.5 h** for AM-settled SPX
monthlies, whose true expiry is 09:30 ET. Measured on the August monthly:
**+3.33 h** — the same as the PM contracts. An offset that does not vary with
settlement type is not an expiry-time convention. A constant added to T for
every contract regardless of expiry is more consistent with a clock or
timezone offset in their pipeline, and EDT being UTC−4 makes that arithmetically
suggestive, but the study does not need the answer.

**Why it is dropped rather than pursued.** The study's core output is Q3's
frozen/actual decomposition, which is a *difference* between two quantities
computed under the same convention. Whatever constant sits in T appears on both
sides and cancels. Chasing it would improve nothing the study reports.

**What it does not change.** Our own GEX keeps the §6.2 settlement times.
16:00 ET is the correct PM settlement instant for SPXW as a matter of market
fact, and the vendor's convention being different does not make ours wrong.
The offset lives only inside Path A, whose job is to isolate *our formula* from
*their conventions* — comparing our gamma at the true expiry against theirs at
their expiry measures the convention gap and tells us nothing about the
formula. Per §8.2 this is a `definition_gap`, not a `numerical_error`.

**Independent corroboration, from the vendor's own product.** At 16:22 ET —
22 minutes after PM settlement, when SPXW 0DTE contracts no longer exist — the
vendor still reported a finite Jul 27 gamma of −$4.57B, and the values were
*recomputed* rather than cached (the flip moved 7409.41 → 7412.73 and the call
wall 7400 → 7415 between readings). Under our 16:00 ET settlement those
contracts have negative T. Under a convention roughly four hours later they
still have ~3.6 hours of life, which is exactly what a product that keeps
quoting them implies. This is a second line of evidence, from the vendor's
behaviour rather than from inverting its gamma, and it points at the same
number.

**Residual after the offset.** All three buckets land within about −2% to −5%,
noticeably uniform. A uniform residual is a scale effect rather than a time
effect, so `r`, `q`, and the discount factor remain the open questions — which
is what the drift scan is for.

## 4b. Vendor reference readings can be transiently wrong (2026-07-27)

The 15:45 reading of the vendor's Jul 27 0DTE row was a ~100× outlier sitting
between two consistent readings of the same series:

| Reading | Net GEX | Call GEX | Put GEX |
|---|---|---|---|
| 14:00 | −5.41B | +8.70B | −14.10B |
| **15:45** | **+191.27B** | **+956.32B** | **−765.04B** |
| 16:22 | −4.57B | +9.75B | −14.32B |

§9.1's `1/√T` allows a 2.83× change between 14:00 and 15:45; the call leg moved
110×, and the two flanking readings agree with each other closely. No market
event produces that shape. It is a vendor-side transient.

**Consequence for §8.** A hand-copied vendor row is a single observation with
no redundancy, and Q1's whole statistic rests on those rows. One bad reading
copied in good faith would move the median error and look like a methodology
finding. So `optioncharts_reference.csv` gains `quarantined` and `quarantine reason`,
and Q1 counts a row only when `paired = true AND quarantined = false`.

The practical discipline this implies: **a vendor reading that jumps by more
than time decay allows against its own neighbours should be re-read before it
is trusted.** On this day the 16:22 refresh is what exposed it — without that
second look the 15:45 row would have entered the study as fact.

The log also gained a `source` column. The spec's §8 schema has none, and day 1
produced readings whose provenance mattered: until it was confirmed that 14:00
and 15:45 came from the same product, the 100× gap was indistinguishable from
two vendors using different normalisations.

## 3.3 r and q are declared, not solved for

```
r = SOFR,  continuously compounded
q = SPX trailing 12-month dividend yield,  continuously compounded
```

Chosen outright. The §7.2 candidate-rate grid search is retired, and no attempt
is made to recover what Massive used. What justifies that is the sensitivity,
measured on the 2026-07-27 frozen map at S = 7462.42:

| Bump | Net GEX | Change | **Flip (pts)** |
|---|---|---|---|
| base | −6.319 B | — | 7468.43 |
| r +50bp | −5.820 B | +7.90% | **−0.47** |
| r −50bp | −6.816 B | −7.86% | **+0.47** |
| q +50bp | −6.812 B | −7.80% | **+0.46** |
| q −50bp | −5.824 B | +7.84% | **−0.47** |

**The flip moves less than half an index point for a 50bp error in either
rate.** The flip is the quantity Q1 and Q2 are about, so being wrong about SOFR
by half a percent costs the study essentially nothing.

Net GEX moves ~8%, which looks larger but is not: net GEX is a small difference
of two large sums, so a modest absolute shift is a large *relative* one. The
absolute change is ±0.5 B against call and put legs of tens of billions.

`r` and `q` enter gamma through `(r − q)` in `d₁` and through `e^{-qT}`, which
is why bumping `r` up and `q` down produce the same answer to within 2% — a
relation the tests assert, so a drift away from §9's formula would surface.

Both values are marked `provisional: true` until the day's actual quotes are
entered, and every output row carries `rates_provisional` so no result can be
read as final while that flag is set.

## 3.3a Where the spot comes from

§5.4 describes `transmission_spot.csv` as a hand-dropped export, and it was
hand-dropped for the first live day. `scripts/fetch_spot.py` now fills it from
Yahoo's `^GSPC` 1-minute bars instead, and the file carries a `source` column so
the two are never confused.

**Validated before it was trusted.** Against the seven values hand-recorded on
2026-07-27:

| Time | Hand-recorded | Yahoo 1m close | Diff |
|---|---|---|---|
| 09:45 | 7462.42 | 7462.42 | 0.00 |
| 09:52 | 7462.03 | 7462.04 | +0.01 |
| 10:00 | 7442.85 | 7442.86 | +0.01 |
| 14:00 | 7399.96 | 7399.97 | +0.01 |
| 14:15 | 7396.32 | 7396.33 | +0.01 |
| 15:45 | 7411.86 | 7411.87 | +0.01 |
| 16:00 | 7413.22 | 7413.18 | −0.04 |

Same bar convention — the close of the bar *labelled* `HH:MM` — not an
approximation of it. The 16:00 point is the closing auction minute and is
expected to differ slightly; nothing in the study reads a 16:00 spot for a 0DTE
layer, which is absent at that row by construction.

Three rules the fetcher keeps:

- **Existing rows are never overwritten.** A typed value and a fetched one are
  not interchangeable. Refetching one means deleting it by hand first.
- **A missing minute is reported, never interpolated.** A halt or a gap comes
  back null and stays null.
- **The label set is derived from config** — target as-ofs, the download times
  derived from `delay_minutes`, and the frozen-map recomputation points — so
  adding a recomputation point cannot leave a hole. On day 1 the only missing
  minute was 11:30, which is exactly why the frozen map produced four
  recomputation rows instead of five.

Yahoo serves 1-minute bars for roughly 30 days, so this backfills recent days
and cannot reconstruct the study retroactively. Running it daily is not
optional for that reason.

## 3.4 Why Path A cannot reach 1%

Massive prices with a **CRR binomial tree and finite-difference vega**. Our §9
gamma is closed-form Black-Scholes. Comparing the two is a cross-model
comparison, and it carries an irreducible residual that no choice of `r`, `q` or
`T` removes — tree discretisation and a differenced vega do not converge to the
analytic surface at the precision the spec's 1% assumed.

So Path A is kept for what it can actually answer — **is our own Black-Scholes
self-consistent** — and demoted from a gate. It is reported per expiry bucket
with both median and p90, because one number for a whole chain hides the shape:
on 2026-07-27 the overall median was 2.09% while the buckets ran 78.3% (0DTE),
4.9% (1-7DTE) and 1.3% (8-30DTE). A single median makes a chain look uniformly
mediocre when it is in fact excellent at one tenor and hopeless at another.

The §7.3 fixture check remains a **hard** gate, and it is the one that matters
for this purpose: it compares our formula against five deterministic values to
1e-9, with no vendor involved.

## 5a. Open question for the first live day

**Does the 16:00 download still return same-day PM-settled 0DTE contracts?**

At 16:00 we request data as-of 15:45, when SPXW 0DTE contracts have 15 minutes
of life left. Whether the vendor still carries them in a snapshot that close to
settlement is unknown and cannot be answered from documentation.

If they are **absent**, §9.2's "capture and mark" decision is moot — there is
nothing to capture, and the 15:45 0DTE layer simply does not exist. That would
be recorded as a finding, not worked around.

Every capture already records `contracts_by_dte` and
`zero_dte_contracts_returned`, and the 15:45 capture prints a loud note if the
count is zero. So the answer lands in the data on the first live day without
anyone having to remember to look.

## 6. Open assumptions (spec §17)

Listed explicitly because none of them are settled by the code as it stands.

1. **`r` and `q` are unknown.** Both null. Whether the vendor's gamma uses a
   discount rate at all is a §7.2 finding, not an assumption to be made here.
2. **Calendar time is provisional.** Set to `calendar` because it is the more
   common vendor convention, not because it has been verified.
3. **Which as-of level will hit is unverified.** Level 2 is expected, but
   whether `underlying_asset.last_updated` exists on Starter, and whether it
   moves intraday, has not been observed. If it turns out to be a daily stamp,
   every T in the study becomes an inference from the assumed 15-minute delay
   rather than a measurement — a materially weaker study, and one worth knowing
   about before day 1 rather than after.
4. **`oi_asof` uses a weekday-only rule.** No exchange holiday calendar is
   applied, so on the session after a holiday the recorded `oi_asof_date` names
   a non-session day. The rule string travels with the value in every
   `.meta.json`. This needs a holiday calendar before the summary is written.
5. **OI is assumed to be the previous session's confirmed figure.** This is
   what OCC publishes overnight and it matches §5.1, but it has not been
   verified against the vendor's own `oi_asof` semantics — the vendor may
   publish on a different schedule.
6. **Root classification is by ticker prefix.** `O:SPXW…` → SPXW, else
   `O:SPX…` → SPX. If the vendor ever emits a third root on the SPX underlying
   it lands in `?` and shows up in the roots histogram rather than being
   silently absorbed.
7. **The dealer sign convention is naive and unverifiable.** See
   KNOWN_LIMITATIONS.
8. **The Massive payload shape is unverified.** The tier is chosen, but the
   adapter is written to the documented shape and has not been run against a
   live key. Field names, nesting, and the pagination contract stay unverified
   until the probe runs.
9. **Path B being N/A is not costless.** Path A checks our formula against the
   vendor's own IV — it cannot detect a systematic error in that IV, because
   the IV is an input to both sides. Path B was the only check in the design
   that used an independent input (mid), and it is unavailable. The study
   therefore has one fewer independent handle on vendor error than the spec
   assumed.
