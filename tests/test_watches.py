import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from movie_monitor.bms import Catalogue, parse_venue
from movie_monitor.config import generation, validate_app
from movie_monitor.core import IST, Observation, Show
from movie_monitor.delivery import deliver
from movie_monitor.engine import run
from movie_monitor.lock import SingleInstance
from movie_monitor.notify import DeliveryError, Telegram
from movie_monitor.state import State
from tests.support import telegram_server

AT = datetime(2029, 12, 1, 12, 0, tzinfo=IST)
DAY = "2030-01-02"


def raw_app():
    raw = json.loads(Path("watches.example.json").read_text())
    raw["aggregation_seconds"] = 0
    raw["watches"][0].pop("date_range")
    raw["watches"][0]["dates"] = [DAY]
    return raw


def watch():
    return validate_app(raw_app())["watches"][0]


def show(provider="district", sid="1", clock="18:00", availability="AVAILABLE"):
    cfg = watch()
    code = "ET00526287" if provider == "bookmyshow" else "176735"
    venue = "CBMC" if provider == "bookmyshow" else "9505"
    name = (
        "Cinepolis: BSR Mall, OMR, Thoraipakkam"
        if provider == "bookmyshow"
        else "Cinepolis BSR Mall OMR, Thoraipakkam, Chennai"
    )
    host = {
        "district": "www.district.in",
        "bookmyshow": "in.bookmyshow.com",
    }[provider]
    return Show(
        provider,
        cfg["movie"],
        code,
        "chennai",
        venue,
        name,
        sid,
        f"{DAY}T{clock}:00+05:30",
        "English",
        "IMAX 3D",
        availability,
        f"https://{host}/movie/{code}",
    )


def obs(provider="district", shows=(), *, status="OK", at=AT, scope="city", complete=True):
    return Observation(
        provider,
        DAY,
        status=status,
        shows=list(shows),
        observed_at=at.isoformat(),
        scope_id=scope,
        complete=complete,
    )


class Sender:
    def __init__(self):
        self.messages = []

    def send(self, target, text):
        self.messages.append((target, text))
        return str(len(self.messages))


class ConfigurationTests(unittest.TestCase):
    def test_inclusive_date_range_and_five_independent_movies(self):
        raw = raw_app()
        raw["watches"][0].pop("dates")
        raw["watches"][0]["date_range"] = {"from": "2030-01-02", "to": "2030-01-05"}
        first = raw["watches"][0]
        raw["watches"] = [
            {**copy.deepcopy(first), "id": f"film-{i}", "movie": f"Film {i}"} for i in range(5)
        ]
        app = validate_app(raw)
        self.assertEqual(len(app["watches"]), 5)
        self.assertEqual(
            app["watches"][0]["dates"], ["2030-01-02", "2030-01-03", "2030-01-04", "2030-01-05"]
        )
        self.assertEqual(len({generation(w) for w in app["watches"]}), 5)
        raw["watches"].append({**copy.deepcopy(first), "id": "film-6"})
        with self.assertRaises(ValueError):
            validate_app(raw)

    def test_reject_duplicate_ids_wrong_groups_and_ambiguous_aliases(self):
        raw = raw_app()
        for mutate in (
            lambda r: r["watches"].append(copy.deepcopy(r["watches"][0])),
            lambda r: r["watches"][0]["providers"]["bookmyshow"].pop("event_group"),
            lambda r: r["theatre_aliases"].append(copy.deepcopy(r["theatre_aliases"][0])),
        ):
            changed = copy.deepcopy(raw)
            mutate(changed)
            with self.assertRaises(ValueError):
                validate_app(changed)

    def test_schedule_changes_preserve_baseline_preferences_change_it(self):
        cfg = watch()
        self.assertEqual(generation(cfg), generation({**cfg, "poll_seconds": 120}))
        self.assertNotEqual(generation(cfg), generation({**cfg, "languages": ["Tamil"]}))

    def test_single_instance_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "monitor.lock")
            first = SingleInstance(path)
            with self.assertRaises(RuntimeError):
                SingleInstance(path)
            first.close()
            second = SingleInstance(path)
            second.close()

    def test_misspelled_preferences_and_malformed_provider_objects_are_rejected(self):
        for key, value in (
            ("langauges", ["English"]),
            ("dates_from", "2030-01-01"),
            ("providers", []),
        ):
            raw = raw_app()
            raw["watches"][0][key] = value
            with self.assertRaises(ValueError):
                validate_app(raw)

    def test_bms_film_group_can_discover_formats_without_hardcoded_child_ids(self):
        raw = raw_app()
        raw["watches"][0]["providers"]["bookmyshow"].pop("event_codes")
        app = validate_app(raw)
        self.assertEqual(app["watches"][0]["providers"]["bookmyshow"]["event_group"], "EG00437704")

    def test_reordered_dates_do_not_reset_baseline(self):
        raw = raw_app()
        raw["watches"][0]["dates"] = ["2030-01-02", "2030-01-03"]
        first = validate_app(raw)["watches"][0]
        raw["watches"][0]["dates"].reverse()
        second = validate_app(raw)["watches"][0]
        self.assertEqual(generation(first), generation(second))

    def test_windows_utf8_bom_configuration_is_supported(self):
        from movie_monitor.config import load_app

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw_app()), encoding="utf-8-sig")
            self.assertEqual(len(load_app(str(path))["watches"]), 1)


