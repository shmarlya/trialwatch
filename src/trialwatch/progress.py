"""Progress lines from long-running steps, for whoever is watching (the web interface's jobs).

A step takes an optional `progress` function and calls it with one line of text per trial or file.
The command line passes nothing, so its output does not change.

Every line, and every smaller step that calls beat() (a document saved, a file read), also says the
work is moving: the worker's watchdog (jobs/worker.py) stops a job that has not moved for a long time.
"""

import time
from collections.abc import Callable

Progress = Callable[[str], None] | None

# When the work last moved, and what it was doing then (written by the job's threads, read by the
# heartbeat's: one tuple assigned at once)
_last = (time.monotonic(), "started")


def report(progress: Progress, message: str) -> None:
    """Send one progress line, if anyone is listening; the work has moved either way."""
    beat(message)
    if progress is not None:
        progress(message)


def beat(what: str) -> None:
    """The work moved: `what` it did last, said if it then stops moving."""
    global _last
    _last = (time.monotonic(), what)


def since_last_beat() -> tuple[float, str]:
    """Seconds since the work last moved, and what it did then."""
    moved_at, what = _last
    return time.monotonic() - moved_at, what
