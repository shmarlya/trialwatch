"""Queries on the jobs schema: the job queue, its progress lines and schedules (see the jobs
section of db/migrations/*_init.sql). The worker (src/trialwatch/jobs/) claims jobs, the web interface adds them
and shows them."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

# A job in one of these has ended
ENDED = ("succeeded", "partial", "failed", "cancelled")
# ... and in one of these someone should look at it (the staff banner). Not partial: what it left undone is
# listed on its page, often for good (a file the source publishes broken), so a retry can't clear it
NEEDS_ATTENTION = ("failed", "held")


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    params: dict
    automatic: bool
    schedule_id: int | None
    priority: int
    status: str
    attempts: int
    max_attempts: int
    run_after: datetime
    cancel_requested: bool
    last_error: str | None
    result: dict
    locked_by: str | None
    heartbeat_at: datetime | None
    created_by: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    # the creator's actor name (an email, or trialwatch)
    actor: str
    # the trace of its latest attempt, when tracing is on (telemetry.py)
    trace_id: str | None = None
    # items done of items to do in its latest attempt (trials of a transfer or a check), when it counts
    progress_done: int | None = None
    progress_total: int | None = None
    attempt_started_at: datetime | None = None
    # what failed, item by item: a trial's key or name (or a step) -> why
    failures: dict = field(default_factory=dict)

    @property
    def ended(self) -> bool:
        return self.status in ENDED


_JOB_COLUMNS = """
    j.id, j.kind, j.params, j.automatic, j.schedule_id, j.priority, j.status, j.attempts,
    j.max_attempts, j.run_after, j.cancel_requested, j.last_error, j.result, j.locked_by, j.heartbeat_at,
    j.created_by, j.created_at, j.started_at, j.finished_at, a.name AS actor, j.trace_id,
    j.progress_done, j.progress_total, j.attempt_started_at, j.failures
"""
_JOBS = f"SELECT {_JOB_COLUMNS} FROM jobs.job j JOIN core.actor a ON a.id = j.created_by"


def enqueue(conn: psycopg.Connection, kind: str, params: dict, *, max_attempts: int, automatic: bool = False,
            schedule_id: int | None = None, priority: int = 100) -> int:
    """Add a queued job, created by the session's actor; returns its id."""
    row = conn.execute(
        "INSERT INTO jobs.job (kind, params, automatic, schedule_id, priority, max_attempts, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, current_setting('trialwatch.actor')::bigint) RETURNING id",
        (kind, Jsonb(params), automatic, schedule_id, priority, max_attempts),
    ).fetchone()
    return row[0]


def job(conn: psycopg.Connection, job_id: int) -> Job | None:
    with conn.cursor(row_factory=class_row(Job)) as cursor:
        return cursor.execute(_JOBS + " WHERE j.id = %s", (job_id,)).fetchone()


def jobs_with_status(conn: psycopg.Connection, statuses: tuple[str, ...], limit: int = 100) -> list[Job]:
    with conn.cursor(row_factory=class_row(Job)) as cursor:
        return cursor.execute(_JOBS + " WHERE j.status = ANY(%s) ORDER BY j.id DESC LIMIT %s",
                              (list(statuses), limit)).fetchall()


def recent_jobs(conn: psycopg.Connection, limit: int = 50) -> list[Job]:
    with conn.cursor(row_factory=class_row(Job)) as cursor:
        return cursor.execute(_JOBS + " ORDER BY j.id DESC LIMIT %s", (limit,)).fetchall()


