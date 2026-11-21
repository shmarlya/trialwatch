"""Progress lines from long-running steps, for whoever is watching (the web interface's jobs).

A step takes an optional `progress` function and calls it with one line of text per trial or file.
The command line passes nothing, so its output does not change.
"""

from collections.abc import Callable

Progress = Callable[[str], None] | None


def report(progress: Progress, message: str) -> None:
    """Send one progress line, if anyone is listening."""
    if progress is not None:
        progress(message)
