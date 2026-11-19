"""BookMyShow confirmation with the explicitly authorized District-only fallback."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from .config import normalized
from .core import now
from .http import FetchError, HttpClient, guest_headers

logger = logging.getLogger(__name__)
DENIALS = {401, 403, 412, 429}


@dataclass(frozen=True)
class ReadinessResult:
    status: str  # READY, NOT_READY, UNKNOWN; unrelated to Telegram delivery states.
    reason: str
    checked_at: str
    http_status: int | None = None
    retry_after: int | None = None


def result(status, reason, http_status=None, retry_after=None):
    return ReadinessResult(status, reason, now().isoformat(), http_status, retry_after)


def parse_bms_map(data: dict) -> ReadinessResult:
    if not isinstance(data, dict) or not isinstance(data.get("BookMyShow"), dict):
        return result("UNKNOWN", "map_schema_unrecognized")
    response = data["BookMyShow"]
    if response.get("blnSuccess") == "false":
        reason = (
            "cinema_connectivity"
            if str(response.get("intExceptionEx")) == "5"
            else "provider_rejected_map"
        )
        return result("NOT_READY", reason, 200)
    if response.get("blnSuccess") != "true":
        return result("UNKNOWN", "map_success_flag_unrecognized", 200)
    if (
        any(
            type(response.get(k)) not in (int, str) or str(response[k]) != "0"
            for k in ("intException", "intExceptionEx")
        )
        or response.get("strException") != ""
    ):
        return result("UNKNOWN", "map_success_fields_conflict", 200)
    # The site returns an encoded map. Do not invent seat counts from its bytes.
    # A recognized successful map response AND fresh positive listing stock are
    # required. Checkout/seat adjacency is not verified by this passive read.
    payload = response.get("strData")
    if not isinstance(payload, str) or not 32 <= len(payload) <= 6_000_000:
        return result("UNKNOWN", "map_payload_missing", 200)
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (ValueError, TypeError):
        return result("UNKNOWN", "map_payload_invalid", 200)
    if len(decoded) < 24:
        return result("UNKNOWN", "map_payload_invalid", 200)
    return result("READY", "map_read_accepted", 200)


def multipart_map_body(offer):
    """Only GETSEATLAYOUT with transaction 0; never allocate/hold any seat."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", offer["cinema_id"]) or not re.fullmatch(
        r"\d{1,30}", offer["session_id"]
    ):
        raise ValueError("Invalid map request identity")
    context = offer.get("booking_data") or {}
    flags = []
    for key in ("show_seat_number", "best_seats_available", "couple_seats"):
        value = context.get(key)
        if value not in (None, "", "Y", "N"):
            raise ValueError("Unrecognized map hint")
        flags.append(value or "")
    params = {
        "strAppCode": "WEB",
        "lngTransactionIdentifier": "0",
        "strCommand": "GETSEATLAYOUT",
        "strVenueCode": offer["cinema_id"],
        "strParam1": offer["session_id"],
        "strParam2": "WEB",
        "strParam3": "",
        "strParam4": "",
        "strParam5": flags[0],
        "strParam6": flags[1],
        "strParam7": flags[2],
        "strParam8": "",
        "strParam9": "",
        "strParam10": "",
        "strFormat": "json",
    }
    boundary = "----MovieWatchReadOnlyMapBoundary"
    body = (
        "".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
            for key, value in params.items()
        )
        + f"--{boundary}--\r\n"
    )
    return body.encode(), f"multipart/form-data; boundary={boundary}"


