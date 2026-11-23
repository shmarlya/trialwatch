"""Read-only queries for the web interface, on the central model (ADR-003) and the evidence check's
results. See db/migrations/*_init.sql."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg.rows import class_row

from trialwatch.db import core

# A page the model could have read: at least 200 letters in its text layer (checks/verify.py MIN_LETTERS)
_READABLE = "length(regexp_replace(p.text, '[^[:alpha:]]', '', 'g')) >= 200"


def like_text(search: str | None) -> str | None:
    """Search text for an ILIKE pattern: its own %, _ and backslash match themselves."""
    if search is None:
        return None
    escaped = []
    for character in search:
        if character in ("\\", "%", "_"):
            escaped.append("\\")
        escaped.append(character)
    return "".join(escaped)

# A trial's display name: its EU CT number, else its first identifier, else "trial <id>"
_NAME = """coalesce(
    (SELECT i.value FROM core.trial_identifier i WHERE i.trial_id = t.id
     ORDER BY i.scheme <> 'eu-ct', i.scheme, i.value LIMIT 1),
    'trial ' || t.id)"""
_LATEST_REGISTERED = """(SELECT r.registered FROM core.trial_record_current r WHERE r.trial_id = t.id
     ORDER BY r.fetched_at DESC LIMIT 1)"""


@dataclass(frozen=True)
class TrialRow:
    id: int
    name: str
    title: str | None
    sponsor: str | None
    status: str | None
    created_at: datetime
    created_by: str
    merged_into: int | None
    documents: int
    results_documents: int
    pages: int
    readable_pages: int
    label: str | None
    label_run: int | None
    # the current label's inputs changed since it was judged (checks.judgement_current)
    label_outdated: bool | None
    open_duplicates: int
    # the best state of its evidence at any source: complete, stale, failed or not transferred
    evidence: str


# A trial's evidence, the best state at any source
_EVIDENCE = """(SELECT CASE WHEN bool_or(e.status = 'complete') THEN 'complete'
                  WHEN bool_or(e.status = 'stale') THEN 'stale'
                  WHEN bool_or(e.status = 'failed') THEN 'failed'
                  ELSE 'not transferred' END
       FROM core.evidence_status e WHERE e.trial_id = t.id)"""
# The title a source's list gives, for trials whose record is not transferred yet
_LISTED_TITLE = """(SELECT l.title FROM core.source_listing l JOIN core.trial_source ts USING (source_id, source_key)
       WHERE ts.trial_id = t.id AND l.title IS NOT NULL ORDER BY l.last_listed_at DESC LIMIT 1)"""


def judgement_view(published_only: bool) -> str:
    """The view of one judgement per trial and requirement set: the newest of a finished run, which is
    public (ADR-007; a withdrawn one too, marked), for staff and the public alike."""
    return "checks.judgement_current"


def _trial_rows(published_only: bool) -> str:
    return f"""
    SELECT t.id, {_NAME} AS name,
           coalesce(reg.r->>'public_title', reg.r->>'full_title', {_LISTED_TITLE}) AS title,
           -- current sponsors, the primary one first
           (SELECT string_agg(o.name, ', ' ORDER BY s.is_primary DESC, o.name)
              FROM core.trial_sponsor s JOIN core.organisation o ON o.id = s.organisation_id
             WHERE s.trial_id = t.id AND s.removed_at IS NULL) AS sponsor,
           reg.r->>'status' AS status,
           t.created_at, a.name AS created_by, t.merged_into,
           (SELECT count(*)::int FROM core.trial_document l WHERE l.trial_id = t.id AND l.removed_at IS NULL)
               AS documents,
           (SELECT count(*)::int FROM core.trial_document l JOIN core.document d ON d.id = l.document_id
              JOIN core.document_kind k ON k.code = d.kind AND k.has_results
             WHERE l.trial_id = t.id AND l.removed_at IS NULL) AS results_documents,
           (SELECT count(*)::int FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id
              JOIN core.page p ON p.file_sha256 = d.file_sha256
             WHERE l.trial_id = t.id AND l.removed_at IS NULL AND d.kind = 'results-summary') AS pages,
           (SELECT count(*)::int FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id
              JOIN core.page p ON p.file_sha256 = d.file_sha256
             WHERE l.trial_id = t.id AND l.removed_at IS NULL AND d.kind = 'results-summary'
               AND {_READABLE}) AS readable_pages,
           jc.label, jc.run_id AS label_run, jc.outdated AS label_outdated,
           (SELECT count(*)::int FROM core.possible_duplicate pd
             WHERE pd.status = 'open' AND t.id IN (pd.trial_a, pd.trial_b)) AS open_duplicates,
           {_EVIDENCE} AS evidence
    FROM core.trial t
    JOIN core.actor a ON a.id = t.created_by
    LEFT JOIN LATERAL (SELECT {_LATEST_REGISTERED} AS r) reg ON true
    LEFT JOIN {judgement_view(published_only)} jc ON jc.trial_id = t.id AND jc.requirements = %(requirements)s
