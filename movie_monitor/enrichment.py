"""Optional provider details; invalid enrichment never establishes bookability."""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .core import IST

logger = logging.getLogger(__name__)
_last_warning: dict[tuple[str, str], float] = {}
_warning_lock = threading.Lock()
MAX_CATEGORIES = 64
_SUBTITLES = {
    "en": "English",
    "eng": "English",
    "english": "English",
    "ta": "Tamil",
    "tam": "Tamil",
    "tamil": "Tamil",
    "hi": "Hindi",
    "hin": "Hindi",
    "hindi": "Hindi",
    "te": "Telugu",
    "tel": "Telugu",
    "telugu": "Telugu",
    "ml": "Malayalam",
    "mal": "Malayalam",
    "malayalam": "Malayalam",
    "kn": "Kannada",
    "kan": "Kannada",
    "kannada": "Kannada",
}


def omitted(provider: str, reason: str) -> None:
    """Bound repeated omission logs; never log raw provider values or URLs."""
    key, tick = (provider, reason), time.monotonic()
    with _warning_lock:
        if tick - _last_warning.get(key, -1000) < 300:
            return
        if len(_last_warning) >= 64:
            del _last_warning[min(_last_warning, key=_last_warning.get)]
        _last_warning[key] = tick
    logger.warning("Optional booking details omitted: provider=%s reason=%s", provider, reason)


def label(value, limit=80) -> str | None:
    if not isinstance(value, str) or any(unicodedata.category(c).startswith("C") for c in value):
        return None
    value = value.strip()
    if (
        not value
        or len(value) > limit
        or value.casefold() in {"unknown", "n/a", "none", "null", "tba", "tbd", "-"}
    ):
        return None
    return value


def count(value) -> int | None:
    return value if type(value) is int and 0 <= value <= 100000 else None


def price(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        text = str(value)
        if not re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
            return None
        result = Decimal(text)
        return format(result, ".2f") if result.is_finite() and 0 < result <= 1000000 else None
    except (InvalidOperation, ValueError):
        return None


def subtitle(*values) -> str | None:
    found = set()
    for value in values:
        if value in (None, ""):
            continue
        text = label(value, 60)
        if not text:
            return None
        language = _SUBTITLES.get(re.sub(r"\s+subtitles?$", "", text.casefold()))
        if not language:
            return None
        found.add(language)
    return found.pop() if len(found) == 1 else None


def _category_id(value) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) else None


def sanitize(details, provider: str) -> dict:
    """Whitelist stored detail fields again; support older offers with no details."""
    if not isinstance(details, dict) or details.get("version") != 1:
        return {}
    result = {"version": 1}
    for key in ("auditorium", "subtitles", "audio"):
        value = label(details.get(key), 80)
        if value:
            result[key] = value
    raw = details.get("categories", [])
    categories = []
    identifiers = (
        Counter(
            item.get("id")
            for item in raw
            if isinstance(item, dict) and _category_id(item.get("id"))
        )
        if isinstance(raw, list) and len(raw) <= MAX_CATEGORIES
        else {}
    )
    complete = isinstance(raw, list) and 0 < len(raw) <= MAX_CATEGORIES
    if isinstance(raw, list) and len(raw) <= MAX_CATEGORIES:
        for item in raw:
            if not isinstance(item, dict):
                complete = False
                continue
            identity, name = _category_id(item.get("id")), label(item.get("name"))
            if not identity or not name or identifiers[identity] != 1:
                complete = False
                continue
            entry = {"id": identity, "name": name}
            if item.get("state") in ("AVAILABLE", "UNAVAILABLE"):
                entry["state"] = item["state"]
            amount = price(item.get("price"))
            if amount:
                entry["price"] = amount
            total, available = count(item.get("total")), count(item.get("available"))
            if (
                provider == "district"
                and total is not None
                and available is not None
                and available <= total
            ):
                entry.update(total=total, available=available)
            categories.append(entry)
    total, available = count(details.get("total")), count(details.get("available"))
    verified_counts = (
        provider == "district"
        and complete
        and len(categories) == len(raw)
        and total is not None
        and available is not None
        and available <= total
        and all(
            "total" in c
            and "available" in c
            and ("state" not in c or (c["state"] == "AVAILABLE") == (c["available"] > 0))
            for c in categories
        )
        and sum(c["total"] for c in categories) == total
        and sum(c["available"] for c in categories) == available
    )
    if verified_counts:
        result.update(total=total, available=available)
    else:
        for category in categories:
            category.pop("total", None)
            category.pop("available", None)
            if provider == "district":
                category.pop("state", None)
    if categories:
        result["categories"] = categories
    bounds = details.get("price_range")
    if isinstance(bounds, list) and len(bounds) == 2:
        low, high = price(bounds[0]), price(bounds[1])
        if (
            low
            and high
            and Decimal(low) <= Decimal(high)
            and all(
                Decimal(low) <= Decimal(c["price"]) <= Decimal(high)
                for c in categories
                if "price" in c
            )
        ):
            result["price_range"] = [low, high]
    return result


