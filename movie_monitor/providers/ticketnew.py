"""TicketNew showtime payload mapping; no network or database access."""

import urllib.parse

from ..core import IST, Show, required, utc_session_time
from .common import inventory_status, same_title


def parse_ticketnew(data: dict, config: dict, content_id: str) -> list[Show]:
    meta = required(data, "meta", dict)
    movies = required(meta, "movies", list)
    movie_map = {str(m["id"]): m for m in movies}
    if not movies or any(str(m.get("contentId")) != content_id for m in movies):
        raise ValueError("TicketNew movie identity mismatch")
    cinemas = {str(c["id"]): c for c in required(meta, "cinemas", list)}
    sessions = required(required(data, "pageData", dict), "sessions", dict)
    shows = []
    for cid, items in sessions.items():
        if not isinstance(items, list) or str(cid) not in cinemas:
            raise ValueError("TicketNew sessions reference an unknown cinema")
        cinema = cinemas[str(cid)]
        cinema_name = cinema.get("name") or cinema.get("cinemaInfo", {}).get("name")
        if not cinema_name:
            raise ValueError("Missing TicketNew cinema name")
        for session in items:
            movie = movie_map.get(str(session.get("mid")))
            if not movie or not same_title(movie.get("name", ""), config["movie"]):
                raise ValueError("TicketNew session movie mismatch")
            if (
                session.get("lang")
                and movie.get("lang")
                and session["lang"].casefold() != movie["lang"].casefold()
            ):
                raise ValueError("TicketNew session language conflicts with movie metadata")
            start = utc_session_time(session["showTime"])
            avail = session.get("avail")
            status = inventory_status(
                session, cinema.get("cinemaDisabled") or cinema.get("cinemaDegraded")
            )
            # Use the provider's movie page, not an invented reservation/session URL.
            url = (
                f"https://ticketnew.com/movies/{config['providers']['ticketnew'].get('slug', 'movie')}-movie-detail-{content_id}?"
                + urllib.parse.urlencode(
                    {
                        "frmtid": session.get("fid", ""),
                        "fromdate": start.astimezone(IST).date().isoformat(),
                    }
                )
            )
            shows.append(
                Show(
                    "ticketnew",
                    config["movie"],
                    content_id,
                    config["city"],
                    str(cid),
                    cinema_name,
                    str(session["sid"]),
                    start.isoformat(),
                    session.get("lang") or movie.get("lang", ""),
                    session.get("scrnFmt") or movie.get("scrnFmt", ""),
                    status,
                    url,
                    session.get("audi", ""),
                    avail,
                )
            )
    return shows