def status_counts(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, count(*) FROM jobs.job GROUP BY status").fetchall()
    counts = {}
    for row in rows:
        counts[row[0]] = row[1]
    return counts


def needing_attention(conn: psycopg.Connection) -> int:
    """How many jobs failed for good or are held by the spending cap."""
    row = conn.execute("SELECT count(*) FROM jobs.job WHERE status = ANY(%s)", (list(NEEDS_ATTENTION),)).fetchone()
    return row[0]


def running_job(conn: psycopg.Connection) -> Job | None:
    """The job a worker is running now (the newest, if more than one)."""
    with conn.cursor(row_factory=class_row(Job)) as cursor:
        return cursor.execute(_JOBS + " WHERE j.status = 'running' ORDER BY j.id DESC LIMIT 1").fetchone()


# --- the worker's side ---------------------------------------------------------------------------

def claim_next(conn: psycopg.Connection, worker: str) -> Job | None:
    """Take the next due queued job for this worker, or None. Two workers never take the same one (SKIP
    LOCKED). The attempt is counted now, so a worker that dies mid-job still used it up."""
    with conn.transaction():
        row = conn.execute(
            """
            UPDATE jobs.job SET status = 'running', attempts = attempts + 1, locked_by = %s,
                   heartbeat_at = clock_timestamp(), started_at = coalesce(started_at, clock_timestamp()),
                   attempt_started_at = clock_timestamp(), progress_done = NULL, progress_total = NULL
             WHERE id = (SELECT id FROM jobs.job WHERE status = 'queued' AND run_after <= clock_timestamp()
                          ORDER BY priority, id FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id
            """,
            (worker,),
        ).fetchone()
    if row is None:
        return None
    return job(conn, row[0])


def work_for_job(conn: psycopg.Connection, job_id: int) -> None:
    """Name the job this session works for: the runs it starts and the calls it records carry it
    (their job_id columns default to the session's trialwatch.job)."""
    conn.execute("SELECT set_config('trialwatch.job', %s, false)", (str(job_id),))


def set_trace_id(conn: psycopg.Connection, job_id: int, trace_id: str) -> None:
    """The trace the job's attempt runs in now."""
    conn.execute("UPDATE jobs.job SET trace_id = %s WHERE id = %s", (trace_id, job_id))


def limit_statements(conn: psycopg.Connection, longest: timedelta) -> None:
    """A statement of this session running longer than `longest` fails (statement_timeout)."""
    conn.execute("SELECT set_config('statement_timeout', %s, false)", (f"{int(longest.total_seconds() * 1000)}ms",))


def heartbeat(conn: psycopg.Connection, job_id: int, worker: str,
              counted: tuple[int, int] | None = None) -> None:
    """The job is alive; with `counted`, how far it is (items done, items to do)."""
    done, total = counted if counted is not None else (None, None)
    conn.execute(
        "UPDATE jobs.job SET heartbeat_at = clock_timestamp(), progress_done = coalesce(%s, progress_done), "
        "progress_total = coalesce(%s, progress_total) WHERE id = %s AND locked_by = %s AND status = 'running'",
        (done, total, job_id, worker),
    )


def record_attempt(conn: psycopg.Connection, job_id: int, failures: dict[str, str],
                   counted: tuple[int, int] | None) -> None:
    """What the attempt that just ended failed on, item by item, and how far it got."""
    done, total = counted if counted is not None else (None, None)
    conn.execute(
        "UPDATE jobs.job SET failures = %s, progress_done = coalesce(%s, progress_done), "
        "progress_total = coalesce(%s, progress_total) WHERE id = %s",
        (Jsonb(failures), done, total, job_id),
    )


def add_line(conn: psycopg.Connection, job_id: int, attempt: int, line: str) -> None:
    conn.execute("INSERT INTO jobs.job_line (job_id, attempt, line) VALUES (%s, %s, %s)", (job_id, attempt, line))


@dataclass(frozen=True)
class JobLine:
    id: int
    attempt: int
    at: datetime
    line: str


def forget_lines(conn: psycopg.Connection, older_than: timedelta) -> int:
    """Delete the progress lines of jobs that ended longer ago than `older_than`; returns how many."""
    cursor = conn.execute(
        "DELETE FROM jobs.job_line l USING jobs.job j WHERE j.id = l.job_id AND j.finished_at < clock_timestamp() - %s",
        (older_than,),
    )
    return cursor.rowcount


def lines(conn: psycopg.Connection, job_id: int, after: int = 0, limit: int | None = None) -> list[JobLine]:
    """The job's progress lines with an id above `after`, in order (the first `limit` of them)."""
    with conn.cursor(row_factory=class_row(JobLine)) as cursor:
        return cursor.execute("SELECT id, attempt, at, line FROM jobs.job_line WHERE job_id = %s AND id > %s "
                              "ORDER BY id LIMIT %s", (job_id, after, limit)).fetchall()


def last_line_id(conn: psycopg.Connection, job_id: int) -> int:
    """The id of the job's newest line (0 without any): where its page's polling starts."""
    row = conn.execute("SELECT coalesce(max(id), 0) FROM jobs.job_line WHERE job_id = %s", (job_id,)).fetchone()
    return row[0]


# The lines a job's page shows that say something went wrong
PROBLEM_LINES = r"(fail|error|stopped|stuck|warning|can't|not read|not moved)"


def line_count(conn: psycopg.Connection, job_id: int, problems: bool = False) -> int:
    """How many lines the job wrote (only those that say something went wrong, with `problems`)."""
    row = conn.execute(
        "SELECT count(*)::int FROM jobs.job_line WHERE job_id = %s AND (NOT %s OR line ~* %s)",
        (job_id, problems, PROBLEM_LINES),
    ).fetchone()
    return row[0]


def line_page(conn: psycopg.Connection, job_id: int, offset: int, limit: int, problems: bool = False) -> list[JobLine]:
    """One page of the job's lines, oldest first (only those that say something went wrong, with `problems`)."""
    with conn.cursor(row_factory=class_row(JobLine)) as cursor:
        return cursor.execute(
            "SELECT id, attempt, at, line FROM jobs.job_line WHERE job_id = %s AND (NOT %s OR line ~* %s) "
            "ORDER BY id OFFSET %s LIMIT %s",
            (job_id, problems, PROBLEM_LINES, offset, limit),
        ).fetchall()


def cancel_requested(conn: psycopg.Connection, job_id: int) -> bool:
    row = conn.execute("SELECT cancel_requested FROM jobs.job WHERE id = %s", (job_id,)).fetchone()
    return row is not None and row[0]


def finish(conn: psycopg.Connection, job_id: int, status: str, result: dict, error: str | None = None) -> None:
    """End the job: succeeded, partial, failed or cancelled."""
    conn.execute(
        "UPDATE jobs.job SET status = %s, result = %s, last_error = %s, finished_at = clock_timestamp(), "
        "locked_by = NULL WHERE id = %s",
        (status, Jsonb(result), error, job_id),
    )


def hold(conn: psycopg.Connection, job_id: int, result: dict, reason: str) -> None:
    """Held by the spending cap: not a failed attempt, so the claim's count is given back."""
    conn.execute(
        "UPDATE jobs.job SET status = 'held', result = %s, last_error = %s, attempts = attempts - 1, "
        "locked_by = NULL WHERE id = %s",
        (Jsonb(result), reason, job_id),
    )


def try_later(conn: psycopg.Connection, job_id: int, delay: timedelta, result: dict, error: str) -> None:
    """A failed attempt with attempts left: queued again after `delay`."""
    conn.execute(
        "UPDATE jobs.job SET status = 'queued', run_after = clock_timestamp() + %s, result = %s, last_error = %s, "
        "locked_by = NULL WHERE id = %s",
        (delay, Jsonb(result), error, job_id),
    )


def requeue_stalled(conn: psycopg.Connection, older_than: timedelta) -> list[int]:
    """Jobs marked running whose worker stopped saying it is alive (it died): their attempt counts as
    failed; with attempts left they are queued again, else they fail. Returns the jobs touched."""
    rows = conn.execute(
        """
        UPDATE jobs.job SET
               status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
               finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE clock_timestamp() END,
               run_after = clock_timestamp(), locked_by = NULL,
               last_error = 'the worker running it stopped (no heartbeat since ' || heartbeat_at::text || ')'
         WHERE status = 'running' AND heartbeat_at < clock_timestamp() - %s
        RETURNING id
        """,
        (older_than,),
    ).fetchall()
    touched = []
    for row in rows:
        touched.append(row[0])
    return touched


def resume_held(conn: psycopg.Connection) -> list[int]:
    """Queue every held job again (the cap allows spending again)."""
    rows = conn.execute("UPDATE jobs.job SET status = 'queued', run_after = clock_timestamp(), last_error = NULL "
                        "WHERE status = 'held' RETURNING id").fetchall()
    resumed = []
    for row in rows:
        resumed.append(row[0])
    return resumed


# --- what people do on /admin/jobs ---------------------------------------------------------------

def request_cancel(conn: psycopg.Connection, job_id: int) -> str | None:
    """Cancel a job: one not running (queued, held) ends at once; a running one stops after the trial or
    page it is on. Returns its status after, None if it has ended."""
    with conn.transaction():
        row = conn.execute("SELECT status FROM jobs.job WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
        if row is None or row[0] in ENDED:
            return None
        if row[0] == "running":
            conn.execute("UPDATE jobs.job SET cancel_requested = true WHERE id = %s", (job_id,))
            return "running"
        conn.execute("UPDATE jobs.job SET status = 'cancelled', cancel_requested = true, finished_at = clock_timestamp() "
                     "WHERE id = %s", (job_id,))
    return "cancelled"


def retry_now(conn: psycopg.Connection, job_id: int) -> bool:
    """Queue a failed, partial, cancelled or held job again now, with one more attempt. False when it is not one."""
    cursor = conn.execute(
        """
        UPDATE jobs.job SET status = 'queued', run_after = clock_timestamp(), finished_at = NULL,
               cancel_requested = false, max_attempts = greatest(max_attempts, attempts + 1)
         WHERE id = %s AND status IN ('failed', 'partial', 'cancelled', 'held')
        """,
        (job_id,),
    )
    return cursor.rowcount == 1


def job_cost(conn: psycopg.Connection, job_id: int) -> Decimal:
    """What the job's billed calls cost."""
    row = conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM core.llm_call WHERE job_id = %s", (job_id,)).fetchone()
    return row[0]


def job_costs(conn: psycopg.Connection, job_ids: list[int]) -> dict[int, Decimal]:
    """Job id -> what it cost."""
    rows = conn.execute(
        "SELECT j.id, (SELECT coalesce(sum(c.cost_usd), 0) FROM core.llm_call c WHERE c.job_id = j.id) "
        "FROM jobs.job j WHERE j.id = ANY(%s)",
        (job_ids,),
    ).fetchall()
    costs = {}
    for row in rows:
        costs[row[0]] = row[1]
    return costs


@dataclass(frozen=True)
class JobRuns:
    check_runs: list[int]
    ingest_runs: list[int]


def runs_of(conn: psycopg.Connection, job_id: int) -> JobRuns:
    check_rows = conn.execute("SELECT id FROM checks.run WHERE job_id = %s ORDER BY id", (job_id,)).fetchall()
    ingest_rows = conn.execute("SELECT id FROM core.ingest_run WHERE job_id = %s ORDER BY id", (job_id,)).fetchall()
    check_runs = []
    for row in check_rows:
        check_runs.append(row[0])
    ingest_runs = []
    for row in ingest_rows:
        ingest_runs.append(row[0])
    return JobRuns(check_runs, ingest_runs)


def trial_errors_of(conn: psycopg.Connection, job_id: int) -> dict[int, str | None]:
    """Trial id -> the error of its newest judgement in the job's check runs (None: judged)."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (j.trial_id) j.trial_id, j.error FROM checks.judgement j
          JOIN checks.run r ON r.id = j.run_id WHERE r.job_id = %s ORDER BY j.trial_id, j.run_id DESC
        """,
        (job_id,),
    ).fetchall()
    errors = {}
    for row in rows:
        errors[row[0]] = row[1]
    return errors


def keys_transferred_since(conn: psycopg.Connection, source: str, keys: list[str], since: datetime) -> set[str]:
    """The keys among these whose evidence transfer completed at or after `since`."""
    rows = conn.execute(
        """
        SELECT e.source_key FROM core.evidence_transfer e JOIN core.source s ON s.id = e.source_id
         WHERE s.code = %s AND e.source_key = ANY(%s) AND e.status = 'complete' AND e.transferred_at >= %s
        """,
        (source, keys, since),
    ).fetchall()
    done = set()
    for row in rows:
        done.add(row[0])
    return done


# --- schedules -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Schedule:
    id: int
    name: str
    kind: str
    params: dict
    # daily | weekly | monthly (on the 1st); weekday counts only when weekly
    frequency: str
    weekday: int
    hour_utc: int
    enabled: bool
    next_run_at: datetime
    # the newest job it started, if any
    last_job_id: int | None
    last_job_status: str | None


_SCHEDULES = """
    SELECT s.id, s.name, s.kind, s.params, s.frequency, s.weekday, s.hour_utc, s.enabled, s.next_run_at,
           last.id AS last_job_id, last.status AS last_job_status
      FROM jobs.schedule s
      LEFT JOIN LATERAL (SELECT id, status FROM jobs.job WHERE schedule_id = s.id ORDER BY id DESC LIMIT 1) last ON true
"""


def schedules(conn: psycopg.Connection) -> list[Schedule]:
    with conn.cursor(row_factory=class_row(Schedule)) as cursor:
        return cursor.execute(_SCHEDULES + " ORDER BY s.id").fetchall()


def schedule(conn: psycopg.Connection, schedule_id: int) -> Schedule | None:
    with conn.cursor(row_factory=class_row(Schedule)) as cursor:
        return cursor.execute(_SCHEDULES + " WHERE s.id = %s", (schedule_id,)).fetchone()


@dataclass(frozen=True)
class ScheduleTimes:
    """For the home page: the newest job a schedule started, and when the next one is due."""
    last_name: str | None
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_status: str | None
    next_name: str | None
    next_run_at: datetime | None


def schedule_times(conn: psycopg.Connection) -> ScheduleTimes:
    row = conn.execute(
        """
        SELECT last.name, last.started_at, last.finished_at, last.status, next.name, next.next_run_at
          FROM (SELECT 1) one
          LEFT JOIN LATERAL (SELECT s.name, j.started_at, j.finished_at, j.status
                               FROM jobs.job j JOIN jobs.schedule s ON s.id = j.schedule_id
                              ORDER BY j.id DESC LIMIT 1) last ON true
          LEFT JOIN LATERAL (SELECT name, next_run_at FROM jobs.schedule
                              WHERE enabled ORDER BY next_run_at LIMIT 1) next ON true
        """
    ).fetchone()
    return ScheduleTimes(*row)


FREQUENCIES = ("daily", "weekly", "monthly")

# The next time the schedule's frequency, weekday (ISO, 1 Monday; weekly only) and hour come round after
# now, UTC: today's, this week's or this month's time, or the one after it if that has passed
_NEXT_TIME = """
    (SELECT (CASE WHEN base <= now() AT TIME ZONE 'UTC' THEN base + step ELSE base END) AT TIME ZONE 'UTC'
       FROM (SELECT CASE %(frequency)s
                      WHEN 'daily' THEN date_trunc('day', now() AT TIME ZONE 'UTC')
                      WHEN 'monthly' THEN date_trunc('month', now() AT TIME ZONE 'UTC')
                      ELSE date_trunc('week', now() AT TIME ZONE 'UTC') + make_interval(days => %(weekday)s - 1)
                    END + make_interval(hours => %(hour)s) AS base,
                    CASE %(frequency)s WHEN 'daily' THEN interval '1 day' WHEN 'monthly' THEN interval '1 month'
                                       ELSE interval '7 days' END AS step) times)
"""


def set_schedule(conn: psycopg.Connection, schedule_id: int, *, frequency: str, weekday: int, hour_utc: int,
                 enabled: bool) -> None:
    """Change how often and when a schedule runs (its next time follows), or switch it on or off."""
    conn.execute(
        f"UPDATE jobs.schedule SET frequency = %(frequency)s, weekday = %(weekday)s, hour_utc = %(hour)s, "
        f"enabled = %(enabled)s, next_run_at = {_NEXT_TIME} WHERE id = %(id)s",
        {"frequency": frequency, "weekday": weekday, "hour": hour_utc, "enabled": enabled, "id": schedule_id},
    )


def switch_schedule(conn: psycopg.Connection, schedule_id: int, enabled: bool) -> None:
    """Switch a schedule on (its next time counted from now) or off, keeping when it runs."""
    row = conn.execute("SELECT frequency, weekday, hour_utc FROM jobs.schedule WHERE id = %s", (schedule_id,)).fetchone()
    if row is None:
        return
    set_schedule(conn, schedule_id, frequency=row[0], weekday=row[1], hour_utc=row[2], enabled=enabled)


def take_due_schedule(conn: psycopg.Connection) -> Schedule | None:
    """A switched-on schedule whose time has come, its next time moved on by its frequency, or None. Call
    it in a transaction that also adds its job: the row stays locked until then, so two workers never
    both start it."""
    row = conn.execute(
        "SELECT id, frequency, weekday, hour_utc FROM jobs.schedule WHERE enabled AND next_run_at <= now() "
        "ORDER BY next_run_at LIMIT 1 FOR UPDATE SKIP LOCKED"
    ).fetchone()
    if row is None:
        return None
    conn.execute(f"UPDATE jobs.schedule SET next_run_at = {_NEXT_TIME} WHERE id = %(id)s",
                 {"frequency": row[1], "weekday": row[2], "hour": row[3], "id": row[0]})
    return schedule(conn, row[0])
