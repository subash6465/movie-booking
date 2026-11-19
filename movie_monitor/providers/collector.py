"""HTTP discovery and collection for District."""

from __future__ import annotations

import time
from dataclasses import replace

from ..core import Observation, finalize_observation, required
from ..http import FetchError, HttpClient, guest_headers
from .common import CatalogWaiting, next_data, same_title
from .district import parse_district


class Collector:
    def __init__(self, config: dict, client: HttpClient | None = None):
        self.config = config
        self.client = client or HttpClient()
        self.discovery: dict[str, dict] = {}

    def _discover(self, provider: str) -> dict:
        if (
            provider in self.discovery
            and time.monotonic() - self.discovery[provider]["cached_at"] < 900
        ):
            return self.discovery[provider]
        cfg = self.config
        settings = cfg["providers"][provider]
        cid = str(settings["content_id"])
        if provider != "district":
            raise ValueError("Unsupported collector provider; use CinemaCollector for BookMyShow")
        slug = settings.get("slug", "movie")
        url = f"https://www.district.in/movies/{slug}-movie-tickets-in-{cfg['city']}-MV{cid}"
        payload = next_data(self.client.get(url).text)["props"]["pageProps"]["data"]
        if payload.get("isGeneric"):
            movie_data = payload.get("movieData", {})
            movie = movie_data.get("meta", {}).get("movie", {})
            if (
                str(movie.get("content_id")) == cid
                and same_title(movie.get("name", ""), cfg["movie"])
                and not movie_data.get("meta", {}).get("isReleased")
            ):
                raise CatalogWaiting(
                    "Movie exists; District has not published a city-specific session catalogue"
                )
        if payload.get("cityName") != cfg["city"]:
            raise ValueError("District discovery selected another city")
        movie = payload["serverState"]["movieContents"][cid]
        if not same_title(movie.get("name", ""), cfg["movie"]):
            raise ValueError("District content ID does not match configured movie")
        groups = required(movie, "languageFormatGroups", list)
        variants = [
            (g.get("language") or g.get("lang", ""), g.get("format") or g.get("fmtGrpId"))
            for g in groups
        ]
        coordinates = {
            "chennai": (13.0827, 80.2707),
            "bangalore": (12.9716, 77.5946),
            "bengaluru": (12.9716, 77.5946),
            "mumbai": (19.0760, 72.8777),
        }.get(cfg["city"])
        coordinates = cfg.get("coordinates", coordinates)
        if not coordinates or len(coordinates) != 2:
            raise ValueError("Set coordinates: [latitude, longitude] for this city")
        languages = {x.casefold() for x in cfg.get("languages", [])}
        variants = sorted(
            set(
                (lang, str(code))
                for lang, code in variants
                if code and (not languages or lang.casefold() in languages)
            )
        )
        if not variants:
            raise CatalogWaiting(
                f"No published {provider} variants satisfy the configured language preference"
            )
        result = {"variants": variants, "coordinates": coordinates, "cached_at": time.monotonic()}
        self.discovery[provider] = result
        return result

    def collect(self, provider: str, day: str) -> Observation:
        obs = Observation(provider, day)
        before = self.client.count
        started = time.monotonic()
        try:
            if provider != "district":
                raise ValueError("Use CinemaCollector for BookMyShow")
            settings = self.config["providers"][provider]
            shows = []
            discovery = self._discover(provider)
            cid = str(settings["content_id"])
            for language, code in discovery["variants"]:
                headers = guest_headers(provider)
                lat, lng = discovery["coordinates"]
                params = {
                    "version": "3",
                    "site_id": "1",
                    "child_site_id": "1",
                    "channel": "mweb",
                    "platform": "district",
                    "movieCode": code,
                    "city_key": self.config["city"],
                    "content_id": cid,
                    "date": day,
                    "latitude": lat,
                    "longitude": lng,
                    "cinemaOrderLogic": "3",
                }
                response = self.client.get(
                    "https://www.district.in/gw/consumer/movies/v5/movie", params, headers
                )
                obs.http_status = response.status
                if response.status == 204:
                    continue
                data = response.json()
                selected_date = data.get("meta", {}).get("selectedShowDate")
                if selected_date != day:
                    obs.warnings.append(
                        f"District selected {selected_date} instead of {day}; checking every actual show date"
                    )
                items = parse_district(data, self.config, cid, day)
                if any(s.language and s.language.casefold() != language.casefold() for s in items):
                    raise ValueError(
                        "District returned a language different from the requested variant"
                    )
                for cinema in data["pageData"]["nearbyCinemas"] + data["pageData"]["farCinemas"]:
                    if any(
                        s.get("mcd") and s["mcd"].casefold() != code.casefold()
                        for s in cinema["sessions"]
                    ):
                        raise ValueError("District returned a different language/format variant")
                # The selected variant is explicit even when the metadata lists several languages.
                shows.extend(replace(s, language=language) for s in items)
            finalize_observation(obs, shows)
        except CatalogWaiting as exc:
            obs.status = "WAITING"
            obs.warnings.append(str(exc))
        except Exception as exc:
            obs.status = (
                "BLOCKED"
                if isinstance(exc, FetchError) and exc.status in (401, 403, 429)
                else "ERROR"
            )
            obs.http_status = exc.status if isinstance(exc, FetchError) else obs.http_status
            obs.error = (
                str(exc)
                if isinstance(exc, (ValueError, FetchError, KeyError, TypeError))
                else f"{type(exc).__name__}: collector failed"
            )
            obs.shows = []  # Never leak partial successful results as complete coverage.
            if provider in self.discovery:
                del self.discovery[provider]  # Re-discover identifiers after a failure.
        obs.requests = self.client.count - before
        obs.duration_ms = round((time.monotonic() - started) * 1000)
        return obs

    def close(self):
        self.client.close()
