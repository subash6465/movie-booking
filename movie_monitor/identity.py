from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlsplit

from .config import normalized
from .core import IST, Show


def theatre_name_key(name, city):
    tokens = normalized(name).split()
    suffix = normalized(city).split()
    if suffix and tokens[-len(suffix) :] == suffix:
        tokens = tokens[: -len(suffix)]
    return " ".join(tokens)


def explicit_theatre(show, aliases):
    for alias in aliases:
        if alias["city"] != show.city:
            continue
        ids = [str(x) for x in alias.get("providers", {}).get(show.provider, [])]
        names = [alias["name"], *alias.get("names", [])]
        if show.cinema_id in ids or theatre_name_key(show.cinema, show.city) in {
            theatre_name_key(name, show.city) for name in names
        }:
            return alias["id"], alias["name"]
    return None


def preference_matches(show, watch, aliases, at):
    from .core import matches

    if matches(show, watch, at, require_available=False):
        return True
    alias_match = explicit_theatre(show, aliases)
    if not alias_match or not watch.get("theatres"):
        return False
    alias = next(a for a in aliases if a["city"] == show.city and a["id"] == alias_match[0])
    choices = {alias["id"], alias["name"], *alias.get("names", [])}
    choices.update(str(venue) for ids in alias.get("providers", {}).values() for venue in ids)
    if not {normalized(x) for x in watch["theatres"]} & {normalized(x) for x in choices}:
        return False
    return matches(show, {**watch, "theatres": []}, at, require_available=False)


def performance_key(show, theatre):
    # Deliberately exclude provider, session ID, language, format and screen:
    # the requested unit is one movie/theatre/date/start-time performance.
    return performance_key_values(show.movie, show.city, theatre, show.starts_at)


def performance_key_values(movie, city, theatre, starts_at):
    parts = [
        normalized(movie),
        city,
        theatre,
        datetime.fromisoformat(starts_at).astimezone(IST).isoformat(timespec="minutes"),
    ]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def validate_show(show: Show, watch: dict, requested_date: str):
    if (
        show.provider not in watch["providers"]
        or normalized(show.movie) != normalized(watch["movie"])
        or show.city != watch["city"]
    ):
        raise ValueError("Show movie, provider or city does not match the watch")
    if not all(
        isinstance(x, str) and x.strip()
        for x in (
            show.movie_id,
            show.cinema_id,
            show.session_id,
            show.cinema,
            show.language,
            show.format,
        )
    ):
        raise ValueError("Missing show identity, language, theatre or format")
    if not normalized(show.cinema) or any(
        not re.fullmatch(r"[A-Za-z0-9_-]+", x)
        for x in (show.movie_id, show.cinema_id, show.session_id)
    ):
        raise ValueError("Malformed provider show or theatre identity")
    settings = watch["providers"][show.provider]
    if show.provider != "bookmyshow" and str(settings["content_id"]) != show.movie_id:
        raise ValueError("Show content ID does not match the watch")
    # BookMyShow child IDs are discovered and checked against event_group by
    # the cinema parser. Requiring them to appear in a hard-coded list misses formats.
    start = datetime.fromisoformat(show.starts_at)
    if (
        start.tzinfo is None
        or show.local_start.date().isoformat() != requested_date
        or start.second
        or start.microsecond
    ):
        raise ValueError("Missing timezone, wrong date or invalid show-start precision")
    if show.availability not in ("AVAILABLE", "SOLD_OUT", "UNAVAILABLE", "UNKNOWN"):
        raise ValueError("Unrecognized availability")
    url = urlsplit(show.booking_url)
    allowed = {
        "district": {"www.district.in", "district.in"},
        "ticketnew": {"ticketnew.com", "www.ticketnew.com"},
        "bookmyshow": {"in.bookmyshow.com"},
    }
    if (
        url.scheme != "https"
        or url.netloc not in allowed[show.provider]
        or url.username
        or url.password
    ):
        raise ValueError("Untrusted booking link")
    if show.movie_id not in show.booking_url:
        raise ValueError("Booking link does not identify the verified movie")
    for value, limit in (
        (show.movie, 160),
        (show.cinema, 200),
        (show.language, 60),
        (show.format, 180),
        (show.booking_url, 1500),
    ):
        if len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid provider text")
