"""Small structural interfaces for future collectors and delivery transports."""

from typing import Protocol

from .core import Observation


class NotificationSender(Protocol):
    def send(self, target: str, text: str) -> str:
        """Return the transport acknowledgement ID or raise DeliveryError."""
        ...


class ProviderCollector(Protocol):
    def collect(self, provider: str, day: str) -> Observation:
        """Return a validated result or an explicit failed observation."""
        ...

    def close(self) -> None:
        """Release the collector's transport resources."""
        ...
