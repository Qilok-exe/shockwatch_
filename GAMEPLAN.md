# SHOCKWATCH: approved implementation gameplan

Prepared 10 September 2026; recovered and verified 11 September 2026 after the workspace outage. This continues the research report and our subsequent decisions. The two alert routes and notification choices here supersede the earlier report's single composite alert sketch. Historical detection and live prediction remain separate workstreams with shared data engineering.

**The immediate objective is a dependable laptop pilot, followed by a measured decision about whether options improve detection and forecasting.** Working software now imports canonical data, computes features, fits baselines, replays decisions, compares alert policies and runs scheduled monitoring. The remaining bottleneck is verified market data and evaluation on it.

## 1. Ownership and chain of command

| Role | What it does | Authority |
|---|---|---|
| You, project owner | Set purpose, budget, watchlist and acceptable interruptions; provide laptop and Bloomberg access | Final say on scope, spending and use of results |
| Me, technical lead | Maintain the method, write/review code, help extract data, build benchmarks, inspect failures, compare models and implement agreed improvements | Responsible for technical quality and explaining evidence; no continuous awareness between chat sessions |
| Python worker | Read new data, validate it, compute approved scores, track episodes, write logs, retry notifications and send health signals | Executes frozen configuration; cannot invent a model or change thresholds |
| Optional LLM | Retrieve and summarize timestamped public explanations for an already detected episode | May attach sourced commentary; cannot suppress numerical evidence, declare insider trading or change production rules |
| External watchdog | Notice that the laptop stopped checking in | Reports an outage independently of the laptop |

The worker handles each interval without an LLM. We review its evidence and revise it when warranted. Your approval covers this build and the agreed design; routine technical work does not require another design questionnaire. Data purchases, broader scope and paid hosting remain your decisions.

## 2. Execution stages

| Stage | What we do and why | Output and completion check | Main owner |
|---|---|---|---|
| 1. Run the local pilot | Install the small Python environment and run the synthetic demonstration. Establish that the laptop reproduces the supplied checks. | Tests pass; replay equals the persistent worker; restarting adds no duplicate decisions. | You run setup; I diagnose issues |
| 2. Prove Bloomberg access | Start with SPY, UAL and LMT for ten completed sessions. Confirm fields, chain coverage, timing and export mechanism before requesting years of data. | Raw exports, stable identifiers, field map, availability assumptions and one reviewed session. | Together |
| 3. Build the collector and checks | Map the sample into the canonical contract; automate the method the account supports. Establish calendar and completeness checks. | Hourly panel arrives automatically; missing/stale inputs generate health failures; source rows remain reproducible. | I implement; you provide account-side access |
| 4. Assemble research data and labels | Acquire broader modern history and documented cases. Build the 2001 test separately using available frequency and historical issuers. | Chronological folds, coverage report and source-linked case registry, with unresolved timestamps marked. | I build and audit |
| 5. Fit and compare models | Establish stock-only and seasonal baselines, then test the extra contribution of options, persistence and OI. | Held-out forecast loss, case coverage, lead time, episode burden and ablation tables. | I implement and review |
| 6. Select and freeze a policy | Compare both alert routes together at several interruption targets. Inspect retained and missed cases. | Versioned policy, chosen tradeoff and untouched final test result. | I recommend; you assess usefulness |
| 7. Shadow operation | Run collection/calculation without urgent notifications, checking stability and explanations. | Proposed minimum 20 complete sessions; repaired data faults; observed episode lifetimes; successful outage/recovery drill. | Worker runs; I review diagnostics |
| 8. Enable monitoring | Add Telegram, email fallback/digest and an independent watchdog. Expand the watchlist after runtime/data checks. | Scheduled worker, notification test on your devices, daily summary, backups and recovery procedure. | Together, then worker |
| 9. Maintain and expand | Monitor drift, fit challengers offline and admit improvements only with measurable benefit. | Periodic comparisons and explicit promotion or rejection of proposed changes. | Worker measures; I review when we reconnect |

Twenty shadow sessions are an operational starting point, not sufficient statistical proof. Stages 2-6 depend mainly on permissions and historical coverage. Once the data are clean, numerical calculations should be easier to maintain than collection.

## 3. Exact first data request

Start with SPY, UAL and LMT, ten recently completed US sessions. Request:

1. Five-minute underlying OHLCV, timestamps, corrections/adjustments and trading calendar.
2. Hourly option interval volume and quote snapshots, with quote timestamps, bid/ask, supplied IV and signed delta where available.
3. Daily contract OI, position date, release convention and actual receipt information where available.
4. Historical contract and underlying IDs, put/call, strike, expiry and last-trade times, multiplier and deliverable status.
5. The eligible chain at each observation time, including expired contracts in archives, and a way to verify completeness.

Separate volume coverage from the quote grid. Broad eligible-contract volume helps detect concentrated trading; selected liquid strikes and bracketing expiries support the IV surface. Quantify that tradeoff on the sample instead of downloading every possible field. The starter uses the supplied scoped chain and records its contract counts, so scope must stay consistent or be explicitly modeled.

