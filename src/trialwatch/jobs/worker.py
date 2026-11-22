"""The worker (`trialwatch worker`): runs queued jobs one at a time, tries failed ones again later,
starts the jobs schedules make, and puts held jobs back in the queue when the spending cap allows.

One job at a time: CTIS asks for polite, one-at-a-time requests, and a source or check run is read by
one process at a time anyway (advisory locks). Several workers can run (each takes a different job),
but one is enough.

Each job runs on a connection of its own, as whoever created it (a person, or trialwatch), with the
session's trialwatch.job naming the job, so the runs and billed calls it makes record it. The worker's
own connection (as trialwatch) keeps the queue: claims, progress lines, heartbeats, outcomes. While a
job runs, a heartbeat thread says it is alive; a job whose worker died (no heartbeat for STALLED) has
lost its attempt and is queued again by whichever worker looks next.
"""

import logging
import os
import socket
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta

import psycopg

from trialwatch import db
from trialwatch.db import core
from trialwatch.db import costs as costs_db
from trialwatch.db import jobs as jobs_db
from trialwatch.db.jobs import Job
from trialwatch.jobs import MAX_ATTEMPTS, PRIORITY_AUTOMATIC, refresh, steps
from trialwatch.jobs.retry import PermanentError, backoff
from trialwatch.jobs.steps import Context, Outcome

HEARTBEAT = timedelta(seconds=30)
STALLED = timedelta(minutes=5)
# How often the worker looks for due schedules, stalled jobs and held jobs it may resume
HOUSEKEEPING = timedelta(minutes=1)
# How long it sleeps when nothing is queued
IDLE = 5.0
LONGEST_ERROR = 4000

RUNNERS: dict[str, Callable[[Context, psycopg.Connection, Job], Outcome]] = {
    "sync": steps.run_sync,
    "transfer": steps.run_transfer,
    "check": steps.run_check,
    "annotate": steps.run_annotate,
    "refresh": refresh.run_refresh,
}

log = logging.getLogger("trialwatch.worker")


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@contextmanager
def job_connection(job: Job) -> Iterator[psycopg.Connection]:
    """The job's own connection: as whoever created it, naming the job for the runs it starts."""
    kind = "person"
    if job.actor == db.SYSTEM_ACTOR:
        kind = "process"
    with db.connect(actor=job.actor, actor_kind=kind) as conn:
        core.act_as(conn, job.actor, kind)
        jobs_db.work_for_job(conn, job.id)
        yield conn


class Heartbeat:
    """Says the job is alive every HEARTBEAT, on a connection of its own, until stopped."""

    def __init__(self, job_id: int, worker: str) -> None:
        self.job_id = job_id
        self.worker = worker
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name=f"heartbeat-{job_id}", daemon=True)

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _beat(self) -> None:
        try:
            with db.connect_system() as conn:
                while not self._stop.wait(HEARTBEAT.total_seconds()):
                    jobs_db.heartbeat(conn, self.job_id, self.worker)
        except Exception:  # a missed heartbeat only means another worker may take the job over later
            log.exception("heartbeat of job %s stopped", self.job_id)


def _short(text: str) -> str:
    if len(text) <= LONGEST_ERROR:
        return text
    return text[:LONGEST_ERROR] + " …"


def failures_text(failed: dict[str, str]) -> str:
    listed = []
    for item, error in failed.items():
        listed.append(f"{item}: {error}")
    return _short(f"{len(failed)} failed: " + "; ".join(listed))


def run_one(queue: psycopg.Connection, job: Job, worker: str) -> str:
    """Run a claimed job's attempt and record how it went; returns the job's status after."""
    ctx = Context(queue, job)
    ctx.add(f"attempt {job.attempts} of {job.max_attempts} on {worker}")
    outcome = None
    error = None
    permanent = False
    try:
        with Heartbeat(job.id, worker), job_connection(job) as conn:
            problem = steps.params_problem(job.kind, job.params)
            if problem is not None:
                raise PermanentError(problem)
            outcome = RUNNERS[job.kind](ctx, conn, job)
    except PermanentError as exc:
        error = str(exc)
        permanent = True
    except Exception as exc:  # anything else may pass (a network error, a busy source): tried again
        error = f"{type(exc).__name__}: {exc}"
        ctx.add(traceback.format_exc())
    status = _record(queue, ctx, job, outcome, error, permanent)
    if job.parent_id is not None and status in jobs_db.ENDED:
        jobs_db.requeue_parent_when_done(queue, job.parent_id)
    return status


