import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from movie_monitor.bms import Catalogue, CinemaCollector
from movie_monitor.config import generation, validate_app
from movie_monitor.health import HealthJournal, conditions
from movie_monitor.http import FetchError, Response
from movie_monitor.schedule import Schedule
from movie_monitor.state import State
from movie_monitor.verification import opening_verification
from tests.test_watches import AT, DAY, Sender, obs, raw_app, show, watch


def venue(code):
    return {
        "VenueCode": code,
        "VenueName": "Cinema " + code,
        "RegionCode": "CHEN",
        "arrDates": [{"ShowDateCode": "20300102"}],
        "isDown": False,
    }


def payload():
    return {
        "ShowDetails": [
            {
                "Date": "20300102",
                "Event": [
                    {
                        "EventTitle": "Avengers: Doomsday",
                        "EventGroup": "EG00437704",
                        "ChildEvents": [
                            {
                                "EventGroup": "EG00437704",
                                "EventCode": "ET00526287",
                                "EventLanguage": "English",
                                "EventDimension": "3D",
                                "ShowTimes": [
                                    {
                                        "ShowDateTime": "203001021800",
                                        "ShowDateCode": "20300102",
                                        "SessionId": "1",
                                        "AvailStatus": "3",
                                        "SessionUnpaidFlag": "N",
                                        "CutOffFlag": "1",
                                        "Categories": [{"AvailStatus": "3", "SeatLayout": "Y"}],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
        "ShowDatesArray": [{"DateCode": "20300102", "isDisabled": False}],
    }


class ScheduleTests(unittest.TestCase):
    def test_bms_requests_revalidation_and_preserves_explicit_headers(self):
        from movie_monitor.http import HttpClient

        class Session:
            kwargs = None

            def get(self, *args, **kwargs):
                self.kwargs = kwargs
                return type(
                    "Response",
                    (),
                    {"status_code": 200, "headers": {"Age": "0"}, "content": b"{}", "text": "{}"},
                )()

        session = Session()
        client = HttpClient(min_gap=0)
        client.bms_session = session
        client.get("https://in.bookmyshow.com/test", headers={"X-Test": "1"}, browser_http=True)
        self.assertEqual(session.kwargs["headers"]["Cache-Control"], "no-cache")
        self.assertEqual(session.kwargs["headers"]["X-Test"], "1")

    def test_revalidation_does_not_disable_stale_response_rejection(self):
        from movie_monitor.http import HttpClient

        class Session:
            def get(self, *args, **kwargs):
                return type(
                    "Response",
                    (),
                    {"status_code": 200, "headers": {"Age": "181"}, "content": b"{}", "text": "{}"},
                )()

        client = HttpClient(min_gap=0)
        client.bms_session = Session()
        with self.assertRaises(FetchError):
            client.get("https://in.bookmyshow.com/test", browser_http=True)

    def test_failure_only_backs_off_its_own_scope(self):
        clock = [0]
        schedule = Schedule(60, 900, lambda: clock[0])
        schedule.failure("bad", "403")
        schedule.success("good")
        clock[0] = 60
        self.assertFalse(schedule.due("bad"))
        self.assertTrue(schedule.due("good"))
        clock[0] = 120
        self.assertTrue(schedule.due("bad"))
        schedule.success("bad")
        self.assertEqual(schedule.failures(), {})

    def test_catalogue_network_does_not_lock_cached_cinema_reads(self):
        entered, release = threading.Event(), threading.Event()

        class Client:
            def get(self, *args, **kwargs):
                entered.set()
                release.wait(3)
                raise FetchError("HTTP 403", 403)

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            catalogue = Catalogue(directory)
            catalogue.memory["chennai"] = {
                "saved_at": (AT - timedelta(minutes=2)).isoformat(),
                "venues": [venue("GOOD")],
            }
            catalogue.clients["chennai"] = Client()
            try:
                with patch("movie_monitor.bms.now", return_value=AT):
                    catalogue.ensure_refresh("chennai", "CHEN")
                    self.assertTrue(entered.wait(1))
                    result = []
                    thread = threading.Thread(
                        target=lambda: result.append(catalogue.snapshot("chennai", "CHEN"))
                    )
                    thread.start()
                    thread.join(timeout=0.5)
                    self.assertFalse(
                        thread.is_alive(), "Cached reads waited for catalogue network I/O"
                    )
                    self.assertEqual(result[0][0][0]["VenueCode"], "GOOD")
            finally:
                release.set()
                catalogue.close()

    def test_bad_cinema_does_not_delay_good_cinema(self):
        calls = []

        class Registry:
            def ensure_refresh(self, *args):
                pass

            def snapshot(self, *args):
                return [venue("GOOD"), venue("BAD")], True, None

            def version(self, *args):
                return "unchanged"

            def known_dates(self, *args):
                return {"20300102"}

            def fetch_venue(self, city, region, code, day, client):
                calls.append(code)
                if code == "BAD":
                    raise FetchError("HTTP 403", 403)
                return payload(), AT.isoformat()

        collector = CinemaCollector(watch(), type("Client", (), {"count": 0})(), Registry())
        clock = [0]
        collector.active.clock = lambda: clock[0]
        collector.collect_dates([DAY])
        clock[0] = 60
        collector.collect_dates([DAY])
        self.assertEqual(calls.count("GOOD"), 2)
        self.assertEqual(calls.count("BAD"), 1)
        self.assertEqual(collector.components()["cinemas"]["failed_scopes"], 1)

    def test_api_cache_preserves_timestamp_and_extracts_all_published_dates(self):
        class Client:
            count = 0

            def get(self, *args, **kwargs):
                self.count += 1
                return Response(200, json.dumps(payload()))

        with tempfile.TemporaryDirectory() as directory:
            registry = Catalogue(directory)
            client = Client()
            first, stamp = registry.fetch_venue("chennai", "CHEN", "GOOD", "20300102", client)
            second, stamp2 = registry.fetch_venue("chennai", "CHEN", "GOOD", "20300102", client)
            self.assertEqual(client.count, 1)
            self.assertEqual(stamp, stamp2)
            self.assertIn(
                "20300102", registry.known_dates("chennai", {**venue("GOOD"), "arrDates": []})
            )
            registry.close()

    def test_normal_refresh_in_progress_is_not_an_outage(self):
        from concurrent.futures import Future

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("movie_monitor.bms.now", return_value=AT),
        ):
            registry = Catalogue(directory)
            registry.memory["chennai"] = {
                "saved_at": (AT - timedelta(seconds=65)).isoformat(),
                "venues": [venue("GOOD")],
            }
            registry.futures["chennai"] = Future()
            _, fresh, error = registry.snapshot("chennai", "CHEN")
            self.assertFalse(fresh)
            self.assertEqual(error, "Catalogue refresh in progress")
            collector = CinemaCollector(watch(), type("Client", (), {"count": 0})(), registry)
            collector.catalogue_error = error
            self.assertEqual(collector.components()["catalogue"]["state"], "REFRESHING")
            registry.close()

    def test_cinema_date_hints_discover_new_dates_while_city_catalogue_is_blocked(self):
        calls, published = [], set()

        class Registry:
            def ensure_refresh(self, *args):
                pass

            def snapshot(self, *args):
                return [{**venue("GOOD"), "arrDates": []}], False, "HTTP 403"

            def version(self, *args):
                return "old-version"

            def known_dates(self, *args):
                return set(published)

            def fetch_venue(self, city, region, code, day, client):
                calls.append(day)
                published.add("20300102")
                return payload(), AT.isoformat()

        collector = CinemaCollector(
            watch(), type("Client", (), {"count": 0})(), Registry(), background_checks=1
        )
        result = collector.collect_dates([DAY])
        self.assertTrue(any(o.scope_id == "GOOD" and len(o.shows) == 1 for o in result))
        self.assertIn("20300102", calls)
        self.assertEqual(collector.components()["cinemas"]["state"], "HEALTHY")
        self.assertEqual(collector.components()["catalogue"]["state"], "DEGRADED")


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.journal = HealthJournal(":memory:")
        self.sender = Sender()

    def tearDown(self):
        self.journal.close()

    def test_debounce_dedup_and_recovery_are_logged_without_deliveries(self):
        fault = {"provider": "Simulated provider failure"}
        self.journal.observe(fault, at=AT)
        self.journal.observe(fault, at=AT + timedelta(seconds=119))
        self.assertEqual(self.journal.summary()["events"], {})
        later = AT + timedelta(seconds=120)
        self.journal.observe(fault, at=later)
        self.journal.observe(fault, at=later)
        self.assertEqual(self.journal.summary()["events"], {"FAILURE": 1})
        self.journal.observe({}, at=later + timedelta(seconds=1))
        self.assertEqual(self.journal.summary()["events"], {"FAILURE": 1, "RECOVERY": 1})
        self.assertEqual(self.journal.summary()["legacy_delivery_states"], {})
        self.assertEqual(self.sender.messages, [])
        self.assertFalse(hasattr(self.journal, "send_due"))

    def test_transient_fault_does_not_generate_incident_events(self):
        self.journal.observe({"fault": "temporary"}, at=AT)
        self.journal.observe({}, at=AT + timedelta(seconds=60))
        self.assertEqual(self.journal.summary()["events"], {})
        self.assertEqual(self.journal.summary()["active_incidents"], 0)

    def test_intentionally_disabled_service_is_not_a_failure(self):
        self.assertIsNone(
            conditions(None, AT, {"UnitFileState": "disabled", "ActiveState": "inactive"})
        )
        self.assertTrue(self.journal.observe(None, at=AT)["paused"])

    def test_stale_heartbeat_invalid_config_and_uncertain_delivery_are_reported(self):
        report = {
            "checked_at": (AT - timedelta(minutes=4)).isoformat(),
            "running": True,
            "config_error": "Invalid JSON",
            "state": {"delivery_states": {"UNKNOWN": 1}},
        }
        issues = conditions(report, AT)
        self.assertEqual(set(issues), {"heartbeat", "configuration", "delivery-review"})

    def test_bookmyshow_components_have_distinct_health_conditions(self):
        report = {
            "checked_at": AT.isoformat(),
            "running": True,
            "providers": [
                {
                    "watch": "avengers",
                    "provider": "bookmyshow",
                    "checked_at": AT.isoformat(),
                    "state": "DEGRADED",
                    "components": {
                        "catalogue": {"state": "DEGRADED", "error": "403"},
                        "cinemas": {"state": "HEALTHY"},
                        "discovery_probes": {"state": "HEALTHY"},
                    },
                }
            ],
        }
        issues = conditions(report, AT)
        self.assertEqual(len(issues), 1)
        self.assertIn("avengers:bookmyshow:catalogue", issues)

    def test_upgrade_cancels_pending_health_messages_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "health.sqlite3")
            journal = HealthJournal(path)
            with journal.db:
                for state in ("PENDING", "SENT", "UNKNOWN", "SENDING"):
                    journal.db.execute(
                        "INSERT INTO deliveries(event,target,state) VALUES(1,?,?)", (state, state)
                    )
            journal.close()
            journal = HealthJournal(path)
            try:
                self.assertEqual(
                    journal.summary()["legacy_delivery_states"],
                    {"CANCELLED": 1, "SENT": 1, "UNKNOWN": 2},
                )
                journal.observe({"fault": "test"}, at=AT, delay=0)
                journal.observe({}, at=AT + timedelta(seconds=1))
                self.assertEqual(
                    journal.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 4
                )
            finally:
                journal.close()

    def test_watchdog_legacy_notify_flag_still_only_logs(self):
        import contextlib
        import io
        import os

        from scripts import check_health

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            report.write_text(
                json.dumps(
                    {"checked_at": AT.isoformat(), "running": True, "config_error": "bad JSON"}
                )
            )
            args = [
                "check_health.py",
                "--ignore-service",
                "--notify",
                "--report",
                str(report),
                "--db",
                str(root / "missing.sqlite3"),
                "--directory",
                str(root / "ops"),
            ]
            with (
                patch("sys.argv", args),
                patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "must-not-be-used"}),
                patch(
                    "movie_monitor.notify.Telegram.send",
                    side_effect=AssertionError("Health messages must never send"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                check_health.main()
            status = json.loads((root / "ops/health-latest.json").read_text())
            self.assertFalse(status["notifications_enabled"])
            self.assertIn("configuration", status["conditions"])
            self.assertEqual(status["journal"]["legacy_delivery_states"], {})


class OpeningEvidenceTests(unittest.TestCase):
    def test_test_events_are_excluded_and_real_mode_requires_telegram_ack(self):
        app = validate_app(raw_app())
        cfg = app["watches"][0]
        key = generation(cfg)
        with tempfile.TemporaryDirectory() as directory:
            for test_mode in (True, False):
                path = str(Path(directory) / f"{test_mode}.sqlite3")
                state = State(path, test_mode=test_mode)
                state.ingest(key, cfg, obs(status="NO_SHOWS"), app["theatre_aliases"], AT)
                state.ingest(key, cfg, obs(shows=[show()]), app["theatre_aliases"], AT)
                before = opening_verification(path)
                self.assertEqual(
                    before["status"],
                    "WAITING_FOR_REAL_EVENT"
                    if test_mode
                    else "REAL_EVENT_DETECTED_AWAITING_DELIVERY",
                )
                state.prepare({key}, ["-1"], 180, 0, AT)
                row = state.due({key}, 180, AT)[0]
                state.claim(row["id"])
                state.delivered(row["id"], "123")
                after = opening_verification(path)
                self.assertEqual(
                    after["status"],
                    "WAITING_FOR_REAL_EVENT"
                    if test_mode
                    else "REAL_EVENT_OBSERVED_AND_TELEGRAM_ACKNOWLEDGED",
                )
                state.close()

    def test_existing_baseline_never_counts_as_a_real_opening(self):
        cfg = watch()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite3")
            state = State(path)
            state.ingest(generation(cfg), cfg, obs(shows=[show()]), (), AT)
            self.assertEqual(opening_verification(path)["status"], "WAITING_FOR_REAL_EVENT")
            state.close()


if __name__ == "__main__":
    unittest.main()
