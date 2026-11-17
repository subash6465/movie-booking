# Movie Booking Watch

A Python service that watches movie listings and sends Telegram alerts when a
new matching show becomes bookable. Supports BookMyShow, District and TicketNew,
with up to five independent movie preferences in one JSON file.

The bot sends **booking-opening alerts only**. Failures, recovery events and
provider coverage details stay in local logs and status files. No seat holds,
booking automation, payment handling or web UI are included.

## Quick start

Requires Python 3.12+; Python 3.12 is the recommended runtime. No browser or
Chromium installation is needed. Run commands from the project directory.

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item watches.example.json config.json
.\.venv\Scripts\python.exe -m movie_monitor --validate
.\.venv\Scripts\python.exe -m movie_monitor
```

Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp watches.example.json config.json
.venv/bin/python -m movie_monitor --validate
.venv/bin/python -m movie_monitor
```

Copy the example only for a **new installation**. Keep an existing `config.json`.
The example watches Avengers: Doomsday, English, Chennai, 17-20 December 2026,
with any theatre, format or show-start time. Movie identifiers are explicit
validation guards and need verification when adding another movie.

The commands above send no messages. To configure Telegram privately:

```bash
# On Windows use .\.venv\Scripts\python.exe instead.
.venv/bin/python scripts/setup_telegram.py
.venv/bin/python -m movie_monitor --loop --notify
```

The helper hides the token, discovers the numeric group ID and saves `.env`.
Its optional setup test needs explicit confirmation. The bot must already be in
the group. Do not put credentials in JSON or commit `.env`.

`monitor.py` and the installed `movie-watch` command call the same CLI as
`python -m movie_monitor`. Stop with Ctrl+C or SIGTERM. `--loop` keeps running
until stopped; expired watches become idle and can be edited without a restart.

## Alert rules

- Existing matching listings are saved silently on the first successful check.
  This includes sold-out and unavailable listings; reopening a baseline show
  does not create an alert.
- A later new matching performance can alert once it is freshly verified as
  bookable. Wrong movies/dates, unknown stock and invalid responses are rejected.
- Deduplication uses movie, city, canonical theatre and local show-start minute.
  It survives restarts. Provider, screen, session ID, language and format do not
  create another alert for the same performance.
- Available platforms found during the short aggregation window are combined.
  A later platform offering the same performance does not trigger another alert.
- Notification time means **first detected bookable**, not the provider's exact
  opening time. Seats may disappear before the user opens the link.
- Ambiguous Telegram delivery outcomes require manual review rather than an
  automatic resend that could duplicate an accepted message.

Polling normally runs about once a minute with independent backoff and paced HTTP
requests. BookMyShow discovery and cinema checks have separate schedules.
A five-second scheduler tick is not a five-second poll of every cinema.

## Configuration, state and logs

Edit `config.json`; valid changes reload about every five seconds. Invalid
preferences pause new alerts while checks continue using the last valid config.
New preferences receive a new silent baseline. Scheduling changes retain it.
See [configuration](docs/configuration.md) for all fields and theatre aliases.

| Path | Purpose |
| --- | --- |
| `config.json` | Private, editable preferences; ignored by Git |
| `.env` | Telegram credentials and group IDs; ignored by Git |
| `data/watch/state.sqlite3` | Production baselines, offers and delivery history |
| `data/dry-run/state.sqlite3` | Separate default state for runs without `--notify` |
| `data/watch/latest.json` | Current provider and delivery status |
| `data/ops/` | Local watchdog logs and genuine-opening verification |

Keep the production database: deleting it loses deduplication history. A process
lock prevents two monitors from using the same database. Runtime data, credentials,
virtual environments and generated artifacts are excluded from source control.

```bash
.venv/bin/python -m movie_monitor --notify --status
sudo journalctl -u movie-watch -n 50 --no-pager
```

`--status` reads JSON only; it does not need credentials or change database state.
Operational messages never go to the Telegram group. For services, logs use the
systemd journal; [operations](docs/operations.md) covers installation, diagnostics
and safe manual database backups. Automatic backups are intentionally absent.

## Development

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m unittest discover -s tests -q
```

Tests use temporary databases and a loopback HTTP server. They do not send real
Telegram messages or poll booking providers. CI defines the same checks on
Windows/Linux with Python 3.12/3.13. See [contributing](CONTRIBUTING.md) and the
[architecture](docs/architecture.md) for module boundaries and extension rules.

## Limits

Provider endpoints are public website implementation details, not contracted
partner APIs. Schema changes, cached data, HTTP 403/429 and outages can reduce
coverage. Polling can miss short-lived openings. A blocked initial scope must
baseline silently when it first recovers. Unknown theatre-name mappings can
prevent reliable cross-platform deduplication until a verified alias is added.

The service runs independently of your computer on Oracle, but free-tier capacity,
VM reclamation and free-account allowances remain external constraints. It cannot
guarantee uninterrupted service, zero missed alerts, or successful checkout.
See [provider contracts and limitations](docs/providers.md).
