"""Readiness is fail-closed, asynchronous and never an extra aggregation delay."""

import base64
import json
import signal
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from movie_monitor.config import generation, validate_app
from movie_monitor.core import Observation, now
from movie_monitor.delivery import deliver
from movie_monitor.engine import run
from movie_monitor.http import FetchError, HttpClient, Response
from movie_monitor.readiness import (
    ReadinessGate,
    ReadinessProber,
    ReadinessResult,
    multipart_map_body,
    parse_bms_map,
    same_show,
)
from movie_monitor.state import State
from tests.support import telegram_server
from tests.test_watches import AT, Sender, obs, raw_app, show


def accepted_map():
    return {
        "BookMyShow": {
            "blnSuccess": "true",
            "intException": "0",
            "intExceptionEx": "0",
            "strException": "",
            "strData": base64.b64encode(bytes(range(64))).decode(),
        }
    }


def outcome(status="READY", at=AT, http=None, retry=None):
    return ReadinessResult(status, "test_" + status.lower(), at.isoformat(), http, retry)


class ManualPool:
    def __init__(self):
        self.calls = []

    def submit(self, function, offer):
        future = Future()
        self.calls.append((offer, future))
        return future

    def shutdown(self, **kwargs):
        pass


class ProberTests(unittest.TestCase):
    def test_recognized_successful_map_and_http_200_business_error(self):
        self.assertEqual(parse_bms_map(accepted_map()).status, "READY")
        failure = {
            "BookMyShow": {
                "blnSuccess": "false",
                "intException": "1135",
                "intExceptionEx": "5",
                "strException": "cinema connection failed",
                "strData": "",
            }
        }
        result = parse_bms_map(failure)
        self.assertEqual(result.status, "NOT_READY")
        self.assertEqual(result.reason, "cinema_connectivity")

    def test_missing_empty_malformed_and_contradictory_map_responses_never_pass(self):
        for change in (
            {"blnSuccess": True},
            {"blnSuccess": "TRUE"},
            {"intException": "5"},
            {"intExceptionEx": None},
            {"intException": False},
            {"strException": "error"},
            {"strData": ""},
            {"strData": "!" * 80},
            {"strData": "?" * 80},
            {"strData": ["map"]},
            {"strData": "YQ=="},
        ):
            data = accepted_map()
            data["BookMyShow"].update(change)
            self.assertEqual(parse_bms_map(data).status, "UNKNOWN", change)
        for data in ({}, [], None, {"BookMyShow": []}):
            self.assertEqual(parse_bms_map(data).status, "UNKNOWN")

    def test_read_command_never_creates_transactions_or_selects_seats(self):
        offer = show("bookmyshow").to_dict()
        offer["booking_data"] = {
            "show_seat_number": "Y",
            "best_seats_available": "Y",
            "couple_seats": "N",
        }
        body, content_type = multipart_map_body(offer)
        self.assertIn("multipart/form-data", content_type)
        self.assertIn(b"GETSEATLAYOUT", body)
        self.assertIn(b'name="lngTransactionIdentifier"\r\n\r\n0\r\n', body)
        for forbidden in (b"INITTRANS", b"ADDSEATS", b"SETSELECTEDSEATS", b"checkout"):
            self.assertNotIn(forbidden, body)
        for field, value in (("session_id", "1\r\nInjected"), ("cinema_id", "https://example.com")):
            with self.assertRaises(ValueError):
                multipart_map_body({**offer, field: value})
        offer["booking_data"]["couple_seats"] = "unrecognized"
        with self.assertRaises(ValueError):
            multipart_map_body(offer)

    def test_post_transport_uses_real_loopback_and_carries_retry_after(self):
        with telegram_server() as (url, received):
            response = HttpClient(min_gap=0).post(
                url + "/map", body=b'{"read":"only"}', headers={"Content-Type": "application/json"}
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(received[0]["payload"], {"read": "only"})

        class Session:
            def post(self, *args, **kwargs):
                return type(
                    "Reply",
                    (),
                    {
                        "status_code": 429,
                        "content": b"{}",
                        "text": "{}",
                        "headers": {"Retry-After": "120"},
                    },
                )()

        client = HttpClient(min_gap=0)
        client.bms_session = Session()
        with self.assertRaises(FetchError) as caught:
            client.post(
                "https://services-in.bookmyshow.com/doTrans.aspx", body=b"read", browser_http=True
            )
        self.assertEqual(caught.exception.retry_after, 120)

    def test_prober_uses_only_fixed_provider_url_and_propagates_denials(self):
        calls = []

        class Client:
            def post(self, url, **kwargs):
                calls.append((url, kwargs))
                return Response(200, json.dumps(accepted_map()))

        prober = ReadinessProber()
        with patch.object(prober, "_client", return_value=Client()):
            self.assertEqual(prober.check(show("bookmyshow").to_dict()).status, "READY")
        self.assertEqual(calls[0][0], "https://services-in.bookmyshow.com/doTrans.aspx")
        self.assertIn(b"GETSEATLAYOUT", calls[0][1]["body"])
        for status in (401, 403, 412, 429):
            client = type(
                "Client",
                (),
                {"post": lambda *a, **kw: (_ for _ in ()).throw(FetchError("denied", status, 120))},
            )()
            with patch.object(prober, "_client", return_value=client):
                value = prober.check(show("bookmyshow").to_dict())
            self.assertEqual(
                (value.status, value.http_status, value.retry_after), ("UNKNOWN", status, 120)
            )

    def test_district_without_verified_success_contract_stays_unknown(self):
        offer = show().to_dict()
        offer["booking_data"] = {
            "provider_id": 1707,
            "movie_code": "OBEOVX",
            "format_code": "Mm5dpqTqJz",
        }
        prober = ReadinessProber()
        for data, expected in (
            ({"seatLayout": {"rows": ["unverified"]}}, "UNKNOWN"),
            ({"error": True}, "NOT_READY"),
        ):
            client = type(
                "Client", (), {"post": lambda *a, **kw: Response(200, json.dumps(data))}
            )()
            with patch.object(prober, "_client", return_value=client):
                self.assertEqual(prober.check(offer).status, expected)
        with patch.object(prober, "_client", return_value=object()):
            self.assertEqual(prober.check(show().to_dict()).status, "UNKNOWN")

    def test_readiness_scheduling_is_validated_without_resetting_preference_generation(self):
        app = validate_app(raw_app())
        key = generation(app["watches"][0])
        for setting, value in (
            ("readiness_retry_seconds", 0),
            ("readiness_timeout_seconds", True),
            ("readiness_freshness_seconds", 100),
        ):
            with self.assertRaises(ValueError):
                validate_app({**raw_app(), setting: value})
        changed = validate_app({**raw_app(), "readiness_retry_seconds": 1})
        self.assertEqual(generation(changed["watches"][0]), key)


class ReadinessDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.app = validate_app(raw_app())
        self.app["aggregation_seconds"] = 60
        self.watch = self.app["watches"][0]
        self.key = generation(self.watch)
        self.state = State(":memory:")
        self.sender = Sender()
        self.clock = [0.0]
        self.gate = ReadinessGate(self.app, clock=lambda: self.clock[0])
        self.gate.pool.shutdown()
        self.pool = self.gate.pool = ManualPool()
        for provider in ("district", "bookmyshow"):
            self.ingest(obs(provider, status="NO_SHOWS"))

    def tearDown(self):
        self.gate.close()
        self.state.close()

    def ingest(self, observation):
        return self.state.ingest(
            self.key,
            self.watch,
            observation,
            self.app["theatre_aliases"],
            AT + timedelta(seconds=self.clock[0]),
        )

    def send(self):
        return deliver(
            self.state,
            self.sender,
            {self.key},
            ["-1"],
            self.app,
            readiness=self.gate,
            at=AT + timedelta(seconds=self.clock[0]),
        )

    def finish(self, index=0, status="READY", http=None, retry=None):
        self.pool.calls[index][1].set_result(
            outcome(status, AT + timedelta(seconds=self.clock[0]), http, retry)
        )

    def test_failed_map_is_pending_then_sends_immediately_on_success_without_aggregation(self):
        self.ingest(obs("bookmyshow", [show("bookmyshow")]))
        self.assertEqual(self.send()["sent"], 0)
        self.finish(status="NOT_READY")
        self.assertEqual(self.send()["sent"], 0)
        row = self.state.db.execute("SELECT state,attempts FROM v2_deliveries").fetchone()
        self.assertEqual(tuple(row), ("PENDING", 0))
        self.assertEqual(self.state.status()["delivery_states"], {"PENDING": 1})
        self.clock[0] = 1.99
        self.send()
        self.assertEqual(len(self.pool.calls), 1)
        self.clock[0] = 2
        self.send()
        self.assertEqual(len(self.pool.calls), 2)
        self.finish(1)
        self.assertEqual(self.send()["sent"], 1)
        self.assertIn("12:00:02 PM", self.sender.messages[0][1])
        self.assertIn("first successful seat-map check", self.sender.messages[0][1])
        self.assertEqual(self.send()["sent"], 0)

    def test_bookmyshow_confirmation_releases_matching_district_and_deduplicates_late_copies(self):
        for provider in ("district", "bookmyshow"):
            self.ingest(obs(provider, [show(provider)]))
        self.send()
        for index, (offer, _) in enumerate(self.pool.calls):
            self.finish(
                index,
                "READY" if offer["provider"] == "bookmyshow" else "UNKNOWN",
                412 if offer["provider"] == "district" else None,
            )
        self.assertEqual(self.send()["sent"], 1)
        text = self.sender.messages[0][1]
        self.assertIn("Platforms: BookMyShow, District", text)
        self.assertIn("district.in", text)
        self.assertEqual(self.send()["sent"], 0)
        self.assertEqual(len(self.pool.calls), 2)

    def test_access_denial_respects_provider_retry_after_and_keeps_receipt_unconsumed(self):
        self.ingest(obs("bookmyshow", [show("bookmyshow")]))
        self.send()
        self.finish(status="UNKNOWN", http=429, retry=120)
        self.send()
        self.clock[0] = 119
        self.send()
        self.assertEqual(len(self.pool.calls), 1)
        self.clock[0] = 120
        self.send()
        self.assertEqual(len(self.pool.calls), 2)
        self.assertEqual(self.sender.messages, [])
        self.assertEqual(
            self.state.db.execute("SELECT attempts FROM v2_deliveries").fetchone()[0], 0
        )

    def test_startup_baseline_and_sent_receipts_never_trigger_map_requests(self):
        other = State(":memory:")
        try:
            other.ingest(self.key, self.watch, obs(shows=[show()]), (), AT)
            deliver(other, self.sender, {self.key}, ["-1"], self.app, readiness=self.gate, at=AT)
            self.assertEqual(self.pool.calls, [])
        finally:
            other.close()
        self.ingest(obs(shows=[show()]))
        self.send()
        self.finish()
        self.send()
        self.send()
        self.assertEqual(len(self.pool.calls), 1)
        self.assertEqual(len(self.sender.messages), 1)

    def test_expired_proof_and_stale_listing_cannot_be_sent(self):
        self.ingest(obs(shows=[show()]))
        self.send()
        self.finish()
        rows = self.state.due({self.key}, 180, AT, all_offers=True)
        self.gate.check(rows, self.state, AT)
        self.assertIsNotNone(self.gate.approved(rows[0], AT))
        self.assertIsNone(self.gate.approved(rows[0], AT + timedelta(seconds=10)))
        self.assertIsNone(self.gate.approved(rows[0], AT + timedelta(seconds=181)))
        self.assertEqual(self.sender.messages, [])

    def test_changed_booking_context_discards_inflight_proof(self):
        original = show("bookmyshow")
        self.ingest(obs("bookmyshow", [original]))
        self.send()
        self.ingest(obs("bookmyshow", [replace(original, booking_data={"couple_seats": "Y"})]))
        self.finish()
        self.send()
        self.assertEqual(self.sender.messages, [])
        self.assertEqual(len(self.pool.calls), 2)
        self.finish(1)
        self.assertEqual(self.send()["sent"], 1)

    def test_same_time_different_sessions_checks_all_and_uses_successful_session(self):
        bad = replace(show("bookmyshow", sid="1"), screen="BAD")
        good = replace(
            show("bookmyshow", sid="2"), screen="GOOD", details={"version": 1, "auditorium": "GOOD"}
        )
        self.ingest(obs("bookmyshow", [bad, good]))
        self.send()
        self.assertEqual(len(self.pool.calls), 2)
        for index, (offer, _) in enumerate(self.pool.calls):
            self.finish(index, "READY" if offer["session_id"] == "2" else "NOT_READY")
        self.assertEqual(self.send()["sent"], 1)
        self.assertIn("Auditorium: GOOD", self.sender.messages[0][1])

    def test_restart_rechecks_pending_maps_and_preserves_first_ready_timestamp(self):
        self.ingest(obs("bookmyshow", [show("bookmyshow")]))
        self.send()
        self.finish(status="NOT_READY")
        self.send()
        self.gate.close()
        self.gate = ReadinessGate(self.app, clock=lambda: self.clock[0])
        self.gate.pool.shutdown()
        self.pool = self.gate.pool = ManualPool()
        self.send()
        self.assertEqual(len(self.pool.calls), 1)
        self.finish()
        self.assertEqual(self.send()["sent"], 1)
        record = self.state.db.execute("SELECT status,first_ready_at FROM v2_readiness").fetchone()
        self.assertEqual(tuple(record), ("READY", AT.isoformat()))

    def test_removed_watch_and_worker_exception_do_not_leak_a_notification(self):
        self.ingest(obs("bookmyshow", [show("bookmyshow")]))
        self.send()
        self.pool.calls[0][1].set_exception(RuntimeError("untrusted upstream secret"))
        self.assertEqual(self.send()["sent"], 0)
        self.assertEqual(
            self.state.db.execute("SELECT reason FROM v2_readiness").fetchone()[0],
            "map_worker_failed",
        )
        deliver(self.state, self.sender, set(), ["-1"], self.app, readiness=self.gate, at=AT)
        self.assertEqual(self.gate.entries, {})
        self.assertEqual(self.sender.messages, [])

    def test_shared_show_waits_for_bookmyshow_even_when_district_map_passes(self):
        for provider in ("district", "bookmyshow"):
            self.ingest(obs(provider, [show(provider)]))
        self.send()
        for index, (offer, _) in enumerate(self.pool.calls):
            self.finish(index, "NOT_READY" if offer["provider"] == "bookmyshow" else "READY")
        self.assertEqual(self.send()["sent"], 0)
        self.assertEqual(self.sender.messages, [])
        self.clock[0] = 2
        self.send()
        self.assertEqual(self.pool.calls[-1][0]["provider"], "bookmyshow")
        self.finish(len(self.pool.calls) - 1)
        self.assertEqual(self.send()["sent"], 1)
        self.assertEqual(self.send()["sent"], 0)

    def test_district_only_listing_sends_when_its_map_is_unknown(self):
        self.ingest(obs(shows=[show()]))
        self.send()
        self.finish(status="UNKNOWN", http=412)
        self.assertEqual(self.send()["sent"], 1)
        text = self.sender.messages[0][1]
        self.assertIn("Platforms: District", text)
        self.assertIn("First detected available:", text)
        self.assertIn("first listing observation", text)
        self.assertNotIn("first successful seat-map check", text)
        self.assertEqual(self.send()["sent"], 0)

    def test_district_explicit_closed_response_is_not_a_fallback(self):
        self.ingest(obs(shows=[show()]))
        self.send()
        self.finish(status="NOT_READY")
        self.assertEqual(self.send()["sent"], 0)

    def test_known_bookmyshow_match_survives_failed_or_empty_scans_and_gate_restart(self):
        for provider in ("district", "bookmyshow"):
            self.ingest(obs(provider, [show(provider)]))
        self.send()
        for index, (offer, _) in enumerate(self.pool.calls):
            self.finish(index, "NOT_READY" if offer["provider"] == "bookmyshow" else "UNKNOWN")
        self.send()
        for observation in (
            obs("bookmyshow", status="ERROR", complete=False),
            obs("bookmyshow", status="NO_SHOWS"),
        ):
            self.ingest(observation)
            self.assertEqual(self.send()["sent"], 0)
        self.gate.close()
        self.gate = ReadinessGate(self.app, clock=lambda: self.clock[0])
        self.gate.pool.shutdown()
        self.pool = self.gate.pool = ManualPool()
        self.send()
        self.finish(status="UNKNOWN")
        self.assertEqual(self.send()["sent"], 0)
        self.assertEqual(
            self.state.db.execute("SELECT COUNT(*) FROM v2_bms_presence").fetchone()[0], 1
        )

    def test_unavailable_bookmyshow_listing_still_blocks_district_fallback(self):
        self.ingest(obs(shows=[show()]))
        self.ingest(obs("bookmyshow", [show("bookmyshow", availability="UNKNOWN")]))
        self.send()
        self.assertEqual(len(self.pool.calls), 1)
        self.finish(status="UNKNOWN")
        self.assertEqual(self.send()["sent"], 0)

    def test_other_format_at_same_time_does_not_block_district_only_print(self):
        self.ingest(obs(shows=[show()]))
        self.ingest(obs("bookmyshow", [replace(show("bookmyshow"), format="2D")]))
        self.send()
        for index, (offer, _) in enumerate(self.pool.calls):
            self.finish(index, "NOT_READY" if offer["provider"] == "bookmyshow" else "UNKNOWN")
        self.assertEqual(self.send()["sent"], 1)
        self.assertIn("Platforms: District", self.sender.messages[0][1])

    def test_bookmyshow_scan_failure_cannot_establish_district_only_absence(self):
        self.ingest(obs(shows=[show()]))
        self.ingest(obs("bookmyshow", status="ERROR", complete=False, scope="CBMC"))
        self.send()
        self.finish(status="UNKNOWN")
        self.assertEqual(self.send()["sent"], 0)
        self.ingest(obs("bookmyshow", status="NO_SHOWS", scope="CBMC"))
        self.assertEqual(self.send()["sent"], 1)

    def test_provider_cooldown_does_not_add_a_wait_to_a_new_district_only_candidate(self):
        self.ingest(obs(shows=[show()]))
        self.send()
        self.finish(status="UNKNOWN", http=412)
        self.send()
        self.ingest(obs(shows=[show(), show(sid="2", clock="20:00")]))
        self.assertEqual(self.send()["sent"], 1)
        self.assertEqual(len(self.pool.calls), 1)
        self.assertEqual(len(self.sender.messages), 2)

    def test_marina_alias_reconciles_existing_history_before_policy_decision(self):
        district = replace(
            show(), cinema_id="1020778", cinema="INOX The Marina Mall OMR, Egatoor, Chennai"
        )
        bms = replace(show("bookmyshow"), cinema_id="INTO", cinema="INOX: The Marina Mall, OMR")
        for value in (district, bms):
            self.state.ingest(self.key, self.watch, obs(value.provider, [value]), (), AT)
        self.assertEqual(self.state.status()["performances"], 2)
        self.state.reconcile_aliases({self.key}, self.app["theatre_aliases"])
        self.send()
        for index, (offer, _) in enumerate(self.pool.calls):
            self.finish(index, "NOT_READY" if offer["provider"] == "bookmyshow" else "UNKNOWN")
        self.assertEqual(self.send()["sent"], 0)
        self.assertEqual(len(self.state.due({self.key}, 180, AT)), 1)

    def test_exact_show_matching_does_not_borrow_other_print_or_auditorium(self):
        district = show().to_dict()
        bms = show("bookmyshow").to_dict()
        self.assertTrue(same_show(district, bms))
        for changes in (
            {"movie": "Dune"},
            {"language": "Tamil"},
            {"format": "IMAX 2D"},
            {"starts_at": show(clock="19:00").starts_at},
        ):
            self.assertFalse(same_show(district, {**bms, **changes}))
        self.assertFalse(same_show({**district, "screen": "AUDI 1"}, {**bms, "screen": "SCREEN 2"}))
        self.assertTrue(
            same_show(
                {**district, "screen": "AUDI03", "format": "MS INFINITY VISION"},
                {**bms, "screen": "SCREEN 3", "format": "MS-Infinity Vsn 3d"},
            )
        )


class AsyncReadinessTests(unittest.TestCase):
    def test_pending_map_retries_without_waiting_for_next_listing_poll(self):
        raw = raw_app()
        raw["aggregation_seconds"] = 60
        raw["readiness_retry_seconds"] = 1
        raw["watches"][0]["dates"] = ["2099-01-02"]
        raw["watches"][0]["providers"].pop("bookmyshow")
        app = validate_app(raw)
        cfg = app["watches"][0]
        key = generation(cfg)
        candidate = replace(show(), starts_at="2099-01-02T18:00:00+05:30")
        collected, checked, handlers = [], [], {}

        class Collector:
            def __init__(self, *args, **kwargs):
                pass

            def collect(self, provider, day):
                collected.append(provider)
                return Observation(provider, day, shows=[candidate])

            def close(self):
                pass

        class Prober:
            def check(self, offer):
                checked.append(time.monotonic())
                return outcome("READY" if len(checked) > 1 else "NOT_READY", now())

            def close(self):
                pass

        class StoppingSender(Sender):
            def send(self, target, text):
                ack = super().send(target, text)
                handlers[signal.SIGTERM](signal.SIGTERM, None)
                return ack

        def replace_handler(sig, handler):
            old = handlers.get(sig, signal.SIG_DFL)
            handlers[sig] = handler
            return old

        def factory(app, **kwargs):
            return ReadinessGate(app, prober=Prober(), **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config = folder / "config.json"
            db = folder / "state.sqlite3"
            config.write_text(json.dumps(raw))
            state = State(str(db))
            at = now()
            state.ingest(
                key,
                cfg,
                Observation(
                    "district", "2099-01-02", status="NO_SHOWS", observed_at=at.isoformat()
                ),
                (),
                at,
            )
            state.close()
            sender = StoppingSender()
            started = time.monotonic()
            with (
                patch("movie_monitor.engine.Collector", Collector),
                patch("movie_monitor.engine.ReadinessGate", factory),
                patch("movie_monitor.engine.signal.signal", replace_handler),
            ):
                run(str(config), str(db), loop=True, sender=sender, targets=["-1"])
            self.assertLess(time.monotonic() - started, 3)
            self.assertGreaterEqual(checked[1] - checked[0], 1)
            self.assertEqual(collected, ["district"])
            self.assertEqual(len(sender.messages), 1)

    def test_blocked_district_workers_do_not_delay_successful_bookmyshow_result(self):
        blocked, release, bms_done = threading.Event(), threading.Event(), threading.Event()

        class Prober:
            def check(self, offer):
                if offer["provider"] == "district":
                    blocked.set()
                    release.wait(3)
                    return outcome("UNKNOWN")
                bms_done.set()
                return outcome()

            def close(self):
                pass

        app = validate_app(raw_app())
        cfg = app["watches"][0]
        key = generation(cfg)
        state = State(":memory:")
        sender = Sender()
        wake = threading.Event()
        gate = ReadinessGate(app, prober=Prober(), wake=wake)
        try:
            for provider in ("district", "bookmyshow"):
                state.ingest(key, cfg, obs(provider, status="NO_SHOWS"), app["theatre_aliases"], AT)
                state.ingest(key, cfg, obs(provider, [show(provider)]), app["theatre_aliases"], AT)
            started = time.monotonic()
            deliver(state, sender, {key}, ["-1"], app, readiness=gate, at=AT)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(blocked.wait(1))
            self.assertTrue(bms_done.wait(1))
            self.assertTrue(wake.wait(1))
            deadline = time.monotonic() + 1
            while not sender.messages and time.monotonic() < deadline:
                deliver(state, sender, {key}, ["-1"], app, readiness=gate, at=AT)
            self.assertEqual(len(sender.messages), 1)
            self.assertIn("Platforms: BookMyShow", sender.messages[0][1])
            self.assertFalse(release.is_set())
        finally:
            release.set()
            gate.close()
            state.close()

    def test_engine_delivers_on_worker_completion_without_waiting_for_legacy_aggregation(self):
        raw = raw_app()
        raw["aggregation_seconds"] = 60
        raw["watches"][0]["dates"] = ["2099-01-02"]
        raw["watches"][0]["providers"].pop("bookmyshow")
        app = validate_app(raw)
        cfg = app["watches"][0]
        key = generation(cfg)
        candidate = replace(show(), starts_at="2099-01-02T18:00:00+05:30")

        class Collector:
            def __init__(self, *args, **kwargs):
                pass

            def collect(self, provider, day):
                return Observation(provider, day, shows=[candidate])

            def close(self):
                pass

        class Prober:
            def check(self, offer):
                return outcome(at=now())

            def close(self):
                pass

        def factory(app, **kwargs):
            return ReadinessGate(app, prober=Prober(), **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config = folder / "config.json"
            db = folder / "state.sqlite3"
            config.write_text(json.dumps(raw))
            state = State(str(db))
            at = now()
            state.ingest(
                key,
                cfg,
                Observation(
                    "district", "2099-01-02", status="NO_SHOWS", observed_at=at.isoformat()
                ),
                (),
                at,
            )
            state.close()
            sender = Sender()
            started = time.monotonic()
            with (
                patch("movie_monitor.engine.Collector", Collector),
                patch("movie_monitor.engine.ReadinessGate", factory),
            ):
                run(str(config), str(db), sender=sender, targets=["-1"])
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(len(sender.messages), 1)