class WatchStateTests(unittest.TestCase):
    def setUp(self):
        self.app = validate_app(raw_app())
        self.watch = self.app["watches"][0]
        self.key = generation(self.watch)
        self.state = State(":memory:")
        self.sender = Sender()

    def tearDown(self):
        self.state.close()

    def ingest(self, observation, at=AT):
        return self.state.ingest(self.key, self.watch, observation, self.app["theatre_aliases"], at)

    def send(self, at=AT, targets=("-100123",)):
        return deliver(self.state, self.sender, {self.key}, targets, self.app, at=at)

    def baseline_empty(self, providers=("district",)):
        for provider in providers:
            self.ingest(obs(provider, status="NO_SHOWS"))

    def test_all_existing_listings_silent_even_sold_out_then_reopened(self):
        for provider in ("district", "bookmyshow"):
            self.ingest(obs(provider, [show(provider, availability="UNKNOWN")]))
        later = AT + timedelta(minutes=1)
        self.ingest(obs(shows=[show()], at=later), later)
        self.assertEqual(self.send(later)["sent"], 0)
        self.assertEqual(self.state.status()["performances"], 1)
        self.assertEqual(self.state.status()["baselined_performances"], 1)

    def test_same_theatre_new_show_and_new_theatre_each_alert_once(self):
        self.ingest(obs(shows=[show()]))
        later = AT + timedelta(minutes=1)
        new = show(sid="2", clock="21:00")
        other = replace(show(sid="3"), cinema_id="999", cinema="Another Cinema, Chennai")
        self.ingest(obs(shows=[show(), new, other], at=later), later)
        self.assertEqual(self.send(later)["sent"], 2)
        self.assertEqual(self.send(later)["sent"], 0)
        self.assertEqual(len(self.sender.messages), 2)

    def test_cross_platform_and_two_minute_late_platform_are_deduplicated(self):
        self.baseline_empty(("district", "bookmyshow"))
        later = AT + timedelta(minutes=1)
        self.ingest(obs(shows=[show()], at=later), later)
        self.assertEqual(self.send(later)["sent"], 1)
        text = self.sender.messages[0][1]
        self.assertIn("Platforms: District", text)
        self.assertNotIn("Platforms: BookMyShow", text)
        later += timedelta(minutes=2)
        self.ingest(obs("bookmyshow", [show("bookmyshow")], at=later), later)
        self.assertEqual(self.send(later)["sent"], 0)
        self.assertEqual(self.state.status()["performances"], 1)

    def test_simultaneous_platforms_all_appear_in_one_message(self):
        self.baseline_empty(("district", "bookmyshow"))
        later = AT + timedelta(minutes=1)
        for provider in ("district", "bookmyshow"):
            self.ingest(obs(provider, [show(provider)], at=later), later)
        self.assertEqual(self.send(later)["sent"], 1)
        text = self.sender.messages[0][1]
        self.assertIn("Platforms: BookMyShow, District", text)
        self.assertIn("Wednesday, 02 January 2030", text)
        self.assertIn("06:00 PM IST", text)
        self.assertIn("Language: English", text)
        self.assertIn("Format: IMAX 3D", text)
        self.assertIn("First detected bookable:", text)

    def test_late_first_snapshot_is_silent_after_startup_failure(self):
        self.ingest(obs("bookmyshow", status="BLOCKED", complete=False))
        later = AT + timedelta(minutes=1)
        result = self.ingest(obs("bookmyshow", [show("bookmyshow")], at=later), later)
        self.assertTrue(result["baseline"])
        self.assertEqual(self.send(later)["sent"], 0)

    def test_late_initial_provider_does_not_cancel_known_new_performance(self):
        self.baseline_empty()
        self.ingest(obs("bookmyshow", status="BLOCKED", complete=False))
        later = AT + timedelta(minutes=1)
        self.ingest(obs(shows=[show()], at=later), later)
        self.ingest(obs("bookmyshow", [show("bookmyshow")], at=later), later)
        self.assertEqual(self.send(later)["sent"], 1)

    def test_newly_discovered_bms_theatre_after_initial_catalogue_alerts(self):
        self.ingest(obs("bookmyshow", status="WAITING", scope="CBMC"))
        self.ingest(obs("bookmyshow", status="NO_SHOWS", scope="discovery"))
        later = AT + timedelta(minutes=1)
        new = replace(show("bookmyshow"), cinema_id="NEW1", cinema="New Cinema, Chennai")
        result = self.ingest(obs("bookmyshow", [new], scope="NEW1", at=later), later)
        self.assertFalse(result["baseline"])
        self.assertEqual(self.send(later)["sent"], 1)

    def test_known_bms_theatre_blocked_at_startup_remains_silent_on_first_success(self):
        self.ingest(obs("bookmyshow", status="BLOCKED", complete=False, scope="CBMC"))
        self.ingest(obs("bookmyshow", status="NO_SHOWS", scope="discovery"))
        later = AT + timedelta(minutes=1)
        result = self.ingest(obs("bookmyshow", [show("bookmyshow")], scope="CBMC", at=later), later)
        self.assertTrue(result["baseline"])
        self.assertEqual(self.send(later)["sent"], 0)

    def test_new_bms_theatre_blocked_after_initial_discovery_can_alert_on_recovery(self):
        self.ingest(obs("bookmyshow", status="NO_SHOWS", scope="discovery"))
        self.ingest(obs("bookmyshow", status="BLOCKED", complete=False, scope="NEW1"))
        later = AT + timedelta(minutes=1)
        new = replace(show("bookmyshow"), cinema_id="NEW1", cinema="New Cinema, Chennai")
        result = self.ingest(obs("bookmyshow", [new], scope="NEW1", at=later), later)
        self.assertFalse(result["baseline"])
        self.assertEqual(self.send(later)["sent"], 1)

    def test_unknown_new_listing_waits_for_verified_availability(self):
        self.baseline_empty()
        later = AT + timedelta(minutes=1)
        self.ingest(obs(shows=[show(availability="UNKNOWN")], at=later), later)
        self.assertEqual(self.send(later)["sent"], 0)
        later += timedelta(minutes=1)
        self.ingest(obs(shows=[show()], at=later), later)
        self.assertEqual(self.send(later)["sent"], 1)

    def test_wrong_movie_date_city_id_url_timezone_and_status_are_rejected(self):
        self.baseline_empty()
        for change in (
            {"movie": "Dune"},
            {"movie_id": "123"},
            {"city": "mumbai"},
            {"starts_at": "2030-01-03T18:00:00+05:30"},
            {"starts_at": "2030-01-02T18:00:00"},
            {"booking_url": "https://evil.example/test"},
            {"availability": "SOMETHING"},
            {"language": ""},
            {"format": ""},
        ):
            with self.subTest(change=change):
                result = self.ingest(obs(shows=[replace(show(), **change)]))
                self.assertEqual(result["status"], "REJECTED")
                self.assertEqual(self.send()["sent"], 0)

    def test_wrong_language_format_time_and_theatre_do_not_match(self):
        self.watch.update(
            formats=["IMAX"], theatres=["9505"], time_windows={"*": [["18:00", "22:00"]]}
        )
        self.baseline_empty()
        for change in (
            {"language": "Tamil"},
            {"format": "2D"},
            {"cinema_id": "999", "cinema": "Other"},
            {"starts_at": f"{DAY}T17:59:00+05:30"},
        ):
            self.ingest(obs(shows=[replace(show(), **change)]))
        self.assertEqual(self.send()["sent"], 0)

    def test_preferred_theatre_alias_applies_across_platform_ids(self):
        self.watch["theatres"] = ["9505"]
        self.baseline_empty(("bookmyshow",))
        self.ingest(obs("bookmyshow", [show("bookmyshow")]))
        self.assertEqual(self.send()["sent"], 1)

    def test_timezone_offsets_do_not_split_identity_or_drop_fresh_offers(self):
        self.baseline_empty(("district", "bookmyshow"))
        district = replace(show(), starts_at=f"{DAY}T12:30:00+00:00")
        self.ingest(obs(shows=[district], at=AT.astimezone(__import__("datetime").timezone.utc)))
        self.ingest(obs("bookmyshow", [show("bookmyshow")]))
        self.assertEqual(self.send()["sent"], 1)
        self.assertEqual(self.state.status()["performances"], 1)
        self.assertIn("BookMyShow, District", self.sender.messages[0][1])

    def test_adding_verified_alias_keeps_sent_history_even_when_session_id_changes(self):
        self.app["theatre_aliases"] = []
        self.baseline_empty()
        self.ingest(obs(shows=[show()]))
        self.assertEqual(self.send()["sent"], 1)
        self.app["theatre_aliases"] = validate_app(raw_app())["theatre_aliases"]
        self.ingest(obs(shows=[replace(show(), session_id="NEWSESSION")]))
        self.assertEqual(self.send()["sent"], 0)
        self.assertEqual(self.state.status()["performances"], 1)

    def test_duplicate_name_with_distinct_same_provider_ids_is_quarantined(self):
        self.app["theatre_aliases"] = []
        self.ingest(obs(shows=[show()]))
        with self.assertRaises(ValueError):
            self.ingest(obs(shows=[replace(show(), cinema_id="OTHER")]))

    def test_identical_snapshot_history_is_bounded_and_retention_prunes(self):
        self.ingest(obs(shows=[show()]))
        later = AT + timedelta(minutes=1)
        self.ingest(obs(shows=[show()], at=later), later)
        self.assertEqual(
            self.state.db.execute("SELECT COUNT(*) FROM v2_observations").fetchone()[0], 1
        )
        self.state.prune(7, AT + timedelta(days=8))
        self.assertEqual(
            self.state.db.execute("SELECT COUNT(*) FROM v2_observations").fetchone()[0], 0
        )

    def test_replaced_sessions_do_not_grow_offer_snapshots_and_old_shows_expire(self):
        self.ingest(obs(shows=[show()]))
        self.ingest(obs(shows=[replace(show(), session_id="NEW")]))
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM v2_offers").fetchone()[0], 1)
        self.assertEqual(self.state.status()["performances"], 1)
        self.state.prune(7, datetime(2030, 7, 1, tzinfo=IST))
        self.assertEqual(self.state.status()["performances"], 0)

    def test_stale_failed_or_removed_provider_is_excluded_from_message(self):
        self.baseline_empty(("district", "bookmyshow"))
        later = AT + timedelta(minutes=1)
        self.ingest(obs(shows=[show()], at=later), later)
        self.ingest(obs("bookmyshow", [show("bookmyshow")], at=later), later)
        self.ingest(obs("bookmyshow", status="BLOCKED", at=later), later)
        self.send(later)
        self.assertNotIn("BookMyShow", self.sender.messages[0][1])

    def test_future_stale_and_naive_observation_times_do_not_initialize(self):
        for stamp in (
            AT - timedelta(minutes=4),
            AT + timedelta(minutes=1),
            AT.replace(tzinfo=None),
        ):
            result = self.ingest(obs(shows=[show()], at=stamp))
            self.assertFalse(result["complete"])
        self.assertEqual(self.state.status()["performances"], 0)

    def test_disappearance_sold_out_and_expired_offers_do_not_send(self):
        self.baseline_empty()
        self.ingest(obs(shows=[show()]))
        self.ingest(obs(status="NO_SHOWS"))
        self.assertEqual(self.send()["sent"], 0)
        self.ingest(obs(shows=[show(availability="SOLD_OUT")]))
        self.assertEqual(self.send()["sent"], 0)
        self.ingest(obs(shows=[show()]))
        self.assertEqual(self.send(AT + timedelta(minutes=4))["sent"], 0)

    def test_disabled_watch_does_not_deliver_and_each_group_has_own_dedup(self):
        self.baseline_empty()
        self.ingest(obs(shows=[show()]))
        self.assertEqual(
            deliver(self.state, self.sender, set(), ["-1"], self.app, at=AT)["sent"], 0
        )
        self.assertEqual(self.send(targets=("-1", "-2"))["sent"], 2)
        self.assertEqual(self.send(targets=("-1", "-2"))["sent"], 0)

    def test_aggregation_collects_platforms_before_sending(self):
        self.app["aggregation_seconds"] = 10
        self.baseline_empty(("district", "bookmyshow"))
        self.ingest(obs(shows=[show()]))
        self.assertEqual(self.send()["sent"], 0)
        later = AT + timedelta(seconds=11)
        self.ingest(obs("bookmyshow", [show("bookmyshow")], at=later), later)
        self.assertEqual(self.send(later)["sent"], 1)

    def test_new_group_receives_only_its_own_alert_without_replaying_other_groups(self):
        self.baseline_empty()
        self.ingest(obs(shows=[show()]))
        self.assertEqual(self.send(targets=("-1",))["sent"], 1)
        self.assertEqual(self.send(targets=("-1", "-2", "-2"))["sent"], 1)
        self.assertEqual([target for target, _ in self.sender.messages], ["-1", "-2"])
        self.assertEqual(self.send(targets=())["sent"], 0)
        self.assertEqual(self.send(targets=("-1", "-2"))["sent"], 0)

    def test_ambiguous_send_is_not_automatically_duplicated(self):
        self.baseline_empty()
        self.ingest(obs(shows=[show()]))
        sender = self.sender

        class Ambiguous:
            def send(self, target, text):
                sender.send(target, text)
                raise DeliveryError("Outcome unknown", ambiguous=True)

        self.assertEqual(
            deliver(self.state, Ambiguous(), {self.key}, ["-1"], self.app, at=AT)["unknown"], 1
        )
        self.assertEqual(
            deliver(self.state, Ambiguous(), {self.key}, ["-1"], self.app, at=AT)["unknown"], 0
        )
        self.assertEqual(len(self.sender.messages), 1)
        self.assertEqual(self.state.status()["delivery_states"]["UNKNOWN"], 1)

    def test_manual_stop_does_not_begin_another_telegram_send(self):
        import threading

        self.baseline_empty()
        self.ingest(obs(shows=[show()]))
        stop = threading.Event()
        stop.set()
        self.assertEqual(
            deliver(self.state, self.sender, {self.key}, ["-1"], self.app, at=AT, stop=stop)[
                "sent"
            ],
            0,
        )
        self.assertEqual(self.sender.messages, [])

    def test_telegram_http_format_retry_and_restart_dedup(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            telegram_server(
                [{"ok": False, "error_code": 429, "parameters": {"retry_after": 60}}]
            ) as (root, received),
        ):
            state = State(str(Path(directory) / "state.sqlite3"))
            state.ingest(
                self.key, self.watch, obs(status="NO_SHOWS"), self.app["theatre_aliases"], AT
            )
            state.ingest(self.key, self.watch, obs(shows=[show()]), self.app["theatre_aliases"], AT)
            client = Telegram("123:test", root)
            self.assertEqual(
                deliver(state, client, {self.key}, ["-1"], self.app, at=AT)["failed"], 1
            )
            self.assertEqual(deliver(state, client, {self.key}, ["-1"], self.app, at=AT)["sent"], 0)
            later = AT + timedelta(seconds=61)
            self.assertEqual(
                deliver(state, client, {self.key}, ["-1"], self.app, at=later)["sent"], 1
            )
            self.assertIn("First detected bookable", received[-1]["payload"]["text"])
            state.close()
            state = State(str(Path(directory) / "state.sqlite3"))
            self.assertEqual(
                deliver(state, client, {self.key}, ["-1"], self.app, at=later)["sent"], 0
            )
            self.assertEqual(len(received), 2)
            state.close()

    def test_crash_during_delivery_remains_unknown_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite3")
            state = State(path)
            state.ingest(self.key, self.watch, obs(status="NO_SHOWS"), (), AT)
            state.ingest(self.key, self.watch, obs(shows=[show()]), (), AT)
            state.prepare({self.key}, ["-1"], 180, 0, AT)
            delivery = state.due({self.key}, 180, AT)[0]
            self.assertTrue(state.claim(delivery["id"]))
            state.close()
            state = State(path)
            self.assertEqual(state.status()["delivery_states"]["UNKNOWN"], 1)
            self.assertEqual(state.due({self.key}, 180, AT), [])
            state.close()


