"""Command-line boundary for configuration, credentials and process locking."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .logging_config import configure_logging
from .notify import Telegram, destinations, load_env


def main(argv=None):
    from .config import load_app
    from .engine import run
    from .lock import SingleInstance

    parser = argparse.ArgumentParser(
        description="Movie alerts: 1-5 JSON watches, silent startup baseline, persistent cross-platform deduplication."
    )
    parser.add_argument("--config", default="config.json")
    parser.add_argument(
        "--db", help="SQLite state file; notification and dry-run defaults are separate"
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run until Ctrl+C or service stop, including after watched dates expire",
    )
    parser.add_argument(
        "--cycles", type=int, default=0, help="Bound a diagnostic loop; zero means indefinitely"
    )
    parser.add_argument("--notify", action="store_true", help="Enable real Telegram alerts")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate JSON without fetching data or changing state",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Read the latest status report without disturbing a running monitor",
    )
    parser.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO"
    )
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    if args.cycles < 0:
        parser.error("cycles cannot be negative")
    db = args.db or ("data/watch/state.sqlite3" if args.notify else "data/dry-run/state.sqlite3")
    if args.status:
        report = Path(db).parent / "latest.json"
        if not report.exists():
            parser.error(f"No status report at {report}")
        try:
            saved = json.loads(report.read_text(encoding="utf-8"))
            if (
                not isinstance(saved, dict)
                or not isinstance(saved.get("providers", []), list)
                or any(not isinstance(provider, dict) for provider in saved.get("providers", []))
            ):
                raise ValueError("Invalid status report structure")
        except (OSError, ValueError) as exc:
            parser.error(f"Cannot read status report: {exc}")
        summary = {
            k: saved.get(k)
            for k in (
                "checked_at",
                "running",
                "coverage",
                "telegram_enabled",
                "config_error",
                "needs_delivery_review",
                "watches",
                "state",
            )
        }
        summary["providers"] = [
            {
                k: provider.get(k)
                for k in ("watch", "provider", "state", "checked_at", "next_poll_seconds")
            }
            for provider in saved.get("providers", [])
        ]
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    try:
        app = load_app(args.config)
        if args.validate:
            print(f"Valid configuration: {len(app['watches'])} watches")
            return 0
        load_env()
        sender, targets = None, []
        if args.notify:
            targets = destinations()
            if not targets:
                raise ValueError("Set TELEGRAM_CHAT_IDS in .env before using --notify")
            sender = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        lock = SingleInstance(str(Path(db).resolve()) + ".lock")
        try:
            report = run(
                args.config, db, loop=args.loop, cycles=args.cycles, sender=sender, targets=targets
            )
        finally:
            lock.close()
        return (
            0
            if not report.get("config_error")
            and all(p["state"] != "DEGRADED" for p in report["providers"])
            else 2
        )
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        parser.error(str(exc))
