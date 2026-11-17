"""Log-only watchdog; never reads Telegram credentials or sends messages."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from movie_monitor.core import now
from movie_monitor.health import HealthJournal, conditions
from movie_monitor.lock import SingleInstance
from movie_monitor.verification import write_verification


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="data/watch/latest.json")
    parser.add_argument("--db", default="data/watch/state.sqlite3")
    parser.add_argument("--directory", default="data/ops")
    # Compatibility with older installed units. Health delivery is always disabled.
    parser.add_argument("--notify", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--ignore-service", action="store_true", help="For local diagnostics without systemd"
    )
    args = parser.parse_args()
    root = Path(args.directory)
    root.mkdir(parents=True, exist_ok=True)
    lock = SingleInstance(str(root / "watchdog.lock"))
    journal = None
    try:
        service = None
        if not args.ignore_service:
            command = subprocess.run(
                [
                    "systemctl",
                    "show",
                    "movie-watch.service",
                    "-p",
                    "ActiveState",
                    "-p",
                    "UnitFileState",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            service = dict(
                line.split("=", 1) for line in command.stdout.splitlines() if "=" in line
            )
        try:
            report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = None
        journal = HealthJournal(str(root / "health.sqlite3"))
        try:
            issues = conditions(report, service=service)
        except (TypeError, ValueError, KeyError, AttributeError):
            issues = {
                "status-schema": "The monitor status report is malformed; coverage cannot be verified."
            }
        observed = journal.observe(issues)
        proof = write_verification(args.db, root / "first-opening-verification.json")
        state = {
            "checked_at": now().isoformat(),
            **observed,
            "conditions": issues,
            "notifications_enabled": False,
            "journal": journal.summary(),
            "opening_verification": proof["status"],
        }
        temporary = root / "health-latest.tmp"
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temporary.replace(root / "health-latest.json")
        print(json.dumps(state))
    finally:
        if journal:
            journal.close()
        lock.close()


if __name__ == "__main__":
    main()
