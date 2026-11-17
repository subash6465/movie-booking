"""Versioned watch configuration. Provider IDs are explicit identity guards."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path

from .core import validate_config


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.findall(r"[^\W_]+", value, re.UNICODE))


def generation(watch: dict) -> str:
    # Scheduling and display ID changes don't create a new startup baseline.
    preferences = {k: v for k, v in watch.items() if k not in ("poll_seconds", "id", "enabled")}
    preferences = copy.deepcopy(preferences)
    if "bookmyshow" in preferences.get("providers", {}):
        preferences["providers"]["bookmyshow"].pop("event_codes", None)
    return hashlib.sha256(json.dumps(preferences, sort_keys=True).encode()).hexdigest()


def validate_app(raw: dict) -> dict:
    raw = copy.deepcopy(raw)
    if not isinstance(raw, dict):
        raise ValueError("The configuration must be a JSON object")
    if "watches" not in raw:
        raw = {
            "version": 2,
            "poll_seconds": raw.get("poll_seconds", 60),
            "watches": [{**raw, "id": "movie-1"}],
        }
    if raw.get("version", 2) != 2:
        raise ValueError("Unsupported configuration version")
    watches = raw.get("watches")
    if not isinstance(watches, list) or not 1 <= len(watches) <= 5:
        raise ValueError("Configure between one and five movies in watches")
    defaults = {
        "poll_seconds": 60,
        "aggregation_seconds": 10,
        "freshness_seconds": 180,
        "max_backoff_seconds": 900,
        "catalogue_refresh_seconds": 60,
        "bms_background_checks": 2,
        "observation_retention_days": 7,
        "bms_probe_interval_seconds": 5,
    }
    if set(raw) - set(defaults) - {"version", "watches", "theatre_aliases"}:
        raise ValueError(
            "Unknown top-level configuration fields: "
            + ", ".join(
                sorted(set(raw) - set(defaults) - {"version", "watches", "theatre_aliases"})
            )
        )
    bounds = {
        "poll_seconds": (60, 3600),
        "aggregation_seconds": (0, 60),
        "freshness_seconds": (30, 600),
        "max_backoff_seconds": (60, 3600),
        "catalogue_refresh_seconds": (60, 600),
        "bms_background_checks": (1, 20),
        "observation_retention_days": (1, 90),
        "bms_probe_interval_seconds": (5, 120),
    }
    for key, default in defaults.items():
        value = raw.setdefault(key, default)
        if type(value) is not int or not bounds[key][0] <= value <= bounds[key][1]:
            raise ValueError(f"{key} must be an integer from {bounds[key][0]} to {bounds[key][1]}")
    ids = set()
    for watch in watches:
        if not isinstance(watch, dict):
            raise ValueError("Every watch must be an object")
        allowed_watch = {
            "id",
            "enabled",
            "movie",
            "city",
            "dates",
            "date_range",
            "time_windows",
            "languages",
            "formats",
            "theatres",
            "providers",
            "poll_seconds",
            "coordinates",
        }
        if set(watch) - allowed_watch:
            raise ValueError(
                "Unknown watch fields: " + ", ".join(sorted(set(watch) - allowed_watch))
            )
        for field in ("movie", "city"):
            if isinstance(watch.get(field), str):
                watch[field] = watch[field].strip()
        if isinstance(watch.get("city"), str):
            watch["city"] = watch["city"].lower()
        for field in ("languages", "formats", "theatres"):
            values = watch.setdefault(field, [])
            if not isinstance(values, list) or any(
                not isinstance(v, str) or not v.strip() for v in values
            ):
                raise ValueError(f"{field} must contain non-empty strings (or [] for any)")
            watch[field] = sorted(set(v.strip() for v in values), key=str.casefold)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", watch.get("id", "")) or watch["id"] in ids:
            raise ValueError("Each movie needs a unique lowercase id (letters, digits and hyphens)")
        ids.add(watch["id"])
        if type(watch.get("enabled", True)) is not bool:
            raise ValueError("enabled must be true or false")
        if "date_range" in watch:
            if "dates" in watch:
                raise ValueError("Use dates or date_range, not both")
            interval = watch.pop("date_range")
            if not isinstance(interval, dict) or set(interval) != {"from", "to"}:
                raise ValueError("date_range requires exactly from and to")
            start, end = date.fromisoformat(interval["from"]), date.fromisoformat(interval["to"])
            if not 0 <= (end - start).days < 14:
                raise ValueError("date_range must contain 1–14 dates, inclusive")
            watch["dates"] = [
                (start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)
            ]
        watch.setdefault("poll_seconds", raw["poll_seconds"])
        if type(watch["poll_seconds"]) is not int or not 60 <= watch["poll_seconds"] <= 3600:
            raise ValueError("Watch poll_seconds must be an integer from 60 to 3600")
        if not isinstance(watch.get("providers"), dict):
            raise ValueError("providers must be an object keyed by provider name")
        for provider, settings in watch["providers"].items():
            allowed = (
                {"slug", "event_codes", "event_group", "region_code"}
                if provider == "bookmyshow"
                else {"slug", "content_id"}
            )
            if not isinstance(settings, dict) or set(settings) - allowed:
                raise ValueError(f"Unknown or invalid {provider} settings")
        validate_config(watch, cinema_mode=True)
        watch["dates"] = sorted(set(watch["dates"]))
        for field, limit in (("movie", 160), ("city", 80)):
            if len(watch[field]) > limit or any(ord(c) < 32 for c in watch[field]):
                raise ValueError(f"Invalid {field} text")
        bms = watch["providers"].get("bookmyshow")
        if bms:
            if not re.fullmatch(r"EG\d+", bms.get("event_group", "")):
                raise ValueError(
                    f"{watch['id']}: BookMyShow requires the verified film event_group (EG...)"
                )
            region = bms.get("region_code") or {
                "chennai": "CHEN",
                "bengaluru": "BANG",
                "bangalore": "BANG",
                "mumbai": "MUMBAI",
            }.get(watch["city"])
            if not region or not re.fullmatch(r"[A-Z0-9]+", region):
                raise ValueError("Set BookMyShow region_code for this city")
            bms["region_code"] = region
    aliases = raw.setdefault("theatre_aliases", [])
    if not isinstance(aliases, list):
        raise ValueError("theatre_aliases must be a list")
    used, alias_ids = set(), set()
    for alias in aliases:
        if not isinstance(alias, dict) or not re.fullmatch(r"[a-z0-9-]+", alias.get("id", "")):
            raise ValueError("Invalid theatre alias id")
        if set(alias) - {"id", "city", "name", "providers", "names"}:
            raise ValueError("Unknown theatre alias fields")
        if (
            not isinstance(alias.get("providers", {}), dict)
            or not isinstance(alias.get("names", []), list)
            or any(not isinstance(n, str) or not n.strip() for n in alias.get("names", []))
        ):
            raise ValueError("Invalid theatre alias providers or names")
        city = alias.get("city", "")
        if (
            not re.fullmatch(r"[a-z-]+", city)
            or (city, alias["id"]) in alias_ids
            or not alias.get("name")
        ):
            raise ValueError("Theatre aliases need unique city/id and a display name")
        alias_ids.add((city, alias["id"]))
        if len(alias["name"]) > 200 or any(ord(c) < 32 for c in alias["name"]):
            raise ValueError("Invalid theatre alias name")
        for provider, venue_ids in alias.get("providers", {}).items():
            if provider not in ("district", "ticketnew", "bookmyshow") or not isinstance(
                venue_ids, list
            ):
                raise ValueError("Invalid alias providers")
            for venue_id in venue_ids:
                token = (city, provider, str(venue_id))
                if token in used:
                    raise ValueError("A provider theatre ID cannot belong to multiple aliases")
                used.add(token)
        for name in alias.get("names", []):
            token = (city, "name", normalized(name))
            if token in used:
                raise ValueError("A theatre name cannot belong to multiple aliases")
            used.add(token)
    return raw


def load_app(path: str) -> dict:
    return validate_app(json.loads(Path(path).read_text(encoding="utf-8-sig")))
