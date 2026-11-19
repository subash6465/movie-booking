# Operations

## Oracle / Linux

The existing installation is `/home/ubuntu/movie-booking`, running Ubuntu/Python
3.12. Use an unprivileged service user. New Ubuntu hosts can use
`scripts/oracle_setup.sh` to install the environment and run tests; it does not
create credentials, configure free-tier resources or start the monitor.

The two service definitions and one timer live in `deploy/`:

```bash
sudo install -m 644 deploy/movie-watch.service /etc/systemd/system/movie-watch.service
sudo install -m 644 deploy/movie-watch-health.service /etc/systemd/system/movie-watch-health.service
sudo install -m 644 deploy/movie-watch-health.timer /etc/systemd/system/movie-watch-health.timer
sudo systemctl daemon-reload
sudo systemctl enable --now movie-watch.service movie-watch-health.timer
```

The paths/user in the unit files are explicit; edit them if deploying elsewhere.
The main service starts at boot, restarts on failure and handles SIGTERM.
Both services restrict writes to `data/`. Memory and journal rate limits are set
in the units. The health service runs once a minute and **never sends Telegram**.

```bash
systemctl status movie-watch movie-watch-health.timer --no-pager
journalctl -u movie-watch -n 50 --no-pager
journalctl -u movie-watch-health -n 20 --no-pager
.venv/bin/python -m movie_monitor --notify --status
```

Logs use UTC timestamps; show times and Telegram booking times use IST.
Current provider errors, retries and rejected scopes are local diagnostics.
Repeated identical scope failures do not flood the main-process log.

Stop deliberately with `sudo systemctl disable --now movie-watch.service`.
To resume, `sudo systemctl enable --now movie-watch.service`. The health evaluator
recognizes a disabled service as an intentional pause. Disable the health timer
as well only if the operator wants to stop local diagnostics entirely.

## Diagnose without changing booking state

`data/watch/latest.json` includes provider scope errors, component retry status,
config errors, pending/failed/unknown deliveries and unresolved theatre names.
Check both `running` and `checked_at`; a forced kill cannot write a final report.
The watchdog writes `data/ops/health-latest.json` and local FAILURE/RECOVERY events
in `data/ops/health.sqlite3`. It cannot run during a full VM outage.

`data/ops/first-opening-verification.json` stays `WAITING_FOR_REAL_EVENT` until a
non-baseline production event has evidence and a Telegram acknowledgement. Tests
are excluded. This records a provider observation and acknowledgement, not
checkout success, exact provider opening time or the recipient reading the message.

Do not instantiate `State` against a live booking database from a diagnostic
process: startup recovery intentionally changes abandoned SENDING rows. Use SQLite
read-only URI connections, JSON status or the online backup utility instead.
Never reset UNKNOWN deliveries blindly; the original POST may have succeeded.

## Safe update procedure

1. Run lint, formatting, unit tests and config validation locally and in isolated
   staging. Never share production state with a dry run.
2. Stop the main service briefly, deploy source, then start it again. Keep
   `config.json`, `.env` and the entire `data/` directory. Do not delete or rebuild
   the SQLite file to apply a source update.
3. Confirm service activity, config validity, preserved baseline/delivery counts,
   provider status and fresh logs. HTTP 403/429 must remain visible as failures;
   a source deployment is not proof of provider availability.
4. Keep the previous source release available for rollback. Schema migrations,
   when introduced, need explicit versioning and compatibility tests.

The 19 November TicketNew retirement migrates only retained District/BookMyShow
watch scopes/offers to the new configuration generation, preserving baselines and
delivery receipts. Removed-provider historical rows stay inactive for audit.
The explicit migration helper requires the main process to be stopped and its
database lock held. Ordinary restarts do not repeat the migration. The old research
scripts and reports are no longer supported commands.

## Optional manual checks

A safe database copy uses SQLite's online backup API, including committed WAL data:

```bash
.venv/bin/python scripts/backup_state.py
```

It verifies integrity and writes a local file under `data/backups/`. No timer,
remote storage or automatic backup has been configured.

`scripts/verify_live_alerts.py --send-tests` explicitly sends **one labelled
booking-alert test per group**, using fresh existing data and an isolated test
database. It also checks restart/late-platform deduplication. It never emits health
messages and never counts as genuine-opening evidence. Run only when the operator
explicitly wants a test message; ordinary tests send nothing to a real group.
The helper now requires a confirmed BookMyShow map read; a positive listing with
a failing map cannot force a test notification.

`latest.json` includes a local `readiness` summary. `v2_readiness` stores map
results separately from Telegram receipts; inspect with a read-only SQLite
connection. BookMyShow NOT_READY/UNKNOWN results keep shared alerts pending.
District UNKNOWN can use the explicitly authorized District-only listing fallback.
`v2_bms_presence` keeps known counterparts through outages and restarts; a missing
or failed scan cannot erase a known BookMyShow veto. Verified alias updates
reconcile stored cinema identities before delivery and preserve receipt history.
See [readiness operations and timing](booking-readiness.md) for retry defaults.

Keep `.env` private (0600 on Linux) and do not paste the token into logs or commands.
The bot only needs permission to post; reading all group messages is unnecessary
for ongoing alerts. Telegram setup is a separate, operator-invoked helper.
