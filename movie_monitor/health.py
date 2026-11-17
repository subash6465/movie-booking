"""Separate-process health evaluation and local incident logging only."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .core import UTC, now
from .delivery import NAMES


def clean(value, limit=220):
    return "".join(c for c in str(value) if ord(c) >= 32)[:limit]


def conditions(report, at=None, service=None):
    at = (at or now()).astimezone(UTC)
    if service and service.get("UnitFileState") in ("disabled", "masked"):
        return None  # Deliberate disable: no fault or false recovery notification.
    result = {}
    if service and service.get("ActiveState") not in ("active", "activating"):
        result["service"] = "Movie monitor is enabled but is not running."
    if not report:
        result["heartbeat"] = "No monitor status report is available."
        return result
    try:
        checked = datetime.fromisoformat(report["checked_at"])
        if (
            checked.tzinfo is None
            or not -30 <= (at - checked).total_seconds() <= 180
            or not report.get("running")
        ):
            result["heartbeat"] = (
                "The monitor heartbeat is stopped, invalid or more than three minutes old."
            )
    except (ValueError, TypeError, KeyError):
        result["heartbeat"] = "The monitor heartbeat timestamp is invalid."
    if report.get("config_error"):
        result["configuration"] = (
            "Invalid preference JSON: booking notifications are paused. "
            + clean(report["config_error"])
        )
    review = report.get("state", {}).get("delivery_states", {})
    if review.get("UNKNOWN") or review.get("FAILED"):
        result["delivery-review"] = (
            f"Booking deliveries need review: UNKNOWN={review.get('UNKNOWN', 0)}, FAILED={review.get('FAILED', 0)}. Unknown outcomes are not automatically resent."
        )
    for provider in report.get("providers", []):
        label = f"{provider.get('movie') or provider.get('watch')} / {NAMES.get(provider.get('provider'), provider.get('provider'))}"
        key = f"{provider.get('watch')}:{provider.get('provider')}"
        components = provider.get("components")
        if components:
            for name in ("catalogue", "cinemas", "discovery_probes"):
                component = components[name]
                if component.get("state") == "DEGRADED":
                    explanation = (
                        component.get("error")
                        or "; ".join(component.get("errors", []))
                        or "Checks failed"
                    )
                    result[key + ":" + name] = clean(
                        f"{label}: {name.replace('_', ' ')} degraded. {explanation}", 400
                    )
        elif provider.get("state") == "DEGRADED":
            errors = [
                s.get("error") or "; ".join(s.get("errors", []))
                for s in provider.get("scopes", [])
                if not s.get("complete")
            ]
            result[key] = clean(
                f"{label}: availability checks are degraded. "
                + "; ".join(str(e) for e in errors[:2] if e),
                400,
            )
        if provider.get("state") != "IDLE_DATES_EXPIRED":
            try:
                age = (at - datetime.fromisoformat(provider["checked_at"])).total_seconds()
                if age > provider.get("next_poll_seconds", 60) + 180:
                    result[key + ":overdue"] = f"{label}: a scheduled scan is overdue."
            except (KeyError, TypeError, ValueError):
                result[key + ":overdue"] = f"{label}: no valid scan timestamp."
    return result


class HealthJournal:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS incidents(
            key TEXT PRIMARY KEY, incident TEXT, first_seen TEXT, last_seen TEXT, detail TEXT, queued INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY, incident TEXT, kind TEXT, body TEXT, created_at TEXT,
            UNIQUE(incident,kind));
          CREATE TABLE IF NOT EXISTS deliveries(
            id INTEGER PRIMARY KEY, event INTEGER, target TEXT, state TEXT, attempts INTEGER DEFAULT 0,
            retry_at TEXT, message_id TEXT, error TEXT, UNIQUE(event,target));
        """)
        with self.db:
            # Preserve historical receipts; pending health messages must never send.
            self.db.execute(
                "UPDATE deliveries SET state='UNKNOWN',error='Legacy health delivery outcome unknown' WHERE state='SENDING'"
            )
            self.db.execute(
                "UPDATE deliveries SET state='CANCELLED',error='Health notifications disabled; log only' WHERE state='PENDING'"
            )

    def observe(self, active, *, at=None, delay=120):
        if active is None:
            return {"paused": True, "active_conditions": 0}
        at = (at or now()).astimezone(UTC)
        with self.db:
            old = {r["key"]: dict(r) for r in self.db.execute("SELECT * FROM incidents").fetchall()}
            for key, entry in old.items():
                if key not in active:
                    if entry["queued"]:
                        self._event(entry["incident"], "RECOVERY", entry["detail"], at)
                    self.db.execute("DELETE FROM incidents WHERE key=?", (key,))
            for key, detail in active.items():
                self.db.execute(
                    "INSERT INTO incidents VALUES(?,?,?,?,?,0) ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen,detail=excluded.detail",
                    (key, uuid.uuid4().hex, at.isoformat(), at.isoformat(), clean(detail, 450)),
                )
                entry = self.db.execute("SELECT * FROM incidents WHERE key=?", (key,)).fetchone()
                if (
                    not entry["queued"]
                    and (at - datetime.fromisoformat(entry["first_seen"])).total_seconds() >= delay
                ):
                    self._event(entry["incident"], "FAILURE", detail, at)
                    self.db.execute("UPDATE incidents SET queued=1 WHERE key=?", (key,))
        return {"paused": False, "active_conditions": len(active)}

    def _event(self, incident, kind, detail, at):
        self.db.execute(
            "INSERT OR IGNORE INTO events(incident,kind,body,created_at) VALUES(?,?,?,?)",
            (incident, kind, clean(detail, 450), at.isoformat()),
        )

    def summary(self):
        return {
            "active_incidents": self.db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0],
            "events": dict(
                self.db.execute("SELECT kind,COUNT(*) FROM events GROUP BY kind").fetchall()
            ),
            "legacy_delivery_states": dict(
                self.db.execute("SELECT state,COUNT(*) FROM deliveries GROUP BY state").fetchall()
            ),
        }

    def close(self):
        self.db.close()
