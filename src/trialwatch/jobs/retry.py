"""When a failed job is tried again, and which failures are worth trying again.

A failed attempt is queued again after a growing pause (BACKOFF, plus a little jitter so many failed
batches don't all come back at once). A PermanentError fails a job at once: trying again would fail
the same way (an unknown source, an invalid setup, bad API credentials).
"""

import random
from datetime import timedelta

# The pause before attempt 2, 3, ...; the last one repeats
BACKOFF = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2),
           timedelta(hours=6))
JITTER = 0.10
# A trial's check error that says the API call failed for a reason that passes (an outage, a rate limit,
# a timeout): the trial is tried again. Other errors (a scanned PDF, a refusal) stay for a person.
TRANSIENT_ERRORS = ("APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError",
                    "OverloadedError", "ServiceUnavailableError", "DeadlineExceededError")


class PermanentError(Exception):
    """A job failure that trying again can't fix."""


def backoff(attempt: int) -> timedelta:
    """The pause after failed attempt number `attempt` (1 is the first)."""
    index = attempt - 1
    if index >= len(BACKOFF):
        index = len(BACKOFF) - 1
    if index < 0:
        index = 0
    pause = BACKOFF[index]
    jitter = pause.total_seconds() * JITTER * random.random()
    return pause + timedelta(seconds=jitter)


def is_transient(error: str | None) -> bool:
    """Whether a trial's check error is worth trying again."""
    if not error:
        return False
    for name in TRANSIENT_ERRORS:
        if name in error:
            return True
    return False
