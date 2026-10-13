# Source adapter contract

Implemented adapter: `shockwatch.sources.SyntheticSource`, version `synthetic-canonical-1`. Raw generator: `shockwatch.scenarios.generate_scenarios`, version `raw-scenarios-2`.

The source bundle contains `raw/sessions.csv`, `raw/batches.csv`, `raw/bars.csv`, `raw/options.csv`, and `raw/oi.csv` with the unchanged canonical schema-1 headers. The numerical panel is built by the actual `features.build_panel` and retained as a hashed immutable cache. The live source delivers that cache through the existing CSV worker inbox. Synthetic source labels, future event outcomes, injection identifiers, and case labels stay under `truth/`; the source adapter never reads that directory.

`manifest.json` records dataset/mode, seed/specification, generator/adapter/schema versions, calendar and selection assumptions, field units, Greek method, requested/received counts, immutable file hashes, instrument mappings, missing codes, and revision/entitlement limits. Changing raw content, panel content, builder configuration, or the generation specification requires a new bundle. A partial interrupted generation has no valid manifest and is rejected as an unmanifested nonempty directory.

## Replaceable interface

`SyntheticSource(bundle, runtime, cfg)` verifies hashes and initializes the persisted source checkpoint. It exposes:

- `deliver(asof, max_cohorts=None)`: publish newly available interval cohorts to `runtime/inbox`; return emitted row/cohort counts, pending counts, file names, partial-cohort diagnostics, simulated time, and actual wall processing time.
- `next_available_at()`: the next fixed decision or later transport-ready time, whichever is later.
- `advance(minutes=60, max_cohorts=None)`: advance the persisted virtual clock without wall-clock sleeping.
- `available_context(asof)`: return already published public context from the hashed `source_archive/public_context.csv`; no detector input or evaluation truth is added.

Every output row carries `dataset_id`, `mode`, and `source_version`. `available_at` and `input_watermark` describe consumed first-release observations and are never later than the fixed decision cutoff. `source_ready_at` is the separate transport/atomic-cohort delivery time; a late release disables affected earlier features. Wall processing time is recorded separately in checkpoints/jobs. `underlying_start_price` and `underlying_end_price` are frozen completed-interval evidence on a consistent split-adjusted basis, computed as raw price divided by the cumulative prior-close factor, with no future factors applied. The original options scorer does not consume these columns; the versioned stock-only comparator may use their already observed return, and outcome evaluation may use the same evidence.

A cohort publishes via a temporary non-CSV file, flush/fsync, and `os.replace`. The checkpoint advances only after that publish. The OS-owned advisory lock is released on process exit. If a process stops after publish and before checkpoint, deterministic re-publication has the same hash and filename. A late old cohort blocks later publication so the worker cannot advance past it. Missing whole calendar sessions remain missing; no fake zero observations are generated. A deliberately missing instrument produces a partial cohort and the existing conditional-flow cohort rule remains active.

## Cumulative volume

`CumulativeVolume.observe(contract_id, session, at, cumulative, baseline=None, reset=False)` produces `interval_volume` and a reason. Differencing never crosses contracts or sessions. An explicit valid starting baseline permits a first difference; absent baseline means missing. Declared resets and negative corrections are separately quarantined as missing and establish a new anchor. An explicit missing counter also makes the following interval ambiguous, so two intervals cannot be assigned to one later hour; a valid new interval-start baseline can restore a first difference. The collector must represent expected but absent scans explicitly. Identical observations are idempotent; changed first releases and out-of-order observations are rejected. The state is JSON-serializable for restart. Observed zero remains zero. The synthetic archive retains an actual reset/correction/first-baseline fixture; resulting missing values reach the real builder.

## Provider field checklist

Before implementing a real adapter, establish these meanings from its field dictionary and permitted sample:

- Stable issuer, instrument and contract identifiers; validity/listing/expiry times; venue/currency; exercise/settlement style; multiplier and standard versus adjusted deliverable.
- Observed raw OHLC prices, shares per five-minute bar, adjustment-factor convention, dividends/splits, timestamp timezone and full expected bar population.
- Option interval volume or cumulative-counter semantics; valid baseline availability, reset/correction markers, missing-versus-zero convention, and declared point-in-time chain selection/population.
- Bid/ask/quote time, annualized decimal IV, signed delta and valuation/year-fraction/dividend conventions; quote freshness distinct from transport delay.
- OI position date, publication time, receiver receipt time, release completeness, matched population and revisions.
- Event/interval/publication/receipt/availability times; actual receiver-captured `received_at`; original-availability assumptions for archives; correction retention policy.
- Requested versus received sessions/contracts; known limitations and licensed automation/redistribution rights.

The synthetic calendar has weekdays, a deliberate two-day closure, DST, and one artificial early close. It is not a verified exchange calendar. Option values use a European Black-Scholes fixture with 2% fixed interest and no dividends; it is not a production American-equity-option engine. Weekly expiry/delta grids deliberately provide bounded coverage rather than full real chains. Version 2 declares six fixed absolute delta anchors (`.02, .15, .35, .50, .75, .98`) per call/put at five expiries, giving 60 selected contracts before expiry exclusions. Wings use genuine coherent Black-Scholes strikes/prices/Greeks and retain ATM/25-delta brackets after weekly price drift; no extrapolation or coverage threshold is changed. The declared grid is fixed independently of scenario truth and future observations. OI net changes are bounded by generated traded volume; gross traded volume and net positions are retained as different quantities.

## Verification and response reports

`tests/test_sources.py` contains meaningful checks for immutable seeded paths, canonical schemas, truth separation, cumulative rules, atomic checkpoint recovery, delayed publication, split adjustment, and tamper rejection. `evaluate_scenarios(bundle, db_path=None)` returns measured raw quality gates and optional read-only stored response counts/timing checks. The catalogue has 22 required scenario families and independent held-out repeated activity paths. Only actual detection injections appear in isolated `truth/cases.csv`; quiet and source-quality faults never inflate case-recall denominators. The related paired activity/outcome cases are one family and their two activity dates share a measured actual calendar week. Stochastic alerts are measured rather than forced to predetermined counts. Every report remains labeled synthetic; none estimates real-market predictive value.

Generator version 2 separates the omitted whole session from partial cohort/OI releases and other reserved scenario dates. Version 1 failed this full-history invariant when both fell on the same date; its failed evidence remains intact. Complete default runs omit a quiet post-training session, allowing daily history to mature before held-out comparisons.

Physical laptop lock/sleep/reboot, scheduler registration/overlap, external watchdog, real notification delivery, and off-device recovery require separate execution evidence. Their presence in a catalogue or test file does not establish that they ran.


## Executed verification update ? 13 October 2026

The complete expanded 86-test suite and raw generator v2 synthetic pipeline passed on
Python 3.13.15/NumPy 2.3.5/pandas 2.2.3. All 9 comparison candidates completed.
See ../../implementation_evidence/verification_20261013_02/FINAL_VERIFICATION.json
and the frozen packet in its synthetic-demo/experiment folder. Earlier statements
of unavailable execution describe the prior checkpoint and are superseded by
this saved evidence. Browser visual and physical/external operational gates remain
unexecuted as documented in PROJECT_STATE.md.