class BmsCinemaParserTests(unittest.TestCase):
    def payload(self, **overrides):
        item = {
            "ShowDateTime": "203001021800",
            "ShowDateCode": "20300102",
            "SessionId": "1",
            "AvailStatus": "3",
            "SessionUnpaidFlag": "N",
            "CutOffFlag": "1",
            "Categories": [{"AvailStatus": "3", "SeatLayout": "Y"}],
        }
        item.update(overrides)
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
                                    "EventCode": "ET999999",
                                    "EventLanguage": "English",
                                    "EventDimension": "3D",
                                    "ShowTimes": [item],
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def parse(self, data):
        return parse_venue(data, watch(), {"VenueCode": "CBMC", "VenueName": "Cinepolis BSR"}, DAY)

    def test_discovers_new_child_id_and_only_recognized_available_flags(self):
        self.assertEqual(self.parse(self.payload())[0].movie_id, "ET999999")
        for raw in ("1", "2", "3"):
            self.assertEqual(self.parse(self.payload(AvailStatus=raw))[0].availability, "AVAILABLE")
        for change in ({"AvailStatus": "0"}, {"AvailStatus": "9"}, {"SessionUnpaidFlag": "Y"}):
            self.assertNotEqual(self.parse(self.payload(**change))[0].availability, "AVAILABLE")

    def test_rejects_wrong_group_conflicting_title_and_date_substitution(self):
        data = self.payload()
        data["ShowDetails"][0]["Event"][0]["EventTitle"] = "Dune"
        with self.assertRaises(ValueError):
            self.parse(data)
        data = self.payload()
        data["ShowDetails"][0]["Event"][0]["ChildEvents"][0]["EventGroup"] = "EG9999"
        with self.assertRaises(ValueError):
            self.parse(data)
        with self.assertRaises(ValueError):
            self.parse(self.payload(ShowDateTime="203001031800", ShowDateCode="20300103"))

    def test_unrelated_films_are_ignored_and_wrong_city_catalogue_rejected(self):
        data = self.payload()
        event = data["ShowDetails"][0]["Event"][0]
        event["EventTitle"] = "Dune"
        event["EventGroup"] = event["ChildEvents"][0]["EventGroup"] = "EG9999"
        self.assertEqual(self.parse(data), [])
        with self.assertRaises(ValueError):
            Catalogue.validate([{"RegionCode": "MUMBAI"}], "CHEN")

    def test_empty_or_changed_schema_is_not_a_valid_empty_baseline(self):
        for data in ({}, {"ShowDetails": []}, {"ShowDetails": [], "ShowDatesArray": []}):
            with self.assertRaises(ValueError):
                self.parse(data)

    def test_no_sellable_categories_or_cutoff_is_not_bookable(self):
        for changes in (
            {"Categories": []},
            {"Categories": [{"AvailStatus": "0", "SeatLayout": "Y"}]},
            {"CutOffFlag": "0"},
        ):
            self.assertNotEqual(self.parse(self.payload(**changes))[0].availability, "AVAILABLE")

    def test_verified_catalogue_is_preserved_when_refresh_is_blocked(self):
        from movie_monitor.http import FetchError, Response

        venue = {
            "VenueCode": "CBMC",
            "VenueName": "Cinepolis BSR",
            "RegionCode": "CHEN",
            "arrDates": [],
        }

        class Client:
            blocked = False

            def get(self, *args, **kwargs):
                if self.blocked:
                    raise FetchError("HTTP 403", 403)
                return Response(
                    200,
                    "<script>window.__INITIAL_STATE__ = "
                    + json.dumps({"venues": [venue]})
                    + "</script>",
                )

        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            with patch("movie_monitor.bms.now", return_value=AT):
                catalogue = Catalogue(directory)
                venues, fresh, error = catalogue.get("chennai", "CHEN", client)
                self.assertTrue(fresh)
            client.blocked = True
            with patch("movie_monitor.bms.now", return_value=AT + timedelta(minutes=2)):
                restarted = Catalogue(directory)
                venues, fresh, error = restarted.get("chennai", "CHEN", client)
                self.assertFalse(fresh)
                self.assertEqual(venues[0]["VenueCode"], "CBMC")
                self.assertIn("403", error)


