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

A job that stops moving (no progress line or beat for TRIALWATCH_STUCK_MINUTES: a read or a query that
never returns) is not kept alive by its heartbeat: the worker logs where it was and exits, so the
container's restart policy starts a fresh one, and the job, requeued after STALLED, resumes where it
was. A lost database connection is opened again (the queue's, the heartbeat's) rather than ending the
worker; the job's own fails its attempt, which is tried again later.
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
from opentelemetry.trace import StatusCode

from trialwatch import config, db, progress, telemetry
from trialwatch.auth import sessions
from trialwatch.db import auth as auth_db
from trialwatch.db import core
from trialwatch.db import costs as costs_db
from trialwatch.db import jobs as jobs_db
from trialwatch.db.jobs import Job
from trialwatch.jobs import PRIORITY_AUTOMATIC, tasks
from trialwatch.jobs.retry import PermanentError, backoff
from trialwatch.jobs.steps import JobContext, Outcome

HEARTBEAT = timedelta(seconds=30)
STALLED = timedelta(minutes=5)
# How often the worker looks for due schedules, stalled jobs and held jobs it may resume
HOUSEKEEPING = timedelta(minutes=1)
# How often it forgets what is kept for a while only, and how long: ended jobs' progress lines
PURGE = timedelta(days=1)
JOB_LINES_KEPT = timedelta(days=90)
# How long it sleeps when nothing is queued
IDLE = 5.0
LONGEST_ERROR = 4000
# The longest a statement of a job may run: a query that never returns fails its attempt instead
STATEMENT_TIMEOUT = timedelta(minutes=15)
# The longest pause before connecting again to a database that went away
LONGEST_RECONNECT_WAIT = 60.0

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
        jobs_db.limit_statements(conn, STATEMENT_TIMEOUT)
        yield conn


def stop_stuck_worker() -> None:
    """What the watchdog does with a stuck job: end this process at once (a stuck thread can't be
    stopped from outside); the container's restart policy starts a new worker."""
    os._exit(1)


