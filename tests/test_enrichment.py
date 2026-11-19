"""Optional details cannot weaken stock guards or prevent a booking alert."""

import copy
import unittest
from dataclasses import replace
from datetime import UTC, timedelta
from unittest.mock import patch

from movie_monitor.config import generation, validate_app
from movie_monitor.core import Show
from movie_monitor.delivery import alert_text, deliver
from movie_monitor.enrichment import bms_details, district_details, price, sanitize
from movie_monitor.providers.district import parse_district
from movie_monitor.state import State
from tests import test_watches
from tests.test_providers import district_payload
from tests.test_watches import AT, DAY, Sender, obs, raw_app, show, watch


def session():
    return {
        "audi": "AUDI 03",
        "subtitle": "ENG",
        "total": 180,
        "avail": 11,
        "areas": [
            {
                "code": "0",
                "sType": "fixed",
                "label": "NORMAL",
                "sTotal": 17,
                "sAvail": 0,
                "price": 61.74,
                "statusColor": "D",
            },
            {
                "code": "2",
                "sType": "fixed",
                "label": "EXECUTIVE",
                "sTotal": 163,
                "sAvail": 11,
                "price": 210.98,
                "statusColor": "R",
            },
        ],
    }


def bms_item():
    return {
        "ScreenName": "SATHYAM",
        "SessionSubTitle": "English Subtitles",
        "SessionSubTitleAcronym": "ENG",
        "IsAtmosEnabled": "Y",
        "MinPrice": "121.74",
        "MaxPrice": "270.98",
        "Categories": [
            {
                "AreaCatCode": "1",
                "PriceDesc": "ELITE",
                "CurPrice": "270.98",
                "AvailStatus": "0",
                "SeatLayout": "Y",
                "BestAvailableSeats": "0",
            },
            {
                "AreaCatCode": "2",
                "PriceDesc": "BUDGET",
                "CurPrice": "121.74",
                "AvailStatus": "1",
                "SeatLayout": "Y",
                "BestAvailableSeats": "0",
            },
        ],
    }


def alert(offers):
    return {
        "movie": "Avengers: Doomsday",
        "theatre_name": "Cinepolis BSR",
        "city": "chennai",
        "starts_at": show().starts_at,
        "first_available": AT.isoformat(),
        "offers": offers,
    }


def offer(provider="district", details=None, at=AT):
    return {**show(provider).to_dict(), "details": details or {}, "observed_at": at.isoformat()}


