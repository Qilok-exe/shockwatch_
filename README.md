# SHOCKWATCH 0.1.0

A runnable Python pilot for unusual stock/options activity and variance forecasts. It includes a canonical CSV adapter, frozen statistical baselines, chronological replay, joint alert-policy calibration, persistent scheduled worker, notification adapters and 27 tests.

**Status:** verified with synthetic data. Bloomberg is not connected. Real-market detection rates and forecast improvements remain unmeasured. Notifications default to disabled. No trade execution is included.

Start with `GAMEPLAN.md`. Read `DATA_CONTRACT.md` before importing exports and `MODEL_NOTES.md` before interpreting scores. `OPERATIONS.md` covers the laptop. `validation/VERIFICATION.md` records the checks and recovery results.

## Windows quick start

Install Python 3.12. Extract the ZIP and open Command Prompt in its `shockwatch` folder, the folder containing this README. Keep the active database on a local disk rather than inside a cloud-synced folder.

```bat
scripts\setup-windows.cmd
.venv\Scripts\python.exe -m shockwatch demo --out work\demo
```

Setup creates a local environment, installs the tested dependencies and runs the tests. The demo creates 160 synthetic sessions for three instruments: 100 training, 30 validation and 30 held out. It checks raw CSV conversion and restart behavior too. Use a fresh output directory when repeating it.

You do not need to activate the environment. Later examples use `python` for readability; on Windows use `.venv\Scripts\python.exe`.

## Linux or macOS

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-tested.txt
.venv/bin/python -m pip install -e . --no-deps
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m shockwatch demo --out work/demo
```

## After the first verified export

These commands illustrate the research workflow. Dates are examples, not acquired history. Time arguments require an explicit timezone. Each command is one line for portability across shells.

```sh
python -m shockwatch templates --out work/export
python -m shockwatch map-csv --input work/vendor.csv --mapping config/my_mapping.json --out work/export/options.csv
python -m shockwatch build-features --input work/export --config config/runtime.json --out work/panel.csv
python -m shockwatch audit --panel work/panel.csv --calendar work/export/sessions.csv --universe SPY UAL LMT --start 2022-01-01T00:00:00Z --end 2024-06-30T23:59:59Z --out work/coverage.json
python -m shockwatch fit --panel work/panel.csv --cutoff 2023-12-31T23:59:59Z --config config/runtime.json --out work/model.json
python -m shockwatch calibrate --panel work/panel.csv --model work/model.json --start 2024-01-01T00:00:00Z --end 2024-03-31T23:59:59Z --target 1 --out work/proposed_policy.json
python -m shockwatch replay --panel work/panel.csv --model work/model.json --policy work/proposed_policy.json --start 2024-04-01T00:00:00Z --end 2024-06-30T23:59:59Z --out work/test
```

The sample field mapping contains invented headings. Complete the map against actual source columns. `map-csv` performs declared renaming/scaling only: it does not guess timezones, manufacture hourly volume or infer receipt times.

Add `--cases path/to/verified_cases.csv` to calibration/replay when a reviewed benchmark exists. The research-leads CSV is intentionally rejected as a performance label file.

Calibration compares 16 whole-system policies and proposes the observed episode rate closest to the target. Review case coverage, lead time, episode duration and message burden before adoption. Rate matching alone is insufficient.

## Shadow worker

Copy the real-data model and verified calendar to `runtime/model.json` and `runtime/sessions.csv`. Keep notifications disabled. Record the reviewed policy with:

```sh
python -m shockwatch review-policy --input work/proposed_policy.json --note "Reviewed validation, cases and data quality; shadow only" --out runtime/policy.json
python -m shockwatch run-once --inbox runtime/inbox --db runtime/monitor.db --model runtime/model.json --policy runtime/policy.json --calendar runtime/sessions.csv --config config/runtime.json
python -m shockwatch status --db runtime/monitor.db
```

The inbox accepts canonical hourly panels produced by `build-features`. Finish files as `.partial` and rename to `.csv` atomically; the builder already does this. A collector that produces them from Bloomberg still needs to be implemented against the actual account.

The scheduled job reads new intervals and checks health every five minutes. It does not refit every five minutes. Historical catch-up is recorded without stale urgent market alerts. New model/policy versions require a separate shadow database to preserve ongoing episode meaning.

## Code map

| Module | Responsibility |
|---|---|
| `features.py` | Calendar, bars/options/OI, quote filters, IV surface and hourly features |
| `models.py` | Conditional flow, pricing and OI baselines; EWMA and log-HAR |
| `detector.py` | Exceptional/persistent paths, fresh daily OI, episode state |
| `engine.py` | Shared live/replay calculations, calibration and evaluation |
| `store.py` | First-release ledger, transactions, restart state, outbox, backup |
| `worker.py` | Incremental cycle, data health, digest and optional heartbeat |
| `notifications.py` | Telegram/SMTP, persisted retries and safe error status |
| `audit.py` | Missing intervals and entire sessions against the calendar |
| `synthetic.py` | Reproducible software fixtures |

CPU, SQLite, NumPy and pandas are sufficient for this pilot. No GPU, paid LLM, server or Bloomberg SDK is needed to run the demonstration.
