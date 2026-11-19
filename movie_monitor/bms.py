"""Cinema-first BookMyShow collection with cached, city-validated discovery."""

from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .core import IST, Observation, Show, finalize_observation, now, objects, required
from .enrichment import bms_details
from .http import FetchError, HttpClient
from .providers import bms_state, same_title
from .schedule import Schedule


class Catalogue:
    def __init__(self, directory: str, refresh_seconds=60, max_backoff=900):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.refresh_seconds = refresh_seconds
        self.lock = threading.RLock()
        self.memory, self.errors, self.futures, self.clients = {}, {}, {}, {}
        self.schedule = Schedule(refresh_seconds, max_backoff)
        self.pool = None
        self.closing = False
        self.hints = {}
        self.loaded_hints = set()
        self.api_cache = OrderedDict()
        self.api_bytes = 0
        self.api_locks = {}

    @staticmethod
    def validate(venues, region):
        if not isinstance(venues, list) or not venues:
            raise ValueError("Empty or changed BookMyShow cinema catalogue")
        result, seen = [], set()
        for venue in venues:
            if not isinstance(venue, dict) or venue.get("RegionCode") != region:
                raise ValueError("BookMyShow cinema catalogue city/schema mismatch")
            code = venue.get("VenueCode", "")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]+", code) or code in seen:
                raise ValueError("Invalid or duplicate BookMyShow venue code")
            if not isinstance(venue.get("VenueName"), str) or not venue["VenueName"].strip():
                raise ValueError("Missing BookMyShow theatre name")
            for day in required(venue, "arrDates", list):
                datetime.strptime(day["ShowDateCode"], "%Y%m%d")
            seen.add(code)
            result.append(
                {
                    k: venue.get(k)
                    for k in (
                        "VenueCode",
                        "VenueName",
                        "RegionCode",
                        "arrDates",
                        "isDown",
                        "CinemaUnpaidFlag",
                        "CinemaIsOnlineNoTransactionMsg",
                    )
                }
            )
        return result

    def _load(self, city, region):
        path = self.directory / f"bms-{city}.json"
        if city not in self.memory and path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                age = (now() - datetime.fromisoformat(cached["saved_at"])).total_seconds()
                if 0 <= age <= 7 * 86400:
                    cached["venues"] = self.validate(cached["venues"], region)
                    self.memory[city] = cached
            except (ValueError, KeyError, TypeError, OSError):
                pass
        if city not in self.loaded_hints:
            self.loaded_hints.add(city)
            try:
                hints = json.loads((self.directory / f"bms-hints-{city}.json").read_text())
                for code, dates in hints.items():
                    if not re.fullmatch(r"[A-Z0-9]+", code):
                        raise ValueError("Invalid hint venue")
                    for day in dates:
                        datetime.strptime(day, "%Y%m%d")
                self.hints[city] = {code: set(dates) for code, dates in hints.items()}
            except (ValueError, KeyError, TypeError, OSError):
                self.hints[city] = {}

    def snapshot(self, city, region):
        with self.lock:
            self._load(city, region)
            cached = self.memory.get(city)
            if not cached:
                return [], False, self.errors.get(city, "Cinema catalogue refresh is pending")
            age = (now() - datetime.fromisoformat(cached["saved_at"])).total_seconds()
            fresh = 0 <= age <= self.refresh_seconds and not self.errors.get(city)
            future = self.futures.get(city)
            if (
                not fresh
                and not self.errors.get(city)
                and future
                and not future.done()
                and 0 <= age <= self.refresh_seconds + 30
            ):
                return cached["venues"], False, "Catalogue refresh in progress"
            return (
                cached["venues"],
                fresh,
                None if fresh else self.errors.get(city, "Cinema catalogue is stale"),
            )

    def version(self, city):
        with self.lock:
            return self.memory.get(city, {}).get("saved_at")

    def refresh(self, city, region, client):
        # Network I/O never holds the catalogue lock: cached cinema checks continue.
        try:
            state = bms_state(
                client.get(f"https://in.bookmyshow.com/{city}/cinemas", browser_http=True).text
            )
            venues = self.validate(
                [n for n in objects(state) if isinstance(n.get("arrDates"), list)], region
            )
            cached = {"saved_at": now().isoformat(), "venues": venues}
            with self.lock:
                path = self.directory / f"bms-{city}.json"
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(cached), encoding="utf-8")
                temporary.replace(path)
                self.memory[city] = cached
                self.errors.pop(city, None)
                self.schedule.interval = self.refresh_seconds
                self.schedule.success(city)
        except Exception as exc:
            error = (
                str(exc)
                if isinstance(exc, (FetchError, ValueError, KeyError))
                else type(exc).__name__
            )
            with self.lock:
                self.errors[city] = error
                self.schedule.failure(city, error)

    def ensure_refresh(self, city, region):
        with self.lock:
            self._load(city, region)
            future = self.futures.get(city)
            if self.closing or (future and not future.done()) or not self.schedule.due(city):
                return
            if self.pool is None:
                self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bms-catalogue")
            if city not in self.clients:
                self.clients[city] = HttpClient()
            client = self.clients[city]
            self.futures[city] = self.pool.submit(self.refresh, city, region, client)

    def get(self, city, region, client):
        # Synchronous compatibility entry point for diagnostics only.
        venues, fresh, error = self.snapshot(city, region)
        if not fresh:
            self.refresh(city, region, client)
            venues, fresh, error = self.snapshot(city, region)
        if not venues:
            raise FetchError(
                "BookMyShow cinema discovery unavailable; no verified cached catalogue"
            )
        return venues, fresh, error

    def known_dates(self, city, venue):
        with self.lock:
            hints = set(self.hints.get(city, {}).get(venue["VenueCode"], set()))
        return hints | {d["ShowDateCode"] for d in venue["arrDates"]}

    def fetch_venue(self, city, region, venue, day_code, client):
        key = (city, venue, day_code)
        with self.lock:
            gate = self.api_locks.setdefault(key, threading.Lock())
        with gate:
            with self.lock:
                cached = self.api_cache.get(key)
                if cached and time.monotonic() - cached["received"] < 15:
                    self.api_cache.move_to_end(key)
                    return cached["data"], cached["stamp"]
            response = client.get(
                "https://in.bookmyshow.com/api/v3/mobile/showtimes/byvenue",
                {"dateCode": day_code, "venueCode": venue, "regionCode": region},
                browser_http=True,
            )
            data, stamp = response.json(), now().isoformat()
            required(data, "ShowDetails", list)
            dates = required(data, "ShowDatesArray", list)
            hints = set()
            for date in dates:
                code = date.get("DateCode", "")
                datetime.strptime(code, "%Y%m%d")
                if date.get("isDisabled") is False:
                    hints.add(code)
            size = len(response.text.encode("utf-8"))
            with self.lock:
                old = self.api_cache.pop(key, None)
                self.api_bytes -= old["size"] if old else 0
                self.api_cache[key] = {
                    "data": data,
                    "stamp": stamp,
                    "received": time.monotonic(),
                    "size": size,
                }
                self.api_bytes += size
                while self.api_bytes > 16_000_000 and self.api_cache:
                    _, evicted = self.api_cache.popitem(last=False)
                    self.api_bytes -= evicted["size"]
                self.hints.setdefault(city, {}).setdefault(venue, set()).update(hints)
                path = self.directory / f"bms-hints-{city}.json"
                temporary = path.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps({v: sorted(d) for v, d in self.hints[city].items()})
                )
                temporary.replace(path)
            return data, stamp

    def close(self):
        with self.lock:
            self.closing = True
        if self.pool:
            self.pool.shutdown(wait=True, cancel_futures=True)
        for client in self.clients.values():
            client.close()


