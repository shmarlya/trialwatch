"""Queries on what TrialWatch spends on model calls (core.llm_call) and its spending cap
(core.app_setting), see the spend section of db/migrations/*_init.sql. Money is US dollars, as Decimal."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

MONTHLY_CAP = "spend.monthly_cap_usd"


def record_call(
    conn: psycopg.Connection,
    *,
    operation: str,
    model: str,
    served_by: str | None,
    request: dict,
    response: str | None,
    stop_reason: str | None,
    latency_ms: int,
    error: str | None,
    request_id: str | None,
    attempts: list[dict],
    check_run_id: int | None = None,
    trial_id: int | None = None,
    call_number: int | None = None,
    annotation_run_id: int | None = None,
    tier: str = "direct",
    llm_batch_id: int | None = None,
    custom_id: str | None = None,
) -> int:
    """Add one model call, made for the session's actor (for a check run, or an AI tagging run); returns
    its id. Its totals are summed here from its billed attempts, each cost as stored (numeric(14, 6))."""
    row = conn.execute(
        """
        INSERT INTO core.llm_call (actor_id, operation, model, served_by, check_run_id, trial_id, call_number,
                                   annotation_run_id, request, response, stop_reason, latency_ms, error, request_id,
                                   attempts, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens,
                                   cost_usd, unpriced_attempts, tier, llm_batch_id, custom_id)
        SELECT current_setting('trialwatch.actor')::bigint, %(operation)s, %(model)s, %(served_by)s, %(run)s,
               %(trial)s, %(number)s, %(annotation_run)s, %(request)s, %(response)s, %(stop_reason)s, %(latency)s,
               %(error)s, %(request_id)s, b.attempts, coalesce(t.input_tokens, 0), coalesce(t.output_tokens, 0),
               coalesce(t.cache_write_tokens, 0), coalesce(t.cache_read_tokens, 0), t.cost_usd, t.unpriced,
               %(tier)s, %(llm_batch)s, %(custom_id)s
        FROM (SELECT %(attempts)s::jsonb AS attempts) b
        CROSS JOIN LATERAL (
            SELECT sum((a->>'input_tokens')::bigint) AS input_tokens, sum((a->>'output_tokens')::bigint) AS output_tokens,
                   sum((a->>'cache_write_tokens')::bigint) AS cache_write_tokens,
                   sum((a->>'cache_read_tokens')::bigint) AS cache_read_tokens,
                   sum((a->>'cost_usd')::numeric(14, 6)) AS cost_usd,
                   count(*) FILTER (WHERE a->>'cost_usd' IS NULL) AS unpriced
            FROM jsonb_array_elements(b.attempts) a
        ) t
        RETURNING id
        """,
        {
            "operation": operation,
            "model": model,
            "served_by": served_by,
            "run": check_run_id,
            "trial": trial_id,
            "number": call_number,
            "annotation_run": annotation_run_id,
            "request": Jsonb(request),
            "response": response,
            "stop_reason": stop_reason,
            "latency": latency_ms,
            "error": error,
            "request_id": request_id,
            "attempts": Jsonb(attempts),
            "tier": tier,
            "llm_batch": llm_batch_id,
            "custom_id": custom_id,
        },
    ).fetchone()
    return row[0]


def run_cost(conn: psycopg.Connection, run_id: int) -> Decimal:
    row = conn.execute(
        "SELECT coalesce(sum(cost_usd), 0) FROM core.llm_call WHERE check_run_id = %s", (run_id,)
    ).fetchone()
    return row[0]


def spent_this_month(conn: psycopg.Connection) -> Decimal:
    """What every paid call cost since the start of this calendar month (UTC)."""
    row = conn.execute(
        "SELECT coalesce(sum(cost_usd), 0) FROM core.llm_call WHERE at >= date_trunc('month', now())"
    ).fetchone()
    return row[0]


@dataclass(frozen=True)
class TrialSpend:
    cost_usd: Decimal
    unpriced_calls: int


def trial_spend(conn: psycopg.Connection, run_id: int) -> dict[int, TrialSpend]:
    """Trial id -> what its billed calls in the run cost."""
    rows = conn.execute(
        "SELECT trial_id, coalesce(sum(cost_usd), 0), sum(unpriced_attempts)::int "
        "FROM core.llm_call WHERE check_run_id = %s AND trial_id IS NOT NULL GROUP BY trial_id",
        (run_id,),
    ).fetchall()
    by_trial = {}
    for row in rows:
        by_trial[row[0]] = TrialSpend(row[1], row[2])
    return by_trial


# --- the monthly cap -----------------------------------------------------------------------------


def monthly_cap(conn: psycopg.Connection) -> Decimal | None:
    """The most automatic checks may spend in a calendar month, or None for no cap."""
    row = conn.execute("SELECT value #>> '{}' FROM core.app_setting WHERE key = %s", (MONTHLY_CAP,)).fetchone()
    if row is None or row[0] is None:
        return None
    return Decimal(row[0])


def set_monthly_cap(conn: psycopg.Connection, cap: Decimal | None) -> None:
    """Set the cap (None removes it), as the session's actor."""
    value = None
    if cap is not None:
        value = str(cap)
    conn.execute(
        "INSERT INTO core.app_setting (key, value, updated_at) VALUES (%s, to_jsonb(%s::numeric), clock_timestamp()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
        (MONTHLY_CAP, value),
    )


