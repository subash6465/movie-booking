# Development guide

Use Python 3.12 and a project virtual environment. From the repository root:

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format --check .
python -m unittest discover -s tests -q
python -m movie_monitor --config watches.example.json --validate
```

Run `python -m ruff check . --fix` and `python -m ruff format .` before review.
CI defines checks on Windows/Linux and Python 3.12/3.13. Keep runtime dependencies
in `requirements.txt`, which also supplies package metadata. Ruff is development
only; Chromium/Playwright are not runtime dependencies.

## Implementation rules

- Keep parsers pure and adapters independent of Telegram and SQLite. Use domain
  `Show`/`Observation` records and the structural interfaces in `ports.py`.
- Validate provider identity, dates, availability and trusted links before matching.
  Preserve explicit incomplete/error results; never convert failure into NO_SHOWS.
- Keep network calls outside booking-state transactions and bound caches/workers.
  Polling retries must not create a busy loop or reset other providers' schedules.
- Preserve canonical keys, startup baselines, schema and persistent delivery state
  unless a reviewed migration and regression tests accompany the change.
- Handle expected transport failures without logging request URLs, headers or
  exception chains that could contain bot tokens. Operational diagnostics stay
  in logs. Only opening alerts use the bot.
- Prefer small interfaces and explicit wiring. Do not add deployment services or
  infrastructure merely to prepare for a hypothetical feature.

Tests use temporary files and loopback HTTP. Add behavioural cases for changes to
parsing, baseline rules, aliases, freshness, retries, outbox state or config reload.
Avoid tests that merely repeat implementation details. Do not run real notification
helpers as part of CI or a normal test suite.

`config.json`, `.env`, `data/`, environments, build output and caches are ignored.
Do not commit user settings or generated verification archives. Do not delete
runtime state as a cleanup step. The source checkout contains one example JSON,
one runtime implementation and documentation under `docs/`.

See [architecture](docs/architecture.md) before adding an API, another provider,
another notification transport or multiple workers/replicas.
