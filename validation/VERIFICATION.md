# Software verification and recovery record

Initial implementation was verified on 10 September 2026. After the workspace outage, source and documentation were recovered from the recorded work and verified on 11 September 2026. Recovery preserved the approved methodology. Formatting was consolidated, so the recovered archive has a new file hash.

Environment: Linux, Python 3.12.14, NumPy 2.3.5, pandas 2.2.3. The Python pipeline and CLI were executed. Windows task registration has not been executed on a Windows laptop here.

**All 27 automated tests passed again.** The log is `unittest-output.txt`. Checks cover:

- Timezone requirements, DST, shortened sessions and missing entire sessions.
- Late arrivals, missing/future quote times, stale quotes, missing volume versus observed zero, missing five-minute bars, no surface extrapolation and split-adjusted overnight returns.
- OI publication timing and repeated daily OI; either exceptional trigger, supported persistence, no weekly cap, quiet closure/reopening and missing data freezing closure.
- Future values not changing fitted models and missing sessions invalidating consecutive HAR history.
- Related case families crossing folds, unverified labels and separate existing-episode update metrics.
- Transaction rollback, first-release revision rejection, backup restoration, persistent retries, email fallback and secret-free error status.
- Exact replay/worker agreement across a split restart, followed by an idempotent repeat.

## Reproduced synthetic results

The deterministic exercise has 160 synthetic weekday sessions and three named instruments: 100 training, 30 validation, 30 held out. The calendar includes an artificial early close and is not an actual exchange calendar. Tickers do not make these real market observations.

| Check | Recovered result |
|---|---|
| Post-training decisions | 1,260 |
| Worker versus replay | Exact equality |
| Repeat worker run | 0 new decisions and 0 new episode events |
| Raw adapter fixture | 7 of 7 intervals passed quality checks |
| Real notifications | None; disabled or mocked |
| Validation new episodes / urgent messages | 5 / 6 over 30 sessions |
| Test new episodes / urgent messages | 7 / 9 over 30 sessions |

The selected 16-candidate calibration policy and printed numerical metrics reproduce the previous run. Test burden was approximately 1.17 new episodes and 1.5 urgent messages per portfolio week. This illustrates a target without a cap, not a forecast of real alert frequency.

One of two held-out injected patterns opened an episode during its artificial case window; the other updated an already open episode. Both measures are retained: strict new-episode hits 1/2, new-or-updated alerts 2/2. These are software fixture results, not insider-trading detection rates. Lead hours include weekends.

On matched synthetic daily test targets, EWMA QLIKE was approximately -7.134870 and HAR -7.132058, with 90 paired targets. Lower is better, so this exercise does not justify promoting HAR. The generator is not intended to rank real financial models.

`synthetic_metrics.json` preserves full outputs. Source checksums in `source-sha256.json` identify the delivered files. The generators reproduce the exercise; large synthetic datasets and active databases are excluded from the source archive.

## Unverified work

No Bloomberg entitlement, actual field export, live collector, historical chain, real-case label dataset or trade outcome was tested. No phone, real email provider, external watchdog or Windows scheduler was activated. Full walk-forward research, daily/stock-only anomaly comparators, clustered uncertainty, detailed corporate-action/roll handling and production quality thresholds remain the next tasks in the gameplan.