"""


# What the trials page filters on, by the evidence state
EVIDENCE_FILTERS = ("complete", "stale", "failed", "not transferred")


def _trial_filter() -> str:
    return f"""
            WHERE t.merged_into IS NULL
              AND (%(search)s::text IS NULL
                   OR EXISTS (SELECT FROM core.trial_identifier i WHERE i.trial_id = t.id
                              AND i.value ILIKE '%%' || %(search)s || '%%')
                   OR coalesce(reg.r->>'public_title', reg.r->>'full_title', {_LISTED_TITLE}) ILIKE '%%' || %(search)s || '%%'
                   OR EXISTS (SELECT FROM core.trial_sponsor s JOIN core.organisation o ON o.id = s.organisation_id
                              WHERE s.trial_id = t.id AND s.removed_at IS NULL
                                AND o.name ILIKE '%%' || %(search)s || '%%'))
              AND (%(evidence)s::text IS NULL OR {_EVIDENCE} = %(evidence)s)
            """


def trial_rows(conn: psycopg.Connection, requirements: str, search: str | None = None, evidence: str | None = None,
               offset: int = 0, limit: int | None = None, published_only: bool = False) -> list[TrialRow]:
    """Trials not merged into another, newest first, with their current judgement under this
    requirement set (only a published one if `published_only`); `search` matches an identifier, the
    title or a sponsor; `evidence` keeps one evidence state (EVIDENCE_FILTERS)."""
    with conn.cursor(row_factory=class_row(TrialRow)) as cur:
        return cur.execute(
            _trial_rows(published_only) + _trial_filter() + " ORDER BY t.id DESC OFFSET %(offset)s LIMIT %(limit)s",
            {"search": like_text(search), "evidence": evidence, "offset": offset, "limit": limit,
             "requirements": requirements},
        ).fetchall()


def trial_count(conn: psycopg.Connection, search: str | None = None, evidence: str | None = None) -> int:
    row = conn.execute(
        "SELECT count(*)::int FROM core.trial t LEFT JOIN LATERAL (SELECT " + _LATEST_REGISTERED + " AS r) reg ON true"
        + _trial_filter(),
        {"search": like_text(search), "evidence": evidence},
    ).fetchone()
    return row[0]


def trial_row(conn: psycopg.Connection, trial_id: int, requirements: str,
              published_only: bool = False) -> TrialRow | None:
    """One trial, with its current judgement under this requirement set (only a published one if
    `published_only`)."""
    with conn.cursor(row_factory=class_row(TrialRow)) as cur:
        return cur.execute(_trial_rows(published_only) + " WHERE t.id = %(trial)s",
                           {"trial": trial_id, "requirements": requirements}).fetchone()


def trial_registered(conn: psycopg.Connection, trial_id: int) -> dict | None:
    row = conn.execute(
        "SELECT registered FROM core.trial_record_current WHERE trial_id = %s ORDER BY fetched_at DESC LIMIT 1",
        (trial_id,),
    ).fetchone()
    if row is None:
        return None
    return row[0]


@dataclass(frozen=True)
class IdentifierRow:
    scheme: str
    label: str
    value: str
    source: str
    created_at: datetime
    created_by: str


def trial_identifiers(conn: psycopg.Connection, trial_id: int) -> list[IdentifierRow]:
    with conn.cursor(row_factory=class_row(IdentifierRow)) as cur:
        return cur.execute(
            """
            SELECT i.scheme, s.label, i.value, src.code AS source, i.created_at, a.name AS created_by
            FROM core.trial_identifier i
            JOIN core.identifier_scheme s ON s.code = i.scheme
            JOIN core.source src ON src.id = i.source_id
            JOIN core.actor a ON a.id = i.created_by
            WHERE i.trial_id = %s
            ORDER BY i.scheme <> 'eu-ct', i.scheme
            """,
            (trial_id,),
        ).fetchall()


@dataclass(frozen=True)
class RecordRow:
    source: str
    source_key: str
    versions: int
    first_fetched: datetime
    last_fetched: datetime


def trial_sources(conn: psycopg.Connection, trial_id: int) -> list[RecordRow]:
    """Which sources gave records of the trial, and how many versions each."""
    with conn.cursor(row_factory=class_row(RecordRow)) as cur:
        return cur.execute(
            """
            SELECT src.code AS source, ts.source_key, count(r.id)::int AS versions,
                   min(r.fetched_at) AS first_fetched, max(r.fetched_at) AS last_fetched
            FROM core.trial_source ts
            JOIN core.source src ON src.id = ts.source_id
            JOIN core.trial_record r ON r.source_id = ts.source_id AND r.source_key = ts.source_key
            WHERE ts.trial_id = %s
            GROUP BY src.code, ts.source_key
            ORDER BY src.code
            """,
            (trial_id,),
        ).fetchall()


@dataclass(frozen=True)
class DocumentRow:
    id: int
    kind: str
    kind_label: str
    title: str | None
    language: str | None
    version: str
    versions: int
    fetched_at: datetime
    sha256: bytes
    size_bytes: int
    pages: int
    readable_pages: int
    basis: str
    link_source: str
    confidence: float
    linked_at: datetime
    linked_by: str


def trial_documents(conn: psycopg.Connection, trial_id: int) -> list[DocumentRow]:
    with conn.cursor(row_factory=class_row(DocumentRow)) as cur:
        return cur.execute(
            f"""
            SELECT d.id, d.kind, k.label AS kind_label, d.title, d.language, d.version,
                   (SELECT count(*)::int FROM core.document_version v WHERE v.document_id = d.id) AS versions,
                   d.fetched_at, d.file_sha256 AS sha256, f.size_bytes::int AS size_bytes,
                   (SELECT count(*)::int FROM core.page p WHERE p.file_sha256 = d.file_sha256) AS pages,
                   (SELECT count(*)::int FROM core.page p WHERE p.file_sha256 = d.file_sha256 AND {_READABLE})
                       AS readable_pages,
                   l.basis, src.code AS link_source, l.confidence, l.created_at AS linked_at, a.name AS linked_by
            FROM core.trial_document l
            JOIN core.document_current d ON d.id = l.document_id
            JOIN core.document_kind k ON k.code = d.kind
            JOIN core.file f ON f.sha256 = d.file_sha256
            JOIN core.source src ON src.id = l.source_id
            JOIN core.actor a ON a.id = l.created_by
            WHERE l.trial_id = %s AND l.removed_at IS NULL
            ORDER BY d.kind <> 'results-summary', NOT k.has_results, d.kind, d.title
            """,
            (trial_id,),
        ).fetchall()


@dataclass(frozen=True)
class PageText:
    page_no: int
    text: str


def file_pages(conn: psycopg.Connection, sha256: bytes) -> list[PageText]:
    with conn.cursor(row_factory=class_row(PageText)) as cur:
        return cur.execute(
            "SELECT page_no, text FROM core.page WHERE file_sha256 = %s ORDER BY page_no", (sha256,)
        ).fetchall()


@dataclass(frozen=True)
class FileOwner:
    document_id: int
    title: str | None
    trial_id: int | None


def file_owner(conn: psycopg.Connection, sha256: bytes) -> FileOwner | None:
    """A document using the file, and a trial it is linked to, for the file page's heading."""
    with conn.cursor(row_factory=class_row(FileOwner)) as cur:
        return cur.execute(
            """
            SELECT d.id AS document_id, d.title, l.trial_id
            FROM core.document_version v JOIN core.document d ON d.id = v.document_id
            LEFT JOIN core.trial_document l ON l.document_id = d.id AND l.removed_at IS NULL
            WHERE v.file_sha256 = %s
            ORDER BY v.id LIMIT 1
            """,
            (sha256,),
        ).fetchone()


def file_content(conn: psycopg.Connection, sha256: bytes) -> tuple[bytes, str | None] | None:
    row = conn.execute("SELECT content, media_type FROM core.file WHERE sha256 = %s", (sha256,)).fetchone()
    if row is None:
        return None
    content = bytes(row[0])
    media_type = row[1]
    return (content, media_type)


