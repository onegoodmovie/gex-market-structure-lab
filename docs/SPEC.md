# SPX Self-Computed GEX — Standalone Research Experiment

**Build spec for Claude Code.** Version 1.0.

---

## 0. What this is

A standalone 15-trading-day research study that computes a transparent SPX/SPXW gamma-exposure proxy from option-chain data, and decomposes its intraday movement into:

1. a **spot/time mechanical component** (frozen IV surface, frozen OI), and
2. a **surface repricing component** (actual refreshed IV surface).

**It is not:** a replacement for a vendor GEX product, a real-time dashboard, a trading signal, or a claim about actual dealer inventory.

**Coupling:** none. This repository does not read from, write to, import from, or schedule alongside any other project. All external inputs arrive as manual file drops into `data/manual_input/`. Do not add integration hooks; a separate decision will be made after the study concludes.

---

## 1. Build order (read this before writing any code)

Build in milestones. **Each milestone must run end-to-end before the next one starts.** Do not scaffold empty modules for later milestones.

| Milestone | Ships | Gate to proceed |
|---|---|---|
| **M0 — Capture** | `capture_oi_base.py`, `capture_surface.py`, raw Parquet persistence | Three raw files land on disk for one real day |
| **M1 — Validate** | `normalize_chain.py`, `greeks.py`, `validate_greeks.py` | Gamma reconciliation passes §7 |
| **M2 — Frozen map** | `exposure.py`, `expiry.py`, `flip_solver.py`, `peaks.py`, `frozen_map.py` | 5 capture-days recompute cleanly |
| **M3 — Attribution** | `actual_map.py`, `attribution.py` | 2×2 identity holds to floating-point tolerance |
| **M4 — Reporting** | `reporting.py`, `build_daily_report.py`, summary | One real-day report renders |

### 1.1 M0 is urgent and everything else is not

**Capture is never gated.** Raw option chains cannot be reconstructed after the fact; code can be written at any time. M0 depends on nothing downstream — it is a fetch, a schema check, and a write.

Ship M0 first, standalone, and start the daily capture cron **before building M1**. Every subsequent milestone recomputes retroactively from stored Parquet, so days captured during the build count as study days.

The only other thing that cannot be backfilled is the vendor reference log (§8). That also starts on day 1, by hand.

### 1.2 Definition of a valid day

A day is **valid** if and only if:

- all three surface captures landed with parseable vendor timestamps, and
- the OI base for that day landed, and
- normalization retained ≥95% of expected contracts, and
- no unresolved severe data-quality flag (§6.4)

Validity is a property of **captured data only**. It does not depend on which analysis milestones existed on that date. A day captured during M1 development is a valid study day once M2 recomputes it.

---

## 2. Research questions

### Q1 — Vendor agreement

Per paired observation, record:

```
flip_self, flip_vendor, flip_error_pts = flip_self - flip_vendor,
flip_abs_error, same_side_of_spot (bool)
```

Report **median error, IQR, and stability of the median across the window**. A stable non-zero median is a calibration finding, not a failure. A wide or drifting IQR is a failure.

**Only paired observations count** — see §8.1. Unpaired vendor rows are stored but excluded from Q1.

### Q2 — Frozen-map descriptive usefulness

15 days is **not powered** to test whether flip crossings predict anything. The answerable question is about accrual rate and coherence:

```
n_crossings_per_day, post_cross_move_30m, post_cross_move_to_close,
realized_range_30m_before vs after, recross_within_30m (bool)
```

Deliverable: a descriptive table plus an estimate of how many trading days would be needed to reach n=40 crossings. **No significance claims.**

### Q3 — Surface repricing contribution

Distribution of mechanical vs surface components (§10) for `net_gex`, `flip`, `call_gex_peak`, `put_gex_peak`, at t1 ∈ {14:00, 15:45}, by expiry bucket.

### Q4 — Research value

Manual field per day: `added_value: yes | partial | no | unclear` plus one line.

---

## 3. Repository layout

