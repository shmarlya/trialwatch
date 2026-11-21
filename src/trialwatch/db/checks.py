"""Queries on the checks schema: the evidence check's runs, questions, evidence, judgements and
traces (see db/migrations/*_init.sql). Trials are core.trial ids (ADR-003)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

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
               input_tokens: int, output_tokens: int, cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> None:
    """Close the run; its cost is the sum of its billed calls (core.api_call)."""
    with conn.transaction():
        conn.execute(
            "UPDATE checks.run SET status = %s, error = %s, finished_at = clock_timestamp(), trials = %s, errors = %s, "
            "input_tokens = %s, output_tokens = %s, cache_write_tokens = %s, cache_read_tokens = %s, "
            "cost_usd = (SELECT coalesce(sum(cost_usd), 0) FROM core.api_call WHERE check_run_id = %s) WHERE id = %s",
            (status, error, trials, errors, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens,
             run_id, run_id),
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
                                     WHERE j.run_id = r.id),
                    cost_usd = (SELECT coalesce(sum(c.cost_usd), 0) FROM core.api_call c WHERE c.check_run_id = r.id)
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
                   input_tokens: int, output_tokens: int, duration_ms: int, error: str | None,
                   cache_write_tokens: int = 0, cache_read_tokens: int = 0, cost_usd: Decimal | None = None) -> None:
    # An empty or missing trace is stored as NULL, not as an empty JSON object
    trace_json = None
    if trace:
        trace_json = Jsonb(trace)
    conn.execute(
        "INSERT INTO checks.judgement (run_id, trial_id, parts, label, trace, served_by, trial_record_id, "
        "document_versions, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd, "
        "duration_ms, error) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (run_id, trial_id, Jsonb(parts), label, trace_json, served_by, record_id, list(document_versions),
         input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd, duration_ms, error),
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
    cost_usd: Decimal | None
    # when the newest document version it read was published, as its source says (None: unknown)
    latest_published_at: datetime | None


def judgements(conn: psycopg.Connection, run_id: int) -> dict[int, RunJudgement]:
    """Trial id -> its judgement in the run."""
    with conn.cursor(row_factory=class_row(RunJudgement)) as cur:
        rows = cur.execute(
            """
            SELECT j.trial_id, j.parts, j.label, j.error, j.input_tokens, j.output_tokens, j.cost_usd,
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


# --- publication (ADR-007): which judgements the public sees ---------------------------------------

def publish_judgement(conn: psycopg.Connection, run_id: int, trial_id: int, confidence: float | None = None,
                      basis: str | None = None) -> bool:
    """Make the judgement public, as the session's actor: reviewed by a person (no confidence), or
    published by a run with the measured confidence and its basis (checks/publishing.py). False if it
    is published already; the database refuses one with an error or of a run still running
    (checks.publishable)."""
    with conn.transaction():
        cursor = conn.execute(
            """
            INSERT INTO checks.publication (run_id, trial_id, published_by, confidence, basis)
            SELECT %(run)s, %(trial)s, current_setting('trialwatch.actor')::bigint, %(confidence)s, %(basis)s
            WHERE NOT EXISTS (SELECT FROM checks.publication
                              WHERE run_id = %(run)s AND trial_id = %(trial)s AND withdrawn_at IS NULL)
            """,
            {"run": run_id, "trial": trial_id, "confidence": confidence, "basis": basis},
        )
    return cursor.rowcount == 1


def withdraw_publication(conn: psycopg.Connection, run_id: int, trial_id: int, note: str | None) -> bool:
    """Take the judgement back from the public, as the session's actor (the publication is kept, marked
    withdrawn). False if it was not published."""
    with conn.transaction():
        cursor = conn.execute(
            """
            UPDATE checks.publication
               SET withdrawn_at = clock_timestamp(), withdrawn_by = current_setting('trialwatch.actor')::bigint,
                   note = coalesce(%s, note)
             WHERE run_id = %s AND trial_id = %s AND withdrawn_at IS NULL
            """,
            (note, run_id, trial_id),
        )
    return cursor.rowcount == 1


@dataclass(frozen=True)
class JudgementKind:
    """What a judgement's confidence is measured by (checks/publishing.py): the exact check that made
    it, its label, and how its evidence came in."""

    run_id: int
    trial_id: int
    label: str | None
    model: str
    prompt_sha256: bytes
    requirements: str
    # every quote the model gave was found on its page
    quotes_verified: bool
    # a question had to be asked again with every page
    widened: bool
    # a question got no answer, or "don't know"
    unanswered: bool
    # when the newest document version it read was posted, as its source says
    latest_published_at: datetime | None