def _district(session) -> dict:
    details = {
        "version": 1,
        "auditorium": label(session.get("audi")),
        "subtitles": subtitle(session.get("subtitle")),
    }
    fmt = label(session.get("scrnFmt"), 180) or ""
    if re.search(r"\bDOLBY\s+ATMOS\b", fmt, re.I):
        details["audio"] = "Dolby Atmos"
    areas = session.get("areas")
    if not isinstance(areas, list) or not 0 < len(areas) <= MAX_CATEGORIES:
        return sanitize(details, "district")
    categories = []
    for area in areas:
        if not isinstance(area, dict):
            categories.append(None)
            continue
        entry = {
            "id": area.get("code"),
            "name": area.get("label"),
            "price": price(area.get("price")),
        }
        total, available = count(area.get("sTotal")), count(area.get("sAvail"))
        aliases_agree = all(
            area.get(alias) is None
            or (type(area[alias]) is int and area[alias] == area.get(primary))
            for alias, primary in (("seatsTotal", "sTotal"), ("seatsAvail", "sAvail"))
        )
        blocked = (
            area.get("statusColor") not in (None, "G", "Y", "R")
            or area.get("disableClick")
            or area.get("isSoldOut")
            or any(
                area.get(flag) is False
                for flag in ("isBookingOpen", "isEnabled", "canBook", "bookable")
            )
        )
        if (
            total is not None
            and available is not None
            and available <= total
            and aliases_agree
            and area.get("sType") == "fixed"
            and not (blocked and available > 0)
        ):
            entry.update(total=total, available=available)
            entry["state"] = "AVAILABLE" if available > 0 else "UNAVAILABLE"
        categories.append(entry)
    details.update(
        categories=categories, total=session.get("total"), available=session.get("avail")
    )
    result = sanitize(details, "district")
    if "total" not in result:
        omitted("district", "category counts incomplete or inconsistent")
    return result


def _bms(item, child) -> dict:
    details = {
        "version": 1,
        "auditorium": label(item.get("ScreenName")),
        "subtitles": subtitle(item.get("SessionSubTitle"), item.get("SessionSubTitleAcronym")),
    }
    if item.get("IsAtmosEnabled") == "Y" and child.get("EventIsAtmosEnabled") in (None, "", "Y"):
        details["audio"] = "Dolby Atmos"
    categories, price_conflict = [], False
    raw = item.get("Categories")
    if isinstance(raw, list) and len(raw) <= MAX_CATEGORIES:
        for category in raw:
            if not isinstance(category, dict):
                continue
            current = price(category.get("CurPrice"))
            updated = category.get("UpdatedPrice")
            if updated is not None and price(updated) != current:
                current = None
                price_conflict = True
                omitted("bookmyshow", "unverified updated category price")
            entry = {
                "id": category.get("AreaCatCode"),
                "name": category.get("PriceDesc"),
                "price": current,
            }
            if (
                category.get("SeatLayout") == "Y"
                and not category.get("isDisabled")
                and not category.get("disabled")
            ):
                status = str(category.get("AvailStatus", ""))
                if status in ("1", "2", "3"):
                    entry["state"] = "AVAILABLE"
                elif status == "0":
                    entry["state"] = "UNAVAILABLE"
            categories.append(entry)
    details["categories"] = categories
    low, high = price(item.get("MinPrice")), price(item.get("MaxPrice"))
    if not price_conflict and all(
        item.get(updated) is None or price(item[updated]) == original
        for updated, original in (("UpdatedMinPrice", low), ("UpdatedMaxPrice", high))
    ):
        details["price_range"] = [low, high]
    # BestAvailableSeats and other numerical status fields are never seat counts.
    return sanitize(details, "bookmyshow")


def district_details(session) -> dict:
    try:
        return _district(session)
    except Exception:
        omitted("district", "optional detail parsing failed")
        return {}


def bms_details(item, child) -> dict:
    try:
        return _bms(item, child)
    except Exception:
        omitted("bookmyshow", "optional detail parsing failed")
        return {}


def detail_lines(offer: dict, at: datetime, freshness: int) -> list[str]:
    """Render only a fresh, validated provider snapshot; never infer missing fields."""
    provider = offer["provider"]
    try:
        checked = datetime.fromisoformat(offer.get("observed_at", ""))
        if checked.tzinfo is None or not -30 <= (at - checked).total_seconds() <= freshness:
            return []
        details = sanitize(offer.get("details"), provider)
        lines = []
        for key, title in (
            ("auditorium", "Auditorium"),
            ("subtitles", "Subtitles"),
            ("audio", "Audio"),
        ):
            if key in details:
                lines.append(f"{title}: {details[key]}")
        if "total" in details:
            lines.append(f"Seats: {details['available']} available / {details['total']} total")
        categories = details.get("categories", [])
        has_prices = any("price" in c for c in categories) or "price_range" in details
        if has_prices:
            lines.append("Listed ticket prices in INR; checkout fees may apply.")
        if "price_range" in details and not any("price" in c for c in categories):
            low, high = details["price_range"]
            lines.append(f"Ticket price range: INR {low}–{high}")
        for category in categories:
            parts = []
            if "total" in category:
                parts.append(f"{category['available']} available / {category['total']} total")
            elif category.get("state"):
                parts.append("available" if category["state"] == "AVAILABLE" else "unavailable")
            if "price" in category:
                parts.append(f"INR {category['price']}")
            # A name alone adds no actionable category information.
            if parts:
                lines.append(f"{category['name']}: " + " | ".join(parts))
        if not lines:
            return []
        return [f"Provider snapshot: {checked.astimezone(IST):%d %b %Y, %I:%M:%S %p} IST", *lines]
    except Exception:
        omitted(provider, "optional detail rendering failed")
        return []
