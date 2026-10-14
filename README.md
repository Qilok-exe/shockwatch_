# SHOCKWATCH

SHOCKWATCH is a read-only stock and options anomaly research monitor. The public
landing page now shows a separate **paper-Yahoo experimental** observation run.
Earlier synthetic software evidence remains available in a dated archive. No
market predictive validity has been established; the paper run places no orders.

[Open the public static results](https://qilok-exe.github.io/shockwatch_/)

The public URL serves the sanitized `/docs` build from this repository's `main`
branch when GitHub Pages is configured. Pages needs no Python or backend in the
visitor's browser. The site reads committed records; loading a page never runs a
worker or writes to a database.

## Local results and verification

Use Python 3.11 or newer in a virtual environment and install the tested
dependencies from `requirements-verified.txt`. From the repository root:

```powershell
python -m pip install -r requirements-verified.txt
python -m unittest discover -s tests -q
python -m shockwatch synthetic-demo --out work/synthetic-demo --seed 1201 --sessions 160
python -m shockwatch serve --site work/synthetic-demo/site --port 8765
```

Open `http://127.0.0.1:8765/` in a browser. The `synthetic-demo` command creates
a finite raw-data fixture, runs the worker, checks recovery and replay, performs
the frozen experiment, and writes a dated static site. Do not reuse a nonempty
output directory. The source and runtime are local and are ignored by Git.

The active Windows `Shockwatch-Synthetic` task on the development laptop uses a
separate original project checkout and runtime. Repository publication does not
register, rename, disable, or run that task. Physical sleep, wake, reboot,
logged-out operation and genuine missed-run recovery have not been verified.
`Shockwatch-Pilot` is not installed because reviewed real-data detector prerequisites are absent.

## Paper-Yahoo observation run

Yahoo ingestion has its own `paper_yahoo_<UTC timestamp>` dataset, SQLite file,
and runtime. Install the optional adapter dependency with `python -m pip install
".[yahoo]"`. The source adapter retrieves at most 60 days of five-minute stock
history on first use, then five days for incremental catch-up, and each currently
listed option expiry during an hourly poll. It does not provide historical expired
chains or dependable vendor delta. Stock-only EWMA variance forecasts can run
after 120 contiguous adjusted returns. Flow, pricing and OI alert routes remain
unevaluated until real-data history and calibration support them. A missing score
never becomes zero or an alert.

The first private runtime can be reproduced with:

```powershell
python -m shockwatch.paper_yahoo --db work\paper-yahoo-<timestamp>\paper.db --dataset-id paper_yahoo_<timestamp> --evidence ..\implementation_evidence\paper_yahoo_<date> --force
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\publish-paper-yahoo.ps1 -Runtime work\paper-yahoo-<timestamp>
python -m shockwatch.paper_publication validate --docs docs
```

The publication script copies only bounded derived summaries, retains the
synthetic archive, validates all relative links and hashes, and never commits.
The separate `Shockwatch-Paper-Yahoo` task can be installed after a successful
manual run with `scripts/install-paper-yahoo-task.ps1 -Runtime <runtime>`.
It polls only during the regular exchange window, suppresses overlapping wakes,
pauses after a source failure, and regenerates a private static site only when a
new completed decision interval appears. Git publication remains a manual step.
See [PAPER_YAHOO.md](PAPER_YAHOO.md) for the quality and data boundary.

## Manual public publication

Both public exporters have explicit file and field allowlists. The original
synthetic exporter refuses real-data modes; the paper-Yahoo exporter accepts only
successful runs from the separate paper database. Neither copies SQLite, raw
vendor inputs, logs,
credentials, local paths or notification destinations into `/docs`.

From this repository, regenerate and validate the static site with one command
(supply the completed synthetic runtime and its matching reports):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\publish-pages.ps1 -Runtime work\synthetic-demo\runtime -Experiment work\synthetic-demo\experiment\packet.json -Scenarios work\synthetic-demo\scenario_evaluation.json
```

The synthetic script writes `/docs` for synthetic-only builds. The paper script
above is the publication path for the current paper landing page. Each checks
internal links and required files, and
verifies the publication-manifest hashes. It makes **no Git commit or push**.
For validation alone, run:

```powershell
python -m shockwatch.paper_publication validate --docs docs
```

To make the site public, commit and push reviewed `/docs` changes deliberately.
Set GitHub **Settings → Pages → Build and deployment → Deploy from a branch →
`main` → `/docs`**. Periodic five-minute Git commits are not part of the worker.

## Reading the results

The landing page shows dataset mode, data and worker timestamps, health, active
episodes, current anomaly, latest transition, forecast comparison, notification
status and version. Scans display current route evidence separately from an
episode's original opening reasons. A scan is one instrument decision; an
episode groups related decisions. Flow and pricing thresholds appear beside
scores. Scenario results separate data invariants from expected detector
responses, and distinguish independent reset-state checks from chronological
episode continuity. Forecasts and later outcomes are separate records. Lower
QLIKE is better; a bootstrap interval crossing zero does not establish a
reliable winner.

Implementation details and operational limits are in [OPERATIONS.md](OPERATIONS.md),
[EXPERIMENTS.md](EXPERIMENTS.md), [MODEL_NOTES.md](MODEL_NOTES.md), and
[DATA_CONTRACT.md](DATA_CONTRACT.md).