_KINDS = """
    SELECT j.run_id, j.trial_id, j.label, r.model, r.prompt_sha256, r.requirements,
           coalesce(bool_and(e.verified) FILTER (WHERE e.found), true) AS quotes_verified,
           coalesce(bool_or(q.widened), false) AS widened,
           coalesce(bool_or(q.key IS NOT NULL AND (e.id IS NULL OR e.found IS NULL)), false) AS unanswered,
           (SELECT max(v.published_at) FROM core.document_version v WHERE v.id = ANY(j.document_versions))
               AS latest_published_at
    FROM checks.judgement j
    JOIN checks.run r ON r.id = j.run_id
    LEFT JOIN checks.question q ON q.run_id = j.run_id AND q.trial_id = j.trial_id
    LEFT JOIN checks.evidence e ON e.run_id = q.run_id AND e.trial_id = q.trial_id AND e.key = q.key
    WHERE j.error IS NULL AND r.status <> 'running'
"""


def judgement_kinds_of_run(conn: psycopg.Connection, run_id: int) -> list[JudgementKind]:
    """The kind of every finished judgement without an error in the run."""
    with conn.cursor(row_factory=class_row(JudgementKind)) as cur:
        return cur.execute(_KINDS + " AND j.run_id = %s GROUP BY j.run_id, j.trial_id, r.id ORDER BY j.trial_id",
                           (run_id,)).fetchall()


def judgement_kinds_of_trials(conn: psycopg.Connection, trial_ids: list[int], requirements: str) -> list[JudgementKind]:
    """The kind of every finished judgement without an error of these trials under the requirement
    set, newest run first."""
    with conn.cursor(row_factory=class_row(JudgementKind)) as cur:
        return cur.execute(_KINDS + " AND j.trial_id = ANY(%s) AND r.requirements = %s "
                           "GROUP BY j.run_id, j.trial_id, r.id ORDER BY j.run_id DESC",
                           (trial_ids, requirements)).fetchall()


def judgement_kind(conn: psycopg.Connection, run_id: int, trial_id: int) -> JudgementKind | None:
    with conn.cursor(row_factory=class_row(JudgementKind)) as cur:
        return cur.execute(_KINDS + " AND j.run_id = %s AND j.trial_id = %s GROUP BY j.run_id, j.trial_id, r.id",
                           (run_id, trial_id)).fetchone()


@dataclass(frozen=True)
class RetrievalRow:
    """One question of one trial in a run, with what retrieval showed the model first (the trace)."""

    trial_id: int
    key: str
    widened: bool
    found: bool | None
    verified: bool | None
    file_sha256: bytes | None
    page_no: int | None
    # the trial's retrieval step: whole documents sent, or pages searched per question
    whole: bool | None
    # the pages of the trial's first model call, as {"file": hex, "page": n}
    first_pages: list | None


def retrieval_rows(conn: psycopg.Connection, run_id: int) -> list[RetrievalRow]:
    """Every question of the run's judgements without an error, for measuring retrieval (gold/evaluate.py)."""
    with conn.cursor(row_factory=class_row(RetrievalRow)) as cur:
        return cur.execute(
            """
            SELECT q.trial_id, q.key, q.widened, e.found, e.verified, e.file_sha256, e.page_no,
                   (SELECT (s.data->>'whole')::boolean FROM checks.step s
                     WHERE s.run_id = q.run_id AND s.trial_id = q.trial_id AND s.name = 'retrieval'
                     ORDER BY s.seq LIMIT 1) AS whole,
                   (SELECT s.data->'pages' FROM checks.step s
                     WHERE s.run_id = q.run_id AND s.trial_id = q.trial_id AND s.name = 'call'
                     ORDER BY s.seq LIMIT 1) AS first_pages
            FROM checks.question q
            JOIN checks.judgement j ON j.run_id = q.run_id AND j.trial_id = q.trial_id AND j.error IS NULL
            LEFT JOIN checks.evidence e ON e.run_id = q.run_id AND e.trial_id = q.trial_id AND e.key = q.key
            WHERE q.run_id = %s
            ORDER BY q.trial_id, q.key
            """,
            (run_id,),
        ).fetchall()


def trials_needing_check(conn: psycopg.Connection, trial_ids: list[int], requirements: str) -> list[int]:
    """The trials among these that have never been judged against the requirement set, or whose current
    judgement is outdated (checks.judgement_current: a record or document it read changed since)."""
    rows = conn.execute(
        """
        SELECT t.id FROM unnest(%(ids)s::bigint[]) AS t(id)
         WHERE NOT EXISTS (SELECT FROM checks.judgement_current c
                            WHERE c.trial_id = t.id AND c.requirements = %(requirements)s AND NOT c.outdated)
         ORDER BY t.id
        """,
        {"ids": trial_ids, "requirements": requirements},
    ).fetchall()
    found = []
    for row in rows:
        found.append(row[0])
    return found
