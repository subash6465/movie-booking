from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

# India has no daylight-saving transitions. Avoid requiring Windows tzdata.
IST = timezone(timedelta(hours=5, minutes=30), "Asia/Kolkata")
UTC = UTC


def now() -> datetime:
    return datetime.now(UTC)


def resolve_date(value: str, today: date | None = None) -> str:
    today = today or now().astimezone(IST).date()
    if value == "today":
        return today.isoformat()
    if value == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if re.fullmatch(r"[+-]\d+d", value):
        return (today + timedelta(days=int(value[:-1]))).isoformat()
    return date.fromisoformat(value).isoformat()


def objects(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def required(value: dict, key: str, kind: type):
    result = value.get(key)
    if not isinstance(result, kind):
        raise ValueError(f"Schema changed: expected {key} to be {kind.__name__}")
    return result


def utc_session_time(value: str) -> datetime:
    """District currently return UTC wall time without a suffix."""
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def experience(value: str) -> str:
    value = value.upper()
    for label in ("IMAX", "EPIQ", "4DX", "SCREENX", "SCREEN X", "DOLBY CINEMA"):
        if label in value:
            return label.replace("SCREEN X", "SCREENX")
    # 2D/3D is evidence of a conventional projection; do not turn missing data
    # into a positive STANDARD match.
    return "STANDARD" if re.search(r"\b[23]D\b", value) else "UNKNOWN"


@dataclass(frozen=True)
class Show:
    provider: str
    movie: str
    movie_id: str
    city: str
    cinema_id: str
    cinema: str
    session_id: str
    starts_at: str
    language: str
    format: str
    availability: str  # AVAILABLE, SOLD_OUT, UNKNOWN
    booking_url: str
    screen: str = ""
    available_seats: int | None = None
    details: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict) -> Show:
        """Read old and new stored offers without requiring optional fields."""
        return cls(**{f.name: payload[f.name] for f in fields(cls) if f.name in payload})

    @property
    def local_start(self) -> datetime:
        return datetime.fromisoformat(self.starts_at).astimezone(IST)

    @property
    def key(self) -> str:
        parts = (self.provider, self.movie_id, self.cinema_id, self.session_id, self.starts_at)
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def to_dict(self) -> dict:
        return {**asdict(self), "key": self.key, "local_start": self.local_start.isoformat()}


@dataclass
class Observation:
    provider: str
    requested_date: str
    status: str = "OK"  # OK, NO_SHOWS, BLOCKED, ERROR
    shows: list[Show] = field(default_factory=list)
    observed_dates: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    http_status: int | None = None
    duration_ms: int = 0
    requests: int = 0
    observed_at: str = field(default_factory=lambda: now().isoformat())
    scope_id: str = "city"
    complete: bool = True

    def to_dict(self) -> dict:
        return {**asdict(self), "shows": [s.to_dict() for s in self.shows]}


def clock_minutes(value: str) -> int:
    if not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError(f"Invalid HH:MM time: {value}")
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid HH:MM time: {value}")
    return hour * 60 + minute


def validate_config(config: dict, *, cinema_mode: bool = False) -> dict:
    for key in ("movie", "city"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"config.{key} must be a non-empty string")
    if not re.fullmatch(r"[a-z-]+", config["city"]):
        raise ValueError("city must be a provider city slug, e.g. chennai")
    dates = required(config, "dates", list)
    if not dates or len(dates) > 14:
        raise ValueError("Configure between 1 and 14 dates")
    for value in dates:
        resolve_date(value)
    providers = required(config, "providers", dict)
    if not providers or set(providers) - {"district", "bookmyshow"}:
        raise ValueError("Configure district and/or bookmyshow")
    for name, settings in providers.items():
        if not isinstance(settings, dict):
            raise ValueError(f"Invalid settings for {name}")
        ids = (
            settings.get("event_codes", [])
            if name == "bookmyshow"
            else [settings.get("content_id", "")]
        )
        allow_empty = name == "bookmyshow" and cinema_mode
        if (
            not isinstance(ids, list)
            or (not ids and not allow_empty)
            or any(
                not re.fullmatch(r"ET\d+" if name == "bookmyshow" else r"\d+", str(x)) for x in ids
            )
        ):
            raise ValueError(f"Invalid {name} movie identifiers")
        if "slug" in settings and not re.fullmatch(r"[a-z0-9-]+", settings["slug"]):
            raise ValueError(f"Invalid {name} movie slug")
    for key in ("languages", "formats", "theatres"):
        if not isinstance(config.get(key, []), list) or any(
            not isinstance(x, str) for x in config.get(key, [])
        ):
            raise ValueError(f"{key} must be a list of strings")
    windows = config.get("time_windows", {"*": [["00:00", "23:59"]]})
    if not isinstance(windows, dict):
        raise ValueError("time_windows must be a date-to-windows object")
    for key, ranges in windows.items():
        if key != "*":
            resolve_date(key)
        if not isinstance(ranges, list) or not ranges:
            raise ValueError("Each date needs at least one time window")
        for pair in ranges:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("Time windows must be [start, end] pairs")
            if clock_minutes(pair[0]) > clock_minutes(pair[1]):
                raise ValueError("Split overnight windows across two calendar dates")
    if not isinstance(config.get("poll_seconds", 60), int) or config.get("poll_seconds", 60) < 60:
        raise ValueError("poll_seconds must be at least 60")
    return config


def matches(
    show: Show, config: dict, at: datetime | None = None, *, require_available: bool = True
) -> bool:
    if (require_available and show.availability != "AVAILABLE") or datetime.fromisoformat(
        show.starts_at
    ) <= (at or now()):
        return False
    local = show.local_start
    if local.date().isoformat() not in {resolve_date(x) for x in config["dates"]}:
        return False
    if config.get("languages") and show.language.casefold() not in {
        x.casefold() for x in config["languages"]
    }:
        return False
    if (
        config.get("theatres")
        and show.cinema_id not in config["theatres"]
        and show.cinema.casefold() not in {x.casefold() for x in config["theatres"]}
    ):
        return False
    for_selected_formats = config.get("formats", [])
    if for_selected_formats:
        labels = {show.format.upper(), experience(show.format)}
        labels.update(re.findall(r"\b[23]D\b", show.format.upper()))
        if not any(x.upper().replace("NORMAL", "STANDARD") in labels for x in for_selected_formats):
            return False
    windows = {
        resolve_date(k) if k != "*" else k: v for k, v in config.get("time_windows", {}).items()
    }
    ranges = windows.get(local.date().isoformat(), windows.get("*", [["00:00", "23:59"]]))
    minute = local.hour * 60 + local.minute
    return any(clock_minutes(start) <= minute <= clock_minutes(end) for start, end in ranges)


def finalize_observation(obs: Observation, shows: list[Show]) -> Observation:
    obs.observed_dates = sorted({s.local_start.date().isoformat() for s in shows})
    correct = {s.key: s for s in shows if s.local_start.date().isoformat() == obs.requested_date}
    skipped = len(shows) - sum(
        s.local_start.date().isoformat() == obs.requested_date for s in shows
    )
    if skipped:
        obs.warnings.append(
            f"Discarded {skipped} shows for other dates (provider date substitution)"
        )
        if not correct:
            obs.complete = False  # Cannot establish a startup baseline from the wrong date.
    obs.shows = sorted(correct.values(), key=lambda s: (s.starts_at, s.cinema, s.session_id))
    obs.status = "OK" if obs.shows else "NO_SHOWS"
    return obs
