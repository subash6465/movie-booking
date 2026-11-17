"""Shared public-page decoding and strict movie/inventory validation."""

from __future__ import annotations

import json
import math
import re
from html.parser import HTMLParser

from ..core import now, utc_session_time


class CatalogWaiting(ValueError):
    """Validated movie exists, but its city-specific session catalogue is unpublished."""


class JsonScripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.capture = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.capture = dict(attrs).get("id") == "__NEXT_DATA__"

    def handle_endtag(self, tag):
        if tag == "script":
            self.capture = False

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)


def next_data(html: str) -> dict:
    parser = JsonScripts()
    parser.feed(html)
    if not parser.parts:
        raise ValueError("Missing __NEXT_DATA__: blocked page or changed schema")
    return json.loads("".join(parser.parts))


def same_title(actual: str, expected: str) -> bool:
    def clean(value):
        return re.sub(r"[^\w]", "", value.casefold())

    return clean(actual) == clean(expected)


def bms_state(html: str) -> dict:
    match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html)
    if not match:
        raise ValueError("Missing BMS application state: blocked page or changed schema")
    return json.JSONDecoder().raw_decode(html[match.end() :])[0]


def inventory_status(session: dict, cinema_disabled=False):
    """Require consistent show and area stock; never promote a contradictory flag."""
    avail = session.get("avail")

    def count(value):
        if value is not None and (
            type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100000
        ):
            raise ValueError("Invalid provider seat availability count")
        return value

    count(avail)
    if cinema_disabled or session.get("disableClick") or session.get("isSoldOut"):
        return "UNAVAILABLE"
    if any(
        session.get(flag) is False for flag in ("bookable", "isEnabled", "isBookingOpen", "canBook")
    ):
        return "UNAVAILABLE"
    label = re.sub(r"[^a-z]", "", str(session.get("seatStatus", "")).casefold())
    if label in ("soldout", "housefull", "closed", "unavailable", "bookingclosed"):
        return "UNAVAILABLE"
    if session.get("statusColor") and session["statusColor"] not in ("G", "Y", "R"):
        return "UNKNOWN"
    if session.get("closeTime") and utc_session_time(session["closeTime"]) <= now():
        return "UNAVAILABLE"
    areas = session.get("areas")
    if avail is None or not isinstance(areas, list) or not areas:
        return "UNKNOWN"
    area_available = False
    for area in areas:
        value = count(
            area.get("sAvail") if area.get("sAvail") is not None else area.get("seatsAvail")
        )
        if value is not None and value > 0 and area.get("statusColor") in (None, "G", "Y", "R"):
            area_available = True
    if avail == 0:
        return "UNKNOWN" if area_available else "SOLD_OUT"
    return "AVAILABLE" if area_available else "UNKNOWN"
