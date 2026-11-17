"""Provider adapters expose validated observations, never send notifications."""

from .collector import Collector
from .common import bms_state, inventory_status, same_title
from .district import parse_district
from .ticketnew import parse_ticketnew

__all__ = [
    "Collector",
    "bms_state",
    "inventory_status",
    "parse_district",
    "parse_ticketnew",
    "same_title",
]