# --- history -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class HistoryRow:
    at: datetime
    actor: str
    actor_kind: str
    action: str
    table_name: str
    row_key: dict
    new_row: dict | None
    old_row: dict | None
    note: str | None


def trial_history(conn: psycopg.Connection, trial_id: int) -> list[HistoryRow]:
    """Who added or changed the trial, its identifiers, records and document links, and when."""
    with conn.cursor(row_factory=class_row(HistoryRow)) as cur:
        return cur.execute(
            """
            SELECT l.at, a.name AS actor, a.kind AS actor_kind, l.action, l.table_name, l.row_key, l.new_row,
                   l.old_row, l.note
            FROM core.audit_log l JOIN core.actor a ON a.id = l.actor_id
            WHERE (l.table_name = 'core.trial' AND l.row_key->>'id' = %(trial)s::text)
               OR (l.table_name IN ('core.trial_identifier', 'core.trial_source', 'core.trial_document')
                   AND coalesce(l.new_row, l.old_row)->>'trial_id' = %(trial)s::text)
               OR (l.table_name = 'core.trial_record' AND (coalesce(l.new_row, l.old_row)->>'source_key',
                   (coalesce(l.new_row, l.old_row)->>'source_id')::smallint) IN
                   (SELECT source_key, source_id FROM core.trial_source WHERE trial_id = %(trial)s))
            ORDER BY l.id
            """,
            {"trial": trial_id},
        ).fetchall()


# --- the evidence check ------------------------------------------------------------------------

@dataclass(frozen=True)
class RunRow:
    id: int
    actor: str
    model: str
    effort: str
    prompt: str
    requirements: str
    status: str
    error: str | None
    started_at: datetime
    finished_at: datetime | None
    trials: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    # billed attempts on a model missing from the price list (not in cost_usd)
    unpriced_calls: int
    full: int
    not_full: int
    undecided: int


_RUN_ROWS = """
    SELECT r.id, a.name AS actor, r.model, r.effort, r.prompt, r.requirements, r.status,
           r.error, r.started_at, r.finished_at, r.trials, r.errors, calls.input_tokens, calls.output_tokens,
           calls.cost_usd, calls.unpriced_calls,
           count(*) FILTER (WHERE j.label = 'full')::int AS full,
           count(*) FILTER (WHERE j.label = 'not full')::int AS not_full,
           count(*) FILTER (WHERE j.label = 'undecided')::int AS undecided
    FROM checks.run r JOIN core.actor a ON a.id = r.actor_id
    -- its tokens and cost: its calls'
    CROSS JOIN LATERAL (
        SELECT coalesce(sum(c.input_tokens), 0)::bigint AS input_tokens,
               coalesce(sum(c.output_tokens), 0)::bigint AS output_tokens, coalesce(sum(c.cost_usd), 0) AS cost_usd,
               coalesce(sum(c.unpriced_attempts), 0)::int AS unpriced_calls
        FROM core.llm_call c WHERE c.check_run_id = r.id
    ) calls
    LEFT JOIN checks.judgement j ON j.run_id = r.id
"""


def runs(conn: psycopg.Connection) -> list[RunRow]:
    with conn.cursor(row_factory=class_row(RunRow)) as cur:
        return cur.execute(_RUN_ROWS + " GROUP BY r.id, a.name, calls.input_tokens, calls.output_tokens, "
                           "calls.cost_usd, calls.unpriced_calls ORDER BY r.id DESC").fetchall()


def run_row(conn: psycopg.Connection, run_id: int) -> RunRow | None:
    with conn.cursor(row_factory=class_row(RunRow)) as cur:
        return cur.execute(_RUN_ROWS + " WHERE r.id = %s GROUP BY r.id, a.name, calls.input_tokens, "
                           "calls.output_tokens, calls.cost_usd, calls.unpriced_calls", (run_id,)).fetchone()


def run_prompt(conn: psycopg.Connection, run_id: int) -> tuple[str, dict]:
    """(prompt text, params) of a run."""
    row = conn.execute("SELECT prompt_text, params FROM checks.run WHERE id = %s", (run_id,)).fetchone()
    prompt_text = row[0]
    params = row[1]
    return (prompt_text, params)


@dataclass(frozen=True)
class JudgementRow:
    run_id: int
    trial_id: int
    name: str
    parts: dict
    label: str | None
    trace: dict | None
    served_by: list[str] | None
    error: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    duration_ms: int | None
    model: str
    prompt: str
    requirements: str
    started_at: datetime
    trial_record_id: int | None
    document_versions: list[int]
    # the run's status: a judgement of a run still running is not compared with gold labels
    run_status: str
    # when the newest document version it read was published, as its source says
    latest_published_at: datetime | None
    # its run published it: the public sees it, withdrawn or not (ADR-007)
    published: bool
    # how its kind compared with gold labels when it was published (checks/publishing.py)
    published_confidence: float | None
    published_basis: str | None
    # staff withdrew it: when, who and why (it stays public, marked)
    withdrawn_at: datetime | None
    withdrawn_by: str | None
    withdrawn_note: str | None


_JUDGEMENT_ROWS = f"""
    SELECT j.run_id, j.trial_id, {_NAME} AS name, j.parts, j.label, rules.trace,
           j.served_by, j.error, calls.input_tokens, calls.output_tokens, calls.cost_usd, j.duration_ms, r.model, r.prompt,
           r.requirements,
           r.started_at, j.trial_record_id, j.document_versions, r.status AS run_status,
           (SELECT max(v.published_at) FROM core.document_version v WHERE v.id = ANY(j.document_versions))
               AS latest_published_at,
           (j.error IS NULL AND r.status <> 'running') AS published,
           (j.agreement->>'confidence')::real AS published_confidence, j.agreement->>'basis' AS published_basis,
           j.withdrawn_at, wa.name AS withdrawn_by, j.withdrawn_reason AS withdrawn_note
    FROM checks.judgement j
    LEFT JOIN core.actor wa ON wa.id = j.withdrawn_by
    JOIN checks.run r ON r.id = j.run_id JOIN core.trial t ON t.id = j.trial_id
    -- how each part was set: its rules step's trace
    LEFT JOIN LATERAL (SELECT nullif(s.data->'trace', '{{}}') AS trace FROM checks.step s
                        WHERE s.run_id = j.run_id AND s.trial_id = j.trial_id AND s.name = 'rules') rules ON true
    -- its tokens and cost: its calls' (no call: nothing spent; one on a model missing from the price list:
    -- unknown)
    CROSS JOIN LATERAL (
        SELECT sum(c.input_tokens)::int AS input_tokens, sum(c.output_tokens)::int AS output_tokens,
               CASE WHEN sum(c.unpriced_attempts) > 0 THEN NULL ELSE coalesce(sum(c.cost_usd), 0) END AS cost_usd
        FROM core.llm_call c WHERE c.check_run_id = j.run_id AND c.trial_id = j.trial_id
    ) calls
"""
# Keeps only published judgements (withdrawn ones too: they stay public, marked) when %(published_only)s
_PUBLISHED_FILTER = """
      AND (NOT %(published_only)s OR (j.error IS NULL AND r.status <> 'running'))
"""


