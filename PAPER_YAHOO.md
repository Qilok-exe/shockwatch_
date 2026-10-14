# Paper-Yahoo observation path

This path has mode `paper`, source `yahoo`, and a new `paper_yahoo_<UTC>` dataset
and database under ignored `work/`. It never imports the reviewed synthetic
detector or its calibration. It observes markets and never places orders.

`shockwatch.yahoo_source.YahooSource` is the sole yfinance boundary. On first
use it requests `Ticker.history(period='60d', interval='5m', auto_adjust=False,
actions=True, repair=False, keepna=True, prepost=False)` for SPY, UAL and LMT.
Later polls request five days to catch late observations. Current option expiries
come from `Ticker.options`; each chain is requested once per cycle. There is no
automatic endpoint retry loop. Raw accepted bars and contracts are private SQLite
records with separate raw close and adjusted close, dividends, splits, timezone,
UTC timestamps, receipt/fetch time, source versions and response hashes.

The adapter uses versioned NYSE and NASDAQ schedules, including holidays and
early closes. It rejects malformed symbol, timestamps, regular-session bars, OHLC,
negative volume/OI, price/IV, bid/ask, expiry, DTE and duplicate contract keys.
An incomplete current bar is excluded. Missing volume/OI stays null. A stale
last trade is flagged, while Yahoo quote publication time remains uncertain.
Repeated stock bars are compared on market fields, so receipt-time changes are
idempotent. Conflicting revisions are recorded and never silently overwritten.
The 14 October first live fetch ran under adapter version 1. An explicit offline
calendar audit then checked all 14,040 stored bars, found no invalid or missing
scheduled bars, and corrected the final 30-minute interval's report to 6/6 bars.
An earlier aggregation correction counted all listed expiries rather than only
the final expiry fetched per ticker.
The first run's forecast and decision issue metadata were also moved
conservatively to run completion, after the last source response; forecast
values did not change. Future cycles stamp the actual processing point.
The original report and each correction remain in the separate evidence packet.
Adapter version 2 applies calendar checks before future ingestion.

The first option snapshot stores OI *levels*, not growth. A daily comparison
requires two completed-day snapshots of the identical contract and records
position differences only. It does not infer buyer initiation or feed an alert.
Vendor delta is always unavailable. Derived Greeks, if added later, require
separate fields and full calculation metadata.

Decisions wait for a completed 60-minute regular-session interval and a ten-minute
processing delay. The final shortened session interval uses its actual scheduled
bar count. Missing stock bars exclude that interval. Existing options
anomaly routes remain unevaluated because historical Yahoo chain observations,
vendor delta and independently calibrated real-data thresholds are absent. Stock
EWMA variance forecasts are separate, based only on contiguous adjusted stock
returns available at issue time. Their target is the next observed regular
five-minute bar. This has not established predictive validity.

The paper worker can wake every five minutes during the regular exchange window.
Option chains are due hourly. Daily OI comparison waits until after the session.
The separate Windows task uses a windowless interpreter, `IgnoreNew`, a file lock,
private bounded logs and a STOP sentinel after source failure. It updates a
private static site only after a new completed decision; it does not commit to
Git. The task uses interactive logon. Sleep, wake, reboot, logged-out operation
and true missed-run recovery remain unverified unless tested physically.

`shockwatch.paper_publication` allowlists the paper homepage, first-run page,
guide, CSS, manifests, and one bounded derived JSON summary. It archives the
previous validated synthetic public site under `docs/archive/synthetic_20261014`.
It rejects unexpected files, non-relative or broken links, private-path patterns,
and hash mismatches. Raw Yahoo bars/chains, SQLite, logs, credentials and machine
paths are not copied. Manual publication uses `scripts/publish-paper-yahoo.ps1`;
reviewed Git commits remain a separate action.