def parse_venue(data, config, venue, day):
    """Ignore other movies; reject conflicting identity and date fields."""
    details = required(data, "ShowDetails", list)
    if not details:
        dates = required(data, "ShowDatesArray", list)
        if not any(
            d.get("DateCode") == day.replace("-", "") and d.get("isDisabled") is False
            for d in dates
        ):
            raise ValueError("BookMyShow returned no verifiable requested-date snapshot")
    expected_group = config["providers"]["bookmyshow"]["event_group"]
    shows = []
    actual_dates = set()
    for detail in details:
        for event in required(detail, "Event", list):
            title = event.get("EventTitle", "")
            for child in required(event, "ChildEvents", list):
                group = child.get("EventGroup")
                parent_group = event.get("EventGroup")
                if group != expected_group:
                    if same_title(title, config["movie"]):
                        raise ValueError("BookMyShow title matches but film group does not")
                    continue
                if not same_title(title, config["movie"]) or (
                    parent_group and parent_group != group
                ):
                    raise ValueError("BookMyShow film group/title identity conflict")
                code = child.get("EventCode", "")
                language, fmt = child.get("EventLanguage", ""), child.get("EventDimension", "")
                if not re.fullmatch(r"ET\d+", code) or not language or not fmt:
                    raise ValueError("Missing BookMyShow child event identity/language/format")
                for item in required(child, "ShowTimes", list):
                    start = datetime.strptime(item["ShowDateTime"], "%Y%m%d%H%M").replace(
                        tzinfo=IST
                    )
                    actual = start.strftime("%Y%m%d")
                    if actual != item.get("ShowDateCode") or actual != detail.get("Date"):
                        raise ValueError("BookMyShow show date fields disagree")
                    actual_dates.add(start.date().isoformat())
                    if start.date().isoformat() != day:
                        continue
                    raw = str(item.get("AvailStatus", ""))
                    categories = required(item, "Categories", list)
                    category_available = any(
                        str(c.get("AvailStatus")) in ("1", "2", "3") and c.get("SeatLayout") == "Y"
                        for c in categories
                    )
                    blocked = (
                        venue.get("isDown")
                        or venue.get("CinemaUnpaidFlag") == "Y"
                        or venue.get("CinemaIsOnlineNoTransactionMsg")
                        or item.get("disabled")
                        or item.get("isDisabled")
                    )
                    availability = (
                        "AVAILABLE"
                        if raw in ("1", "2", "3")
                        and category_available
                        and item.get("SessionUnpaidFlag") == "N"
                        and str(item.get("CutOffFlag")) == "1"
                        and not blocked
                        else "UNKNOWN"
                    )
                    if raw == "0":
                        availability = "UNAVAILABLE"
                    url = f"https://in.bookmyshow.com/movies/{config['city']}/{config['providers']['bookmyshow'].get('slug', 'movie')}/buytickets/{code}/{day.replace('-', '')}"
                    shows.append(
                        Show(
                            "bookmyshow",
                            config["movie"],
                            code,
                            config["city"],
                            venue["VenueCode"],
                            venue["VenueName"],
                            str(item["SessionId"]),
                            start.isoformat(),
                            language,
                            " | ".join(x for x in (fmt, item.get("Attributes", "")) if x),
                            availability,
                            url,
                            item.get("ScreenName", ""),
                            details=bms_details(item, child),
                        )
                    )
    # Validate response dates even when none of its movies match this watch.
    all_dates = {datetime.strptime(d["Date"], "%Y%m%d").date().isoformat() for d in details}
    if details and day not in all_dates:
        raise ValueError(f"BookMyShow substituted another show date; expected {day}")
    return shows


