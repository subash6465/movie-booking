import unittest
from dataclasses import replace
from datetime import date, datetime

from movie_monitor.core import (
    IST,
    UTC,
    Show,
    matches,
    resolve_date,
    utc_session_time,
    validate_config,
)
from movie_monitor.notify import DeliveryError, Telegram
from movie_monitor.providers import parse_district
from tests.support import telegram_server


def config():
    return {
        "movie": "Test Movie",
        "city": "chennai",
        "dates": ["2030-01-02"],
        "languages": ["Tamil"],
        "formats": ["IMAX"],
        "theatres": ["C1"],
        "time_windows": {"*": [["18:00", "22:00"]]},
        "providers": {"bookmyshow": {"event_codes": ["ET123456"], "slug": "test-movie"}},
    }


def sample():
    return Show(
        "bookmyshow",
        "Test Movie",
        "ET123456",
        "chennai",
        "C1",
        "Test Cinema",
        "S1",
        "2030-01-02T18:00:00+05:30",
        "Tamil",
        "IMAX 2D",
        "AVAILABLE",
        "https://in.bookmyshow.com/test",
    )


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2029, 12, 1, tzinfo=UTC)

    def test_all_preferences_and_inclusive_edges(self):
        self.assertTrue(matches(sample(), config(), self.at))
        self.assertTrue(
            matches(replace(sample(), starts_at="2030-01-02T22:00:00+05:30"), config(), self.at)
        )
        self.assertFalse(
            matches(replace(sample(), starts_at="2030-01-02T22:01:00+05:30"), config(), self.at)
        )
        for change in (
            {"language": "Hindi"},
            {"format": "2D"},
            {"cinema_id": "OTHER", "cinema": "Other"},
            {"availability": "UNKNOWN"},
            {"availability": "SOLD_OUT"},
            {"starts_at": "2030-01-03T18:00:00+05:30"},
        ):
            with self.subTest(change=change):
                self.assertFalse(matches(replace(sample(), **change), config(), self.at))

    def test_past_shows_never_match(self):
        self.assertFalse(matches(sample(), config(), datetime(2030, 2, 1, tzinfo=UTC)))

    def test_unknown_format_is_not_normal_and_projection_is_separate(self):
        cfg = {**config(), "formats": ["NORMAL"]}
        self.assertFalse(matches(replace(sample(), format=""), cfg, self.at))
        self.assertTrue(matches(replace(sample(), format="2D | 4K DOLBY ATMOS"), cfg, self.at))
        self.assertFalse(matches(sample(), cfg, self.at))
        self.assertTrue(matches(sample(), {**cfg, "formats": ["2D"]}, self.at))

    def test_per_date_windows_and_exact_theatre_name(self):
        cfg = config()
        cfg["theatres"] = ["test cinema"]
        cfg["time_windows"]["2030-01-02"] = [["10:00", "12:00"]]
        self.assertFalse(matches(sample(), cfg, self.at))
        cfg["time_windows"]["2030-01-02"] = [["17:00", "19:00"]]
        self.assertTrue(matches(sample(), cfg, self.at))

    def test_timezone_and_calendar_boundary(self):
        self.assertEqual(
            utc_session_time("2030-01-02T14:00").astimezone(IST).strftime("%H:%M"), "19:30"
        )
        self.assertEqual(
            utc_session_time("2030-01-01T20:00").astimezone(IST).date(), date(2030, 1, 2)
        )
        self.assertEqual(resolve_date("+2d", date(2026, 12, 31)), "2027-01-02")

    def test_validation_rejects_invalid_input(self):
        cfg = config()
        validate_config(cfg)
        for change in (
            {"dates": ["2030-02-31"]},
            {"time_windows": {"*": [["22:00", "06:00"]]}},
            {"poll_seconds": 1},
            {"city": "../../wrong"},
            {"languages": "Tamil"},
        ):
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                validate_config({**cfg, **change})


class ParserTests(unittest.TestCase):
    def test_bad_identity_is_rejected(self):
        payload = {
            "meta": {"movie": {"content_id": 999, "name": "Different Movie"}},
            "pageData": {},
        }
        with self.assertRaises(ValueError):
            parse_district(payload, config(), "123", "2030-01-02")


class TelegramTransportTests(unittest.TestCase):
    def test_telegram_error_does_not_expose_token(self):
        with telegram_server([{"ok": False, "error_code": 403}]) as (root, _):
            with self.assertRaises(DeliveryError) as error:
                Telegram("123:secret", root).send("123", "test")
            self.assertTrue(error.exception.permanent)
            self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
