"""District showtime payload mapping; no network or database access."""

import urllib.parse

from ..core import Show, required, utc_session_time
from .common import inventory_status, same_title


def parse_district(data: dict, config: dict, content_id: str, day: str) -> list[Show]:
    meta = required(data, "meta", dict)
    movie = required(meta, "movie", dict)
    if str(movie.get("content_id")) != content_id or not same_title(
        movie.get("name", ""), config["movie"]
    ):
        raise ValueError("District movie identity mismatch")
    page = required(data, "pageData", dict)
    cinemas = required(page, "nearbyCinemas", list) + required(page, "farCinemas", list)
    language = meta.get("movieLanguage") or (
        movie.get("languages", [""])[0] if len(movie.get("languages", [])) == 1 else ""
    )
    shows = []
    for cinema in cinemas:
        info = required(cinema, "cinemaInfo", dict)
        for session in required(cinema, "sessions", list):
            start = utc_session_time(session["showTime"])
            avail = session.get("avail")
            status = inventory_status(
                session, cinema.get("cinemaDisabled") or cinema.get("cinemaDegraded")
            )
            enc = session.get("encSessionId")
            if not enc:
                enc = (
                    f"{cinema['id']}-{session['sid']}-{str(session['mid']).lower()}-{cinema['id']}"
                )
            query = urllib.parse.urlencode(
                {
                    "encsessionid": enc,
                    "fromdate": day,
                    "freeseating": "false",
                    "fromsessions": "true",
                    "type": "MOVIES",
                    "contentid": content_id,
                }
            )
            url = f"https://www.district.in/movies/seat-layout/{session.get('mcd', '')}?{query}"
            shows.append(
                Show(
                    "district",
                    config["movie"],
                    content_id,
                    config["city"],
                    str(cinema["id"]),
                    info["name"],
                    str(session["sid"]),
                    start.isoformat(),
                    session.get("lang") or language,
                    session.get("scrnFmt", ""),
                    status,
                    url,
                    session.get("audi", ""),
                    avail,
                )
            )
    return shows