class Heartbeat:
    """Says the job is alive every HEARTBEAT, on a connection of its own (opened again if it is lost),
    until stopped; unless the job has not moved for `stuck` (progress.beat), then it says nothing more
    and calls `on_stuck` (by default, ends the worker)."""

    def __init__(
        self,
        job_id: int,
        worker: str,
        stuck: timedelta | None = None,
        on_stuck: Callable[[], None] = stop_stuck_worker,
        every: timedelta = HEARTBEAT,
    ) -> None:
        self.job_id = job_id
        self.worker = worker
        if stuck is None:
            stuck = timedelta(minutes=config.stuck_minutes())
        self.stuck = stuck
        self.on_stuck = on_stuck
        self.every = every
        # the database session the heartbeat writes from (tests end it to see it come back)
        self.backend_pid: int | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name=f"heartbeat-{job_id}", daemon=True)

    def __enter__(self) -> "Heartbeat":
        progress.start_counting()
        progress.beat(f"job {self.job_id} started")
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _beat(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                with db.connect_system() as conn:
                    self.backend_pid = conn.info.backend_pid
                    failures = 0
                    while not self._stop.wait(self.every.total_seconds()):
                        if self._is_stuck():
                            return
                        jobs_db.heartbeat(conn, self.job_id, self.worker, progress.counted())
                        # the worker itself is alive too, while a long job runs
                        telemetry.heartbeat()
                return
            except psycopg.Error as exc:  # the database went away: try again, the job may still finish
                failures += 1
                wait = min(LONGEST_RECONNECT_WAIT, 2.0**failures)
                log.warning("heartbeat of job %s lost the database (%s): connecting again in %.0f s",
                            self.job_id, exc, wait)
                if self._stop.wait(wait):
                    return
            except Exception:  # a missed heartbeat only means another worker may take the job over later
                log.exception("heartbeat of job %s stopped", self.job_id)
                return

    def _is_stuck(self) -> bool:
        seconds, what = progress.since_last_beat()
        if seconds < self.stuck.total_seconds():
            return False
        log.error("job %s has not moved for %.0f min (last: %s): stopping the worker, the job will be "
                  "queued again", self.job_id, seconds / 60, what)
        self.on_stuck()
        return True


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
    """Run a claimed job's attempt and record how it went; returns the job's status after. The attempt
    is one trace (span jobs.run), which the job's page names."""
    started = time.monotonic()
    with telemetry.tracer.start_as_current_span("jobs.run") as span:
        span.set_attribute("trialwatch.job.id", job.id)
        span.set_attribute("trialwatch.job.kind", job.kind)
        span.set_attribute("trialwatch.job.attempt", job.attempts)
        trace_id = telemetry.trace_id()
        if trace_id is not None:
            jobs_db.set_trace_id(queue, job.id, trace_id)
        status = _attempt(queue, job, worker)
        span.set_attribute("trialwatch.job.status", status)
        if status in ("failed", "queued"):
            span.set_status(StatusCode.ERROR, f"the attempt failed; the job is {status}")
    labels = {"kind": job.kind, "status": status}
    telemetry.jobs_finished.add(1, labels)
    telemetry.jobs_duration.record(time.monotonic() - started, labels)
    return status


def _attempt(queue: psycopg.Connection, job: Job, worker: str) -> str:
    task = tasks.TASKS.get(job.kind)
    ctx = JobContext(queue, job, capped=task is not None and task.capped)
    ctx.add(f"attempt {job.attempts} of {job.max_attempts} on {worker}")
    outcome = None
    error = None
    permanent = False
    try:
        with Heartbeat(job.id, worker), job_connection(job) as conn:
            problem = tasks.problem(job.kind, job.params)
            if problem is not None:
                raise PermanentError(problem)
            outcome = task.run(ctx, conn, job.params)
    except PermanentError as exc:
        error = str(exc)
        permanent = True
    except Exception as exc:  # anything else may pass (a network error, a busy source): tried again
        error = f"{type(exc).__name__}: {exc}"
        ctx.add(traceback.format_exc())
    return _record(queue, ctx, job, outcome, error, permanent)


def _record(
    queue: psycopg.Connection, ctx: JobContext, job: Job, outcome: Outcome | None, error: str | None, permanent: bool
) -> str:
    result = job.result
    failures = {}
    if outcome is not None:
        result = outcome.result
        # what another attempt can't fix is shown with the rest, but fails nothing (Outcome)
        failures = {**outcome.failed, **outcome.left}
    jobs_db.record_attempt(queue, job.id, failures, progress.counted())
    if ctx.cancelled:
        jobs_db.finish(queue, job.id, "cancelled", result, "cancelled by a person")
        ctx.add("cancelled")
        return "cancelled"
    if ctx.hold_reason is not None and error is None:
        jobs_db.hold(queue, job.id, result, ctx.hold_reason)
        return "held"
    if error is None and outcome is not None and outcome.failed:
        error = failures_text(outcome.failed)
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


def purge(queue: psycopg.Connection) -> None:
    """Forget what is kept for a while only: progress lines of jobs ended JOB_LINES_KEPT ago (the job
    and its result stay), and ended sign-in sessions."""
    forgotten = jobs_db.forget_lines(queue, JOB_LINES_KEPT)
    auth_db.drop_expired_sessions(queue, sessions.now() - sessions.IDLE)
    if forgotten:
        log.info("forgot %s progress lines of jobs ended %s days ago", forgotten, JOB_LINES_KEPT.days)


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
            log.info("held jobs resumed: %s", resumed)


def start_due_schedules(queue: psycopg.Connection) -> list[int]:
    """Add a job for each schedule whose time has come; returns the jobs added."""
    started = []
    while True:
        with queue.transaction():
            schedule = jobs_db.take_due_schedule(queue)
            if schedule is None:
                return started
            max_attempts = tasks.TASKS[schedule.kind].max_attempts
            job_id = jobs_db.enqueue(
                queue,
                schedule.kind,
                schedule.params,
                max_attempts=max_attempts,
                automatic=True,
                schedule_id=schedule.id,
                priority=PRIORITY_AUTOMATIC,
            )
        log.info("schedule %s started job %s", schedule.name, job_id)
        started.append(job_id)


def run(once: bool = False, should_stop: Callable[[], bool] | None = None) -> int:
    """Work until stopped (Ctrl-C), or with `once` until the queue is empty. Returns the jobs run.
    A lost database is connected to again, after a growing pause (a job it was recording is queued
    again once its heartbeat lapses)."""
    worker = worker_name()
    ran = [0]
    failures = 0
    while True:
        try:
            with db.connect_system() as queue:
                failures = 0
                _work(queue, worker, once, should_stop, ran)
                return ran[0]
        except psycopg.OperationalError as exc:
            if once:
                raise
            failures += 1
            wait = min(LONGEST_RECONNECT_WAIT, 2.0**failures)
            log.warning("the worker lost the database (%s): connecting again in %.0f s", exc, wait)
            time.sleep(wait)


def _work(queue: psycopg.Connection, worker: str, once: bool, should_stop: Callable[[], bool] | None,
          ran: list[int]) -> None:
    """The worker's loop on one queue connection: housekeeping, then the next job, until stopped."""
    last_housekeeping = None
    last_purge = None
    while True:
        if should_stop is not None and should_stop():
            return
        telemetry.heartbeat()
        now = time.monotonic()
        if last_housekeeping is None or now - last_housekeeping >= HOUSEKEEPING.total_seconds():
            housekeeping(queue)
            last_housekeeping = now
        if last_purge is None or now - last_purge >= PURGE.total_seconds():
            purge(queue)
            last_purge = now
        job = jobs_db.claim_next(queue, worker)
        if job is None:
            if once:
                return
            time.sleep(IDLE)
            continue
        log.info("job %s (%s) attempt %s", job.id, job.kind, job.attempts)
        status = run_one(queue, job, worker)
        log.info("job %s: %s", job.id, status)
        ran[0] += 1