def _record(queue: psycopg.Connection, ctx: Context, job: Job, outcome: Outcome | None, error: str | None,
            permanent: bool) -> str:
    result = job.result
    if outcome is not None:
        result = outcome.result
    if ctx.cancelled:
        jobs_db.finish(queue, job.id, "cancelled", result, "cancelled by a person")
        ctx.add("cancelled")
        return "cancelled"
    if ctx.hold_reason is not None and error is None:
        jobs_db.hold(queue, job.id, result, ctx.hold_reason)
        return "held"
    if error is None and outcome is not None and outcome.failed:
        error = failures_text(outcome.failed)
        permanent = outcome.final
    if error is None and outcome is not None and outcome.wait:
        jobs_db.wait_for_children(queue, job.id, result)
        ctx.add("waiting for its jobs")
        # children that all ended before this point (none could, but a worker may be quick) still move it on
        jobs_db.requeue_parent_when_done(queue, job.id)
        return "waiting"
    if error is None:
        jobs_db.finish(queue, job.id, "succeeded", result)
        ctx.add("succeeded")
        return "succeeded"
    ctx.add(f"failed: {error}")
    if permanent or job.attempts >= job.max_attempts:
        jobs_db.finish(queue, job.id, "failed", result, error)
        return "failed"
    delay = backoff(job.attempts)
    jobs_db.try_later(queue, job.id, delay, result, error)
    ctx.add(f"tried again in {_minutes(delay)}")
    return "queued"


def _minutes(delay: timedelta) -> str:
    minutes = delay.total_seconds() / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    return f"{minutes / 60:.1f} h"


def housekeeping(queue: psycopg.Connection) -> None:
    """Queue again the jobs of dead workers, start the jobs of due schedules, and resume held jobs once
    the spending cap allows (a new month, or a raised or removed cap)."""
    stalled = jobs_db.requeue_stalled(queue, STALLED)
    if stalled:
        log.warning("jobs whose worker stopped, queued again or failed: %s", stalled)
    start_due_schedules(queue)
    if not costs_db.cap_reached(queue):
        resumed = jobs_db.resume_held(queue)
        if resumed:
            log.warning("held jobs resumed: %s", resumed)


def start_due_schedules(queue: psycopg.Connection) -> list[int]:
    """Add a job for each schedule whose time has come; returns the jobs added."""
    started = []
    while True:
        with queue.transaction():
            schedule = jobs_db.take_due_schedule(queue)
            if schedule is None:
                return started
            job_id = jobs_db.enqueue(queue, schedule.kind, schedule.params, max_attempts=MAX_ATTEMPTS[schedule.kind],
                                     automatic=True, schedule_id=schedule.id, priority=PRIORITY_AUTOMATIC)
        log.warning("schedule %s started job %s", schedule.name, job_id)
        started.append(job_id)


def run(once: bool = False, should_stop: Callable[[], bool] | None = None) -> int:
    """Work until stopped (Ctrl-C), or with `once` until the queue is empty. Returns the jobs run."""
    worker = worker_name()
    ran = 0
    last_housekeeping = None
    with db.connect_system() as queue:
        while True:
            if should_stop is not None and should_stop():
                return ran
            now = time.monotonic()
            if last_housekeeping is None or now - last_housekeeping >= HOUSEKEEPING.total_seconds():
                housekeeping(queue)
                last_housekeeping = now
            job = jobs_db.claim_next(queue, worker)
            if job is None:
                if once:
                    return ran
                time.sleep(IDLE)
                continue
            log.warning("job %s (%s) attempt %s", job.id, job.kind, job.attempts)
            status = run_one(queue, job, worker)
            log.warning("job %s: %s", job.id, status)
            ran += 1
