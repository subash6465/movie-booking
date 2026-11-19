from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from .config import generation
from .core import IST, now
from .enrichment import detail_lines, omitted
from .notify import DeliveryError
from .ports import BookingReadiness, NotificationSender

logger = logging.getLogger(__name__)

NAMES = {"bookmyshow": "BookMyShow", "district": "District"}


def alert_text(row, *, at=None, freshness=180):
    at = at or now()
    start = datetime.fromisoformat(row["starts_at"]).astimezone(IST)
    detected = datetime.fromisoformat(
        row.get("first_ready_at") or row["first_available"]
    ).astimezone(IST)
    offers = sorted(row["offers"], key=lambda x: x["provider"])
    lines = [
        "New matching movie show",
        "",
        row["movie"],
        f"Theatre: {row['theatre_name']}",
        f"City: {row['city'].title()}",
        f"Date: {start:%A, %d %B %Y}",
        f"Show starts: {start:%I:%M %p} IST",
        "Platforms: " + ", ".join(NAMES[o["provider"]] for o in offers),
        "Language: " + ", ".join(sorted({o["language"] for o in offers})),
        "Format: " + " / ".join(sorted({o["format"] for o in offers})),
        f"{'First detected available' if row.get('detection_basis') == 'district_listing' else 'First detected bookable'}: {detected:%d %b %Y, %I:%M:%S %p} IST",
        "",
        "Booking links:",
    ]
    for offer in offers:
        lines.extend(
            [
                f"{NAMES[offer['provider']]} — {offer['language']} | {offer['format']}",
                offer["booking_url"],
            ]
        )
    lines.extend(
        [
            "",
            "Detection time is our first listing observation, not a provider-published opening time. Availability can change."
            if row.get("detection_basis") == "district_listing"
            else "Detection time is our first successful seat-map check, not a provider-published opening time. Availability can change.",
        ]
    )
    text = "\n".join(lines)
    if len(text.encode("utf-16-le")) // 2 > 3900:
        raise ValueError("Notification exceeds Telegram message length; delivery requires review")
    # Keep the complete booking alert and links even if enrichment is malformed
    # or too long. Optional lines must never turn a valid alert into a failure.
    for offer in offers:
        try:
            extra = detail_lines(offer, at, freshness)
            if not extra:
                continue
            heading = f"\n\n{NAMES[offer['provider']]} details\n{extra[0]}"
            pending = heading
            for line in extra[1:]:
                candidate = text + pending + "\n" + line
                if len(candidate.encode("utf-16-le")) // 2 <= 3900:
                    text = candidate
                    pending = ""
        except Exception:
            omitted(offer["provider"], "optional detail rendering failed")
    return text


def deliver(
    state,
    sender: NotificationSender,
    active,
    targets,
    app,
    *,
    readiness: BookingReadiness,
    at=None,
    last_sent=None,
    stop=None,
):
    at = at or now()
    if stop is not None and stop.is_set():
        return {"sent": 0, "failed": 0, "unknown": 0}
    started = time.monotonic()
    # A successful map read never waits for an aggregation window or another
    # platform. The persistent performance/group key still suppresses late copies.
    state.prepare(active, targets, app["freshness_seconds"], 0, at)
    bms_active = {
        generation(w)
        for w in app["watches"]
        if w.get("enabled", True) and "bookmyshow" in w["providers"]
    }
    aliases = app["theatre_aliases"]
    rows = state.due(
        active,
        app["freshness_seconds"],
        at,
        all_offers=True,
        bms_active=bms_active,
        aliases=aliases,
    )
    rows = readiness.check(rows, state, at)
    counts = {"sent": 0, "failed": 0, "unknown": 0}
    last_sent = last_sent if last_sent is not None else {}
    for row in rows:
        if stop is not None and stop.is_set():
            break
        if row["target"] not in targets:
            continue
        # A negative acknowledgement (e.g. 429) stops this destination for this
        # pass; rows are not discarded and remain due after backoff.
        if last_sent.get(("blocked", row["target"]), 0) > time.monotonic():
            continue
        delay = 1.1 - (time.monotonic() - last_sent.get(row["target"], -10))
        if delay > 0:
            if stop is not None:
                stop.wait(delay)
            else:
                time.sleep(delay)
        if stop is not None and stop.is_set():
            break
        row_at = at + timedelta(seconds=max(0, time.monotonic() - started))
        row = readiness.approved(
            {
                **row,
                **state.booking_context(
                    row["performance"],
                    active,
                    app["freshness_seconds"],
                    row_at,
                    bms_active=bms_active,
                    aliases=aliases,
                ),
                "offers": state.fresh_offers(
                    row["performance"],
                    active,
                    app["freshness_seconds"],
                    row_at,
                    all_offers=True,
                ),
            },
            row_at,
        )
        if row is None:
            continue
        try:
            text = alert_text(row, at=row_at, freshness=app["freshness_seconds"])
        except ValueError as exc:
            state.delivery_failed(row["id"], str(exc), permanent=True, at=row_at)
            counts["failed"] += 1
            continue
        if not state.claim(row["id"]):
            continue
        try:
            message_id = sender.send(row["target"], text)
            state.delivered(row["id"], message_id)
            logger.info(
                "Booking alert acknowledged: delivery_id=%s message_id=%s", row["id"], message_id
            )
            counts["sent"] += 1
            last_sent[row["target"]] = time.monotonic()
        except DeliveryError as exc:
            retry = exc.retry_after or min(
                60 * 2 ** min(row["attempts"], 4), app["max_backoff_seconds"]
            )
            state.delivery_failed(
                row["id"],
                str(exc),
                ambiguous=exc.ambiguous,
                permanent=exc.permanent,
                retry_after=retry,
                at=at,
            )
            logger.warning(
                "Booking delivery failed: delivery_id=%s ambiguous=%s permanent=%s retry_seconds=%s reason=%s",
                row["id"],
                exc.ambiguous,
                exc.permanent,
                retry,
                exc,
            )
            counts["unknown" if exc.ambiguous else "failed"] += 1
            last_sent[("blocked", row["target"])] = time.monotonic() + retry
        except Exception as exc:
            state.delivery_failed(
                row["id"],
                f"Unexpected delivery failure: {type(exc).__name__}",
                ambiguous=True,
                at=at,
            )
            counts["unknown"] += 1
    return counts
