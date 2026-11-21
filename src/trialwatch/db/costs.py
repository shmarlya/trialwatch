"""Queries on what TrialWatch spends on paid APIs (core.api_call) and its spending cap
(core.app_setting), see db/migrations/*_spend.sql. Money is US dollars, as Decimal."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from trialwatch.costs import CallCost

MONTHLY_CAP = "spend.monthly_cap_usd"


def record_call(conn: psycopg.Connection, cost: CallCost, *, operation: str, check_run_id: int | None = None,
                trial_id: int | None = None, call_number: int | None = None, request_id: str | None = None) -> None:
    """Add one billed attempt, made for the session's actor."""
    prices = None
    if cost.prices is not None:
        prices = Jsonb(cost.prices)
    conn.execute(
        "INSERT INTO core.api_call (actor_id, model, operation, check_run_id, trial_id, call_number, input_tokens, "
        "output_tokens, cache_write_tokens, cache_read_tokens, cost_usd, prices, request_id) "
        "VALUES (current_setting('trialwatch.actor')::bigint, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (cost.model, operation, check_run_id, trial_id, call_number, cost.input_tokens, cost.output_tokens,
         cost.cache_write_tokens, cost.cache_read_tokens, cost.cost_usd, prices, request_id),
    )


def run_cost(conn: psycopg.Connection, run_id: int) -> Decimal:
    row = conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM core.api_call WHERE check_run_id = %s",
                       (run_id,)).fetchone()
    return row[0]


def spent_since(conn: psycopg.Connection, since: datetime) -> Decimal:
    row = conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM core.api_call WHERE at >= %s", (since,)).fetchone()
    return row[0]


def spent_this_month(conn: psycopg.Connection) -> Decimal:
    """What every paid call cost since the start of this calendar month (UTC)."""
    row = conn.execute(
        "SELECT coalesce(sum(cost_usd), 0) FROM core.api_call WHERE at >= date_trunc('month', now())"
    ).fetchone()
    return row[0]


@dataclass(frozen=True)
class TrialSpend:
    cost_usd: Decimal
    unpriced_calls: int


def trial_spend(conn: psycopg.Connection, run_id: int) -> dict[int, TrialSpend]:
    """Trial id -> what its billed calls in the run cost."""
    rows = conn.execute(
        "SELECT trial_id, coalesce(sum(cost_usd), 0), count(*) FILTER (WHERE cost_usd IS NULL) "
        "FROM core.api_call WHERE check_run_id = %s AND trial_id IS NOT NULL GROUP BY trial_id",
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
    calls: int
    unpriced_calls: int


def spend_totals(conn: psycopg.Connection) -> SpendTotals:
    row = conn.execute(
        """
        SELECT coalesce(sum(cost_usd), 0),
               coalesce(sum(cost_usd) FILTER (WHERE at >= date_trunc('month', now())), 0),
               coalesce(sum(cost_usd) FILTER (WHERE at >= now() - interval '30 days'), 0),
               count(*), count(*) FILTER (WHERE cost_usd IS NULL)
          FROM core.api_call
        """
    ).fetchone()
    return SpendTotals(row[0], row[1], row[2], row[3], row[4])


@dataclass(frozen=True)
class SpendRow:
    """Spend of one group (a month, a model, an actor, ...)."""

    name: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    unpriced_calls: int


# Grouping expressions the spend page offers; fixed SQL text, chosen by name, never from input
_GROUPS = {
    "month": ("to_char(date_trunc('month', c.at), 'YYYY-MM')", "1 DESC"),
    "model": ("c.model", "5 DESC"),
    "operation": ("c.operation", "5 DESC"),
    "actor": ("a.kind || ':' || a.name", "5 DESC"),
}


def spend_by(conn: psycopg.Connection, group: str) -> list[SpendRow]:
    """Spend grouped by month, model, operation or actor (an actor shown as "kind:name")."""
    expression, order = _GROUPS[group]
    query = (
        f"SELECT {expression}, count(*), coalesce(sum(c.input_tokens), 0), coalesce(sum(c.output_tokens), 0), "
        "coalesce(sum(c.cost_usd), 0), count(*) FILTER (WHERE c.cost_usd IS NULL) "
        "FROM core.api_call c JOIN core.actor a ON a.id = c.actor_id "
        f"GROUP BY 1 ORDER BY {order}"
    )
    rows = conn.execute(query).fetchall()
    found = []
    for row in rows:
        found.append(SpendRow(row[0], row[1], row[2], row[3], row[4], row[5]))
    return found


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
            "SELECT r.id, r.started_at, a.name AS actor, r.model, r.trials, r.cost_usd "
            "FROM checks.run r JOIN core.actor a ON a.id = r.actor_id "
            "WHERE r.cost_usd > 0 ORDER BY r.cost_usd DESC, r.id DESC LIMIT %s",
            (limit,),
        )
        return cursor.fetchall()
