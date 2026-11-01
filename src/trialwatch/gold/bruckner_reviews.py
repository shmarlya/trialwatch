"""Loads Bruckner et al.'s manual review of the results documents into gold.bruckner_document and
gold.bruckner_review (see db/migrations/*_gold_bruckner_reviews.sql).

The file is their ctis_merged_reviews_consolidated_with_comments.csv: one row per results document,
written by R ('NA' for empty), each reviewer's answers in columns of their own. It is not valid
UTF-8 (text copied from CTIS was mangled before publication), so it is read with broken bytes
replaced by U+FFFD. Only free text is affected, never a label.
"""

import csv
import io
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from trialwatch.ctis.client import CT_NUMBER
from trialwatch.ctis.store import RunResult, content_hash, finish_run, start_run

# Reviewer -> suffix of their columns in the file
REVIEWERS = {"first": "till_christie", "second": "team", "third": "ghassan"}
# Their "asked for a check" columns don't follow the suffix pattern for the first reviewer
CHECK_COLUMN = {"first": "check", "second": "check_team", "third": "check_ghassan"}
PARTS = ("participants", "outcomes", "cancelled")
COLUMNS = (
    "trial_number", "results_pub_date_DDMMYYYY", "agreement_flag", "comments_final",
    *(f"{part}_{suffix}" for suffix in REVIEWERS.values() for part in PARTS),
    *(f"comments_{suffix}" for suffix in REVIEWERS.values()),
    *CHECK_COLUMN.values(),
)
FLAGS = {"1": True, "0": False, "NA": None}


def _text(value: str) -> str | None:
    value = value.strip()
    return None if value in ("", "NA") else value


def _flag(row: dict[str, str], column: str) -> bool | None:
    if row[column] not in FLAGS:
        raise ValueError(f"{column} is {row[column]!r}, expected 1, 0 or NA")
    return FLAGS[row[column]]


def document(row: dict[str, str]) -> tuple:
    """(results_posted_on, reviewers_agreed, final_comment) of one row; ValueError if malformed."""
    posted_on = datetime.strptime(row["results_pub_date_DDMMYYYY"].strip(), "%d/%m/%Y").date()
    if row["agreement_flag"] not in ("TRUE", "FALSE"):
        raise ValueError(f"agreement_flag is {row['agreement_flag']!r}, expected TRUE or FALSE")
    return posted_on, row["agreement_flag"] == "TRUE", _text(row["comments_final"])


def reviews(row: dict[str, str]) -> list[tuple]:
    """(reviewer, participants, outcomes, cancelled, asked_to_check, comment) for every reviewer who
    answered at least one part."""
    found = []
    for reviewer, suffix in REVIEWERS.items():
        parts = [_flag(row, f"{part}_{suffix}") for part in PARTS]
        if any(p is not None for p in parts):
            found.append((reviewer, *parts, _flag(row, CHECK_COLUMN[reviewer]), _text(row[f"comments_{suffix}"])))
    return found


def read_rows(path: Path) -> tuple[list[dict[str, str]], int]:
    """The rows, and how many broken byte sequences were replaced by U+FFFD."""
    text = path.read_bytes().decode("utf-8", errors="replace").removeprefix("﻿")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"{path.name} has no {', '.join(missing)} column(s); is it the merged reviews file?")
    return list(reader), text.count("�")


def seed_reviews(conn: psycopg.Connection, path: Path) -> RunResult:
    """Store every valid row in one transaction. Documents already stored are skipped; a stored
    document that differs from the file (a later release) is reported as failed and left as it was."""
    rows, replaced = read_rows(path)
    result = RunResult(run_id=start_run(conn, "seed", {
        "source_file": path.name, "table": "gold.bruckner_document", "replaced_characters": replaced,
    }))
    error = None
    try:
        documents, review_rows = [], []
        # Rows, not lines: comments contain line breaks
        for number, row in enumerate(rows, start=1):
            ct_number = row["trial_number"].strip()
            if not CT_NUMBER.fullmatch(ct_number):
                result.failed[f"row {number}"] = f"invalid EU CT number {ct_number!r}"
                continue
            try:
                doc, answers = document(row), reviews(row)
            except ValueError as exc:
                result.failed[ct_number] = str(exc)
                continue
            documents.append((ct_number, *doc, content_hash(row), Jsonb(row)))
            review_rows += [(ct_number, *answer) for answer in answers]
        result.fetched = len(documents)
        with conn.transaction():
            conn.execute(
                """
                CREATE TEMP TABLE document_row (
                    ct_number text, results_posted_on date, reviewers_agreed boolean, final_comment text,
                    content_hash bytea, payload jsonb
                ) ON COMMIT DROP
                """
            )
            conn.execute(
                """
                CREATE TEMP TABLE review_row (
                    ct_number text, reviewer text, participants boolean, outcomes boolean, cancelled boolean,
                    asked_to_check boolean, comment text
                ) ON COMMIT DROP
                """
            )
            with conn.cursor() as cur:
                with cur.copy("COPY document_row FROM STDIN") as copy:
                    for item in documents:
                        copy.write_row(item)
                with cur.copy("COPY review_row FROM STDIN") as copy:
                    for item in review_rows:
                        copy.write_row(item)
            conn.execute(
                "INSERT INTO ctis.trial (ct_number) SELECT DISTINCT ct_number FROM document_row "
                "ON CONFLICT (ct_number) DO NOTHING"
            )
            inserted = conn.execute(
                """
                INSERT INTO gold.bruckner_document (ct_number, fetch_run_id, source_file, results_posted_on,
                                                    reviewers_agreed, final_comment, content_hash, payload)
                SELECT ct_number, %s, %s, results_posted_on, reviewers_agreed, final_comment, content_hash, payload
                FROM document_row
                ON CONFLICT (ct_number) DO NOTHING
                """,
                (result.run_id, path.name),
            )
            result.changed = inserted.rowcount
            # Reviews only for documents stored from this same row, never mixed with another release
            conn.execute(
                """
                INSERT INTO gold.bruckner_review (ct_number, reviewer, participants, outcomes, cancelled,
                                                  asked_to_check, comment)
                SELECT r.ct_number, r.reviewer, r.participants, r.outcomes, r.cancelled, r.asked_to_check, r.comment
                FROM review_row r
                JOIN document_row d USING (ct_number)
                JOIN gold.bruckner_document g ON g.ct_number = d.ct_number AND g.content_hash = d.content_hash
                ON CONFLICT (ct_number, reviewer) DO NOTHING
                """
            )
            differing = conn.execute(
                """
                SELECT d.ct_number, g.source_file FROM document_row d JOIN gold.bruckner_document g USING (ct_number)
                WHERE g.content_hash <> d.content_hash ORDER BY d.ct_number
                """
            ).fetchall()
            for ct_number, source_file in differing:
                result.failed[ct_number] = f"differs from the row loaded from {source_file}; left as it was"
            # ON COMMIT DROP only fires at the outermost commit; drop now so a second load works
            conn.execute("DROP TABLE document_row, review_row")
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
