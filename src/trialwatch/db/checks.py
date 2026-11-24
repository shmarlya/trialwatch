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
    model: str
    effort: str
    prompt: str
    requirements: str
    status: str


def start_run(conn: psycopg.Connection, *, model: str, effort: str, prompt: str,
              prompt_sha256: bytes, prompt_text: str, requirements: str, requirements_sha256: bytes,
              requirements_text: str, params: dict) -> int:
    """Open a run as the session's actor, with the exact prompt and requirement set it uses. The
    session holds the run's lock until finish_run, so close_abandoned_runs can tell it is alive."""
    with conn.transaction():
        cursor = conn.execute(
            "INSERT INTO checks.run (actor_id, model, effort, prompt, prompt_sha256, prompt_text, "
            "requirements, requirements_sha256, requirements_text, params) "
            "VALUES (current_setting('trialwatch.actor')::bigint, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (model, effort, prompt, prompt_sha256, prompt_text, requirements, requirements_sha256,
             requirements_text, Jsonb(params)),
        )
        row = cursor.fetchone()
        run_id = row[0]
        lock(conn, LOCK_CHECK_RUN, run_id)
    return run_id


def finish_run(conn: psycopg.Connection, run_id: int, *, status: str, error: str | None, trials: int,
               errors: int) -> None:
    """Close the run (its tokens and cost are its calls', core.llm_call)."""
    with conn.transaction():
        conn.execute(
            "UPDATE checks.run SET status = %s, error = %s, finished_at = clock_timestamp(), trials = %s, errors = %s "
            "WHERE id = %s",
            (status, error, trials, errors, run_id),
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
                    errors = (SELECT count(*) FROM checks.judgement j WHERE j.run_id = r.id AND j.error IS NOT NULL)
                WHERE r.id = %s AND r.status = 'running'
                """,
                (ABANDONED, run_id),
            )
            if cursor.rowcount == 1:
                closed.append(run_id)
        finally:
            unlock(conn, LOCK_CHECK_RUN, run_id)
    return closed


def runs_without_agreement(conn: psycopg.Connection) -> list[int]:
    """Finished runs with a public judgement whose gold agreement was never recorded: a run whose
    process died (close_abandoned_runs), or whose publishing failed after it finished."""
    rows = conn.execute(
        """
        SELECT DISTINCT r.id FROM checks.run r JOIN checks.judgement j ON j.run_id = r.id
         WHERE r.status <> 'running' AND j.error IS NULL AND j.agreement IS NULL
         ORDER BY r.id
        """
    ).fetchall()
    return [row[0] for row in rows]


def run_config(conn: psycopg.Connection, run_id: int) -> RunConfig | None:
    with conn.cursor(row_factory=class_row(RunConfig)) as cur:
        return cur.execute(
            "SELECT id, model, effort, prompt, requirements, status FROM checks.run WHERE id = %s",
            (run_id,),
        ).fetchone()


@dataclass(frozen=True)
class Answer:
    """The answer the model gave to one question, its quote checked against its page."""

    # True: found; False: not found; None: the model said it doesn't know
    found: bool | None
    kind: str | None
    quote: str | None
    document_version_id: int | None
    file_sha256: bytes | None
    page_no: int | None
    note: str | None
    verified: bool


def save_answer(conn: psycopg.Connection, run_id: int, trial_id: int, key: str, item_text: str | None,
                pages: list[dict], widened: bool, answer: Answer | None) -> int | None:
    """One question asked about a trial, with the model's answer (None: it gave none). Returns the
    answer's evidence id, which the rules trace names, or None."""
    if answer is None:
        conn.execute(
            "INSERT INTO checks.answer (run_id, trial_id, key, item_text, pages, widened) VALUES (%s, %s, %s, %s, %s, %s)",
            (run_id, trial_id, key, item_text, Jsonb(pages), widened),
        )
        return None
    cursor = conn.execute(
        "INSERT INTO checks.answer (run_id, trial_id, key, item_text, pages, widened, evidence_id, found, kind, quote, "
        "document_version_id, file_sha256, page_no, note, verified) "
        "VALUES (%s, %s, %s, %s, %s, %s, nextval('checks.answer_evidence_id_seq'), %s, %s, %s, %s, %s, %s, %s, %s) "
        "RETURNING evidence_id",
        (run_id, trial_id, key, item_text, Jsonb(pages), widened, answer.found, answer.kind, answer.quote,
         answer.document_version_id, answer.file_sha256, answer.page_no, answer.note, answer.verified),
    )
    row = cursor.fetchone()
    return row[0]


def save_judgement(conn: psycopg.Connection, run_id: int, trial_id: int, *, parts: dict, label: str | None,
                   served_by: list[str], record_id: int | None, document_versions: list[int], duration_ms: int,
                   error: str | None) -> None:
    conn.execute(
        "INSERT INTO checks.judgement (run_id, trial_id, parts, label, served_by, trial_record_id, document_versions, "
        "duration_ms, error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (run_id, trial_id, Jsonb(parts), label, served_by, record_id, list(document_versions), duration_ms, error),
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
            SELECT j.trial_id, j.parts, j.label, j.error, c.input_tokens, c.output_tokens, c.cost_usd,
                   (SELECT max(v.published_at) FROM core.document_version v WHERE v.id = ANY(j.document_versions))
                       AS latest_published_at
            FROM checks.judgement j
            CROSS JOIN LATERAL (SELECT sum(input_tokens)::int AS input_tokens, sum(output_tokens)::int AS output_tokens,
                                       sum(cost_usd) AS cost_usd FROM core.llm_call
                                 WHERE check_run_id = j.run_id AND trial_id = j.trial_id) c
            WHERE j.run_id = %s
            """,
            (run_id,),
        ).fetchall()
    by_trial = {}
    for row in rows:
        by_trial[row.trial_id] = row
    return by_trial


# --- publication (ADR-007): every judgement without an error of a finished run is public -------------

def set_agreement(conn: psycopg.Connection, run_id: int, trial_id: int, confidence: float | None, basis: str) -> bool:
    """Record how judgements of this one's kind agreed with gold labels when its run finished
    (checks/publishing.py). False if the judgement has an error (it is not public)."""
    cursor = conn.execute(
        "UPDATE checks.judgement SET agreement = %s WHERE run_id = %s AND trial_id = %s AND error IS NULL",
        (Jsonb({"confidence": confidence, "basis": basis}), run_id, trial_id),
    )
    return cursor.rowcount == 1


def withdraw_judgement(conn: psycopg.Connection, run_id: int, trial_id: int, reason: str) -> bool:
    """Withdraw a public judgement, as the session's actor, with the reason the public sees: it stays
    public, marked withdrawn (audited). False if it is not public or was withdrawn already."""
    with conn.transaction():
        cursor = conn.execute(
            """
            UPDATE checks.judgement j
               SET withdrawn_at = clock_timestamp(), withdrawn_by = current_setting('trialwatch.actor')::bigint,
                   withdrawn_reason = %s
              FROM checks.run r
             WHERE r.id = j.run_id AND j.run_id = %s AND j.trial_id = %s
               AND j.error IS NULL AND r.status <> 'running' AND j.withdrawn_at IS NULL
            """,
            (reason, run_id, trial_id),
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
           coalesce(bool_and(a.verified) FILTER (WHERE a.found), true) AS quotes_verified,
           coalesce(bool_or(a.widened), false) AS widened,
           coalesce(bool_or(a.key IS NOT NULL AND (a.evidence_id IS NULL OR a.found IS NULL)), false) AS unanswered,
           (SELECT max(v.published_at) FROM core.document_version v WHERE v.id = ANY(j.document_versions))
               AS latest_published_at
    FROM checks.judgement j
    JOIN checks.run r ON r.id = j.run_id
    LEFT JOIN checks.answer a ON a.run_id = j.run_id AND a.trial_id = j.trial_id
    WHERE j.error IS NULL AND r.status <> 'running'
"""


def judgement_kinds_of_run(conn: psycopg.Connection, run_id: int) -> list[JudgementKind]:
    """The kind of every finished judgement without an error in the run."""
    with conn.cursor(row_factory=class_row(JudgementKind)) as cur:
        return cur.execute(_KINDS + " AND j.run_id = %s GROUP BY j.run_id, j.trial_id, r.id ORDER BY j.trial_id",
                           (run_id,)).fetchall()


def judgement_kinds_of_trials(conn: psycopg.Connection, trial_ids: list[int], requirements: str,
                              before_run: int | None = None) -> list[JudgementKind]:
    """The kind of every finished judgement without an error of these trials under the requirement
    set, newest run first; with `before_run`, only of runs started before that one."""
    with conn.cursor(row_factory=class_row(JudgementKind)) as cur:
        return cur.execute(_KINDS + " AND j.trial_id = ANY(%(trials)s) AND r.requirements = %(requirements)s"
                           " AND (%(before_run)s::bigint IS NULL OR r.id < %(before_run)s)"
                           " GROUP BY j.run_id, j.trial_id, r.id ORDER BY j.run_id DESC",
                           {"trials": trial_ids, "requirements": requirements, "before_run": before_run}).fetchall()


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
            SELECT a.trial_id, a.key, a.widened, a.found, a.verified, a.file_sha256, a.page_no,
                   (SELECT (s.data->>'whole')::boolean FROM checks.step s
                     WHERE s.run_id = a.run_id AND s.trial_id = a.trial_id AND s.name = 'retrieval'
                     ORDER BY s.seq LIMIT 1) AS whole,
                   (SELECT s.data->'pages' FROM checks.step s
                     WHERE s.run_id = a.run_id AND s.trial_id = a.trial_id AND s.name = 'call'
                     ORDER BY s.seq LIMIT 1) AS first_pages
            FROM checks.answer a
            JOIN checks.judgement j ON j.run_id = a.run_id AND j.trial_id = a.trial_id AND j.error IS NULL
            WHERE a.run_id = %s
            ORDER BY a.trial_id, a.key
            """,
            (run_id,),
        ).fetchall()


@dataclass(frozen=True)
class AnswerDetail:
    """One question of one trial in a run: the model's answer and what the rules made of it."""

    trial_id: int
    key: str
    item_text: str | None
    widened: bool
    # False: the model gave no answer to it
    answered: bool
    found: bool | None
    kind: str | None
    quote: str | None
    page_no: int | None
    note: str | None
    verified: bool | None
    # from the trial's rules step: the question's value (None: undecided) and the rule that set it
    value: bool | None
    rule: str | None


def answer_details(conn: psycopg.Connection, run_id: int) -> list[AnswerDetail]:
    """Every question the run asked, with its answer and its rule (gold/compare.py shows them for the
    items a gold evaluation disagrees on or leaves open)."""
    with conn.cursor(row_factory=class_row(AnswerDetail)) as cur:
        return cur.execute(
            """
            SELECT a.trial_id, a.key, a.item_text, a.widened, a.evidence_id IS NOT NULL AS answered, a.found, a.kind,
                   a.quote, a.page_no, a.note, a.verified,
                   (s.data->'trace'->a.key->>'value')::boolean AS value, s.data->'trace'->a.key->>'rule' AS rule
            FROM checks.answer a
            LEFT JOIN checks.step s ON s.run_id = a.run_id AND s.trial_id = a.trial_id AND s.name = 'rules'
            WHERE a.run_id = %s
            ORDER BY a.trial_id, a.key
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


def failed_on_unchanged_evidence(conn: psycopg.Connection, trial_ids: list[int], requirements: str) -> dict[int, str]:
    """The trials among these whose newest judgement against the requirement set ended in an error and
    read the evidence that is still current (nothing it read changed since, as in
    checks.judgement_current), each with its error."""
    rows = conn.execute(
        """
        SELECT trial_id, error FROM (
            SELECT DISTINCT ON (j.trial_id) j.trial_id, j.error,
                   (EXISTS (SELECT FROM core.document_version v
                            JOIN core.trial_document l ON l.document_id = v.document_id AND l.trial_id = j.trial_id
                            WHERE v.id = ANY (j.document_versions)
                              AND (v.replaced_at IS NOT NULL OR l.removed_at IS NOT NULL))
                    OR EXISTS (SELECT FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id
                               WHERE l.trial_id = j.trial_id AND l.removed_at IS NULL
                                 AND NOT (d.version_id = ANY (j.document_versions))
                                 AND d.kind IN (SELECT jsonb_array_elements_text(r.requirements_text::jsonb
                                                                                 -> 'document_kinds')))
                    OR EXISTS (SELECT FROM core.trial_record_current c
                               WHERE c.trial_id = j.trial_id AND c.id IS DISTINCT FROM j.trial_record_id
                                 AND c.fetched_at > r.started_at)) AS outdated
              FROM checks.judgement j JOIN checks.run r ON r.id = j.run_id
             WHERE j.trial_id = ANY(%(ids)s) AND r.requirements = %(requirements)s AND r.status <> 'running'
             ORDER BY j.trial_id, j.run_id DESC
        ) newest
        WHERE error IS NOT NULL AND NOT outdated
        """,
        {"ids": trial_ids, "requirements": requirements},
    ).fetchall()
    found = {}
    for row in rows:
        found[row[0]] = row[1]
    return found