class ReadinessProber:
    """Thread-local transports; no database, Telegram, customer auth or holds."""

    def __init__(self, timeout=8):
        self.timeout = timeout
        self.local = threading.local()
        self.clients = []
        self.lock = threading.Lock()

    def _client(self):
        if not hasattr(self.local, "client"):
            client = HttpClient(timeout=self.timeout, max_age=5)
            self.local.client = client
            with self.lock:
                self.clients.append(client)
        self.local.client.timeout = self.timeout
        return self.local.client

    def check(self, offer):
        try:
            client = self._client()
            if offer["provider"] == "bookmyshow":
                body, content_type = multipart_map_body(offer)
                response = client.post(
                    "https://services-in.bookmyshow.com/doTrans.aspx",
                    body=body,
                    browser_http=True,
                    headers={
                        "Content-Type": content_type,
                        "Origin": "https://in.bookmyshow.com",
                        "Referer": "https://in.bookmyshow.com/",
                    },
                )
                return parse_bms_map(response.json())
            if offer["provider"] == "district":
                context = offer.get("booking_data") or {}
                provider_id = context.get("provider_id")
                if (
                    type(provider_id) is not int
                    or provider_id <= 0
                    or not all(
                        isinstance(context.get(k), str)
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", context[k])
                        for k in ("movie_code", "format_code")
                    )
                    or not offer["cinema_id"].isdigit()
                    or not offer["session_id"].isdigit()
                ):
                    return result("UNKNOWN", "map_request_context_missing")
                body = {
                    "cinemaId": int(offer["cinema_id"]),
                    "sessionId": offer["session_id"],
                    "providerId": provider_id,
                    "screenOnTop": True,
                    "freeSeating": False,
                    "screenFormat": offer["format"],
                    "moviecode": context["movie_code"],
                    "config": {"socialDistancing": 1},
                    "retrieve": False,
                    "contentId": offer["movie_id"],
                    "cityKey": offer["city"],
                    "sessionDate": offer["local_start"][:10],
                    "fetchSessions": True,
                    "movieFormatCode": context["format_code"],
                }
                data = client.post(
                    "https://www.district.in/gw/consumer/movies/v1/select-seat",
                    params={
                        "version": 3,
                        "site_id": 1,
                        "channel": "ANDROIDAPPINSIDER",
                        "child_site_id": 1,
                        "platform": "district",
                    },
                    body=json.dumps(body).encode(),
                    headers={
                        "Content-Type": "application/json; charset=utf-8",
                        **guest_headers("district"),
                        "paytmCinemaId": offer["cinema_id"],
                    },
                ).json()
                if isinstance(data, dict) and data.get("error") is True:
                    return result("NOT_READY", "provider_rejected_map", 200)
                # No successful District map contract has been live-verified.
                # HTTP 200 or a guessed layout shape must never mean READY.
                return result("UNKNOWN", "district_map_contract_unverified", 200)
            return result("UNKNOWN", "unsupported_provider")
        except FetchError as exc:
            return result(
                "UNKNOWN",
                "provider_access_denied" if exc.status in DENIALS else "map_transport_failed",
                exc.status,
                exc.retry_after,
            )
        except Exception:
            return result("UNKNOWN", "map_validation_failed")

    def close(self):
        for client in self.clients:
            client.close()


@dataclass
class Entry:
    performance: str
    offer: dict
    signature: str
    future: object = None
    result: ReadinessResult | None = None
    next_at: float = 0
    failures: int = 0
    first_ready: str | None = None