def trial_judgements(conn: psycopg.Connection, trial_id: int, published_only: bool = False) -> list[JudgementRow]:
    """Every evidence-run judgement of the trial (only published ones if `published_only`), newest run first."""
    with conn.cursor(row_factory=class_row(JudgementRow)) as cur:
        return cur.execute(_JUDGEMENT_ROWS + " WHERE j.trial_id = %(trial)s" + _PUBLISHED_FILTER + " ORDER BY j.run_id DESC",
                           {"trial": trial_id, "published_only": published_only}).fetchall()


def run_judgements(conn: psycopg.Connection, run_id: int, published_only: bool = False) -> list[JudgementRow]:
    with conn.cursor(row_factory=class_row(JudgementRow)) as cur:
        return cur.execute(_JUDGEMENT_ROWS + " WHERE j.run_id = %(run)s" + _PUBLISHED_FILTER + " ORDER BY name",
                           {"run": run_id, "published_only": published_only}).fetchall()


@dataclass(frozen=True)
class QuestionRow:
    key: str
    item_text: str | None
    pages: list
    widened: bool
    evidence_id: int | None
    found: bool | None
    kind: str | None
    quote: str | None
    file_sha256: bytes | None
    page_no: int | None
    note: str | None
    verified: bool | None


def run_questions(conn: psycopg.Connection, run_id: int, trial_id: int) -> list[QuestionRow]:
    """The questions one run asked about a trial, each with the evidence the model gave for it."""
    with conn.cursor(row_factory=class_row(QuestionRow)) as cur:
        return cur.execute(
            """
            SELECT a.key, a.item_text, a.pages, a.widened, a.evidence_id, a.found, a.kind, a.quote,
                   a.file_sha256, a.page_no, a.note, a.verified
            FROM checks.answer a
            WHERE a.run_id = %s AND a.trial_id = %s
            ORDER BY a.key = 'participants' DESC, a.key = 'cancelled' DESC, a.key
            """,
            (run_id, trial_id),
        ).fetchall()


@dataclass(frozen=True)
class StepRow:
    seq: int
    name: str
    at: datetime
    duration_ms: int | None
    data: dict


def run_steps(conn: psycopg.Connection, run_id: int, trial_id: int) -> list[StepRow]:
    """The trace of judging one trial in one run, in order; a call step with its call (core.llm_call):
    the request as sent, the answer, how it ended, its tokens and cost (None: nothing billed, or a model
    missing from the price list)."""
    with conn.cursor(row_factory=class_row(StepRow)) as cur:
        return cur.execute(
            """
            SELECT s.seq, s.name, s.at, s.duration_ms,
                   CASE WHEN c.id IS NULL THEN s.data
                        ELSE (s.data - 'llm_call') || jsonb_build_object(
                            'request', c.request, 'served_by', c.served_by, 'stop_reason', c.stop_reason,
                            'input_tokens', c.input_tokens, 'output_tokens', c.output_tokens,
                            'cache_write_tokens', c.cache_write_tokens, 'cache_read_tokens', c.cache_read_tokens,
                            'cost_usd', CASE WHEN c.unpriced_attempts > 0 OR c.attempts = '[]' THEN NULL
                                             ELSE c.cost_usd::text END,
                            'error', c.error, 'answer', c.response) END AS data
            FROM checks.step s
            LEFT JOIN core.llm_call c ON s.name = 'call' AND c.id = (s.data->>'llm_call')::bigint
            WHERE s.run_id = %s AND s.trial_id = %s ORDER BY s.seq
            """,
            (run_id, trial_id),
        ).fetchall()


def coverage(conn: psycopg.Connection) -> list[tuple[str, int]]:
    """How far the trials have come through the workflow, step by step."""
    row = conn.execute(
        """
        SELECT
            (SELECT count(*) FROM core.trial WHERE merged_into IS NULL),
            (SELECT count(DISTINCT l.trial_id) FROM core.trial_document l WHERE l.removed_at IS NULL),
            (SELECT count(DISTINCT l.trial_id) FROM core.trial_document l
               JOIN core.document_current d ON d.id = l.document_id AND d.kind = 'results-summary'
               JOIN core.file f ON f.sha256 = d.file_sha256 AND f.text_fingerprint IS NOT NULL
              WHERE l.removed_at IS NULL),
            (SELECT count(DISTINCT trial_id) FROM checks.judgement_current),
            (SELECT count(*) FROM core.possible_duplicate WHERE status = 'open')
        """
    ).fetchone()
    return [
        ("trials", row[0]),
        ("with documents", row[1]),
        ("with readable results summaries", row[2]),
        ("judged by an evidence run", row[3]),
        ("possible duplicates to decide", row[4]),
    ]


# The results the public's home page filters on (ADR-007)
PUBLIC_LABELS = ("full", "not full", "undecided")


@dataclass(frozen=True)
class PublishedTrialRow:
    """A trial on the public's home page, with its newest published judgement (ADR-007)."""

    id: int
    name: str
    title: str | None
    sponsor: str | None
    status: str | None
    label: str
    run_id: int
    model: str
    # when the run that judged it started, and when the judgement was published
    checked_at: datetime
    published_at: datetime
    # its documents or record changed since it was judged
    outdated: bool
    # staff withdrew it, and why: it stays listed, marked
    withdrawn_at: datetime | None
    withdrawn_note: str | None
    # the countries it runs in (core.trial_location), sorted; and its sites in the city searched for
    countries: list[str]
    cities: list[str]