```
spx_gex_experiment/
├── README.md
├── config/
│   ├── experiment.yaml
│   └── secrets.example.yaml        # never commit real keys
├── data/
│   ├── raw_chain/YYYY-MM-DD/{oi_base,0945,1400,1545}.parquet
│   ├── raw_chain/YYYY-MM-DD/*.meta.json
│   ├── normalized/YYYY-MM-DD/
│   ├── manual_input/
│   │   ├── vendor_reference.csv    # hand-maintained, see §8
│   │   └── transmission_spot.csv   # hand-dropped export, see §5.3
│   └── derived/YYYY-MM-DD/
├── src/spx_gex/
│   ├── config.py
│   ├── providers/                  # one file per data vendor, see §4
│   │   ├── base.py
│   │   └── <vendor>.py
│   ├── normalize.py
│   ├── expiry.py
│   ├── greeks.py
│   ├── exposure.py
│   ├── flip_solver.py
│   ├── peaks.py
│   ├── frozen_map.py
│   ├── actual_map.py
│   ├── attribution.py
│   └── reporting.py
├── scripts/
│   ├── capture_oi_base.py
│   ├── capture_surface.py
│   ├── validate_greeks.py
│   ├── run_frozen_map.py
│   ├── run_actual_map.py
│   ├── build_daily_report.py
│   └── build_summary.py
├── output/daily/, output/summary/
├── tests/
└── docs/METHODOLOGY.md, KNOWN_LIMITATIONS.md
```

No UI. No web server. No plotting library beyond static PNG in the daily report if useful.

---

## 4. Data provider adapter

The vendor is **not yet chosen** (candidates: Massive/ex-Polygon, ThetaData, Tradier, CBOE delayed). Build a thin adapter so switching is a parser change, not a rewrite.

`providers/base.py` defines:

```python
class ChainProvider(Protocol):
    def fetch_chain(self, underlying: str, asof: datetime) -> pd.DataFrame: ...
    # MUST return the RAW vendor payload flattened to a DataFrame,
    # with vendor column names preserved. No renaming, no filtering.
    def provider_name(self) -> str: ...
```

Normalization happens in `normalize.py`, never in the provider. **Raw Parquet stores the provider's own column names**, so a later vendor switch does not invalidate history.

Implement one provider. Leave `base.py` as the contract.

---

## 5. Capture

### 5.1 OI base — once per day, before 09:30 ET

Open interest is published by OCC overnight and does **not** change intraday. The value fetched in the morning is as-of the prior session's close. Record this explicitly as `oi_asof`.

Required fields (map from vendor names in `normalize.py`):

```
root, symbol, option_type, strike, expiration_date,
expiration_timestamp, settlement_type, multiplier,
open_interest, volume, quote_timestamp, underlying_spot
```

Capture both `SPX` and `SPXW`. Do not merge roots before settlement metadata is retained.

Expiry scope: all expirations with DTE ≤ 45. Configurable as `expiry_scope_max_dte`.

### 5.2 Surface snapshots — three per day

```
09:45 ET   surface base for the frozen map
14:00 ET   attribution t1 (must match vendor screenshot time, §8.1)
15:45 ET   attribution t2
```

Fields: `bid, ask, mid, last, vendor_iv, vendor_delta, vendor_gamma, volume, open_interest, underlying_spot, quote_timestamp`.

**Do not capture an IV surface pre-market.** SPX/SPXW quotes outside regular hours are too thin to anchor a day.

### 5.3 Delayed-data handling

If the provider tier is delayed (commonly 15 min), the delay's impact on gamma scales as `delay / T`:

| Time | Time left (0DTE) | ATM gamma error from 15-min staleness |
|---|---|---|
| 09:45 | 6.25 h | ≈ 2% |
| 14:00 | 2.0 h | ≈ 6% |
| 15:30 | 0.5 h | ≈ 18% |

So delayed data is acceptable early and unusable late for 0DTE.

Rules:

- **Always compute T from the vendor's `quote_timestamp`, never from wall-clock execution time.**
- Set `config.provider_is_delayed`. If true, the 15:45 capture is labelled `1545` but its effective asof is whatever the vendor timestamp says; the report must display both.
- If `provider_is_delayed` is true, the 0DTE layer is **excluded** from the 15:45 row (§9.2). Other buckets are retained.

