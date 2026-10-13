# Frozen SHOCKWATCH comparisons

The runner in shockwatch/experiments.py implements chronological, paired comparisons. Synthetic results describe artificial software behavior only. It never promotes a candidate, changes the production policy or interprets unlabeled alerts as crime-detection false positives.

## Specification and files

freeze_manifest freezes canonical hashes for the input panel, explicit calendar, case labels and optional raw bars, plus the universe, named dataset/mode, training/validation/test sessions, full fit configuration, candidate list, full base episode policy and threshold grid, warm-state convention, target definition, case-family assignment/exclusions, numerical variance floor, primary metrics and resampling settings. run_experiment rejects changed hashes or a changed manifest. The initial synthetic design is 100/30/30 calendar sessions; the final training session is purged because its one-session outcome window reaches validation. Shorter designs are explicitly labeled reduced history. Multiple non-overlapping chronological test folds can be prespecified in a versioned manifest; never randomly shuffle hourly observations.

Run all final candidates once under the frozen specification. Reusing a test to choose a new candidate makes that test development data. A new specification requires a fresh final holdout for any empirical claim.

save_packet writes manifest.json, results.json, paired_forecast_losses.csv, detector_comparisons.csv and measurement_comparison.csv. Dataset and mode remain in machine-readable exports. Results retain all calibration grid entries, selected policies, fitted artifacts, predictions, separate matured outcomes, test decisions/events, missed families, coverage exclusions, failed candidates and neutral ablations. Numerical outputs are reproducible from the seed and frozen inputs; actual recorded_at timestamps identify when each run created its historical/synthetic ledger.

persist_packet appends the packet and summary metrics to a database already bound to the exact same dataset and mode. The experiment ID is the frozen manifest ID. Repeating insertion of the same packet is harmless; a different result under that ID is rejected. Retain the old packet and version the specification for another run. The website can read experiments and experiment_metrics without running models.

## Forecast contract and losses

The runner uses outcomes.build_forecasts and calculate_forecast_outcome, shared with the worker. It first issues immutable predictions and only then evaluates targets. The daily comparison uses seasonal_intraday_ewma, which integrates the frozen hourly seasonal factors over the next complete session, and intraday_har, fitted to open-to-close intraday RV. Both methods share exact target IDs and issue-time rules. The delayed decision skips an already-started interval; a delayed close skips an already-started next-session target. Legacy reconstructed current-interval and close-to-close metrics are not used for prospective accuracy.

Historical/synthetic forecasts identify virtual issuance and actual wall-clock recording separately. Prospective experiments require original immutable forecasts and outcome records; the runner refuses to reconstruct old live predictions from a panel. Original stored forecast schema is normalized without changing its target or issue contract. Latest revisions are selected per forecast, then paired only when their observed target source evidence agrees. Mode mismatches, premature evaluation and false complete coverage are rejected; conflicting paired source revisions remain visible as comparison exclusions.

Only complete matured common targets enter paired QLIKE. Pending, incomplete, missing-prediction and unpaired records remain visible in the ledger. QLIKE is log(v)+y/v; the runner reports challenger-minus-baseline differences, with lower losses preferred. It does not compute percentages from raw QLIKE levels. The positive variance floor is frozen at 1e-12 by default and clipping is counted. Log-RV diagnostics and instrument/regime breakdowns are retained. Historical/synthetic regimes use training-only daily RV tertiles and the known EWMA state at issuance. Original prospective records without a frozen regime tag remain unclassified.

Optional five-minute bars support a measurement comparison on identical open-to-close sessions. Actual adjusted consecutive five-minute returns are summed within each hourly/calendar-close interval for coarse measurement. The same immutable daily forecasts are scored against both actual-path measurements on common coverage. Missing paths are excluded visibly; no coarse path is invented from an RV scalar. Neither measurement is asserted to equal latent true variance.

## Independent detector comparisons

The matrix contains stock_only_hourly, flow_only_hourly, pricing_only_hourly, flow_pricing_hourly, full_hourly, full_without_persistence_hourly, full_without_oi_hourly, full_daily and stock_only_daily. The full hourly detector uses the existing shared flow/pricing scorer and episode transition. Single-channel candidates use independent exceptional triggers; full and flow-plus-pricing candidates retain the agreed Route A/Route B structure. OI and persistence ablations disable their own contribution and remain in results even if neutral.

