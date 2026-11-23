"""Workflow steps started from the web interface: sync a source's list, transfer trials' evidence,
check trials (ADR-005). A form adds a queued job (jobs.job) as the signed-in person; `trialwatch
worker` runs it (src/trialwatch/jobs/), tries it again when it fails, and the job's page follows its
progress lines from the database. Jobs survive a server restart; many can be queued at once and run
one after the other.
"""

from dataclasses import dataclass, field

import psycopg

from trialwatch.checks import settings as check_settings
from trialwatch.db import jobs as jobs_db
from trialwatch.jobs import PRIORITY_PERSON, tasks

# The forms' step names and the kind of job each becomes
STEP_KINDS = {"list": "sync", "transfer": "transfer", "evidence": "check"}


@dataclass
class JobRequest:
    """What the form asked for."""

    step: str
    # list and transfer: the registered source (core.source code)
    source: str = ""
    # transfer: the source's keys of the chosen trials (e.g. EU CT numbers), or which of its trials
    # (core.TRANSFER_CHOICES: pending, failed, stale, recruiting, listed)
    keys: list[str] = field(default_factory=list)
    which: str = ""
    # core.trial ids, for checking trials chosen in the interface (e.g. "check again" on a trial page)
    trial_ids: list[int] = field(default_factory=list)
    # evidence: every trial ready for a check
    every: bool = False
    limit: int | None = None
    # evidence: the setup the check runs with (a file in checks/configs/)
    config: str = check_settings.DEFAULT
    dry_run: bool = False


def job_params(request: JobRequest) -> tuple[str, dict]:
    """The kind of job and its params (jobs.job.params) for a form's request."""
    kind = STEP_KINDS[request.step]
    params = {}
    if request.limit is not None:
        params["limit"] = request.limit
    if kind in ("sync", "transfer"):
        params["source"] = request.source
    if kind == "transfer":
        if request.keys:
            params["keys"] = list(request.keys)
        if request.which:
            params["which"] = request.which
    if kind == "check":
        if request.trial_ids:
            params["trial_ids"] = list(request.trial_ids)
        if request.every:
            params["every"] = True
        params["config"] = request.config
        if request.dry_run:
            params["dry_run"] = True
    return kind, params


def check_request(request: JobRequest) -> None:
    """Raise ValueError when the request can't run."""
    if request.step not in STEP_KINDS:
        raise ValueError(f"unknown step {request.step!r}")
    kind, params = job_params(request)
    problem = tasks.problem(kind, params)
    if problem is not None:
        raise ValueError(problem)


def enqueue(conn: psycopg.Connection, request: JobRequest) -> int:
    """Check the request and queue it as the connection's actor (the signed-in person); returns the
    job's id. Raises ValueError."""
    check_request(request)
    kind, params = job_params(request)
    return jobs_db.enqueue(conn, kind, params, max_attempts=tasks.TASKS[kind].max_attempts, priority=PRIORITY_PERSON)