### 5.4 Transmission spot — manual drop

`data/manual_input/transmission_spot.csv`:

```csv
date,time_label,spot,source_timestamp
2026-07-27,0945,7411.98,2026-07-27T09:45:03-04:00
```

Times: `0945, 1130, 1400, 1545, 1600`. Hand-exported. The experiment never reads any external project's files.

---

## 6. Normalization

### 6.1 Timestamps

All timestamps stored **tz-aware UTC** in Parquet. Convert to `America/New_York` only for display.

### 6.2 Expiration timestamps

This is the highest-risk field. Derive it yourself; do not trust the vendor's value without checking it in §7.

```
SPXW (PM-settled):  expiration_date @ 16:00:00 America/New_York
SPX  (AM-settled):  expiration_date @ 09:30:00 America/New_York
```

AM-settled SPX monthlies (third Friday) stop trading at Thursday's close and settle Friday morning against SET. From Thursday 15:45 the true remaining time is ≈17.75 h, not ≈24.25 h — a ~27% error in T, ~17% in gamma, recurring once a month.

### 6.3 Time to expiry

```
T_years = (expiration_timestamp - quote_timestamp).total_seconds() / (365.0 * 86400)
```

Calendar-time convention by default. `config.time_convention: calendar | trading` — §7 determines which the vendor uses; set it to match, and record the choice with every result.

### 6.4 Filters

Apply and **count** each. Every filter writes a row to the day's data-quality record:

- drop `open_interest == 0` (retain in raw)
- drop crossed markets (`bid > ask`)
- flag `bid == 0` (retain, mark `zero_bid = true`)
- flag `quote_timestamp` older than 5 min from capture time (`stale_quote = true`)
- drop duplicate `(root, expiration_date, strike, option_type)`
- flag `vendor_iv` null or ≤ 0

Severe flag (invalidates the day): >5% of expected contracts dropped, or the underlying spot missing.

### 6.5 Expiry buckets

Emit every result separately for: `0DTE`, `1-7DTE`, `8-30DTE`, `aggregate`. Never collapse before storing the layers.

---

## 7. Greek validation — mandatory gate before M2

Sample ~24 contracts covering: ITM/ATM/OTM × call/put × {0DTE, 1–7 DTE, 8–30 DTE}, and **at least two AM-settled SPX contracts** (§6.2).

### 7.1 Two separate reconciliation paths

These test different things and must not share one tolerance.

**Path A — formula check.** Feed `vendor_iv` into your own Black–Scholes gamma and compare to `vendor_gamma`. Same closed form, same inputs; any gap is a bug in your `T`, `r`, or `q`.

> **Hard gate: median absolute percentage error < 1%.** Do not proceed to M2 if this fails. A 5% systematic `T` error passes a 10% gate and silently corrupts all 15 days.

**Path B — inversion check.** Invert IV yourself from `mid`, then compute gamma, compare to `vendor_gamma`.

> **Soft gate: median absolute percentage error < 10%.** Exceeding this is expected on wide-spread strikes; record the cause and continue.

### 7.2 Reverse-engineering the conventions

Path A failing is informative. Grid-search to find which convention reconciles the sample:

