"""Queries on the checks schema: the evidence check's runs, questions, evidence, judgements and
traces (see db/migrations/*_init.sql). Trials are core.trial ids (ADR-003)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from trialwatch.db.core import ABANDONED, LOCK_CHECK_RUN, lock, try_lock, unlock


@dataclass(frozen=True)
class RunConfig:
    id: int
    check_name: str
    model: str
    effort: str
    prompt: str
    requirements: str
    status: str


def start_run(conn: psycopg.Connection, *, check_name: str, model: str, effort: str, prompt: str,
              prompt_sha256: bytes, prompt_text: str, requirements: str, requirements_sha256: bytes,
              requirements_text: str, params: dict) -> int:
    """Open a run as the session's actor, with the exact prompt and requirement set it uses. The
    session holds the run's lock until finish_run, so close_abandoned_runs can tell it is alive."""
    with conn.transaction():
        cursor = conn.execute(
            "INSERT INTO checks.run (check_name, actor_id, model, effort, prompt, prompt_sha256, prompt_text, "
            "requirements, requirements_sha256, requirements_text, params) "
            "VALUES (%s, current_setting('trialwatch.actor')::bigint, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (check_name, model, effort, prompt, prompt_sha256, prompt_text, requirements, requirements_sha256,
             requirements_text, Jsonb(params)),
        )
        row = cursor.fetchone()
        run_id = row[0]
        lock(conn, LOCK_CHECK_RUN, run_id)
    return run_id


def finish_run(conn: psycopg.Connection, run_id: int, *, status: str, error: str | None, trials: int, errors: int,
               input_tokens: int, output_tokens: int) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE checks.run SET status = %s, error = %s, finished_at = clock_timestamp(), trials = %s, errors = %s, "
            "input_tokens = %s, output_tokens = %s WHERE id = %s",
            (status, error, trials, errors, input_tokens, output_tokens, run_id),
        )
    unlock(conn, LOCK_CHECK_RUN, run_id)


def close_abandoned_runs(conn: psycopg.Connection) -> list[int]:
    """Close as failed the check runs still marked running whose process is gone (nobody holds their
    lock). The trials they judged keep their judgements. Call it before this session starts a run of
    its own. Returns the closed runs."""
    rows = conn.execute("SELECT id FROM checks.run WHERE status = 'running' ORDER BY id").fetchall()
    closed = []
    for row in rows:
        run_id = row[0]
        if not try_lock(conn, LOCK_CHECK_RUN, run_id):
            # its process is alive
            continue
        try:
            cursor = conn.execute(
                """
                UPDATE checks.run r SET status = 'failed', error = %s, finished_at = clock_timestamp(),
                    trials = (SELECT count(*) FROM checks.judgement j WHERE j.run_id = r.id),
                    errors = (SELECT count(*) FROM checks.judgement j WHERE j.run_id = r.id AND j.error IS NOT NULL),
                    input_tokens = (SELECT coalesce(sum(j.input_tokens), 0) FROM checks.judgement j WHERE j.run_id = r.id),
                    output_tokens = (SELECT coalesce(sum(j.output_tokens), 0) FROM checks.judgement j
                                     WHERE j.run_id = r.id)
                WHERE r.id = %s AND r.status = 'running'
                """,
                (ABANDONED, run_id),
            )
            if cursor.rowcount == 1:
                closed.append(run_id)
        finally:
            unlock(conn, LOCK_CHECK_RUN, run_id)
    return closed


def run_config(conn: psycopg.Connection, run_id: int) -> RunConfig | None:
    with conn.cursor(row_factory=class_row(RunConfig)) as cur:
        return cur.execute(
            "SELECT id, check_name, model, effort, prompt, requirements, status FROM checks.run WHERE id = %s",
            (run_id,),
        ).fetchone()


def save_question(conn: psycopg.Connection, run_id: int, trial_id: int, key: str, item_text: str | None,
                  pages: list[dict], widened: bool) -> None:
    conn.execute(
        "INSERT INTO checks.question (run_id, trial_id, key, item_text, pages, widened) VALUES (%s, %s, %s, %s, %s, %s)",
        (run_id, trial_id, key, item_text, Jsonb(pages), widened),
    )


def save_evidence(conn: psycopg.Connection, run_id: int, trial_id: int, key: str, *, found: bool | None,
                  kind: str | None, quote: str | None, document_version_id: int | None, file_sha256: bytes | None,
                  page_no: int | None, note: str | None, verified: bool) -> int:
    cursor = conn.execute(
        "INSERT INTO checks.evidence (run_id, trial_id, key, found, kind, quote, document_version_id, file_sha256, "
        "page_no, note, verified) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (run_id, trial_id, key, found, kind, quote, document_version_id, file_sha256, page_no, note, verified),
    )
    row = cursor.fetchone()
    return row[0]


def save_judgement(conn: psycopg.Connection, run_id: int, trial_id: int, *, parts: dict, label: str | None,
                   trace: dict | None, served_by: list[str], record_id: int | None, document_versions: list[int],
                   input_tokens: int, output_tokens: int, duration_ms: int, error: str | None) -> None:
    # An empty or missing trace is stored as NULL, not as an empty JSON object
    trace_json = None
    if trace:
        trace_json = Jsonb(trace)
    conn.execute(
        "INSERT INTO checks.judgement (run_id, trial_id, parts, label, trace, served_by, trial_record_id, "
        "document_versions, input_tokens, output_tokens, duration_ms, error) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (run_id, trial_id, Jsonb(parts), label, trace_json, served_by, record_id, list(document_versions),
         input_tokens, output_tokens, duration_ms, error),
    )


def save_steps(conn: psycopg.Connection, run_id: int, trial_id: int, steps: Sequence[dict]) -> None:
    """Store the trace of judging one trial: each step's name, time, duration and data, in order."""
    with conn.transaction():
        for seq, step in enumerate(steps, start=1):
            conn.execute(
                "INSERT INTO checks.step (run_id, trial_id, seq, name, at, duration_ms, data) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (run_id, trial_id, seq, step["name"], step["at"], step.get("duration_ms"), Jsonb(step["data"])),
            )


@dataclass(frozen=True)
class RunJudgement:
    """One trial's judgement in a run, by core trial id, with its parts by name."""

    trial_id: int
    parts: dict
    label: str | None
    error: str | None
    input_tokens: int | None
    output_tokens: int | None
    # when the newest document version it read was published, as its source says (None: unknown)
    latest_published_at: datetime | None


def judgements(conn: psycopg.Connection, run_id: int) -> dict[int, RunJudgement]:
    """Trial id -> its judgement in the run."""
    with conn.cursor(row_factory=class_row(RunJudgement)) as cur:
        rows = cur.execute(
            """
            SELECT j.trial_id, j.parts, j.label, j.error, j.input_tokens, j.output_tokens,
                   (SELECT max(v.published_at) FROM core.document_version v WHERE v.id = ANY(j.document_versions))
                       AS latest_published_at
            FROM checks.judgement j WHERE j.run_id = %s
            """,
            (run_id,),
        ).fetchall()
    by_trial = {}
    for row in rows:
        by_trial[row.trial_id] = row
    return by_trial