The stock-only baseline fits a seasonal stock-volume ridge residual using market/sector volume context that excludes the same instrument (an absent sector peer is imputed from training), a training-normalized log-RV surprise and, when the input supplies it, an absolute standardized interval return. Its small frozen combination is the maximum of those signals. It does not depend on options eligibility, surfaces, flow or OI. Each stock/options candidate receives the same validation policy grid and the same closest-burden selection rule; labels do not optimize that rule.

The daily comparator sums true interval stock/options volume once, retains the closing contract-population descriptors and eligible closing surfaces, and uses the last available daily OI token. It fits its own ridge/robust transformations and independently recalibrates its episode policy for daily frequency. Daily issuance is the maximum actual source availability across the day's observations. It does not repeat one daily volume/OI observation seven times.

Calendar completeness and paired complete instrument-days control hourly/daily and stock/options comparisons. Every required calendar interval must be present with usable underlying, flow and surface evidence for the common sample. Entirely absent days are exclusions rather than disappearing from denominators. Broader method-specific coverage, burden, case responses and events are also reported using the same frozen policy. Its eligible instrument-day has at least one usable observation; it is explicitly distinct from the complete common sample and does not support a paired improvement claim. At least 80% of expected validation instrument-days and five calendar sessions are required to calibrate a policy. Multiple valid weekly episode openings remain allowed; the weekly target is not a quota.

Inputs received after their recorded decision_at are excluded from fitting, detector state and common coverage under the frozen first-release timing convention. They cannot change earlier decisions. Later available underlying observations may still mature a target outcome through the shared availability-aware evaluator. The packet reports those transport exclusions separately.

The default continuous warm state runs post-training observations through validation before reporting test decisions. A versioned reset convention starts each reporting window with empty detector state. All candidates use the same chosen convention. A pre-test active episode can update during test; it is not counted as a new test opening. Duration records identify right censoring. Production promotion and episode-state migration remain explicit operations outside this runner.

## Families and uncertainty

Only synthetic labels in synthetic mode and verified_documented labels in historical/prospective mode are accepted. Research leads cannot be performance labels. Labels never enter feature construction or fitting. Related trades, issuers and announcements belong to event_group. Any family spanning a training/validation/test boundary is excluded as a whole, and its related observations are removed consistently. Label windows include the final session evening and end at the following local midnight. Families with missing common coverage are explicit case exclusions.

The case report distinguishes strict new-episode detection from new-or-updated detection, includes wall-clock and trading-time lead, and lists missed families. Paired family-detection differences are bootstrapped by independent family. One shock across many related names remains one family observation.

Forecast uncertainty uses prespecified circular contiguous market-date blocks, retaining every instrument in each selected date. The default is five-session blocks, 400 resamples and seed 731. These settings are frozen before test examination. The interval is on paired differences, not on unrelated marginal averages. Small synthetic case samples are software checks, not reliable estimates of real detection uncertainty.

Reliability is explicitly unmeasured by this offline runner. Worker processing delay, retries, outages, notifications, restart/recovery and backup evidence come from the operational ledger. No numerical reliability values are invented.

## Verification status

Tests in tests/test_experiments.py cover post-cutoff fit and calibration isolation, grouped family boundary exclusions, research-lead/mode rejection, frozen-source identity, daily once-only volume and missing-session behavior, immutable dataset-scoped persistence, target/issuance validation, incomplete outcomes, cross-instrument dependence and actual-path measurement.

This implementation session could not execute Python because the machine-side runtime path was inaccessible to the available execution process. These tests and the comparison packet must be executed by the project's documented verification command before calling P5 complete. Do not replace this limitation with a claimed test pass or invented experiment numbers.


## Executed verification update ? 13 October 2026

The complete expanded 86-test suite and raw generator v2 synthetic pipeline passed on
Python 3.13.15/NumPy 2.3.5/pandas 2.2.3. All 9 comparison candidates completed.
See ../../implementation_evidence/verification_20261013_02/FINAL_VERIFICATION.json
and the frozen packet in its synthetic-demo/experiment folder. Earlier statements
of unavailable execution describe the prior checkpoint and are superseded by
this saved evidence. Browser visual and physical/external operational gates remain
unexecuted as documented in PROJECT_STATE.md.
