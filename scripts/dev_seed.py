"""Writes db/dev/20261101150000_bruckner_seed.sql: Bruckner et al.'s 2025-11-10 CTIS exports, their
final verdicts and their manual reviews as one dev-only dbmate migration (applied by the
migrate-dev compose service).

Run from the repo root, with their files in data/bruckner/ (in git, see its README), after changing
those files, the loaders or the tables they fill:

    uv run python scripts/dev_seed.py

Rows, hashes and labels come from the same code as `trialwatch ctis seed` and `trialwatch gold
bruckner`, so the migration and the CLI never store the same row twice.
"""

import json
from pathlib import Path

from trialwatch.ctis.client import CT_NUMBER
from trialwatch.ctis.export import CT_COLUMN, captured_on_from_name
from trialwatch.ctis.export import read_rows as read_export
from trialwatch.ctis.store import content_hash
from trialwatch.gold.bruckner import labels
from trialwatch.gold.bruckner import read_rows as read_final
from trialwatch.gold.bruckner_reviews import document, reviews
from trialwatch.gold.bruckner_reviews import read_rows as read_reviews

ROOT = Path(__file__).resolve().parent.parent
BRUCKNER = ROOT / "data/bruckner"  # the four files we read, with their OSF paths in its README
EXPORT = BRUCKNER / "CTIS_trials_Phases_II-IV_20251110.csv"
FINAL = BRUCKNER / "2026-03-16_ctis_study_final_dataset.csv"
# Same day's export of the trials with a paediatric investigation plan (decides paediatric status)
PIP = BRUCKNER / "CTIS_trials_with_PIP_20251110.csv"
REVIEWS = BRUCKNER / "ctis_merged_reviews_consolidated_with_comments.csv"
OUT = ROOT / "db/dev/20261101150000_bruckner_seed.sql"
LOADED_BY = f"db/dev/{OUT.name}"
# The fetch_run row inserted just before, in the same session
RUN = "currval(pg_get_serial_sequence('ctis.fetch_run', 'id'))"


def literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, bytes):
        return f"'\\x{value.hex()}'"
    return "'" + str(value).replace("'", "''") + "'"


def payload(row: dict[str, str]) -> str:
    return literal(json.dumps(row, ensure_ascii=False, separators=(",", ":")))


def ct_number(row: dict[str, str], column: str) -> str:
    value = row[column].strip()
    if not CT_NUMBER.fullmatch(value):
        raise ValueError(f"invalid EU CT number {value!r}; a seed must hold every row")
    return value


def section(title: str, source: Path, args: dict, staged: str, columns: str, rows: list[str],
            insert: str, target: str, extra_staging: str = "") -> str:
    return f"""-- {title}
INSERT INTO ctis.fetch_run (kind, args) VALUES ('seed', {literal(json.dumps({"source_file": source.name, **args, "loaded_by": LOADED_BY}))});
CREATE TEMP TABLE {staged} ({columns}) ON COMMIT DROP;
INSERT INTO {staged} VALUES
{",\n".join(rows)};
{extra_staging}INSERT INTO ctis.trial (ct_number) SELECT DISTINCT ct_number FROM {staged} ON CONFLICT (ct_number) DO NOTHING;
{insert}
UPDATE ctis.fetch_run
SET status = 'succeeded', finished_at = now(),
    trials_fetched = (SELECT count(*) FROM {staged}),
    trials_changed = (SELECT count(*) FROM {target} WHERE fetch_run_id = {RUN})
WHERE id = {RUN};
"""


def export_section(path: Path, staged: str) -> str:
    captured_on = captured_on_from_name(path)
    rows = [
        f"({literal(ct_number(row, CT_COLUMN))}, {literal(content_hash(row))}, {payload(row)})"
        for row in read_export(path)
    ]
    return section(
        f"CTIS search export {path.name} ({len(rows)} rows) -> ctis.search_export_snapshot",
        path, {"captured_on": captured_on.isoformat()}, staged,
        "ct_number text, content_hash bytea, payload jsonb", rows,
        f'''INSERT INTO ctis.search_export_snapshot (ct_number, fetch_run_id, captured_on, source_file, content_hash, payload)
SELECT ct_number, {RUN}, {literal(captured_on.isoformat())}, {literal(path.name)}, content_hash, payload
FROM {staged}
ON CONFLICT (ct_number, captured_on, source_file, content_hash) DO NOTHING;''',
        "ctis.search_export_snapshot",
    )