- `r` ∈ {0, 3m T-bill, SOFR, vendor-implied}
- `q` ∈ {0, trailing SPX dividend yield, vendor-implied}
- `time_convention` ∈ {calendar, trading}
- expiration timestamp ∈ {your §6.2 derivation, vendor's field}

Write the winning combination into `config/experiment.yaml` and `docs/METHODOLOGY.md`. Report residual bias by moneyness and by DTE — an unexplained systematic tilt in either dimension is a stop condition even if the median passes.

### 7.3 Unit-test fixtures

Deterministic values, `q`-adjusted Black–Scholes, `Γ = e^{-qT}·φ(d₁)/(S·σ·√T)`:

| S | K | r | q | σ | T | gamma |
|---|---|---|---|---|---|---|
| 100 | 100 | 0.00 | 0.000 | 0.20 | 1.0 | 0.0198476274 |
| 100 | 110 | 0.00 | 0.000 | 0.20 | 1.0 | 0.0185819222 |
| 7400 | 7400 | 0.045 | 0.013 | 0.15 | 0.25 | 0.0007090754 |
| 7400 | 7400 | 0.045 | 0.013 | 0.15 | 1/365 | 0.0068654434 |
| 7400 | 7000 | 0.045 | 0.013 | 0.18 | 0.5 | 0.0003459537 |

Match to 1e-9.

---

## 8. Vendor reference log — starts day 1, by hand

`data/manual_input/vendor_reference.csv`:

```csv
date,snapshot_time,vendor_net_gex,vendor_flip,vendor_call_wall,vendor_put_wall,expiry_basis,notes
```

### 8.1 Time alignment is required for Q1

`flip_error_pts` must not absorb the flip's own intraday drift. On a normal day the flip moves 15–25 points, which is the same order as the methodology gap being measured.

> **Take the mid-session vendor screenshot at 14:00 ET**, matching the surface capture. Set `paired = true` only for vendor rows whose `snapshot_time` matches a self-computed time exactly.

Q1 statistics use paired rows only. Unpaired rows are retained for context.

### 8.2 Attribute disagreement to one of three buckets

Never silently tune parameters to close a gap. Classify each:

```
definition_gap        known convention difference (expiry scope, sign, wall definition)
numerical_error       your bug
unknown_methodology   vendor's undisclosed processing (smoothing, flow overlay)
```

---

## 9. GEX definition

Declare it; do not claim vendor equivalence.

```
d₁ = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
Γ  = e^{-qT} · φ(d₁) / (S·σ·√T)

GEX_contract = Γ · OI · multiplier · S² · 0.01        # dollars per 1% move
Net GEX      = Σ_calls GEX_contract - Σ_puts GEX_contract
```

Sign convention: dealers assumed long calls / short puts relative to customers — the standard naive convention. It is an assumption, not a measurement. State it in every output.

Stamp on every result row:

```
gex_definition_version, sign_convention, expiry_scope, iv_alignment,
oi_asof, surface_timestamp, spot_timestamp, time_convention, t_floored
```

### 9.1 0DTE numerical instability

Gamma scales as `1/√T`. With spot completely unchanged, a single ATM strike holding 1,000 contracts of OI contributes:

| Time | Time left | $GEX per 1k OI |
|---|---|---|
| 09:45 | 6.25 h | 0.32 B |
| 14:00 | 2.0 h | 0.56 B |
| 15:30 | 0.5 h | 1.13 B |
| 15:45 | 0.25 h | 1.59 B |

A 5× swing from time decay alone, against a total Net GEX that typically runs −1.5 B to −3 B. This is a real ATM-gamma singularity (it is the mechanism behind pinning), but the resulting number is **not comparable across days**.

### 9.2 Treatment

```yaml
zero_dte_late_session: exclude        # exclude | floor
min_time_to_expiry_minutes: 30        # used only when mode == floor
```

Default `exclude`: when remaining time drops below the floor, the 0DTE layer is omitted from that row and other buckets are reported normally. Set `zero_dte_excluded = true` on the row.

**The 16:00 row never contains a 0DTE layer.** For PM-settled SPXW, 16:00 *is* the settlement instant — `T = 0` and the contracts no longer exist. Emit only `1-7DTE`, `8-30DTE`, `aggregate` at 16:00.

---

## 10. Frozen-surface mechanical map (M2)

### 10.1 Inputs

```
OI            frozen at the day's OI base
IV surface    frozen at the 09:45 actual surface, attached per strike
S             current Transmission spot
T             recomputed at every point
```

**Both `S` and `T` update at every recomputation.** Updating spot alone is a specification violation — for 0DTE, time decay alone reshapes the gamma profile more than a typical intraday spot move does.

### 10.2 IV alignment

```yaml
iv_alignment: sticky_strike
```

IV stays attached to its original strike. When the flip solver shifts the trial spot, σ_K does not move with it. Sticky-delta is out of scope for this study; do not implement a switch that could be silently flipped.

### 10.3 Recomputation points

`09:45, 11:30, 14:00, 15:45, 16:00` — from `transmission_spot.csv`.

### 10.4 Output schema

One row per `(date, time_label, expiry_bucket)`:

```
spot, spot_timestamp, t_min_remaining, net_gex, flip, all_roots,
call_gex_peak, put_gex_peak, call_oi_peak, put_oi_peak,
spot_vs_flip, dist_to_flip_pct, zero_dte_excluded, t_floored,
n_contracts, + the §9 provenance stamps
```

**Do not use the word "wall" anywhere in self-computed output.** The four peak proxies are separate columns precisely because their vendor definition is unknown; after 15 days, compare each against the vendor's wall and report which is closest.

---

## 11. Flip solver

The flip is the zero of the aggregated exposure curve — not a level, not a linear field.

```
for S' in grid:
    recompute Γ at S' for every contract (d₁ changes; σ_K, T, OI fixed)
    net_gex(S') = Σ signed contributions
find all sign changes; Brent-solve within each bracketing interval
```

Config:

```yaml
grid_min_pct: -5.0
grid_max_pct: +5.0
grid_step_points: 5        # must be finer than the 25-pt SPX strike spacing
root_selection_rule: nearest_to_spot
```

Return: `all_roots` (list), `primary_flip`, `n_roots`, `root_selection_rule`, and `no_root` when the curve never crosses.

> **Multiple roots are the normal case for 0DTE, not an edge case.** Near expiry the exposure curve oscillates sharply between strikes. Do not treat `n_roots > 1` as an error, and do not average the roots. A grid coarser than the strike spacing will both miss real roots and manufacture false ones.

Tests: single root, multiple roots, no root, near-flat crossing, root exactly on a grid node.

Fixture — two contracts, `r=0.045, q=0.013, T=0.02`, multiplier 100:

```
call K=7500 σ=0.14 OI=20000
put  K=7300 σ=0.17 OI=20000
```

Expected: exactly one root, `primary_flip ≈ 7377.91` (±0.01). Curve runs −1.19 B at S=7300 to +0.39 B at S=7400.

---

## 12. Actual-surface refresh and 2×2 attribution (M3)

For baseline `t0 = 09:45` and each `t1 ∈ {14:00, 15:45}`, evaluate four states with OI fixed throughout:

| State | Spot / Time | IV surface |
|---|---|---|
| A | t0 | t0 |
| B | t1 | t0 |
| C | t0 | t1 |
| D | t1 | t1 |

C is a counterfactual state that never existed in the market — the t1 surface applied at the t0 spot. Under sticky-strike this is a straight column swap. Store the moneyness shifts and per-strike IV changes for audit.

### 12.1 Reporting

```
total_change            = D - A
mechanical_component    = B - A
surface_component       = D - B

mechanical_contribution = 0.5 · ((B - A) + (D - C))
surface_contribution    = 0.5 · ((C - A) + (D - B))
```

The second pair is the order-independent (Shapley) allocation for two factors. Report both.

Apply to `net_gex`, `flip`, `call_gex_peak`, `put_gex_peak`, per expiry bucket.

**Identity test:** `mechanical_contribution + surface_contribution == total_change` to floating-point tolerance. This is an M3 gate.

Language discipline: this is a counterfactual attribution under a declared model. It is **not** a causal decomposition, and the two components are not additive in any deeper sense.

---

## 13. Volume

Capture contract volume in every snapshot. It is free in the same response and it is the only observable proxy for the model's central blind spot.

Intraday OI is frozen. New 0DTE opens, closes, rolls, spreads, and dealer inventory changes are all invisible until the next overnight OCC publication. The computed quantity is therefore precisely:

> a same-day exposure proxy conditioned on the **previous** session's confirmed OI.

Derive: `total_volume, volume_by_bucket, volume_by_type, volume_near_flip (±25 pts), volume_near_peaks`.

Flag `possible_intraday_positioning_residual = true` when the frozen-OI map implies a stable regime but realized 30-minute range lands in the top decile of the study window *and* 0DTE volume is elevated. Diagnostic only.

**Never infer the direction of new dealer inventory from unsigned volume.**

---

## 14. Daily report

`output/daily/YYYY-MM-DD.md`, machine-readable YAML frontmatter plus a short human body.

Sections: data quality (captures present, delay status, vendor timestamps, contracts retained/filtered, `oi_asof`, 0DTE exclusion status) → frozen map (5×4 table) → actual refresh (14:00, 15:45) → attribution → vendor comparison (paired rows only) → research note.

Manual tail, appended by hand:

```yaml
added_value: yes | partial | no | unclear
one_line_reason: ""
```

---

## 15. Stop / continue

**Stop and report** if: Path A gamma error ≥ 1% and unresolvable; expiry/settlement mapping unresolved; vendor timestamps unreliable or absent; raw Parquet not reproducible; 0DTE instability dominates aggregates with no documented treatment.

**Proceed past M2** after 5 valid days if: Path A passed; flip solver stable across all five daily points; data-quality flags manageable; frozen-map output interpretable.

**Conclude at 15 valid days** by answering Q1–Q4. Q2 answers accrual rate, not significance. No p-values anywhere in this study.

---

## 16. Acceptance criteria

**Gating M0 → capture goes live (build these first, ship same day):**

1. Raw Parquet persists the provider's unmodified column names
2. `.meta.json` per capture records provider, endpoint, request time, vendor timestamp, row count
3. Both `SPX` and `SPXW` present
4. Re-running a capture never overwrites an existing file

**Gating M2 → analysis begins:**

5. Path A median gamma error < 1%; Path B < 10%
6. `r`, `q`, time convention, expiration-timestamp source documented in METHODOLOGY.md
7. AM-settled contracts present in the validation sample and reconciling
8. Expiry buckets correct across a month boundary
9. Flip solver passes the §11 fixture and all four root cases
10. Four peak proxies stored separately, no column named `wall`
11. 0DTE exclusion behaviour explicit and logged
12. Frozen map produces all five time rows

**Gating M3 → M4:**

13. 2×2 identity holds
14. `iv_alignment` stamped on every attribution row

**Throughout:**

15. No file outside `spx_gex_experiment/` is read or written, except manual drops into `data/manual_input/`

---

## 17. Deliverables

Repository tree; README with exact daily commands; `docs/METHODOLOGY.md` (every convention, with the §7 reconciliation result); `docs/KNOWN_LIMITATIONS.md` (frozen OI, naive dealer sign, delay, 0DTE instability, single vendor); all code and tests; one synthetic-fixture run; one real-day dry run; estimated daily API usage; **an explicit list of unresolved assumptions**.

---

## 18. Daily commands

```bash
python3 scripts/capture_oi_base.py    --date YYYY-MM-DD
python3 scripts/capture_surface.py    --date YYYY-MM-DD --time 0945
python3 scripts/capture_surface.py    --date YYYY-MM-DD --time 1400
python3 scripts/capture_surface.py    --date YYYY-MM-DD --time 1545
python3 scripts/run_frozen_map.py     --date YYYY-MM-DD
python3 scripts/run_actual_map.py     --date YYYY-MM-DD
python3 scripts/build_daily_report.py --date YYYY-MM-DD
python3 scripts/build_summary.py      --from YYYY-MM-DD --to YYYY-MM-DD
```

All analysis scripts must be re-runnable against any past date purely from stored Parquet, with byte-identical output. Reproducibility from raw is a test, not an aspiration.

---

## 19. Open questions — ask before assuming

1. **Data provider not chosen.** Confirm before implementing `providers/<vendor>.py`. Report whether the chosen tier is delayed; it changes §5.3 and §9.2.
2. `expiry_scope_max_dte` default 45 — confirm.
3. Whether `mid` should be `(bid+ask)/2` or the vendor's own mark when they differ.
4. Anything in §7.2 that fails to reconcile — stop and report rather than picking the closest fit.
