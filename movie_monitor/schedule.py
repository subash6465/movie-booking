"""Independent retry clocks for catalogue refreshes and individual cinema scopes."""

import time
from dataclasses import dataclass


@dataclass
class Retry:
    failures: int = 0
    next_at: float = 0
    error: str | None = None


class Schedule:
    def __init__(self, interval=60, maximum=900, clock=None):
        self.interval, self.maximum = interval, maximum
        self.clock = clock or time.monotonic
        self.entries = {}

    def due(self, key):
        return self.entries.get(key, Retry()).next_at <= self.clock()

    def success(self, key, interval=None):
        self.entries[key] = Retry(
            next_at=self.clock() + (self.interval if interval is None else interval)
        )

    def failure(self, key, error):
        streak = self.entries.get(key, Retry()).failures + 1
        delay = min(self.interval * 2 ** min(streak, 4), max(self.interval, self.maximum))
        self.entries[key] = Retry(streak, self.clock() + delay, str(error)[:240])

    def failures(self, keys=None):
        return {
            key: entry
            for key, entry in self.entries.items()
            if entry.failures and (keys is None or key in keys)
        }