class InventoryTests(unittest.TestCase):
    def test_positive_counts_need_consistent_sellable_categories(self):
        from movie_monitor.providers import inventory_status

        good = {"avail": 5, "statusColor": "R", "areas": [{"sAvail": 5, "statusColor": "R"}]}
        self.assertEqual(inventory_status(good), "AVAILABLE")
        for override in (
            {"disableClick": True},
            {"seatStatus": "Sold Out"},
            {"statusColor": "D"},
            {"areas": [{"sAvail": 0, "statusColor": "D"}]},
            {"areas": []},
            {"avail": 0},
        ):
            self.assertNotEqual(inventory_status({**good, **override}), "AVAILABLE")
        for invalid in (True, -1, float("inf"), float("nan"), "5"):
            with self.assertRaises(ValueError):
                inventory_status({**good, "avail": invalid})


class EngineTests(unittest.TestCase):
    def test_verified_scope_is_ingested_before_slow_provider_batch_finishes(self):
        import threading

        ingested = threading.Event()
        raw = raw_app()
        raw["watches"][0]["dates"] = ["2099-01-01", "2099-01-02"]
        raw["watches"][0]["providers"] = {"district": raw["watches"][0]["providers"]["district"]}

        class FakeCollector:
            def __init__(self, *args, **kwargs):
                pass

            def collect(self, provider, day):
                if day == "2099-01-02" and not ingested.wait(timeout=5):
                    raise RuntimeError("First snapshot was withheld until the batch finished")
                return Observation(provider, day, status="NO_SHOWS")

            def close(self):
                pass

        class SpyState(State):
            def ingest(self, *args, **kwargs):
                result = super().ingest(*args, **kwargs)
                ingested.set()
                return result

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("movie_monitor.engine.Collector", FakeCollector),
            patch("movie_monitor.engine.State", SpyState),
        ):
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw))
            report = run(str(path), str(Path(directory) / "state.sqlite3"))
        self.assertTrue(ingested.is_set())
        self.assertEqual(report["coverage"], "HEALTHY")

    def test_five_movies_run_independently_and_baseline_without_network(self):
        raw = raw_app()
        first = raw["watches"][0]
        first["providers"] = {"district": first["providers"]["district"]}
        raw["watches"] = [
            {**copy.deepcopy(first), "id": f"movie-{i}", "movie": f"Movie {i}"} for i in range(5)
        ]
        raw["watches"][0]["dates"] = ["2099-01-01"]
        for w in raw["watches"]:
            w["dates"] = ["2099-01-01"]
        calls = []

        class FakeCollector:
            def __init__(self, config, **kwargs):
                self.config = config

            def collect(self, provider, day):
                calls.append(self.config["id"])
                return Observation(provider, day, status="NO_SHOWS")

            def close(self):
                pass

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("movie_monitor.engine.Collector", FakeCollector),
        ):
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw))
            report = run(str(path), str(Path(directory) / "state.sqlite3"))
        self.assertEqual(set(calls), {f"movie-{i}" for i in range(5)})
        self.assertEqual(report["coverage"], "HEALTHY")
        self.assertTrue(all(p["scopes"][0]["baseline"] for p in report["providers"]))

    def test_blocked_provider_does_not_discard_other_provider_results(self):
        raw = raw_app()
        raw["watches"][0]["dates"] = ["2099-01-01"]

        class FakeCollector:
            def __init__(self, *args, **kwargs):
                self.client = object()

            def collect(self, provider, day):
                if provider == "district":
                    raise RuntimeError("Do not include untrusted exception details")
                return Observation(provider, day, status="NO_SHOWS")

            def close(self):
                pass

        class FakeCinema:
            def __init__(self, *args, **kwargs):
                from movie_monitor.schedule import Schedule

                self.active = Schedule()
                self.probes = Schedule()

            def collect_dates(self, days, emit=None):
                observations = [Observation("bookmyshow", day, status="NO_SHOWS") for day in days]
                for item in observations:
                    if emit:
                        emit(item)
                return observations

            def components(self):
                return {
                    key: {"state": "HEALTHY"}
                    for key in ("catalogue", "cinemas", "discovery_probes")
                }

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("movie_monitor.engine.Collector", FakeCollector),
            patch("movie_monitor.engine.CinemaCollector", FakeCinema),
        ):
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw))
            report = run(str(path), str(Path(directory) / "state.sqlite3"))
        statuses = {p["provider"]: p["state"] for p in report["providers"]}
        self.assertEqual(statuses, {"district": "DEGRADED", "bookmyshow": "HEALTHY"})

    def test_indefinite_expired_watches_reload_and_invalid_json_pauses_notifications(self):
        # A real child process exercises signals, file replacement and the loop.
        # All dates have expired, so no provider HTTP calls are possible.
        harness = """
import sys
import movie_monitor.engine as engine
engine.deliver = lambda *a, **kw: {'simulation_only': True}
engine.run(sys.argv[1], sys.argv[2], loop=True, sender=object(), targets=['-1'])
"""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            raw = raw_app()
            raw["watches"][0]["dates"] = ["2000-01-01"]
            path = folder / "config.json"
            path.write_text(json.dumps(raw))
            db = folder / "state.sqlite3"
            report_path = folder / "latest.json"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(
                [sys.executable, "-c", harness, str(path), str(db)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )

            def wait_for(predicate, timeout=12):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail("Monitor stopped despite indefinite mode")
                    try:
                        current = json.loads(report_path.read_text())
                        if predicate(current):
                            return current
                    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
                        pass
                    time.sleep(0.1)
                self.fail("Monitor did not observe the expected configuration state")

            try:
                report = wait_for(
                    lambda r: r["providers"] and r["providers"][0]["state"] == "IDLE_DATES_EXPIRED"
                )
                self.assertTrue(report["running"])
                path.write_text("{invalid json")
                report = wait_for(lambda r: bool(r["config_error"]))
                self.assertIsNone(report["delivery"])
                added = copy.deepcopy(raw["watches"][0])
                added.update(id="second-movie", movie="Another Movie")
                raw["watches"].append(added)
                path.write_text(json.dumps(raw))
                report = wait_for(
                    lambda r: r["config_error"] is None
                    and len(r["watches"]) == 2
                    and len(r["providers"]) == 4
                )
                self.assertTrue(report["delivery"]["simulation_only"])
                self.assertTrue(
                    all(p["state"] == "IDLE_DATES_EXPIRED" for p in report["providers"])
                )
            finally:
                process.terminate()
                process.wait(timeout=10)
            if os.name != "nt":
                self.assertFalse(json.loads(report_path.read_text())["running"])

    def test_sqlite_backup_preserves_dedup_state(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "state.sqlite3"
            state = State(str(source))
            cfg = watch()
            state.ingest(generation(cfg), cfg, obs(shows=[show()]), (), AT)
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/backup_state.py",
                    "--db",
                    str(source),
                    "--directory",
                    str(folder / "backups"),
                ],
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            backups = list((folder / "backups").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            backup = sqlite3.connect(backups[0])
            self.assertEqual(
                backup.execute("SELECT COUNT(*) FROM v2_performances WHERE baseline=1").fetchone()[
                    0
                ],
                1,
            )
            backup.close()
            state.close()


if __name__ == "__main__":
    unittest.main()
