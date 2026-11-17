"""Contract checks through the supported District/TicketNew collection path."""

import json
import time
import unittest

from movie_monitor.http import FetchError, Response
from movie_monitor.providers import Collector
from tests.test_watches import DAY, watch


class Client:
    count = 0

    def __init__(self, result):
        self.result = result

    def get(self, *args, **kwargs):
        self.count += 1
        if isinstance(self.result, Exception):
            raise self.result
        return Response(200, json.dumps(self.result))


def collector(provider, payload):
    result = Collector(watch(), client=Client(payload))
    result.discovery[provider] = {
        "variants": [("English", "english")],
        "coordinates": (13.0827, 80.2707),
        "cached_at": time.monotonic(),
    }
    return result


def district_payload(day=DAY):
    return {
        "meta": {
            "movie": {"content_id": "176735", "name": "Avengers: Doomsday"},
            "movieLanguage": "English",
            "selectedShowDate": day,
        },
        "pageData": {
            "nearbyCinemas": [
                {
                    "id": 9505,
                    "cinemaInfo": {"name": "Cinepolis BSR"},
                    "sessions": [
                        {
                            "sid": "1",
                            "mid": "1",
                            "mcd": "english",
                            "showTime": day + "T12:30:00",
                            "lang": "English",
                            "scrnFmt": "3D",
                            "avail": 5,
                            "areas": [{"sAvail": 5}],
                        }
                    ],
                }
            ],
            "farCinemas": [],
        },
    }


class ProviderContractTests(unittest.TestCase):
    def test_provider_denial_is_not_an_empty_catalogue(self):
        for provider in ("district", "ticketnew"):
            for status in (403, 429):
                with self.subTest(provider=provider, status=status):
                    result = collector(provider, FetchError(f"HTTP {status}", status)).collect(
                        provider, DAY
                    )
                    self.assertEqual(result.status, "BLOCKED")
                    self.assertEqual(result.shows, [])
                    self.assertEqual(result.http_status, status)

    def test_district_positive_stock_and_actual_date_are_preserved(self):
        result = collector("district", district_payload()).collect("district", DAY)
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.shows[0].availability, "AVAILABLE")
        self.assertEqual(result.shows[0].language, "English")
        self.assertEqual(result.shows[0].local_start.strftime("%H:%M"), "18:00")
        substituted = collector("district", district_payload("2030-01-03")).collect("district", DAY)
        self.assertEqual(substituted.shows, [])
        self.assertFalse(substituted.complete)

    def test_ticketnew_incomplete_pagination_never_leaks_partial_shows(self):
        payload = {
            "data": {"cinemasOrder": ["9505", "9999"], "pageSize": 1},
            "pageData": {"sessions": {"9505": []}},
            "meta": {"cinemas": []},
        }
        result = collector("ticketnew", payload).collect("ticketnew", DAY)
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(result.shows, [])
        self.assertIn("incomplete cinema/session coverage", result.error)