class CinemaCollector:
    def __init__(
        self,
        config,
        client,
        catalogue,
        background_checks=2,
        stop=None,
        probe_interval=5,
        maximum_backoff=900,
    ):
        self.config, self.client, self.catalogue = config, client, catalogue
        self.background_checks, self.probe_interval = background_checks, probe_interval
        self.rotation, self.background_due = 0, 0
        self.stop = stop or threading.Event()
        self.active = Schedule(config.get("poll_seconds", 60), maximum_backoff)
        self.probes = Schedule(60, maximum_backoff)
        self.catalogue_fresh, self.catalogue_error = False, "Initial catalogue refresh pending"
        self.seen_version = None
        self.active_keys, self.probe_keys = set(), set()
        self.last_data_at = None
        self.known_cinemas = 0

    def components(self):
        def report(schedule, keys):
            failed = schedule.failures(keys)
            return {
                "state": "DEGRADED" if failed else "HEALTHY",
                "failed_scopes": len(failed),
                "errors": [v.error for v in list(failed.values())[:3]],
                "next_retry_seconds": min(
                    (max(0, round(v.next_at - schedule.clock())) for v in failed.values()),
                    default=0,
                ),
            }

        return {
            "catalogue": {
                "state": "HEALTHY"
                if self.catalogue_fresh
                else "REFRESHING"
                if self.catalogue_error == "Catalogue refresh in progress"
                else "DEGRADED",
                "error": self.catalogue_error,
            },
            "cinemas": report(self.active, self.active_keys),
            "discovery_probes": report(self.probes, self.probe_keys),
            "known_cinemas": self.known_cinemas,
            "last_cinema_data_at": self.last_data_at,
            "active_poll_seconds": self.active.interval,
            "background_interval_seconds": 60 if self.catalogue_fresh else self.probe_interval,
        }

    def collect_dates(self, days, emit=None):
        city = self.config["city"]
        region = self.config["providers"]["bookmyshow"]["region_code"]
        self.catalogue.ensure_refresh(city, region)
        venues, fresh, error = self.catalogue.snapshot(city, region)
        self.catalogue_fresh, self.catalogue_error = fresh, error
        self.known_cinemas = len(venues)
        observations = []

        def publish(item):
            observations.append(item)
            if emit:
                emit(item)

        if not venues:
            for day in days:
                publish(
                    Observation(
                        "bookmyshow",
                        day,
                        status="ERROR",
                        scope_id="discovery",
                        complete=False,
                        error=error,
                    )
                )
            return observations
        if not fresh:
            for day in days:
                publish(
                    Observation(
                        "bookmyshow",
                        day,
                        status="STALE_DISCOVERY",
                        scope_id="discovery",
                        complete=False,
                        error=error,
                    )
                )
        version = self.catalogue.version(city)
        new_snapshot = fresh and version != self.seen_version
        candidates = [
            v
            for v in venues
            if any(d.replace("-", "") not in self.catalogue.known_dates(city, v) for d in days)
        ]
        self.probe_keys = {v["VenueCode"] for v in candidates}
        # Establish the same silent, catalogue-based negative baseline as before.
        # Reused/scheduled scopes are not re-ingested or given artificial freshness.
        if new_snapshot:
            for venue in venues:
                for day in days:
                    if day.replace("-", "") not in self.catalogue.known_dates(city, venue):
                        publish(
                            Observation(
                                "bookmyshow",
                                day,
                                status="WAITING",
                                scope_id=venue["VenueCode"],
                                warnings=["Date absent from verified city catalogue"],
                            )
                        )
        # A single cinema probe returns all its published date hints. During a
        # city-catalogue failure this scans known cinemas independently of dates.
        if time.monotonic() >= self.background_due:
            if candidates:
                selected = 0
                for step in range(len(candidates)):
                    venue = candidates[(self.rotation + step) % len(candidates)]
                    if selected >= self.background_checks or self.stop.is_set():
                        break
                    code = venue["VenueCode"]
                    if not self.probes.due(code):
                        continue
                    selected += 1
                    try:
                        self.catalogue.fetch_venue(
                            city,
                            region,
                            code,
                            now().astimezone(IST).strftime("%Y%m%d"),
                            self.client,
                        )
                        self.probes.success(code)
                    except Exception as exc:
                        self.probes.failure(
                            code,
                            str(exc)
                            if isinstance(exc, (FetchError, ValueError, KeyError))
                            else type(exc).__name__,
                        )
                self.rotation = (self.rotation + max(1, selected)) % len(candidates)
            self.background_due = time.monotonic() + (60 if fresh else self.probe_interval)
        # Each active cinema/date has its own success interval and failure clock.
        # Discovery failures and failures at other cinemas do not alter this clock.
        self.active_keys = {
            (v["VenueCode"], day)
            for v in venues
            for day in days
            if day.replace("-", "") in self.catalogue.known_dates(city, v)
        }
        for venue in venues:
            for day in days:
                if self.stop.is_set():
                    return observations
                key = (venue["VenueCode"], day)
                if key not in self.active_keys or not self.active.due(key):
                    continue
                obs = Observation("bookmyshow", day, scope_id=venue["VenueCode"])
                before, started = self.client.count, time.monotonic()
                try:
                    payload, stamp = self.catalogue.fetch_venue(
                        city, region, venue["VenueCode"], day.replace("-", ""), self.client
                    )
                    obs.http_status, obs.observed_at = 200, stamp
                    finalize_observation(obs, parse_venue(payload, self.config, venue, day))
                    self.active.success(key)
                    self.last_data_at = stamp
                except Exception as exc:
                    obs.status = (
                        "BLOCKED"
                        if isinstance(exc, FetchError) and exc.status in (401, 403, 429)
                        else "ERROR"
                    )
                    obs.complete = False
                    obs.error = (
                        str(exc)
                        if isinstance(exc, (ValueError, KeyError, FetchError))
                        else type(exc).__name__
                    )
                    self.active.failure(key, obs.error)
                obs.requests = self.client.count - before
                obs.duration_ms = round((time.monotonic() - started) * 1000)
                publish(obs)
        if new_snapshot:
            for day in days:
                publish(Observation("bookmyshow", day, status="NO_SHOWS", scope_id="discovery"))
            self.seen_version = version
        return observations
