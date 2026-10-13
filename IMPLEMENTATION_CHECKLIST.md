# Acceptance evidence ? 13 October 2026

The finite synthetic Python pipeline passed. This is software verification with
artificial inputs, not evidence of real-market predictive value or unattended laptop operation.

| Delivery | Evidence | Status |
|---|---|---|
| P0 original baseline | 27 original tests plus original demo; preserved source hashes | Passed |
| P1 identity/timing | Immutable registries, future issue/target ordering, catch-up, rollback, calendar-HAR gap tests | Passed |
| P2 raw source | Versioned 160-session raw bundle, checkpoint delivery, delayed/partial/duplicate tests; 16 measured invariants | Passed |
| P3 outcome ledger | 1577 forecasts; 1563 complete latest outcomes, 14 incomplete retained; source-to-outcome trace | Passed |
| P4 local website | 46 pages, links, full HTTP bodies, read-only routes, provenance/escaping tests | Programmatic checks passed; browser visual review declined |
| P5 comparisons | All 9 candidates, 77 paired daily targets, 90 fine/coarse price paths, blocked resampling and family grouping | Passed synthetic execution |
| P6 core recovery | 421 wakes, 211 forward recovery wakes, numerical/cursor parity, duplicate rerun and complete-state backup restore | Passed software checks; physical/scheduler checks remain |

The final expanded suite ran 86 tests successfully. The preserved original 27 tests
were also executed independently. The largest sampled Python peak working set
was 321.3 MiB; numerical threads were capped at 1 and verification jobs ran serially
BelowNormal. No owned compute job or preview server remains active.

Primary evidence is ../../implementation_evidence/verification_20261013_02/FINAL_VERIFICATION.json.
The full run's VALIDATION.json, unit logs, VERIFICATION.json, experiment packet,
scenario evaluation, source/forecast/outcome trace and both recovery reports are
saved alongside it. Final website-only checks are in
../../implementation_evidence/website_verification_20261013_final/VALIDATION.json.
The earlier failed v1 experiment and access-block records are preserved.

Repaired defects: mixed aware timestamp parsing; intraday HAR gaps; overlapping
missing-session/partial-OI fixtures; a fixed quote grid lacking genuine brackets
after price drift; status-only experiment acceptance; and prematurely closed HTTP
verification responses. Quality thresholds, no-extrapolation rules and 80% common
validation coverage were maintained. Generator v2 preflight reached 79/90 days.

Remaining: browser visual review (localhost permission declined), physical lock/
suspend/reboot tests, Task Scheduler registration/overlap, independent watchdog,
off-device recovery, real feeds/entitlements, external delivery, hosting and empirical
research value. None was presented as passed. Use README for launch/status/stop.


Forecast availability detail: 1,577 immutable records include 1,499 issued numerical
predictions and 78 explicitly unavailable forecasts after history gaps. 1,563 targets
were fully observed and 14 remain incomplete; 1,486 forecast losses were evaluated.
Both negative and positive episode follow-up returns are retained; no trade win
criterion is implied. requirements-verified.txt pins the resolved dependencies.
