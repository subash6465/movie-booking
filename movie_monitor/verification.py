"""Read-only verification of genuine post-baseline detections and Telegram ACKs."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .core import now


def opening_verification(db_path):
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "WAITING_FOR_MONITOR", "checked_at": now().isoformat()}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("""SELECT e.payload,e.observed_at,d.message_id,d.target,d.state
              FROM v2_opening_evidence e JOIN v2_performances p ON p.key=e.performance
              JOIN v2_deliveries d ON d.performance=e.performance
              WHERE e.test_mode=0 AND p.baseline=0 AND p.retired=0 AND d.state='SENT'
              ORDER BY e.observed_at,d.id LIMIT 1""").fetchone()
        except sqlite3.OperationalError:
            return {"status": "EVIDENCE_JOURNAL_NOT_AVAILABLE", "checked_at": now().isoformat()}
        if row:
            return {
                "status": "REAL_EVENT_OBSERVED_AND_TELEGRAM_ACKNOWLEDGED",
                "checked_at": now().isoformat(),
                "evidence": json.loads(row["payload"]),
                "telegram_message_id": row["message_id"],
                "target": row["target"],
                "user_read_receipt_verified": False,
            }
        count = db.execute("SELECT COUNT(*) FROM v2_opening_evidence WHERE test_mode=0").fetchone()[
            0
        ]
        legacy = db.execute(
            "SELECT COUNT(*) FROM v2_deliveries d JOIN v2_performances p ON p.key=d.performance LEFT JOIN v2_opening_evidence e ON e.performance=p.key WHERE d.state='SENT' AND p.baseline=0 AND e.performance IS NULL"
        ).fetchone()[0]
        if legacy and not count:
            return {
                "status": "PRE_JOURNAL_ALERT_PRESENT_REQUIRES_REVIEW",
                "checked_at": now().isoformat(),
                "legacy_alerts": legacy,
            }
        return {
            "status": "REAL_EVENT_DETECTED_AWAITING_DELIVERY"
            if count
            else "WAITING_FOR_REAL_EVENT",
            "checked_at": now().isoformat(),
            "real_detections": count,
        }


def write_verification(db_path, output):
    report = opening_verification(db_path)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous.get(
            "status"
        ) == "REAL_EVENT_OBSERVED_AND_TELEGRAM_ACKNOWLEDGED" and not previous.get(
            "evidence", {}
        ).get("test_mode"):
            # Keep the first genuine proof even after the 90-day source-row cleanup.
            previous["last_checked_at"] = report["checked_at"]
            previous["current_journal_status"] = report["status"]
            report = previous
    except (OSError, ValueError, TypeError):
        pass
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return report
