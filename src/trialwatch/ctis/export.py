"""Loads a CTIS portal CSV export into ctis.search_export_snapshot (see db/migrations/*_init.sql).

The export is what the public CTIS search lets you download: one row per trial, human-readable
labels, dates as dd/mm/yyyy text. Rows are stored exactly as they are in the file.
"""

import csv
import re
from datetime import date, datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from trialwatch.ctis.client import CT_NUMBER
from trialwatch.ctis.store import RunResult, content_hash, finish_run, start_run

CT_COLUMN = "Trial number"
# CTIS names its exports ..._YYYYMMDD.csv, e.g. CTIS_trials_Phases_II-IV_20251110.csv
FILE_DATE = re.compile(r"_(\d{8})\.csv$", re.IGNORECASE)


def captured_on_from_name(path: Path) -> date:
    match = FILE_DATE.search(path.name)
    if not match:
        raise ValueError(f"no _YYYYMMDD date in {path.name}; pass the export date explicitly")
    return datetime.strptime(match[1], "%Y%m%d").date()


def read_rows(path: Path) -> list[dict[str, str]]:
    # utf-8-sig: the portal writes a byte-order mark
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if CT_COLUMN not in (reader.fieldnames or []):
            raise ValueError(f"{path.name} has no '{CT_COLUMN}' column; is it a CTIS export?")
        return list(reader)


def seed_export(conn: psycopg.Connection, path: Path, captured_on: date) -> RunResult:
    """Store every row with a valid EU CT number in one transaction. Rows already stored are skipped."""
    rows = read_rows(path)
    result = RunResult(run_id=start_run(
        conn, "seed", {"source_file": path.name, "captured_on": captured_on.isoformat()}
    ))
    error = None
    try:
        valid = []
        for line, row in enumerate(rows, start=2):  # line 1 is the header
            ct_number = row[CT_COLUMN].strip()
            if CT_NUMBER.fullmatch(ct_number):
                valid.append((ct_number, content_hash(row), Jsonb(row)))
            else:
                result.failed[f"line {line}"] = f"invalid EU CT number {ct_number!r}"
        result.fetched = len(valid)
        with conn.transaction():
            conn.execute(
                "CREATE TEMP TABLE export_row (ct_number text, content_hash bytea, payload jsonb) ON COMMIT DROP"
            )
            with conn.cursor() as cur, cur.copy("COPY export_row (ct_number, content_hash, payload) FROM STDIN") as copy:
                for item in valid:
                    copy.write_row(item)
            conn.execute(
                "INSERT INTO ctis.trial (ct_number) SELECT DISTINCT ct_number FROM export_row "
                "ON CONFLICT (ct_number) DO NOTHING"
            )
            inserted = conn.execute(
                """
                INSERT INTO ctis.search_export_snapshot
                    (ct_number, fetch_run_id, captured_on, source_file, content_hash, payload)
                SELECT ct_number, %s, %s, %s, content_hash, payload FROM export_row
                ON CONFLICT (ct_number, captured_on, source_file, content_hash) DO NOTHING
                """,
                (result.run_id, captured_on, path.name),
            )
            result.changed = inserted.rowcount
            # ON COMMIT DROP only fires at the outermost commit; drop now so a second load works
            conn.execute("DROP TABLE export_row")
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if result.failed and not error:
            error = f"{len(result.failed)} row(s) skipped: " + "; ".join(
                f"{where}: {msg}" for where, msg in result.failed.items()
            )
        finish_run(
            conn, result.run_id,
            fetched=result.fetched, changed=result.changed, failed=len(result.failed), error=error,
        )
    return result
