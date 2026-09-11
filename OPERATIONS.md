# Laptop operation

The numerical worker is scheduled Python and can run without an open LLM conversation. Bloomberg collection is a separate adapter to implement after account inspection. The starter continuously processes canonical CSVs delivered into its inbox.

## Local setup and scheduling

Use Python 3.12 and the README commands. Keep the project and live SQLite database on a local disk. Start with three instruments and notifications disabled; measure memory/runtime before expanding. Do not put the active database in a cloud-synced folder.

Run `scripts/run-once.cmd` after placing a real-data model, reviewed policy, verified calendar and first panels at the documented runtime paths. The policy must belong to the model. `review-policy` records the technical review; it is not a request to reconsider the approved project design.

After a successful manual shadow cycle, run `scripts/install-task.ps1` in PowerShell. It registers a five-minute task for the current user at limited privilege, ignores overlapping runs and limits each invocation to ten minutes. It works while that user is logged in, including a locked desktop. Running while logged out needs a separately tested account/service arrangement; Bloomberg may require its own logged-in session. Scheduling uses [Microsoft's ScheduledTasks interface](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasktrigger?view=windowsserver2025-ps).

The script requests wake-to-run but does not change global sleep settings or guarantee hardware wake support. Test lock, suspend/resume, reboot/login and temporary network loss. It refuses to overwrite an existing task. If execution policy blocks the script, use equivalent Task Scheduler UI settings or adapt it to the machine's policy; do not broadly weaken system policy.

On Linux, the same command can run under a five-minute systemd timer or cron entry. Do not enable two schedulers for one database. A single-worker SQLite lease guards normal overlap; a crashed worker can leave a lease for up to 30 minutes. Inspect the process before restarting during that period. Windows task registration has not been executed on Windows here.

## Each cycle

1. Acquire the lease and verify model/policy identity.
2. Validate new inbox CSVs, transactionally insert first-release rows, and archive successful files under `runtime/archive/YYYY-MM-DD`.
3. Load pending decision groups plus prior context; calculate scores, forecasts and episode transitions with the shared replay engine.
4. Commit decisions, episode events, state and outbox together for each group.
5. Compare expected and received calendar intervals. Missing/degraded inputs, failed messages and calendar expiry are health failures.
6. Dispatch due messages, create the daily digest after 23:00 UTC, update heartbeat and create the daily backup.

Market messages more than 90 minutes old at processing time are `historical_only`, avoiding a phone flood during backlog recovery. Health warnings have separate hourly throttling and do not count toward the market-episode target. Daily summaries include episode-state-change counts and data/outbox health. Richer episode narratives are later work.

Failed imports remain in the inbox with a reported reason. Preserve raw source before editing a rejected file. Revised rows cannot replace the original ledger. Late rows for processed timestamps are retained but do not rewrite live state; investigate with a separate replay.

## Notifications and credentials

Nothing sends until `notifications.enabled` is set to `true`. Synthetic models are blocked from sending even if that flag changes. Automated verification used mock senders and sent no real messages.

Supply secrets through environment variables available to the scheduled user. Do not put tokens/passwords in shared code, configuration, screenshots or reports. After changing user-level environment variables, verify that the scheduled process sees them.

| Adapter | Environment variables |
|---|---|
| Telegram | `SHOCKWATCH_TELEGRAM_TOKEN`, `SHOCKWATCH_TELEGRAM_CHAT_ID` |
| STARTTLS email | `SHOCKWATCH_SMTP_HOST`, optional `SHOCKWATCH_SMTP_PORT` (587 default), `SHOCKWATCH_SMTP_USER`, `SHOCKWATCH_SMTP_PASSWORD`, `SHOCKWATCH_EMAIL_FROM`, `SHOCKWATCH_EMAIL_TO` |
| External watchdog | Optional HTTPS `SHOCKWATCH_HEARTBEAT_OK_URL` and `SHOCKWATCH_HEARTBEAT_FAIL_URL` |

Set and verify your own recipient addresses/chat before enabling messages. The email adapter specifically uses STARTTLS; OAuth-only providers or SMTP-over-TLS on port 465 need an adapter change.

Telegram success is `accepted_telegram`; an API error can route to email as `accepted_email_fallback`. SMTP acceptance does not prove inbox delivery. The [Telegram success response](https://core.telegram.org/bots/api#sendmessage) is not a read receipt. An unacknowledged or muted phone cannot be detected without an explicit acknowledgement mechanism.

Outbox states include `pending`, `dry_run`, `historical_only`, `accepted_telegram`, `accepted_email`, `accepted_email_fallback`, `failed`. Retries survive restarts, back off without blocking sleep and stop after eight attempts. A crash after remote acceptance but before its local commit can duplicate a message on retry. Delivery is at-least-once in that window, not exactly-once; episode IDs identify duplicates.

## Health and recovery

`python -m shockwatch status --db runtime/monitor.db` shows the last run, age, stale-worker flag and outbox counts. `runtime/heartbeat.json` records the latest successful cycle. Neither local record can notify you while the laptop is off.

Configure an independent watchdog to expect a success check-in every five minutes, initially allowing 15 minutes of absence. Its HTTPS success/failure endpoints are read from the environment and never logged. Without a failure URL, a degraded cycle omits the success ping, allowing timeout. The watchdog needs an independent notification channel and a real stop-the-worker test.

Maintain a verified calendar of upcoming sessions. The worker does not infer holidays from absent prices. A stale calendar or feed is a data fault, not a quiet market.

SQLite uses explicit transactions, full synchronous commits and one writer. The [online backup API](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup) copies committed state. One backup is made daily; the worker retains the last seven backup dates it recorded creating. Raw/canonical archives are not automatically deleted.

Copy selected completed backups, model/policy/configuration, calendar and licensed source archives to another device or trusted backup destination. Seven local copies do not protect against laptop loss. A weekly remote copy and monthly restore drill are reasonable initial habits; automate that copy after the pilot paths stabilize.

To restore: stop scheduling, preserve the current/damaged database, restore a completed backup to a new path, supply subsequent archived panels for catch-up, and run manually with notifications disabled. Compare watermark, episode state and outbox before resuming. Resetting notification state casually can resend previously accepted messages.

Model, policy, feature or watchlist changes begin in a separate shadow database. Automatic refitting/promotion is absent. Drift reporting and the offline promotion workflow are next steps in the gameplan.
