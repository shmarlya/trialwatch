"""Queries on core.reference_load: each load of reference data (GeoNames, MeSH), with the files it read."""

import psycopg
from psycopg.types.json import Jsonb


def start_load(conn: psycopg.Connection, name: str, license: str, params: dict) -> int:
    """A load of `name` (geonames, mesh) begins, by the session's actor; returns its id."""
    row = conn.execute(
        "INSERT INTO core.reference_load (name, license, params, created_by) "
        "VALUES (%s, %s, %s, current_setting('trialwatch.actor')::bigint) RETURNING id",
        (name, license, Jsonb(params)),
    ).fetchone()
    return row[0]


def finish_load(
    conn: psycopg.Connection, load_id: int, files: list[dict], counts: dict, version: str | None = None
) -> None:
    """The load ended: each file it read ({name, from, sha256}), the rows it holds, the release read."""
    conn.execute(
        "UPDATE core.reference_load SET files = %s, counts = %s, version = %s, finished_at = clock_timestamp() "
        "WHERE id = %s",
        (Jsonb(files), Jsonb(counts), version, load_id),
    )