_PUBLISHED_TRIALS = f"""
    FROM core.trial t
    JOIN checks.judgement_current jc ON jc.trial_id = t.id AND jc.requirements = %(requirements)s
    LEFT JOIN LATERAL (SELECT {_LATEST_REGISTERED} AS r) reg ON true
""" + _trial_filter() + """
              AND (%(label)s::text IS NULL OR jc.label = %(label)s)
              AND (%(status)s::text IS NULL OR reg.r->>'status' = %(status)s)
              AND (%(checked_from)s::date IS NULL OR jc.started_at >= %(checked_from)s::date)
              AND (%(checked_to)s::date IS NULL OR jc.started_at < %(checked_to)s::date + 1)
              AND (%(country)s::text IS NULL OR EXISTS (SELECT FROM core.trial_location l WHERE l.trial_id = t.id
                                                          AND lower(l.country) = lower(%(country)s)))
              AND (%(city)s::text IS NULL OR EXISTS (SELECT FROM core.trial_location l WHERE l.trial_id = t.id
                                                       AND l.city ILIKE '%%' || %(city)s || '%%'
                                                       AND (%(country)s::text IS NULL OR lower(l.country) = lower(%(country)s))))
"""


@dataclass(frozen=True)
class PublicFilter:
    """What the public's home page filters on: `search` matches an identifier, the title or a sponsor;
    `label` one result (PUBLIC_LABELS); `status` one registry status; the run started between
    `checked_from` and `checked_to`, both days included; it runs in `country` (any case), at a site
    in a city whose name contains `city` (in that country, when both are given)."""

    search: str | None = None
    label: str | None = None
    status: str | None = None
    checked_from: date | None = None
    checked_to: date | None = None
    country: str | None = None
    city: str | None = None

    def params(self, requirements: str) -> dict:
        return {"search": like_text(self.search), "evidence": None, "label": self.label, "status": self.status,
                "checked_from": self.checked_from, "checked_to": self.checked_to, "country": self.country,
                "city": like_text(self.city), "requirements": requirements}


def published_trial_rows(conn: psycopg.Connection, requirements: str, found: PublicFilter = PublicFilter(),
                         offset: int = 0, limit: int | None = None) -> list[PublishedTrialRow]:
    """Trials with a published judgement under this requirement set, the newest published first."""
    params = found.params(requirements)
    params.update({"offset": offset, "limit": limit})
    with conn.cursor(row_factory=class_row(PublishedTrialRow)) as cur:
        return cur.execute(
            f"""SELECT t.id, {_NAME} AS name,
                       coalesce(reg.r->>'public_title', reg.r->>'full_title', {_LISTED_TITLE}) AS title,
                       (SELECT string_agg(o.name, ', ' ORDER BY s.is_primary DESC, o.name)
                          FROM core.trial_sponsor s JOIN core.organisation o ON o.id = s.organisation_id
                         WHERE s.trial_id = t.id AND s.removed_at IS NULL) AS sponsor,
                       reg.r->>'status' AS status, jc.label, jc.run_id, jc.model,
                       jc.started_at AS checked_at, jc.published_at, jc.outdated, jc.withdrawn_at,
                       jc.withdrawn_reason AS withdrawn_note,
                       coalesce((SELECT array_agg(DISTINCT l.country ORDER BY l.country) FROM core.trial_location l
                                  WHERE l.trial_id = t.id), '{{}}') AS countries,
                       coalesce((SELECT array_agg(DISTINCT l.city ORDER BY l.city) FROM core.trial_location l
                                  WHERE l.trial_id = t.id AND l.city IS NOT NULL AND %(city)s::text IS NOT NULL
                                    AND l.city ILIKE '%%' || %(city)s || '%%'
                                    AND (%(country)s::text IS NULL OR lower(l.country) = lower(%(country)s))),
                                '{{}}') AS cities"""
            + _PUBLISHED_TRIALS + " ORDER BY jc.published_at DESC, t.id DESC OFFSET %(offset)s LIMIT %(limit)s",
            params,
        ).fetchall()


def published_trial_count(conn: psycopg.Connection, requirements: str, found: PublicFilter = PublicFilter()) -> int:
    row = conn.execute("SELECT count(*)::int" + _PUBLISHED_TRIALS, found.params(requirements)).fetchone()
    return row[0]


def published_trial_countries(conn: psycopg.Connection, requirements: str) -> list[str]:
    """The countries the trials with a published judgement run in, for the home page's filter."""
    rows = conn.execute(
        "SELECT DISTINCT l.country FROM core.trial_location l JOIN core.trial t ON t.id = l.trial_id"
        " JOIN checks.judgement_current jc ON jc.trial_id = t.id AND jc.requirements = %(requirements)s"
        " WHERE t.merged_into IS NULL ORDER BY 1",
        {"requirements": requirements},
    ).fetchall()
    return [row[0] for row in rows]


@dataclass(frozen=True)
class LocationRow:
    country: str
    city: str | None
    postcode: str | None
    site: str | None
    status: str | None
    source: str


@dataclass(frozen=True)
class CountryLocations:
    country: str
    # the trial's status there, as the first row that gives one says
    status: str | None
    # its sites (rows with a site or city), by city
    sites: list[LocationRow]


def by_country(rows: list[LocationRow]) -> list[CountryLocations]:
    """The rows grouped by country: countries with a site first, then the rest, each by name."""
    grouped: dict[str, list[LocationRow]] = {}
    for row in rows:
        grouped.setdefault(row.country, []).append(row)
    found = []
    for country, places in grouped.items():
        status = None
        sites = []
        for place in places:
            if status is None:
                status = place.status
            if place.site or place.city:
                sites.append(place)
        found.append(CountryLocations(country, status, sites))
    found.sort(key=lambda group: (not group.sites, group.country))
    return found


def trial_locations(conn: psycopg.Connection, trial_id: int) -> list[LocationRow]:
    """Where the trial runs, by country: its sites first (by city), then countries with no site listed."""
    with conn.cursor(row_factory=class_row(LocationRow)) as cur:
        return cur.execute(
            """
            SELECT l.country, l.city, l.postcode, l.site, l.status, s.code AS source
            FROM core.trial_location l JOIN core.source s ON s.id = l.source_id
            WHERE l.trial_id = %s
            ORDER BY l.country, l.site IS NULL, l.city, l.site
            """,
            (trial_id,),
        ).fetchall()


