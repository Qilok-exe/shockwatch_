# Canonical input contract

These are internal CSV names and columns, not Bloomberg mnemonics. Exact empty headers are in `templates/canonical` or can be generated with `shockwatch templates`. Schema version is `1`; feature version is `0.1.0`.

Every timestamp requires `Z` or a numeric UTC offset. Ordering uses UTC, while a supplied exchange calendar defines sessions. Normal US cash sessions produce six 60-minute intervals and a final 30 minutes; shortened sessions use their actual close. Naive timestamps are rejected.

## Input files

| File | Key and contents |
|---|---|
| `sessions.csv` | One `session` date with `open_utc`, `close_utc`. Include prior dates for OI/overnight joins and future dates for forecast targets. |
| `batches.csv` | Instrument and interval end. `instrument_id`, `sector`, `interval_start`, `interval_end`, `available_at`, `expected_contracts`, `complete`, `source`, `schema_version`. |
| `bars.csv` | Instrument and five-minute bar end. `bar_start`, `bar_end`, raw `open`, `high`, `low`, `close`, share `volume`, `previous_close_factor`, `available_at`. |
| `options.csv` | Contract and interval end. Instrument/contract IDs, interval end, expiry/last-trade UTC, put/call C/P, strike, multiplier, standard-deliverable flag, bid, ask, quote time, IV, signed delta, interval volume, availability. |
| `oi.csv` | Contract and position date. `contract_id`, `position_date`, integer `open_interest`, `published_at`, `available_at`. |

`available_at` is the earliest time the completed observation could actually be used. Live data use local receipt. Historical studies without receipt logs use documented conservative availability assumptions; a later archive download timestamp is not the original publication. Preserve receipt/publication evidence and adapter assumptions with the raw export.

`interval_volume` counts contracts traded in that interval, not daily cumulative volume. A vendor-specific adapter must difference cumulative counts within contract/session, handle resets/corrections and leave unobservable first differences missing. Empty exports are not observed zero.

The batch manifest attests that the explicitly scoped chain is complete. `expected_contracts` must match unique returned rows. Parsing all returned rows does not establish chain completeness. Source or documented collection rules must define point-in-time membership; do not select contracts using later activity. Main flow requires the whole scoped batch to be timely and complete.

IV is annualized decimal volatility: 25% is `0.25`. Delta is signed in (-1,1). Supply a consistent, documented vendor methodology. Quote time and Greek/price fields may be missing, in which case quote-dependent features are unavailable; that alone does not erase valid observed trade volume.

`standard_deliverable` explicitly controls eligibility. The starter excludes adjusted/nonstandard deliverables and nontrading/expired contracts. The source archive must still retain exercise style, settlement, currency, deliverables, corporate actions and issuer identity. Those are not inferred by this adapter. Foreign markets, index options and adjusted contracts need separate reviewed rules.

`previous_close_factor` multiplies the immediately prior raw close onto the current share basis. Use `0.5` for a 2-for-1 split. Within-session returns compare the current close with the adjusted prior close; first-session-bar returns compare close with current open. Overnight return compares current open with adjusted prior-session close. Do not feed adjusted prices and apply the same adjustment twice. Cash dividends and option deliverables require explicit reconciliation before production.

## Timing and quality

The initial decision deadline is interval end plus ten minutes, to be changed if measured feed delay requires it. Later arrivals cannot repair an original decision. Keep late data for separately labeled research. Production availability conventions must be checked with the Bloomberg sample.

Underlying intervals require every expected five-minute bar, finite positive OHLC, consistent high/low, nonnegative volume and a positive adjustment factor. Incomplete bars mark RV missing and block that interval's alert inputs. Overnight variance is separate.

Pricing and flow have independent gates. A stale or unavailable quote can disable the surface while its observed interval volume remains usable. Valid quotes require positive bids, uncrossed asks, acceptable relative spreads, positive bounded IV and permissible quote age. Future quote times cannot enter either surfaces or delta-based contract-mix covariates. The initial age/spread/coverage settings are provisional.

No pricing feature extrapolates beyond available deltas or expiries. The 30-day surface can exist while the 60-day term slope is unavailable. Main bands are [7,30] and (30,90] actual days to expiry; `7_30` and `31_90` are short labels. [0,7) and (90,180] volumes remain separate research features.

Half-hour volume is converted to an equivalent hourly rate for modeling. Contract count, mean DTE and fraction of timely known-delta contracts with |delta| below .35 describe chain mix. The last measure is a delta-based proxy, not forward moneyness or a volume-weighted signal.

OI compares matched contracts across consecutive exchange position dates, requiring at least 80% main-chain matching coverage. Net change is divided by prior matched OI. Both publication and availability must precede the decision. The position-date token can supply fresh support once only. This is conservative for new listings and staggered releases; a full publication-batch manifest, gross position attribution and roll matching are later work.

The adapter accepts first-release input. Duplicate keys/revisions are rejected. The ledger retains the first accepted row and rejects changed contents under that key. Keep revisions in a separate source/version archive and replay them into an isolated research database. Original live decisions remain immutable.

## Panel and worker boundary

`build-features` produces the hourly panel for fitting, replay and monitoring. It includes interval/decision/calendar-target timestamps, RV, stock volume, four main flow groups, chain covariates, surface values, OI token/ratios, eligibility, source/version and quality reasons.

Run `audit` against the supplied calendar and expected watchlist before fitting or evaluating real data. It identifies wholly missing sessions. Calibration's observed-row eligible fraction alone cannot detect a day omitted from every file.

Publish all pilot instruments for an interval together. Conditional flow context requires the full model cohort; a partial cohort disables that flow calculation, while valid exceptional pricing may still be evaluated. The global watermark freezes processed groups. Later rows at a processed timestamp are retained but not back-inserted into live state. Missing observations cannot clear an episode.

The worker archives successful panel imports. Preserve original bars/options/OI separately: derived panels cannot support a later redesign of strike selection or surface construction. The ten-session pilot tests data access; it does not supply enough history to meet every fitting requirement.

## Benchmark labels

The compact evaluation file has `case_id`, `event_group`, stable `instrument_id`, `window_start`, `public_at`, `status`, `source_url`. Accepted statuses are `verified_documented` and `synthetic`. Related labels share a group; a family crossing an evaluation boundary is excluded. Metrics count groups and distinguish new episode hits from updates to an existing episode.

Maintain a richer source registry with trade-time ranges, first announcement and timezone, case publication, source passage, jurisdiction, asset type, outcome/date, reviewer and uncertainty. If only a date is known, retain an interval and perform timing sensitivity analysis. Do not fabricate an exact hour for the parser.

`benchmark_research_leads.csv` is a research starting list. It is not a finished benchmark. 9/11 is not an insider-positive label, and synthetic cases never mix with actual enforcement labels.
