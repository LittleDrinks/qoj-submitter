"""A shared, bounded budget for read requests and receipt polling."""
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class ReadFailure(Exception):
    pass


class ReadBudget:
    def __init__(self, seconds=20, requests=12):
        self.deadline = time.monotonic() + seconds
        self.requests = requests

    def take(self):
        remaining = self.deadline - time.monotonic()
        if self.requests <= 0 or remaining <= 0:
            raise ReadFailure('Read retry budget exhausted; try recovery again later')
        self.requests -= 1
        return min(8, remaining)

    def pause(self, attempt, retry_after=None):
        delay = random.uniform(0.2, min(2, 0.5 * 2 ** attempt))
        if isinstance(retry_after, str):
            try:
                minimum = float(retry_after)
            except ValueError:
                try:
                    date = parsedate_to_datetime(retry_after)
                    minimum = (date - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    minimum = 0
            delay = max(delay, minimum)
        if self.requests <= 0 or delay >= self.deadline - time.monotonic():
            raise ReadFailure('Read retry budget exhausted; try recovery again later')
        time.sleep(delay)