def identity(performance, offer):
    signature = hashlib.sha256(
        json.dumps(
            [
                performance,
                offer["key"],
                offer["movie"],
                offer["movie_id"],
                offer["city"],
                offer["cinema_id"],
                offer["session_id"],
                offer["starts_at"],
                offer["language"],
                offer["format"],
                offer.get("booking_data", {}),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return (performance, offer["provider"], offer["key"], signature)


def collapse_confirmed(offers):
    selected, variants = {}, {}
    for offer in offers:
        provider = offer["provider"]
        selected.setdefault(provider, dict(offer))
        variants.setdefault(provider, set()).add(
            (offer["session_id"], offer.get("screen", ""), offer["language"], offer["format"])
        )
    for provider, distinct in variants.items():
        if len(distinct) > 1:
            selected[provider]["details"] = {}
    return list(selected.values())


def same_show(district, bms):
    """Called only within a verified canonical theatre/performance identity."""
    if any(normalized(district[k]) != normalized(bms[k]) for k in ("movie", "city", "language")):
        return False
    if datetime.fromisoformat(district["starts_at"]) != datetime.fromisoformat(bms["starts_at"]):
        return False

    def room(value):
        text = normalized(value)
        match = re.fullmatch(r"(?:audi|auditorium|screen)\s*0*(\d+)(.*)", text)
        return str(int(match[1])) + match[2] if match else text

    if (
        district.get("screen")
        and bms.get("screen")
        and room(district["screen"]) != room(bms["screen"])
    ):
        return False

    def projection(value):
        # Attributes after '|' are session audio tags added by our BMS parser.
        text = re.sub(r"([23])\s*d\b", r" \1d ", value.split("|", 1)[0], flags=re.I)
        tokens = normalized(text).split()
        dimension = next((x for x in tokens if x in ("2d", "3d")), None)
        base = " ".join("vision" if x == "vsn" else x for x in tokens if x not in ("2d", "3d"))
        return base or ("standard" if dimension else ""), dimension

    d_base, d_dim = projection(district["format"])
    b_base, b_dim = projection(bms["format"])
    if not d_base or d_base != b_base or (d_dim and b_dim and d_dim != b_dim):
        return False
    # A missing projection dimension is not guessed from the title. Shared native
    # session identity supplies the extra evidence for the observed MS variants.
    return d_dim == b_dim or district["session_id"] == bms["session_id"]


class ReadinessGate:
    def __init__(self, app, *, wake=None, prober=None, clock=None):
        self.app = app
        self.wake = wake or threading.Event()
        self.prober = prober or ReadinessProber(app["readiness_timeout_seconds"])
        self.clock = clock or time.monotonic
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="booking-readiness")
        self.entries = {}
        self.retired = []
        self.cooldown = {}
        self.denials = Counter()

    def check(self, rows, state, at):
        tick = self.clock()
        candidates = {
            identity(row["performance"], offer): (row["performance"], offer)
            for row in rows
            for offer in row["offers"]
        }
        for key in list(self.entries):
            if key not in candidates:
                old = self.entries.pop(key)
                if old.future is not None:
                    self.retired.append((old.offer["provider"], old.future))
        self.retired = [(provider, f) for provider, f in self.retired if not f.done()]
        for key, (performance, offer) in candidates.items():
            if key not in self.entries:
                self.entries[key] = Entry(performance, offer, key[-1])
            entry = self.entries[key]
            entry.offer = offer
            if (
                entry.result is None
                and entry.future is None
                and offer["provider"] == "district"
                and self.cooldown.get("district", 0) > tick
            ):
                # The provider is already refusing map reads. Do not impose its
                # access-denial wait on a District-only listing fallback.
                entry.result = ReadinessResult("UNKNOWN", "provider_backoff", at.isoformat())
                entry.next_at = self.cooldown["district"]
                state.record_readiness(performance, offer, entry.signature, entry.result)
            if entry.future is None or not entry.future.done():
                continue
            try:
                value = entry.future.result()
                age = (at - datetime.fromisoformat(value.checked_at)).total_seconds()
                if (
                    value.status not in ("READY", "NOT_READY", "UNKNOWN")
                    or not -2 <= age <= self.app["readiness_freshness_seconds"]
                ):
                    value = ReadinessResult("UNKNOWN", "stale_map_result", at.isoformat())
            except Exception:
                value = ReadinessResult("UNKNOWN", "map_worker_failed", at.isoformat())
            previous = (entry.result.status, entry.result.reason) if entry.result else None
            entry.future, entry.result = None, value
            entry.first_ready = state.record_readiness(performance, offer, entry.signature, value)
            retry = self.app["readiness_retry_seconds"]
            if value.status == "READY":
                entry.failures = 0
                self.denials[offer["provider"]] = 0
                entry.next_at = tick + self.app["readiness_freshness_seconds"]
            else:
                entry.failures += 1
                if value.status == "UNKNOWN":
                    retry = min(30, retry * 2 ** min(entry.failures - 1, 4))
                if value.http_status in DENIALS:
                    provider = offer["provider"]
                    self.denials[provider] += 1
                    retry = max(
                        value.retry_after or 0,
                        min(300, 30 * 2 ** min(self.denials[provider] - 1, 4)),
                    )
                    self.cooldown[provider] = max(self.cooldown.get(provider, 0), tick + retry)
                entry.next_at = tick + retry
            if previous != (value.status, value.reason):
                logger.info(
                    "Booking readiness: provider=%s session=%s state=%s reason=%s http_status=%s retry_seconds=%s",
                    offer["provider"],
                    offer["session_id"],
                    value.status,
                    value.reason,
                    value.http_status,
                    0 if value.status == "READY" else retry,
                )
        running = Counter(provider for provider, _ in self.retired)
        for entry in self.entries.values():
            if entry.future is not None:
                running[entry.offer["provider"]] += 1
        # Reserve at most two workers per provider: District failures cannot
        # occupy the BookMyShow workers. Never queue an unbounded executor batch.
        for entry in sorted(self.entries.values(), key=lambda e: e.next_at):
            provider = entry.offer["provider"]
            if (
                entry.future is not None
                or entry.next_at > tick
                or self.cooldown.get(provider, 0) > tick
                or running[provider] >= 2
                or sum(running.values()) >= 4
            ):
                continue
            entry.future = self.pool.submit(self.prober.check, dict(entry.offer))
            entry.future.add_done_callback(lambda _: self.wake.set())
            running[provider] += 1
        return [approved for row in rows if (approved := self.approved(row, at))]

    def approved(self, row, at):
        def fresh(offer):
            return (
                -30
                <= (at - datetime.fromisoformat(offer["observed_at"])).total_seconds()
                <= self.app["freshness_seconds"]
            )

        def confirmed(offer):
            entry = self.entries.get(identity(row["performance"], offer))
            if entry is None or entry.result is None or entry.result.status != "READY":
                return None
            age = (at - datetime.fromisoformat(entry.result.checked_at)).total_seconds()
            if not 0 <= age < self.app["readiness_freshness_seconds"] or not fresh(offer):
                return None
            return {**offer, "first_ready_at": entry.first_ready}

        bms_ready = [
            value
            for offer in row["offers"]
            if offer["provider"] == "bookmyshow" and (value := confirmed(offer))
        ]
        offers = list(bms_ready)
        listing_fallback = False
        for offer in row["offers"]:
            if offer["provider"] != "district" or not fresh(offer):
                continue
            entry = self.entries.get(identity(row["performance"], offer))
            if entry is None or entry.result is None or entry.result.status == "NOT_READY":
                continue
            known = [
                *row.get("bms_known", []),
                *(b for b in row["offers"] if b["provider"] == "bookmyshow"),
            ]
            matches = [b for b in known if same_show(offer, b)]
            if matches:
                matching_ready = [b for b in bms_ready if same_show(offer, b)]
                if not matching_ready:
                    continue
                offers.append(
                    {**offer, "first_ready_at": min(b["first_ready_at"] for b in matching_ready)}
                )
            elif entry.result.status == "READY":
                value = confirmed(offer)
                if value:
                    offers.append(value)
            elif entry.result.status == "UNKNOWN" and row.get("bms_absence_checked", True):
                offers.append({**offer, "first_ready_at": None})
                listing_fallback = True
        if not offers:
            return None
        verified_times = [o["first_ready_at"] for o in offers if o.get("first_ready_at")]
        return {
            **row,
            "offers": collapse_confirmed(offers),
            "first_ready_at": min(verified_times) if verified_times else None,
            "detection_basis": "bookmyshow_map"
            if verified_times
            else "district_listing"
            if listing_fallback
            else "map",
        }

    @property
    def inflight(self):
        return sum(e.future is not None for e in self.entries.values())

    def summary(self):
        return {
            "candidates": len(self.entries),
            "inflight": self.inflight,
            "states": dict(
                Counter(e.result.status if e.result else "CHECKING" for e in self.entries.values())
            ),
        }

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.prober.close()