Confirm earliest dates per field, expired-chain access, intraday volume definition, cumulative resets, delays, revisions, IV/Greek methodology, unattended collection, allowed machine/session, export limits and permitted retention. These are account-specific questions. The [Bloomberg developer guide](https://data.bloomberglp.com/professional/sites/10/2017/03/BLPAPI-Core-Developer-Guide.pdf) is a legacy reference for request concepts; current access must be established directly.

Next test one complete 2019 session and one 2020 stress session before requesting bulk history. If historical hourly options are unavailable, continue hourly stock work and the daily historical benchmark. Do not manufacture hourly OI or infer an intraday chain from end-of-day survivors.

For 9/11, begin with the original 2001 AMR and UAL issuers and SPX around 27 August-21 September 2001. Request daily observations where available, then earlier history for the papers' baselines. Keep SPX separate and preserve the market closure calendar. Exact article replication requires verified definitions and inputs, not merely similarly named formulas.

## 4. What detects discrepancies

| Component | Calculation and purpose | Implemented now | Before judging market value |
|---|---|---|---|
| Conditional option activity | Calls/puts and 7-30/30-90-day groups. Model log hourly-rate volume using slot, stock/market/peer activity, contract count, maturity mix, delta-based contract mix and spread. Retain raw anomalies. | Ridge models and robust residual scales | Check chain/liquidity effects; compare simpler baselines and finer moneyness strata |
| Option repricing | Fix maturity at 30 days and deltas; calculate ATM-proxy IV, put-call skew and a 60/30-day slope. Score changes against stock/slot history. | Quote filters; delta and total-variance maturity interpolation without extrapolation | Review Greek conventions, sparse strikes, news and peer-relative pricing |
| Persistent flow | CUSUM accumulates above a training reference; centering accounts for the upward bias of taking the largest score. | Separate call/put accumulators, trading-time weights and missing-data handling | Compare recall, lead time and episode duration across folds |
| OI confirmation | Compare the same contracts across consecutive position dates using available releases. Repeated hourly copies cannot create fresh evidence. | Net matched-contract OI change, coverage and release token | Full roll/corporate-action reconciliation, gross additions/removals and publication-batch audit |
| Volatility | Five-minute RV, separate overnight RV, seasonal EWMA and regularized daily log-HAR using 1/5/22 completed sessions | Next-interval EWMA; next-session EWMA/HAR; QLIKE comparison | Tune on training/validation; add known IV and intraday state if useful |

**Route A:** exceptional flow or option repricing can trigger alone. **Route B:** persistent moderate call/put activity needs pricing support or fresh OI support. A strong single feature is not diluted by averaging unrelated features. Call activity alone does not identify buying, and OI growth alone does not identify a bullish trader.

The 7-90-day group is the main detector. Volumes in [0,7) and (90,180] days are separate research summaries with no main alerts. These are option maturities, not assumptions about how early insiders trade. Whether most documented activity falls in the chosen range remains a benchmark question.

Raw scores are retained for diagnosis, not yet an independent trigger. Peer-relative surface models, exact article replications, GJR-GARCH, CPIR, five-session forecasts, forecast bands, conformal methods and supervised insider classifiers are not claimed as implemented.

## 5. Statistical evaluation

Use a proposed initial split of 24 months training, three months validation and three untouched testing, adjusted for available history. Repeat chronologically across regimes. Fit transformations, seasonality, scales and coefficients inside training; choose policies/hyperparameters on validation. Do not repeatedly reuse a final test to choose a winner.

Group related trades, announcements and issuers into event families; purge labels crossing fold boundaries. Record the earliest public catalyst time and the documented trade-time interval. Start with a work target of 20 carefully resolved independent families, then broaden as coverage permits. This count is not a guarantee of statistical power.

Panuwat options and Zina share cases are starting leads. The SEC's [2024 Panuwat release](https://www.sec.gov/enforcement-litigation/litigation-releases/lr-25970) reports a civil jury finding, and the [FCA's Zina release](https://www.fca.org.uk/news/press-releases/mohammed-zina-found-guilty-insider-dealing-fraud) describes share transactions. Extract actual identifiers and times before performance calculations. These are not already downloaded market-data benchmarks in the package.

Measure four things separately:

1. **Forecast quality:** QLIKE on identical target observations; distinguish intraday and overnight-inclusive session targets. Compare EWMA, HAR and challengers. Add interval coverage only when forecast intervals exist.
2. **Documented-case coverage:** independent eligible events warned before public news, lead time and missed cases. Report new episode detections separately from material updates to an existing episode.
3. **Interruption burden:** new episodes/week, material updates/week, episode duration, instrument concentration and daily summaries. Unlabeled alerts are not automatically false positives.
4. **Reliability:** expected/received observations, stale features, runtime, notification status, outages and restoration time.

Hourly/daily comparisons must share instruments, source coverage, event families and comparable alert burden. Compare stock-only, flow-only, pricing-only, flow plus pricing, and the contribution of persistence/OI. The kit supplies replay and metrics. Full walk-forward experiments, the daily detector, stock-only anomaly comparator, clustered uncertainty estimates and ablation runner are the next research implementation after sample verification; they have not already been run.

Resample whole market-date blocks and event families for uncertainty. Related hourly observations and sector-wide moves are not independent successes. Enforcement cases describe discovered activity, not all insider trading. The [9/11 Commission's findings](https://www.9-11commission.gov/report/911Report.pdf) also mean 9/11 stays a historical test, rather than a labeled proven-insider positive.

## 6. Alert volume and adoption

Compare provisional targets of 0.5, 1, 2 and 5 new high-priority episodes per portfolio week. One per week means a threshold whose validation behavior is roughly that busy. It never suppresses a second qualifying event because an allowance has been used up. A continuing episode produces an opening alert, then a new evidence route or substantial severity escalation.

The calibrator compares thresholds, persistence and quiet-time closure together, reporting both new episodes and urgent updates. Its rate-matching proposal starts the review; it does not optimize case detection. A busier candidate can be preferable if extra coverage or lead time justifies it.

Adopt a model only when it improves the agreed held-out metric at comparable burden, preserves acceptable data coverage and does not depend on one event or fragile feed. With a small uncertain gain, keep the simpler model. Scores are not probabilities of insider trading or attacks.

## 7. Laptop operation

Use the second laptop first. No GPU or dedicated server is required for the pilot. On Windows the supplied task checks every five minutes while the user is logged in, including a locked screen. Wake depends on the machine; the script does not guarantee operation while logged out. Test actual sleep/restart behavior.

The worker ingests incrementally, calculates new intervals, retains immutable decisions, retries a persistent notification outbox and creates a daily SQLite backup. It retains seven locally generated daily backups and archives processed input panels automatically. Keep the original licensed raw exports too. A remote watchdog is necessary for total laptop failure. The adapter is present; configure the external service and test an outage.

Telegram is the urgent channel; email handles API-failure fallback and the daily digest. Telegram acceptance is not confirmation of phone display or reading. The [Bot API](https://core.telegram.org/bots/api#sendmessage) returns a message after a successful request; end-user acknowledgement is not implemented. An explicit acknowledgement button with later escalation is a possible improvement.

A laptop may also handle the first 12 names, subject to measured memory/runtime. As a scale reference, 35 names at 78 five-minute bars and 252 sessions produce about 688,000 underlying rows/year; option chains drive the larger workload. Partition raw imports, cache metadata and avoid refitting every hour. A full historical dataset should not remain in the actively scanned inbox.

Daily: automated health and digest. Weekly initially: inspect misses, stale feeds and episode duration. Monthly as an initial review cadence: compare forecast loss, score distributions, coverage and burden with calibration. Drift opens a research task; it does not silently retune production.

Move to an always-on mini-PC or server if laptop outages or feed constraints justify it. Keep SQLite first; consider Parquet/DuckDB or a larger database after measured costs warrant it. Confirm the allowed Bloomberg host before migrating. Back up completed database snapshots and source/configuration to another device, and test restoration.

## 8. Expansion order

| Priority | Improvement | Reason |
|---|---|---|
| Next: data quality | OI release manifests, corporate actions, chain coverage and event calendars | Prevent false discrepancies before model complexity |
| Next: evaluation | Stock-only/daily comparators, walk-forward folds, ablations and uncertainty estimates | Establish whether options/hourly sampling add value |
| Then: features | Finer maturity/moneyness groups, peer-relative surfaces, gross OI/roll matching, signed opening flow if licensed | Separate broad activity from concentrated positioning |
| Then: forecasts | IV-augmented HAR, GJR-GARCH, robust covariance, five-session targets and calibrated ranges | Improve a defined target when held-out evidence supports it |
| Then: explanations | Timestamped earnings/news context and optional LLM summaries | Make alerts easier to review without changing the detector |
| Later research | 0-6-day specialist, 91-180-day features, CPIR, exact article replications and wider event/sector coverage | Requires different microstructure assumptions or richer data |

The original candidate watchlist has 35 names. The data pilot is SPY, UAL and LMT. The proposed 12-name seed is SPY, QQQ, UAL, DAL, JPM, BAC, LMT, NOC, XOM, CVX, GLD and TLT. Expansion depends on point-in-time liquidity and fields. Remaining candidates: AAL, LUV, MAR, HLT, RCL, CCL, LVS, MGM, ABNB, BKNG, CB, TRV, AIG, GS, RTX, GD, KTOS, OXY, USO, VIXY, CRWD, PANW and BA. SPX stays separate.

**Next action:** run the supplied demo on the laptop. Then work through the small Bloomberg export together. No further methodological confirmation is needed; account capabilities determine collector details.