class EnrichmentTests(unittest.TestCase):
    def test_district_exact_category_counts_and_prices(self):
        result = district_details(session())
        self.assertEqual((result["available"], result["total"]), (11, 180))
        self.assertEqual(result["categories"][0]["available"], 0)
        self.assertEqual(result["categories"][1]["price"], "210.98")
        self.assertEqual(result["subtitles"], "English")

    def test_inconsistent_totals_drop_counts_but_retain_prices_and_screen(self):
        for change in ({"total": 181}, {"avail": 12}, {"total": None}, {"avail": True}):
            result = district_details({**session(), **change})
            self.assertNotIn("total", result)
            self.assertNotIn("available", result["categories"][1])
            self.assertEqual(result["categories"][1]["price"], "210.98")
            self.assertEqual(result["auditorium"], "AUDI 03")

    def test_bad_category_counts_and_aliases_are_omitted(self):
        for change in (
            {"sAvail": True},
            {"sAvail": -1},
            {"sAvail": 164},
            {"sTotal": "163"},
            {"seatsAvail": 12},
            {"seatsTotal": 160},
            {"disableClick": True},
            {"isSoldOut": True},
            {"isBookingOpen": False},
            {"isEnabled": False},
            {"canBook": False},
            {"bookable": False},
            {"sType": None},
            {"sType": "free"},
            {"statusColor": "D"},
        ):
            raw = session()
            raw["areas"][1].update(change)
            result = district_details(raw)
            self.assertNotIn("total", result)
            self.assertNotIn("available", result["categories"][1])

    def test_duplicate_ids_and_invalid_labels_are_not_arbitrarily_selected(self):
        raw = session()
        raw["areas"].append(copy.deepcopy(raw["areas"][1]))
        result = district_details(raw)
        self.assertNotIn("total", result)
        self.assertEqual([c["name"] for c in result["categories"]], ["NORMAL"])
        raw = session()
        raw["areas"][1]["label"] = "EXECUTIVE\nFalse message"
        self.assertEqual(len(district_details(raw)["categories"]), 1)

    def test_missing_optional_data_does_not_change_bookability(self):
        data = district_payload()
        result = parse_district(data, watch(), "176735", DAY)
        self.assertEqual(result[0].availability, "AVAILABLE")
        self.assertNotIn("total", result[0].details)
        parser = test_watches.BmsCinemaParserTests()
        parsed = parser.parse(parser.payload())[0]
        self.assertEqual(parsed.availability, "AVAILABLE")
        self.assertNotIn("categories", parsed.details)

    def test_price_values_are_strict_and_independent_from_counts(self):
        for value in (None, True, -1, 0, "NaN", float("inf"), "121.741", "free", "1e2"):
            self.assertIsNone(price(value))
        raw = session()
        raw["areas"][1]["price"] = -1
        result = district_details(raw)
        self.assertEqual(result["available"], 11)
        self.assertNotIn("price", result["categories"][1])

    def test_bms_statuses_are_not_numeric_inventory(self):
        result = bms_details(bms_item(), {})
        self.assertEqual(result["categories"][0]["state"], "UNAVAILABLE")
        self.assertEqual(result["categories"][1]["state"], "AVAILABLE")
        self.assertNotIn("available", result)
        self.assertTrue(
            all("available" not in c and "total" not in c for c in result["categories"])
        )
        forged = district_details(session())
        self.assertNotIn("total", sanitize(forged, "bookmyshow"))

    def test_bms_conflicting_price_updates_and_bounds_are_omitted(self):
        raw = bms_item()
        raw["Categories"][0]["UpdatedPrice"] = "300.00"
        result = bms_details(raw, {})
        self.assertNotIn("price", result["categories"][0])
        self.assertNotIn("price_range", result)
        self.assertEqual(result["categories"][1]["price"], "121.74")
        self.assertNotIn("price_range", bms_details({**bms_item(), "MaxPrice": "200"}, {}))
        self.assertNotIn("price_range", bms_details({**bms_item(), "UpdatedMinPrice": "130"}, {}))

    def test_subtitles_and_audio_require_explicit_nonconflicting_evidence(self):
        raw = bms_item()
        self.assertEqual(bms_details(raw, {})["audio"], "Dolby Atmos")
        self.assertNotIn("audio", bms_details(raw, {"EventIsAtmosEnabled": "N"}))
        self.assertNotIn("audio", bms_details({**raw, "IsAtmosEnabled": "N"}, {}))
        self.assertNotIn("subtitles", bms_details({**raw, "SessionSubTitleAcronym": "TAM"}, {}))
        self.assertNotIn("subtitles", district_details({**session(), "subtitle": "UNKNOWN"}))

    def test_parser_errors_are_confined_to_optional_details(self):
        with patch("movie_monitor.enrichment._district", side_effect=TypeError):
            data = parse_district(district_payload(), watch(), "176735", DAY)
        self.assertEqual(data[0].availability, "AVAILABLE")
        self.assertEqual(data[0].details, {})
        with patch("movie_monitor.enrichment._bms", side_effect=ValueError):
            self.assertEqual(bms_details(bms_item(), {}), {})

    def test_show_roundtrip_preserves_identity_and_supports_old_payloads(self):
        original = show()
        rich = replace(original, details=district_details(session()))
        self.assertEqual(original.key, rich.key)
        self.assertEqual(Show.from_dict(rich.to_dict()), rich)
        legacy = original.to_dict()
        legacy.pop("details")
        self.assertEqual(Show.from_dict(legacy), original)


