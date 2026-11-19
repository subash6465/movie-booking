"""Opt-in booking-alert test using isolated state, never a real opening claim."""

import argparse
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from movie_monitor.config import generation, load_app
from movie_monitor.core import Observation, Show, now
from movie_monitor.delivery import deliver
from movie_monitor.identity import (
    explicit_theatre,
    performance_key,
    theatre_name_key,
    validate_show,
)
from movie_monitor.notify import Telegram, destinations, load_env
from movie_monitor.readiness import ReadinessGate
from movie_monitor.state import State
from movie_monitor.verification import opening_verification


class TestSender:
    def __init__(self, real):
        self.real, self.sent = real, []

    def send(self, target, text):
        text = "[TEST ONLY — EXISTING LISTING, NOT A NEW OPENING]\n\n" + text.replace(
            "New matching movie show", "Controlled movie-alert delivery test", 1
        ).replace("First detected bookable:", "Test observation time:")
        message_id = self.real.send(target, text)
        self.sent.append({"target": target, "message_id": message_id})
        return message_id


def deliver_confirmed(state, sender, active, targets, app, at):
    """Even explicitly labelled transport tests require a successful map read."""
    gate = ReadinessGate(app)
    started = time.monotonic()
    try:
        while time.monotonic() - started < 12:
            current = at + timedelta(seconds=time.monotonic() - started)
            counts = deliver(state, sender, active, targets, app, readiness=gate, at=current)
            if any(counts.values()) or not gate.inflight:
                return counts
            gate.wake.wait(0.05)
            gate.wake.clear()
        return {"sent": 0, "failed": 0, "unknown": 0}
    finally:
        gate.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--send-tests",
        action="store_true",
        help="Required: explicitly send one labelled booking-alert test per configured group",
    )
    args = parser.parse_args()
    if not args.send_tests:
        parser.error("Use --send-tests to explicitly authorize the labelled Telegram messages")
    load_env()
    targets = destinations()
    if not targets:
        parser.error("Telegram groups are not configured")
    app = load_app("config.json")
    at = now()
    source = Path("data/watch/state.sqlite3").resolve()
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT watch,payload,observed_at FROM v2_offers WHERE valid=1 ORDER BY observed_at DESC"
        ).fetchall()
    selected = None
    for watch in app["watches"]:
        for row in rows:
            if (
                row["watch"] != generation(watch)
                or (at - datetime.fromisoformat(row["observed_at"])).total_seconds()
                > app["freshness_seconds"]
            ):
                continue
            payload = json.loads(row["payload"])
            if (
                payload["provider"] == "bookmyshow"
                and payload["availability"] == "AVAILABLE"
                and datetime.fromisoformat(payload["starts_at"]) > at + timedelta(minutes=5)
            ):
                show = Show.from_dict(payload)
                validate_show(show, watch, show.local_start.date().isoformat())
                selected = watch, show
                break
        if selected:
            break
    if selected is None:
        raise SystemExit(
            "No fresh, positively bookable production listing available for a truthful existing-listing test. No tests sent."
        )
    watch, show = selected
    alias = explicit_theatre(show, app["theatre_aliases"])
    canonical = alias[0] if alias else "name:" + theatre_name_key(show.cinema, show.city)
    other_platform = None
    for row in rows:
        if (
            row["watch"] != generation(watch)
            or (at - datetime.fromisoformat(row["observed_at"])).total_seconds()
            > app["freshness_seconds"]
        ):
            continue
        candidate = json.loads(row["payload"])
        other = Show.from_dict(candidate)
        if other.provider == show.provider or other.availability != "AVAILABLE":
            continue
        other_alias = explicit_theatre(other, app["theatre_aliases"])
        other_key = (
            other_alias[0] if other_alias else "name:" + theatre_name_key(other.cinema, other.city)
        )
        if performance_key(other, other_key) == performance_key(show, canonical):
            validate_show(other, watch, show.local_start.date().isoformat())
            other_platform = other
            break
    directory = Path("data/ops-tests") / at.strftime("%Y%m%dT%H%M%SZ")
    directory.mkdir(parents=True)
    path = directory / "state.sqlite3"
    key = generation(watch)
    sender = TestSender(Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", "")))
    test_app = {**app, "aggregation_seconds": 0}
    state = State(str(path), test_mode=True)
    try:
        day = show.local_start.date().isoformat()
        state.ingest(
            key,
            watch,
            Observation(
                show.provider,
                day,
                status="NO_SHOWS",
                observed_at=(at - timedelta(seconds=1)).isoformat(),
            ),
            app["theatre_aliases"],
            at,
        )
        if other_platform:
            state.ingest(
                key,
                watch,
                Observation(
                    other_platform.provider,
                    day,
                    status="NO_SHOWS",
                    observed_at=(at - timedelta(seconds=1)).isoformat(),
                ),
                app["theatre_aliases"],
                at,
            )
        state.ingest(
            key,
            watch,
            Observation(show.provider, day, shows=[show], observed_at=at.isoformat()),
            app["theatre_aliases"],
            at,
        )
        first = deliver_confirmed(state, sender, {key}, targets, test_app, at)
    finally:
        state.close()
    state = State(str(path), test_mode=True)
    try:
        later = at + timedelta(minutes=2)
        if other_platform:
            state.ingest(
                key,
                watch,
                Observation(
                    other_platform.provider,
                    day,
                    shows=[other_platform],
                    observed_at=later.isoformat(),
                ),
                app["theatre_aliases"],
                later,
            )
        second = deliver_confirmed(state, sender, {key}, targets, test_app, later)
    finally:
        state.close()
    proof = opening_verification(path)
    if (
        first["sent"] != len(targets)
        or second["sent"]
        or proof["status"] != "WAITING_FOR_REAL_EVENT"
    ):
        raise SystemExit(
            "Controlled alert/dedup/isolation verification failed; inspect the test report directory."
        )
    report = {
        "kind": "CONTROLLED_TEST_ONLY",
        "checked_at": now().isoformat(),
        "real_opening_observed": False,
        "source": "Fresh production listing read through a read-only database connection",
        "late_platform": other_platform.provider if other_platform else None,
        "late_platform_delay": "Simulated two minutes; no real opening claimed",
        "movie_alert_test": first,
        "restart_duplicate_attempt": second,
        "test_excluded_from_real_event_proof": proof["status"],
        "telegram_test_receipts": sender.sent,
        "production_state_modified": False,
    }
    report["passed"] = (
        first["sent"] == len(targets)
        and second["sent"] == 0
        and proof["status"] == "WAITING_FOR_REAL_EVENT"
    )
    (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit("Booking-alert delivery verification failed; inspect the saved report.")


if __name__ == "__main__":
    main()