def cap_reached(conn: psycopg.Connection) -> bool:
    """Whether this month's spend has reached the cap (never, without a cap)."""
    cap = monthly_cap(conn)
    if cap is None:
        return False
    return spent_this_month(conn) >= cap


# --- the spend page ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SpendTotals:
    all_time: Decimal
    this_month: Decimal
    last_30_days: Decimal
    # billed calls (an attempt priced or not), and attempts on a model missing from the price list
    calls: int
    unpriced_calls: int
    # this month's calls answered in Message Batches (ADR-009): what they cost, and how many
    batch_this_month: Decimal = Decimal(0)
    batch_calls_this_month: int = 0


def spend_totals(conn: psycopg.Connection) -> SpendTotals:
    row = conn.execute(
        """
        SELECT coalesce(sum(cost_usd), 0),
               coalesce(sum(cost_usd) FILTER (WHERE at >= date_trunc('month', now())), 0),
               coalesce(sum(cost_usd) FILTER (WHERE at >= now() - interval '30 days'), 0),
               count(*) FILTER (WHERE attempts <> '[]'), coalesce(sum(unpriced_attempts), 0)::int,
               coalesce(sum(cost_usd) FILTER (WHERE tier = 'batch' AND at >= date_trunc('month', now())), 0),
               count(*) FILTER (WHERE tier = 'batch' AND at >= date_trunc('month', now()))
          FROM core.llm_call
        """
    ).fetchone()
    return SpendTotals(row[0], row[1], row[2], row[3], row[4], row[5], row[6])


@dataclass(frozen=True)
class CostlyRun:
    id: int
    started_at: datetime
    actor: str
    model: str
    trials: int
    cost_usd: Decimal


def costliest_runs(conn: psycopg.Connection, limit: int = 10) -> list[CostlyRun]:
    with conn.cursor(row_factory=class_row(CostlyRun)) as cursor:
        cursor.execute(
            "SELECT r.id, r.started_at, a.name AS actor, r.model, r.trials, s.cost_usd "
            "FROM checks.run r JOIN core.actor a ON a.id = r.actor_id "
            "CROSS JOIN LATERAL (SELECT coalesce(sum(c.cost_usd), 0) AS cost_usd FROM core.llm_call c "
            "                    WHERE c.check_run_id = r.id) s "
            "WHERE s.cost_usd > 0 ORDER BY s.cost_usd DESC, r.id DESC LIMIT %s",
            (limit,),
        )
        return cursor.fetchall()


def update_call(
    conn: psycopg.Connection,
    call_id: int,
    *,
    request: dict | None,
    served_by: str | None,
    response: str | None,
    stop_reason: str | None,
    error: str | None,
    request_id: str | None,
    attempts: list[dict],
    check_run_id: int | None = None,
    trial_id: int | None = None,
    call_number: int | None = None,
    annotation_run_id: int | None = None,
) -> None:
    """Bring a recorded call up to date (ADR-009): a batch answer recorded when it was collected gets the
    request its trial sent (as recorded) once a trial uses it, or the direct call that answered after a
    refusal (its billed attempts then are both). A value given as None keeps what is there; the totals
    are summed again from the attempts."""
    conn.execute(
        """
        UPDATE core.llm_call c SET
               request = coalesce(%(request)s, c.request), served_by = %(served_by)s, response = %(response)s,
               stop_reason = %(stop_reason)s, error = %(error)s, request_id = coalesce(%(request_id)s, c.request_id),
               attempts = b.attempts, input_tokens = coalesce(t.input_tokens, 0),
               output_tokens = coalesce(t.output_tokens, 0), cache_write_tokens = coalesce(t.cache_write_tokens, 0),
               cache_read_tokens = coalesce(t.cache_read_tokens, 0), cost_usd = t.cost_usd,
               unpriced_attempts = t.unpriced,
               check_run_id = coalesce(%(run)s, c.check_run_id), trial_id = coalesce(%(trial)s, c.trial_id),
               call_number = coalesce(%(number)s, c.call_number),
               annotation_run_id = coalesce(%(annotation_run)s, c.annotation_run_id)
        FROM (SELECT %(attempts)s::jsonb AS attempts) b
        CROSS JOIN LATERAL (
            SELECT sum((a->>'input_tokens')::bigint) AS input_tokens, sum((a->>'output_tokens')::bigint) AS output_tokens,
                   sum((a->>'cache_write_tokens')::bigint) AS cache_write_tokens,
                   sum((a->>'cache_read_tokens')::bigint) AS cache_read_tokens,
                   sum((a->>'cost_usd')::numeric(14, 6)) AS cost_usd,
                   count(*) FILTER (WHERE a->>'cost_usd' IS NULL) AS unpriced
            FROM jsonb_array_elements(b.attempts) a
        ) t
        WHERE c.id = %(id)s
        """,
        {
            "id": call_id,
            "request": Jsonb(request) if request is not None else None,
            "served_by": served_by,
            "response": response,
            "stop_reason": stop_reason,
            "error": error,
            "request_id": request_id,
            "attempts": Jsonb(attempts),
            "run": check_run_id,
            "trial": trial_id,
            "number": call_number,
            "annotation_run": annotation_run_id,
        },
    )