class AlertDetailsTests(unittest.TestCase):
    def test_provider_specific_snapshot_and_correct_ist_time(self):
        row = alert(
            [
                offer(details=district_details(session())),
                offer("bookmyshow", bms_details(bms_item(), {})),
            ]
        )
        text = alert_text(row, at=AT)
        self.assertIn("Seats: 11 available / 180 total", text)
        self.assertIn("EXECUTIVE: 11 available / 163 total | INR 210.98", text)
        self.assertIn("BUDGET: available | INR 121.74", text)
        self.assertIn("01 Dec 2029, 12:00:00 PM IST", text)
        self.assertIn("Auditorium: AUDI 03", text)
        self.assertIn("Subtitles: English", text)
        self.assertNotIn("total", text.split("BookMyShow details")[1].split("District details")[0])

    def test_partial_missing_and_stale_data_keep_the_booking_message(self):
        base = offer()
        expected = alert_text(alert([base]), at=AT)
        for value in (None, {}, "invalid", {"version": 99}, {"version": 1, "categories": [None]}):
            self.assertEqual(alert_text(alert([{**base, "details": value}]), at=AT), expected)
        for stamp in (
            (AT - timedelta(seconds=181)).isoformat(),
            "invalid",
            None,
            AT.replace(tzinfo=None).isoformat(),
            (AT + timedelta(minutes=1)).isoformat(),
        ):
            value = {**base, "observed_at": stamp, "details": district_details(session())}
            self.assertEqual(alert_text(alert([value]), at=AT), expected)

    def test_optional_rendering_exception_keeps_booking_links(self):
        row = alert([offer(details=district_details(session()))])
        with patch("movie_monitor.delivery.detail_lines", side_effect=RuntimeError):
            text = alert_text(row, at=AT)
        self.assertIn("New matching movie show", text)
        self.assertIn(row["offers"][0]["booking_url"], text)

    def test_long_details_do_not_overflow_telegram_or_drop_links(self):
        details = {
            "version": 1,
            "categories": [
                {
                    "id": str(i),
                    "name": "CATEGORY " + str(i) + " X" * 28,
                    "price": "100.00",
                    "state": "AVAILABLE",
                }
                for i in range(64)
            ],
        }
        row = alert([offer(details=details), offer("bookmyshow", details)])
        text = alert_text(row, at=AT)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 3900)
        self.assertIn("CATEGORY", text)
        for item in row["offers"]:
            self.assertIn(item["booking_url"], text)

    def test_excluded_and_unverified_fields_never_render(self):
        details = {"version": 1, "auditorium": "AUDI 03"}
        for key in (
            "address",
            "coordinates",
            "facilities",
            "runtime",
            "certificate",
            "genre",
            "synopsis",
            "poster",
            "trailer",
            "cast",
            "ticket_limit",
            "cutoff",
            "cancellation",
            "offers",
        ):
            details[key] = "MUST_NOT_APPEAR"
        text = alert_text(alert([offer(details=details)]), at=AT)
        self.assertIn("Auditorium: AUDI 03", text)
        self.assertNotIn("MUST_NOT_APPEAR", text)


class EnrichedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.app = validate_app(raw_app())
        self.watch = self.app["watches"][0]
        self.key = generation(self.watch)
        self.state = State(":memory:")
        self.sender = Sender()
        self.ingest(obs(status="NO_SHOWS"))

    def tearDown(self):
        self.state.close()

    def ingest(self, observation):
        return self.state.ingest(self.key, self.watch, observation, self.app["theatre_aliases"], AT)

    def send(self):
        return deliver(self.state, self.sender, {self.key}, ("-123",), self.app, at=AT)

    def test_detail_updates_and_later_platform_do_not_duplicate_alerts(self):
        self.ingest(obs(shows=[replace(show(), details=district_details(session()))]))
        self.assertEqual(self.send()["sent"], 1)
        self.assertIn("Seats: 11 available", self.sender.messages[0][1])
        changed = session()
        changed["avail"] = changed["areas"][1]["sAvail"] = 10
        self.ingest(obs(shows=[replace(show(), details=district_details(changed))]))
        self.ingest(obs("bookmyshow", [show("bookmyshow")]))
        self.assertEqual(self.send()["sent"], 0)
        self.assertEqual(len(self.sender.messages), 1)

    def test_multiple_rooms_at_same_time_suppress_ambiguous_enrichment(self):
        data = district_details(session())
        shows = [
            replace(show(sid="1"), screen="1", details=data),
            replace(show(sid="2"), screen="2", details=data),
        ]
        self.ingest(obs(shows=shows))
        self.assertEqual(self.send()["sent"], 1)
        self.assertNotIn("Seats:", self.sender.messages[0][1])
        self.assertIn("Booking links:", self.sender.messages[0][1])

    def test_optional_failure_does_not_prevent_delivery(self):
        self.ingest(obs(shows=[show()]))
        with patch("movie_monitor.delivery.detail_lines", side_effect=RuntimeError):
            self.assertEqual(self.send()["sent"], 1)
        self.assertEqual(self.state.status()["delivery_states"]["SENT"], 1)

    def test_fresh_offers_use_database_timestamp(self):
        self.ingest(obs(shows=[replace(show(), details=district_details(session()))]))
        self.state.prepare({self.key}, ("-123",), 180, 0, AT)
        row = self.state.due({self.key}, 180, AT)[0]
        snapshot = row["offers"][0]
        self.assertEqual(snapshot["observed_at"], AT.astimezone(UTC).isoformat())
        self.assertEqual(snapshot["details"]["available"], 11)
