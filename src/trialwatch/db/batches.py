"""Queries on Message Batches (core.llm_batch, core.llm_batch_item; ADR-009): a run's rounds of model
requests sent together at batch prices, and which of their answers are recorded as calls."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class Batch:
    id: int
    provider_batch_id: str | None
    operation: str
    job_id: int | None
    check_run_id: int | None
    annotation_run_id: int | None
    round: int
    status: str
    request_count: int
    estimated_usd: Decimal | None
    counts: dict
    error: str | None
    created_at: datetime
    submitted_at: datetime | None
    ended_at: datetime | None
    collected_at: datetime | None


@dataclass(frozen=True)
class BatchItem:
    llm_batch_id: int
    custom_id: str
    trial_id: int
    call_number: int
    model: str
    llm_call_id: int | None


BATCH_COLUMNS = (
    "id, provider_batch_id, operation, job_id, check_run_id, annotation_run_id, round, status, request_count, "
    "estimated_usd, counts, error, created_at, submitted_at, ended_at, collected_at"
)


def _batch(row: tuple) -> Batch:
    return Batch(*row)


def create(
    conn: psycopg.Connection,
    *,
    operation: str,
    round_number: int,
    items: list[BatchItem],
    estimated_usd: Decimal | None,
    check_run_id: int | None = None,
    annotation_run_id: int | None = None,
) -> int:
    """Record a batch about to be sent (submitting), with its requests; returns its id. Recorded before it
    is sent, so a batch whose sending was cut off is still known (submitting, without Anthropic's id)."""
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO core.llm_batch (operation, check_run_id, annotation_run_id, round, request_count, "
            "estimated_usd) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (operation, check_run_id, annotation_run_id, round_number, len(items), estimated_usd),
        ).fetchone()
        batch_id = row[0]
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO core.llm_batch_item (llm_batch_id, custom_id, trial_id, call_number, model) "
                "VALUES (%s, %s, %s, %s, %s)",
                [(batch_id, item.custom_id, item.trial_id, item.call_number, item.model) for item in items],
            )
    return batch_id


def submitted(conn: psycopg.Connection, batch_id: int, provider_batch_id: str) -> None:
    conn.execute(
        "UPDATE core.llm_batch SET status = 'submitted', provider_batch_id = %s, submitted_at = clock_timestamp() "
        "WHERE id = %s",
        (provider_batch_id, batch_id),
    )


def not_sent(conn: psycopg.Connection, batch_id: int, error: str) -> None:
    """A batch Anthropic did not take: nothing of it was billed."""
    conn.execute(
        "UPDATE core.llm_batch SET status = 'failed', error = %s WHERE id = %s AND status = 'submitting'",
        (error, batch_id),
    )


def set_counts(conn: psycopg.Connection, batch_id: int, counts: dict, ended: bool) -> None:
    """Anthropic's counts when last asked, and when it ended."""
    conn.execute(
        "UPDATE core.llm_batch SET counts = %s, "
        "ended_at = CASE WHEN %s THEN coalesce(ended_at, clock_timestamp()) ELSE ended_at END WHERE id = %s",
        (Jsonb(counts), ended, batch_id),
    )


def collected(conn: psycopg.Connection, batch_id: int) -> None:
    conn.execute(
        "UPDATE core.llm_batch SET status = 'collected', collected_at = coalesce(collected_at, clock_timestamp()), "
        "ended_at = coalesce(ended_at, clock_timestamp()) WHERE id = %s AND status IN ('submitted', 'collected')",
        (batch_id,),
    )


def batch(conn: psycopg.Connection, batch_id: int) -> Batch | None:
    row = conn.execute(f"SELECT {BATCH_COLUMNS} FROM core.llm_batch WHERE id = %s", (batch_id,)).fetchone()
    if row is None:
        return None
    return _batch(row)


def batches_of_run(
    conn: psycopg.Connection, *, check_run_id: int | None = None, annotation_run_id: int | None = None
) -> list[Batch]:
    """A run's batches that Anthropic took, in the order they were sent."""
    rows = conn.execute(
        f"SELECT {BATCH_COLUMNS} FROM core.llm_batch "
        "WHERE (check_run_id = %s OR annotation_run_id = %s) AND provider_batch_id IS NOT NULL ORDER BY id",
        (check_run_id, annotation_run_id),
    ).fetchall()
    return [_batch(row) for row in rows]


def items(conn: psycopg.Connection, batch_id: int) -> list[BatchItem]:
    rows = conn.execute(
        "SELECT llm_batch_id, custom_id, trial_id, call_number, model, llm_call_id FROM core.llm_batch_item "
        "WHERE llm_batch_id = %s ORDER BY trial_id, call_number",
        (batch_id,),
    ).fetchall()
    return [BatchItem(*row) for row in rows]


def link_call(conn: psycopg.Connection, batch_id: int, custom_id: str, llm_call_id: int) -> None:
    """The answer of this request is recorded as this call."""
    conn.execute(
        "UPDATE core.llm_batch_item SET llm_call_id = %s WHERE llm_batch_id = %s AND custom_id = %s "
        "AND llm_call_id IS NULL",
        (llm_call_id, batch_id, custom_id),
    )


def open_batch_of_job(conn: psycopg.Connection, job_id: int) -> Batch | None:
    """The batch a job waits on (jobs.job.waiting_on)."""
    row = conn.execute(
        f"SELECT {BATCH_COLUMNS} FROM core.llm_batch WHERE id = (SELECT waiting_on FROM jobs.job WHERE id = %s)",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return _batch(row)


@dataclass(frozen=True)
class CallTotals:
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: Decimal
    unpriced_attempts: int


def call_totals(
    conn: psycopg.Connection, *, check_run_id: int | None = None, annotation_run_id: int | None = None
) -> CallTotals:
    """What a run's recorded calls used and cost (priced ones), and how many attempts were not priced."""
    row = conn.execute(
        "SELECT coalesce(sum(input_tokens), 0)::bigint, coalesce(sum(output_tokens), 0)::bigint, "
        "coalesce(sum(cache_write_tokens), 0)::bigint, coalesce(sum(cache_read_tokens), 0)::bigint, "
        "coalesce(sum(cost_usd), 0), coalesce(sum(unpriced_attempts), 0)::int FROM core.llm_call "
        "WHERE check_run_id = %s OR annotation_run_id = %s",
        (check_run_id, annotation_run_id),
    ).fetchone()
    return CallTotals(*row)
