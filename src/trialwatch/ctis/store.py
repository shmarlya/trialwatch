"""Writes CTIS trials into the `ctis` schema (see db/migrations/*_init.sql)."""

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import psycopg
from psycopg.types.json import Jsonb

# Top-level fields that change when CTIS republishes, without the trial itself changing.
# They stay in the stored payload but are left out of the content hash.
VOLATILE_FIELDS = frozenset({"publishDate"})


def content_hash(payload: dict) -> bytes:
    stable = {k: v for k, v in payload.items() if k not in VOLATILE_FIELDS}
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).digest()


def start_run(conn: psycopg.Connection, kind: str, args: dict) -> int:
    with conn.transaction():
        return conn.execute(
            "INSERT INTO ctis.fetch_run (kind, args) VALUES (%s, %s) RETURNING id",
            (kind, Jsonb(args)),
        ).fetchone()[0]


def save_trial(conn: psycopg.Connection, run_id: int, ct_number: str, payload: dict) -> bool:
    """Record that the trial was seen; add a snapshot if its content changed. Returns True if it did."""
    digest = content_hash(payload)
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO ctis.trial (ct_number) VALUES (%s)
            ON CONFLICT (ct_number) DO UPDATE SET last_seen_at = now()
            """,
            (ct_number,),
        )
        latest = conn.execute(
            """
            SELECT content_hash FROM ctis.trial_snapshot
            WHERE ct_number = %s ORDER BY fetched_at DESC LIMIT 1
            """,
            (ct_number,),
        ).fetchone()
        if latest and latest[0] == digest:
            return False
        conn.execute(
            """
            INSERT INTO ctis.trial_snapshot (ct_number, fetch_run_id, content_hash, payload)
            VALUES (%s, %s, %s, %s)
            """,
            (ct_number, run_id, digest, Jsonb(payload)),
        )
        return True


def finish_run(
    conn: psycopg.Connection, run_id: int, *, fetched: int, changed: int, failed: int, error: str | None
) -> None:
    with conn.transaction():
        conn.execute(
            """
            UPDATE ctis.fetch_run
            SET status = %s, finished_at = now(), error = %s,
                trials_fetched = %s, trials_changed = %s, trials_failed = %s
            WHERE id = %s
            """,
            ("failed" if error else "succeeded", error, fetched, changed, failed, run_id),
        )


@dataclass
class RunResult:
    run_id: int
    fetched: int = 0
    changed: int = 0
    failed: dict[str, str] = field(default_factory=dict)


def fetch_trials(
    conn: psycopg.Connection,
    retrieve: Callable[[str], dict],
    ct_numbers: Iterable[str],
    *,
    kind: str,
    args: dict,
) -> RunResult:
    """Fetch each trial and save it. One bad trial doesn't stop the run; it's counted as failed."""
    result = RunResult(run_id=start_run(conn, kind, args))
    error = None
    try:
        for ct_number in ct_numbers:
            try:
                payload = retrieve(ct_number)
            except Exception as exc:  # recorded per trial, the run continues
                result.failed[ct_number] = f"{type(exc).__name__}: {exc}"
                continue
            result.fetched += 1
            result.changed += save_trial(conn, result.run_id, ct_number, payload)
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if result.failed and not error:
            error = f"{len(result.failed)} trial(s) failed: " + "; ".join(
                f"{ct}: {msg}" for ct, msg in result.failed.items()
            )
        finish_run(
            conn, result.run_id,
            fetched=result.fetched, changed=result.changed, failed=len(result.failed), error=error,
        )
    return result
