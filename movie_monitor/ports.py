"""Small structural interfaces for future collectors and delivery transports."""

from datetime import datetime
from typing import Protocol

from .core import Observation


class NotificationSender(Protocol):
    def send(self, target: str, text: str) -> str:
        """Return the transport acknowledgement ID or raise DeliveryError."""
        ...


class BookingReadiness(Protocol):
    def check(self, rows: list[dict], state, at: datetime) -> list[dict]:
        """Schedule passive checks and return only currently confirmed rows."""
        ...

    def approved(self, row: dict, at: datetime) -> dict | None:
        """Recheck proof/listing freshness just before a delivery is claimed."""
        ...


class ProviderCollector(Protocol):
    def collect(self, provider: str, day: str) -> Observation:
        """Return a validated result or an explicit failed observation."""
        ...

    def close(self) -> None:
        """Release the collector's transport resources."""
        ...