def render() -> str:
    """The migration's full text; tests/test_dev_seed.py checks the committed file still matches it."""
    final = [
        "(" + ", ".join(literal(v) for v in (ct_number(row, "trial_number"), *labels(row), content_hash(row)))
        + f", {payload(row)})"
        for row in read_final(FINAL)
    ]
    review_rows, replaced = read_reviews(REVIEWS)
    documents, answers = [], []
    for row in review_rows:
        ct = ct_number(row, "trial_number")
        documents.append("(" + ", ".join(literal(v) for v in (ct, *document(row), content_hash(row))) + f", {payload(row)})")
        answers += ["(" + ", ".join(literal(v) for v in (ct, *answer)) + ")" for answer in reviews(row)]
    return f"""-- migrate:up

-- Dev and test data only. db/migrations never includes it: only the migrate-dev compose service
-- passes db/dev/ to dbmate, and records it in its own table, dev_schema_migrations.
-- Generated by scripts/dev_seed.py; don't edit by hand.
--
-- Data: Bruckner T, Dike CE, Caquelin L, et al. Assessing Compliance with Reporting Requirements in
-- European Phase II-IV Clinical Trials (medRxiv preprint, 2026); data and code on OSF
-- (osf.io/sn4j2), used here with the authors' permission (see data/bruckner/README.md). The CTIS
-- export rows are EMA data (reuse allowed, crediting EMA). This file is built from data/bruckner/
-- and is not committed; rebuild it with `uv run python scripts/dev_seed.py`.
-- Rows are stored as `trialwatch ctis seed`, `trialwatch gold bruckner` and `trialwatch gold
-- bruckner-reviews` store them; rows already loaded by those commands are skipped.

{export_section(EXPORT, "dev_export_row")}
{export_section(PIP, "dev_pip_row")}
{section(
    f"Final verdict per study trial ({len(final)} rows) -> gold.bruckner_verdict",
    FINAL, {"table": "gold.bruckner_verdict"}, "dev_verdict_row",
    "ct_number text, verdict text, results_document text, lay_summary boolean, days_to_results integer, "
    "comment text, content_hash bytea, payload jsonb", final,
    f'''INSERT INTO gold.bruckner_verdict (ct_number, fetch_run_id, source_file, verdict, results_document,
                                   lay_summary, days_to_results, comment, content_hash, payload)
SELECT ct_number, {RUN}, {literal(FINAL.name)}, verdict, results_document,
       lay_summary, days_to_results, comment, content_hash, payload
FROM dev_verdict_row
ON CONFLICT (ct_number) DO NOTHING;''',
    "gold.bruckner_verdict",
)}
{section(
    f"Manual review of the results documents ({len(documents)} documents, {len(answers)} reviewer answers)"
    " -> gold.bruckner_document, gold.bruckner_review",
    REVIEWS, {"table": "gold.bruckner_document", "replaced_characters": replaced}, "dev_document_row",
    "ct_number text, results_posted_on date, reviewers_agreed boolean, final_comment text, content_hash bytea, "
    "payload jsonb", documents,
    f'''INSERT INTO gold.bruckner_document (ct_number, fetch_run_id, source_file, results_posted_on,
                                    reviewers_agreed, final_comment, content_hash, payload)
SELECT ct_number, {RUN}, {literal(REVIEWS.name)}, results_posted_on, reviewers_agreed, final_comment, content_hash, payload
FROM dev_document_row
ON CONFLICT (ct_number) DO NOTHING;
INSERT INTO gold.bruckner_review (ct_number, reviewer, participants, outcomes, cancelled, asked_to_check, comment)
SELECT r.ct_number, r.reviewer, r.participants, r.outcomes, r.cancelled, r.asked_to_check, r.comment
FROM dev_review_row r
JOIN dev_document_row d USING (ct_number)
JOIN gold.bruckner_document g ON g.ct_number = d.ct_number AND g.content_hash = d.content_hash
ON CONFLICT (ct_number, reviewer) DO NOTHING;''',
    "gold.bruckner_document",
    extra_staging=f'''CREATE TEMP TABLE dev_review_row (ct_number text, reviewer text, participants boolean, outcomes boolean, cancelled boolean, asked_to_check boolean, comment text) ON COMMIT DROP;
INSERT INTO dev_review_row VALUES
{",\n".join(answers)};
''',
)}

-- migrate:down
-- Removes what this seed added, and the trials nothing else refers to any more.
DELETE FROM gold.bruckner_review
WHERE ct_number IN (SELECT ct_number FROM gold.bruckner_document WHERE fetch_run_id IN
                    (SELECT id FROM ctis.fetch_run WHERE args->>'loaded_by' = {literal(LOADED_BY)}));
DELETE FROM gold.bruckner_document
WHERE fetch_run_id IN (SELECT id FROM ctis.fetch_run WHERE args->>'loaded_by' = {literal(LOADED_BY)});
DELETE FROM gold.bruckner_verdict
WHERE fetch_run_id IN (SELECT id FROM ctis.fetch_run WHERE args->>'loaded_by' = {literal(LOADED_BY)});
DELETE FROM ctis.search_export_snapshot
WHERE fetch_run_id IN (SELECT id FROM ctis.fetch_run WHERE args->>'loaded_by' = {literal(LOADED_BY)});
DELETE FROM ctis.fetch_run WHERE args->>'loaded_by' = {literal(LOADED_BY)};
DELETE FROM ctis.trial t
WHERE NOT EXISTS (SELECT FROM ctis.trial_snapshot s WHERE s.ct_number = t.ct_number)
  AND NOT EXISTS (SELECT FROM ctis.search_export_snapshot e WHERE e.ct_number = t.ct_number)
  AND NOT EXISTS (SELECT FROM gold.bruckner_verdict v WHERE v.ct_number = t.ct_number)
  AND NOT EXISTS (SELECT FROM gold.bruckner_document d WHERE d.ct_number = t.ct_number);
"""


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