def published_trial_statuses(conn: psycopg.Connection, requirements: str) -> list[str]:
    """The registry statuses of the trials with a published judgement, for the home page's filter."""
    rows = conn.execute(
        "SELECT DISTINCT reg.r->>'status' AS status FROM core.trial t"
        " JOIN checks.judgement_current jc ON jc.trial_id = t.id AND jc.requirements = %(requirements)s"
        " LEFT JOIN LATERAL (SELECT " + _LATEST_REGISTERED + " AS r) reg ON true"
        " WHERE t.merged_into IS NULL AND reg.r->>'status' IS NOT NULL ORDER BY 1",
        {"requirements": requirements},
    ).fetchall()
    return [row[0] for row in rows]


def process_actor_names(conn: psycopg.Connection) -> set[str]:
    """The names of actors that are processes (a source's adapter, the migration): shown to the
    public as they are, while people are shown only as staff (ADR-007)."""
    rows = conn.execute("SELECT name FROM core.actor WHERE kind = 'process'").fetchall()
    names = set()
    for row in rows:
        names.add(row[0])
    return names


def file_is_public(conn: psycopg.Connection, sha256: bytes) -> bool:
    """Whether the public may open this stored file (ADR-007): a version of a document a registry or
    publications source gave, or a file a published judgement quotes (withdrawn or not). A file only a
    person uploaded stays with staff until a published judgement cites it."""
    row = conn.execute(
        """
        SELECT EXISTS (SELECT FROM core.document_version v JOIN core.document d ON d.id = v.document_id
                         JOIN core.source s ON s.id = d.source_id
                        WHERE v.file_sha256 = %(sha)s AND s.kind <> 'manual')
            OR EXISTS (SELECT FROM checks.answer a
                         JOIN checks.judgement j ON j.run_id = a.run_id AND j.trial_id = a.trial_id AND j.error IS NULL
                         JOIN checks.run r ON r.id = a.run_id AND r.status <> 'running'
                        WHERE a.file_sha256 = %(sha)s)
        """,
        {"sha": sha256},
    ).fetchone()
    return row[0]


@dataclass(frozen=True)
class TrialChoice:
    """A trial a check may read now (trials/readiness.py ready_trials), as a form offers it."""

    id: int
    name: str
    title: str | None
    readable_pages: int
    label: str | None


def trial_choices(conn: psycopg.Connection, trial_ids: list[int], kinds: list[str], requirements: str
                  ) -> list[TrialChoice]:
    """These trials with their readable pages of these kinds and their current label under the set."""
    with conn.cursor(row_factory=class_row(TrialChoice)) as cur:
        return cur.execute(
            f"""
            SELECT t.id, {_NAME} AS name,
                   coalesce(reg.r->>'public_title', reg.r->>'full_title') AS title,
                   (SELECT count(*)::int FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id
                      JOIN core.page p ON p.file_sha256 = d.file_sha256
                     WHERE l.trial_id = t.id AND l.removed_at IS NULL AND d.kind = ANY(%(kinds)s)
                       AND {_READABLE}) AS readable_pages,
                   jc.label
            FROM core.trial t
            LEFT JOIN LATERAL (SELECT {_LATEST_REGISTERED} AS r) reg ON true
            LEFT JOIN checks.judgement_current jc ON jc.trial_id = t.id AND jc.requirements = %(requirements)s
            WHERE t.id = ANY(%(trials)s)
            ORDER BY name
            """,
            {"kinds": kinds, "trials": trial_ids, "requirements": requirements},
        ).fetchall()


def trials_of_keys(conn: psycopg.Connection, source: str, keys: list[str]) -> list[int]:
    """The trials (after merges) of these keys at a source, in the keys' order, each once."""
    found = []
    for key in keys:
        row = conn.execute(
            "SELECT ts.trial_id FROM core.trial_source ts JOIN core.source s ON s.id = ts.source_id "
            "WHERE s.code = %s AND ts.source_key = %s",
            (source, key),
        ).fetchone()
        if row is None:
            continue
        trial_id = core.resolved_trial(conn, row[0])
        if trial_id not in found:
            found.append(trial_id)
    return found


def trial_source_keys(conn: psycopg.Connection, trial_id: int, source: str) -> list[str]:
    """The trial's own keys at one source, e.g. its EU CT number at CTIS (to sync it again)."""
    rows = conn.execute(
        "SELECT ts.source_key FROM core.trial_source ts JOIN core.source s ON s.id = ts.source_id "
        "WHERE ts.trial_id = %s AND s.code = %s ORDER BY ts.source_key",
        (trial_id, source),
    ).fetchall()
    keys = []
    for row in rows:
        keys.append(row[0])
    return keys


@dataclass(frozen=True)
class PromptUse:
    prompt: str
    prompt_sha256: bytes
    runs: int
    last_run: int
    last_used: datetime


def prompt_uses(conn: psycopg.Connection) -> list[PromptUse]:
    """Which prompt texts check runs used, by version and exact text (sha256)."""
    with conn.cursor(row_factory=class_row(PromptUse)) as cur:
        return cur.execute(
            """
            SELECT prompt, prompt_sha256, count(*)::int AS runs, max(id) AS last_run,
                   max(started_at) AS last_used
            FROM checks.run GROUP BY prompt, prompt_sha256 ORDER BY max(started_at) DESC
            """
        ).fetchall()


def run_prompt_text(conn: psycopg.Connection, prompt_sha256: bytes) -> str | None:
    """A prompt text as a run stored it (it may differ from today's file of the same name)."""
    row = conn.execute("SELECT prompt_text FROM checks.run WHERE prompt_sha256 = %s LIMIT 1", (prompt_sha256,)).fetchone()
    if row is None:
        return None
    return row[0]


# --- gold datasets: which items name a trial we hold, and our latest judgement of it ---------------

