"""Loads Bruckner et al.'s final verdict per trial into gold.bruckner_verdict (see db/migrations/*_init.sql).

The file is their <date>_ctis_study_final_dataset.csv: one row per study trial, written by R, so
empty values are 'NA' and yes/no flags are '1' or 'NA'. Each row is stored exactly as it is, and
the labels we compare against are also copied into typed columns.
"""

import csv
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from trialwatch.ctis.client import CT_NUMBER
from trialwatch.ctis.store import RunResult, content_hash, finish_run, start_run

VERDICTS = frozenset({"reported fully on time", "reported fully late", "not fully reported", "extended", "cancelled"})
# Their three flags for what the scientific results document contained; exactly one is set per trial
RESULTS_DOCUMENT = {
    "full_scientific_results": "full",
    "incomplete_scientific_results": "incomplete",
    "no_scientific_results_document": "none",
}
COLUMNS = ("trial_number", "legal_compliance_assessment", "laypersons_results", "days_to_results", "comments",
           *RESULTS_DOCUMENT)


def _value(text: str) -> str | None:
    return None if text == "NA" else text


def labels(row: dict[str, str]) -> tuple:
    """(verdict, results_document, lay_summary, days_to_results, comment) of one row; ValueError if inconsistent."""
    verdict = row["legal_compliance_assessment"]
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    documents = [label for column, label in RESULTS_DOCUMENT.items() if row[column] == "1"]
    if len(documents) != 1:
        raise ValueError(f"expected one results-document flag, found {len(documents)}")
    days = _value(row["days_to_results"])
    if (days is None) == verdict.startswith("reported fully"):
        raise ValueError(f"days_to_results {row['days_to_results']!r} doesn't fit verdict {verdict!r}")
    return (verdict, documents[0], row["laypersons_results"] == "1",
            None if days is None else int(days), _value(row["comments"]))


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name} has no {', '.join(missing)} column(s); is it the final study dataset?")
        return list(reader)


def seed_verdicts(conn: psycopg.Connection, path: Path) -> RunResult:
    """Store every valid row in one transaction. Trials already stored are skipped; a stored row that
    differs from the file (a later release) is reported as failed and left as it was."""
    rows = read_rows(path)
    result = RunResult(run_id=start_run(conn, "seed", {"source_file": path.name, "table": "gold.bruckner_verdict"}))
    error = None
    try:
        valid = []
        # Rows, not lines: titles and comments contain line breaks
        for number, row in enumerate(rows, start=1):
            ct_number = row["trial_number"].strip()
            if not CT_NUMBER.fullmatch(ct_number):
                result.failed[f"row {number}"] = f"invalid EU CT number {ct_number!r}"
                continue
            try:
                valid.append((ct_number, *labels(row), content_hash(row), Jsonb(row)))
            except ValueError as exc:
                result.failed[ct_number] = str(exc)
        result.fetched = len(valid)
        with conn.transaction():
            conn.execute(
                """
                CREATE TEMP TABLE verdict_row (
                    ct_number text, verdict text, results_document text, lay_summary boolean,
                    days_to_results integer, comment text, content_hash bytea, payload jsonb
                ) ON COMMIT DROP
                """
            )
            with conn.cursor() as cur, cur.copy("COPY verdict_row FROM STDIN") as copy:
                for item in valid:
                    copy.write_row(item)
            conn.execute(
                "INSERT INTO ctis.trial (ct_number) SELECT DISTINCT ct_number FROM verdict_row "
                "ON CONFLICT (ct_number) DO NOTHING"
            )
            inserted = conn.execute(
                """
                INSERT INTO gold.bruckner_verdict (ct_number, fetch_run_id, source_file, verdict, results_document,
                                                   lay_summary, days_to_results, comment, content_hash, payload)
                SELECT ct_number, %s, %s, verdict, results_document,
                       lay_summary, days_to_results, comment, content_hash, payload
                FROM verdict_row
                ON CONFLICT (ct_number) DO NOTHING
                """,
                (result.run_id, path.name),
            )
            result.changed = inserted.rowcount
            differing = conn.execute(
                """
                SELECT r.ct_number, v.source_file FROM verdict_row r JOIN gold.bruckner_verdict v USING (ct_number)
                WHERE v.content_hash <> r.content_hash ORDER BY r.ct_number
                """
            ).fetchall()
            for ct_number, source_file in differing:
                result.failed[ct_number] = f"differs from the row loaded from {source_file}; left as it was"
            # ON COMMIT DROP only fires at the outermost commit; drop now so a second load works
            conn.execute("DROP TABLE verdict_row")
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
