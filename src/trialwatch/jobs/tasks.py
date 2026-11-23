"""Every kind of job, in one place (jobs.job.kind): what it is called, how often it is tried, whether the
monthly spending cap holds it when TrialWatch started it, how its params are checked, and the step that
runs it (steps.py, refresh.py). The web interface queues jobs, checking their params here; the worker
runs them (worker.py); the command line runs the same steps here and now (cli.py, run_here)."""

from collections.abc import Callable
from dataclasses import dataclass

import psycopg

from trialwatch.checks import settings as check_settings
from trialwatch.db import core
from trialwatch.jobs import refresh, steps
from trialwatch.jobs.retry import PermanentError
from trialwatch.jobs.steps import Context, Outcome


@dataclass(frozen=True)
class Task:
    label: str
    # how many times a job is tried before it is left failed for a person (/admin/jobs)
    max_attempts: int
    # it calls a paid model: a job TrialWatch started is held by the monthly spending cap
    capped: bool
    # the step, with the job's params
    run: Callable[[Context, psycopg.Connection, dict], Outcome]


TASKS = {
    "sync": Task("Sync a source's list (source sync)", 5, False, steps.run_sync),
    "transfer": Task("Transfer evidence (source transfer)", 5, False, steps.run_transfer),
    "check": Task("Evidence check (check evidence)", 3, True, steps.run_check),
    "annotate": Task("AI tags for patient search (search annotate)", 3, True, steps.run_annotate),
    # its sync and transfer are free: the refresh watches the cap itself, before its paid steps
    "refresh": Task("Refresh: sync, transfer what changed, tag and check what changed", 3, False, refresh.run_refresh),
}
# kind -> label, for pages
LABELS = {kind: task.label for kind, task in TASKS.items()}


def problem(kind: str, params: dict) -> str | None:
    """Why a job of `kind` can't run with these params, or None. Checked when it is queued (so the form
    says so at once) and again when it runs."""
    if kind not in TASKS:
        return f"unknown kind of job {kind!r}"
    limit = params.get("limit")
    if limit is not None and limit < 1:
        return "the limit must be 1 or more"
    if kind in ("sync", "transfer") and not params.get("source"):
        return "choose a source"
    if kind == "transfer":
        keys = params.get("keys")
        which = params.get("which")
        if keys and which:
            return "choose trials, or which of the source's trials, not both"
        if not keys and not which:
            return "choose trials, or which of the source's trials to transfer"
        if which and which not in core.TRANSFER_CHOICES:
            return f"choose one of {', '.join(core.TRANSFER_CHOICES)}"
    if kind == "annotate" and not params.get("trial_ids") and not params.get("every"):
        return "choose trials, or every recruiting trial not tagged yet"
    if kind == "check" and not params.get("trial_ids") and not params.get("every"):
        return "choose trials, or every trial ready for a check"
    if kind in ("check", "refresh"):
        try:
            check_settings.load(params.get("config", check_settings.DEFAULT))
        except (FileNotFoundError, ValueError) as exc:
            return str(exc)
    return None


def run_here(conn: psycopg.Connection, kind: str, params: dict, echo: Callable[[str], None]) -> Outcome:
    """Run a kind's step here and now, as the command line does: nothing queued, its lines given to
    `echo`, its runs made as the connection's actor. Raises PermanentError when it can't run."""
    found = problem(kind, params)
    if found is not None:
        raise PermanentError(found)
    return TASKS[kind].run(Context(echo), conn, params)