def trials_for_identifiers(conn: psycopg.Connection, identifiers: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """(scheme, value) -> trial id (after merges), for the identifiers we hold; values as stored
    (upper case, no spaces)."""
    schemes = []
    values = []
    for scheme, value in identifiers:
        schemes.append(scheme)
        values.append(value)
    rows = conn.execute(
        """
        SELECT i.scheme, i.value, i.trial_id
        FROM core.trial_identifier i
        JOIN unnest(%s::text[], %s::text[]) AS wanted(scheme, value)
          ON wanted.scheme = i.scheme AND wanted.value = i.value
        """,
        (schemes, values),
    ).fetchall()
    found = {}
    for scheme, value, trial_id in rows:
        found[(scheme, value)] = core.resolved_trial(conn, trial_id)
    return found


@dataclass(frozen=True)
class LatestJudgement:
    run_id: int
    trial_id: int
    parts: dict
    label: str | None
    model: str
    started_at: datetime
    # its inputs changed since (checks.judgement_current)
    outdated: bool
    # when the newest document version it read was published, as its source says
    latest_published_at: datetime | None


def latest_judgements(conn: psycopg.Connection, trial_ids: list[int], requirements: str) -> dict[int, LatestJudgement]:
    """Trial id -> its newest finished judgement under this requirement set (a gold dataset's labels
    answer one requirement set, so judgements under another can't be compared with them)."""
    with conn.cursor(row_factory=class_row(LatestJudgement)) as cur:
        rows = cur.execute(
            """
            SELECT jc.run_id, jc.trial_id, jc.parts, jc.label, jc.model, jc.started_at, jc.outdated,
                   (SELECT max(v.published_at) FROM core.document_version v WHERE v.id = ANY(jc.document_versions))
                       AS latest_published_at
            FROM checks.judgement_current jc
            WHERE jc.trial_id = ANY(%s) AND jc.requirements = %s
            """,
            (trial_ids, requirements),
        ).fetchall()
    found = {}
    for row in rows:
        found[row.trial_id] = row
    return found


# --- sponsors: organisations and the trials they sponsor ----------------------------------------

@dataclass(frozen=True)
class SponsorRow:
    organisation_id: int
    name: str
    kind: str | None
    country: str | None
    commercial: bool | None
    is_primary: bool
    source: str
    created_at: datetime
    created_by: str
    removed_at: datetime | None
    removed_by: str | None
    removal: str | None
    identifiers: list[str]


def trial_sponsors(conn: psycopg.Connection, trial_id: int) -> list[SponsorRow]:
    """The trial's sponsors, current first (the primary one first), then removed ones."""
    with conn.cursor(row_factory=class_row(SponsorRow)) as cur:
        return cur.execute(
            """
            SELECT o.id AS organisation_id, o.name, o.kind, o.country, o.commercial, s.is_primary,
                   src.code AS source, s.created_at, a.name AS created_by, s.removed_at, r.name AS removed_by,
                   s.removal,
                   coalesce((SELECT array_agg(i.scheme || ' ' || i.value ORDER BY i.scheme, i.value)
                               FROM core.organisation_identifier i WHERE i.organisation_id = o.id), '{}') AS identifiers
            FROM core.trial_sponsor s
            JOIN core.organisation o ON o.id = s.organisation_id
            JOIN core.source src ON src.id = s.source_id
            JOIN core.actor a ON a.id = s.created_by
            LEFT JOIN core.actor r ON r.id = s.removed_by
            WHERE s.trial_id = %s
            ORDER BY s.removed_at IS NOT NULL, s.is_primary DESC, o.name
            """,
            (trial_id,),
        ).fetchall()


@dataclass(frozen=True)
class OrganisationRow:
    id: int
    name: str
    kind: str | None
    country: str | None
    commercial: bool | None
    created_at: datetime
    created_by: str
    first_source: str
    merged_into: int | None
    identifiers: list[str]
    trials: int


_ORGANISATION_ROWS = """
    SELECT o.id, o.name, o.kind, o.country, o.commercial, o.created_at, a.name AS created_by,
           src.code AS first_source, o.merged_into,
           coalesce((SELECT array_agg(i.scheme || ' ' || i.value ORDER BY i.scheme, i.value)
                       FROM core.organisation_identifier i WHERE i.organisation_id = o.id), '{}') AS identifiers,
           (SELECT count(*)::int FROM core.trial_sponsor s WHERE s.organisation_id = o.id AND s.removed_at IS NULL)
               AS trials
    FROM core.organisation o
    JOIN core.actor a ON a.id = o.created_by
    JOIN core.source src ON src.id = o.first_source
"""


def organisation_rows(conn: psycopg.Connection, search: str | None = None) -> list[OrganisationRow]:
    """Every organisation not merged into another, most trials first; `search` matches its name or an identifier."""
    with conn.cursor(row_factory=class_row(OrganisationRow)) as cur:
        return cur.execute(
            _ORGANISATION_ROWS + """
            WHERE o.merged_into IS NULL
              AND (%(search)s::text IS NULL OR o.name ILIKE '%%' || %(search)s || '%%'
                   OR EXISTS (SELECT FROM core.organisation_identifier i WHERE i.organisation_id = o.id
                              AND i.value ILIKE '%%' || %(search)s || '%%'))
            ORDER BY trials DESC, o.name
            """,
            {"search": like_text(search)},
        ).fetchall()


def organisation_row(conn: psycopg.Connection, organisation_id: int) -> OrganisationRow | None:
    with conn.cursor(row_factory=class_row(OrganisationRow)) as cur:
        return cur.execute(_ORGANISATION_ROWS + " WHERE o.id = %s", (organisation_id,)).fetchone()


@dataclass(frozen=True)
class SponsoredTrialRow:
    trial_id: int
    name: str
    title: str | None
    is_primary: bool
    removed_at: datetime | None
    label: str | None
    label_run: int | None


def sponsored_trials(conn: psycopg.Connection, organisation_id: int, requirements: str,
                     published_only: bool = False) -> list[SponsoredTrialRow]:
    """The trials this organisation sponsors or sponsored, with our latest label for each (only a
    published one if `published_only`)."""
    with conn.cursor(row_factory=class_row(SponsoredTrialRow)) as cur:
        return cur.execute(
            f"""
            SELECT t.id AS trial_id, {_NAME} AS name,
                   coalesce(reg.r->>'public_title', reg.r->>'full_title') AS title,
                   s.is_primary, s.removed_at, jc.label, jc.run_id AS label_run
            FROM core.trial_sponsor s
            JOIN core.trial t ON t.id = s.trial_id
            LEFT JOIN LATERAL (SELECT {_LATEST_REGISTERED} AS r) reg ON true
            LEFT JOIN {judgement_view(published_only)} jc ON jc.trial_id = t.id AND jc.requirements = %s
            WHERE s.organisation_id = %s
            ORDER BY s.removed_at IS NOT NULL, t.id DESC
            """,
            (requirements, organisation_id),
        ).fetchall()


# --- data sources (ADR-005) ----------------------------------------------------------------------

@dataclass(frozen=True)
class SourceRow:
    code: str
    name: str
    kind: str
    url: str | None
    adapter: str | None
    enabled: bool
    config: dict
    listed: int
    delisted: int
    not_transferred: int
    complete: int
    stale: int
    failed: int
    last_listed: datetime | None
    last_transferred: datetime | None


_SOURCE_ROWS = """
    SELECT s.code, s.name, s.kind, s.url, s.adapter, s.enabled, s.config,
           count(*) FILTER (WHERE e.listed AND e.delisted_at IS NULL)::int AS listed,
           count(*) FILTER (WHERE e.delisted_at IS NOT NULL)::int AS delisted,
           count(*) FILTER (WHERE e.listed AND e.delisted_at IS NULL AND e.status = 'not transferred')::int
               AS not_transferred,
           count(*) FILTER (WHERE e.status = 'complete')::int AS complete,
           count(*) FILTER (WHERE e.status = 'stale')::int AS stale,
           count(*) FILTER (WHERE e.status = 'failed')::int AS failed,
           (SELECT max(r.finished_at) FROM core.ingest_run r WHERE r.source_id = s.id AND r.kind = 'list') AS last_listed,
           (SELECT max(r.finished_at) FROM core.ingest_run r WHERE r.source_id = s.id AND r.kind = 'transfer')
               AS last_transferred
    FROM core.source s
    LEFT JOIN core.evidence_status e ON e.source_id = s.id
"""


def source_rows(conn: psycopg.Connection) -> list[SourceRow]:
    """Every registered source with how many of its trials are in each state."""
    with conn.cursor(row_factory=class_row(SourceRow)) as cur:
        return cur.execute(_SOURCE_ROWS + " GROUP BY s.id ORDER BY s.id").fetchall()


def source_row(conn: psycopg.Connection, code: str) -> SourceRow | None:
    with conn.cursor(row_factory=class_row(SourceRow)) as cur:
        return cur.execute(_SOURCE_ROWS + " WHERE s.code = %s GROUP BY s.id", (code,)).fetchone()


@dataclass(frozen=True)
class SourceTrialRow:
    source_key: str
    trial_id: int
    title: str | None
    listed: bool
    delisted_at: datetime | None
    source_updated_on: date | None
    summary: dict | None
    status: str
    transferred_at: datetime | None
    ingest_run_id: int | None
    documents: int | None
    pages: int | None
    errors: dict | None


# What the source page filters on: a state of core.evidence_status, or no longer listed
SOURCE_TRIAL_FILTERS = ("not transferred", "complete", "stale", "failed", "no longer listed")


def _source_trial_filter() -> str:
    return """
        FROM core.evidence_status e
        JOIN core.source s ON s.id = e.source_id
        LEFT JOIN LATERAL (SELECT r.registered FROM core.trial_record r
                           WHERE r.source_id = e.source_id AND r.source_key = e.source_key
                           ORDER BY r.fetched_at DESC LIMIT 1) rec ON true
        WHERE s.code = %(source)s
          AND (%(state)s::text IS NULL
               OR (%(state)s = 'no longer listed' AND e.delisted_at IS NOT NULL)
               OR (%(state)s <> 'no longer listed' AND e.status = %(state)s))
          AND (%(search)s::text IS NULL OR e.source_key ILIKE '%%' || %(search)s || '%%'
               OR coalesce(e.title, rec.registered->>'public_title') ILIKE '%%' || %(search)s || '%%'
               OR e.summary::text ILIKE '%%' || %(search)s || '%%')
    """


def source_trials(conn: psycopg.Connection, code: str, state: str | None = None, search: str | None = None,
                  offset: int = 0, limit: int = 100) -> list[SourceTrialRow]:
    """A source's trials with their listing and evidence state, by key."""
    with conn.cursor(row_factory=class_row(SourceTrialRow)) as cur:
        return cur.execute(
            """
            SELECT e.source_key, e.trial_id, coalesce(e.title, rec.registered->>'public_title') AS title,
                   e.listed, e.delisted_at, e.source_updated_on, e.summary, e.status, e.transferred_at,
                   e.ingest_run_id, e.documents, e.pages, e.errors
            """ + _source_trial_filter() + " ORDER BY e.source_key OFFSET %(offset)s LIMIT %(limit)s",
            {"source": code, "state": state, "search": like_text(search), "offset": offset, "limit": limit},
        ).fetchall()


def source_trial_count(conn: psycopg.Connection, code: str, state: str | None = None, search: str | None = None) -> int:
    row = conn.execute("SELECT count(*)::int " + _source_trial_filter(),
                       {"source": code, "state": state, "search": like_text(search)}).fetchone()
    return row[0]


@dataclass(frozen=True)
class IngestRunRow:
    id: int
    kind: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    actor: str
    args: dict
    counts: dict
    errors: dict


def source_runs(conn: psycopg.Connection, code: str, limit: int = 10) -> list[IngestRunRow]:
    """A source's latest list and transfer runs, newest first."""
    with conn.cursor(row_factory=class_row(IngestRunRow)) as cur:
        return cur.execute(
            """
            SELECT r.id, r.kind, r.status, r.started_at, r.finished_at, a.name AS actor, r.args, r.counts, r.errors
            FROM core.ingest_run r JOIN core.source s ON s.id = r.source_id JOIN core.actor a ON a.id = r.actor_id
            WHERE s.code = %s ORDER BY r.started_at DESC LIMIT %s
            """,
            (code, limit),
        ).fetchall()


@dataclass(frozen=True)
class TransferRow:
    source: str
    adapter: str | None
    source_key: str
    listed: bool
    delisted_at: datetime | None
    source_updated_on: date | None
    status: str
    transferred_at: datetime | None
    ingest_run_id: int | None
    documents: int | None
    pages: int | None
    errors: dict | None


def trial_transfers(conn: psycopg.Connection, trial_id: int) -> list[TransferRow]:
    """The trial's state at each source it came from."""
    with conn.cursor(row_factory=class_row(TransferRow)) as cur:
        return cur.execute(
            """
            SELECT s.code AS source, s.adapter, e.source_key, e.listed, e.delisted_at, e.source_updated_on, e.status,
                   e.transferred_at, e.ingest_run_id, e.documents, e.pages, e.errors
            FROM core.evidence_status e JOIN core.source s ON s.id = e.source_id
            WHERE e.trial_id = %s ORDER BY s.id, e.source_key
            """,
            (trial_id,),
        ).fetchall()


def listing_updated_on(conn: psycopg.Connection, trial_id: int) -> date | None:
    """The day a source last changed the trial, as its list says (the newest of its sources)."""
    row = conn.execute(
        "SELECT max(l.source_updated_on) FROM core.trial_source ts JOIN core.source_listing l USING (source_id, source_key) "
        "WHERE ts.trial_id = %s",
        (trial_id,),
    ).fetchone()
    return row[0]
