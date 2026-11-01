import csv

import pytest

from trialwatch.gold.bruckner import COLUMNS, seed_verdicts

# Valid format but not real trials, so tests never collide with seeded or fetched data
CT_A, CT_B = "1999-000000-00-01", "1999-000000-00-02"
HEADER = [*COLUMNS, "Title.of.the.trial"]


def row(ct_number, verdict, document, lay="1", days="NA", comment="NA", title="A trial"):
    flags = {"full": "NA", "incomplete": "NA", "none": "NA", document: "1"}
    return [ct_number, verdict, lay, days, comment, flags["full"], flags["incomplete"], flags["none"], title]


def write_dataset(path, rows):
    # Written by R: no byte-order mark, 'NA' for empty
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(rows)
    return path


def stored(conn):
    return conn.execute(
        """
        SELECT ct_number, verdict, results_document, lay_summary, days_to_results, comment
        FROM gold.bruckner_verdict WHERE ct_number IN (%s, %s) ORDER BY ct_number
        """,
        (CT_A, CT_B),
    ).fetchall()


def test_labels_stored_typed_and_reload_adds_nothing(conn, tmp_path):
    path = write_dataset(tmp_path / "2026-03-16_ctis_study_final_dataset.csv", [
        row(CT_A, "reported fully late", "full", days="400", title="Line one\nline two"),
        row(CT_B, "not fully reported", "incomplete", lay="NA", comment="Only 1/2 primary outcomes reported."),
    ])

    first = seed_verdicts(conn, path)
    again = seed_verdicts(conn, path)

    assert (first.fetched, first.changed, again.changed, again.failed) == (2, 2, 0, {})
    assert stored(conn) == [
        (CT_A, "reported fully late", "full", True, 400, None),
        (CT_B, "not fully reported", "incomplete", False, None, "Only 1/2 primary outcomes reported."),
    ]
    payload = conn.execute("SELECT payload FROM gold.bruckner_verdict WHERE ct_number = %s", (CT_A,)).fetchone()[0]
    assert payload["Title.of.the.trial"] == "Line one\nline two" and payload["comments"] == "NA"


def test_inconsistent_rows_are_skipped_and_recorded(conn, tmp_path):
    two_flags = row(CT_B, "not fully reported", "none")
    two_flags[COLUMNS.index("full_scientific_results")] = "1"
    path = write_dataset(tmp_path / "final.csv", [
        row(CT_A, "reported fully on time", "full", days="NA"),  # fully reported needs days_to_results
        two_flags,
        row("not-a-number", "cancelled", "incomplete"),
    ])

    result = seed_verdicts(conn, path)

    assert (result.fetched, result.changed) == (0, 0)
    assert list(result.failed) == [CT_A, CT_B, "row 3"]
    assert "one results-document flag" in result.failed[CT_B]
    status, error = conn.execute("SELECT status, error FROM ctis.fetch_run WHERE id = %s", (result.run_id,)).fetchone()
    assert status == "failed" and "days_to_results" in error


def test_changed_release_is_reported_not_written_over(conn, tmp_path):
    seed_verdicts(conn, write_dataset(tmp_path / "old.csv", [row(CT_A, "extended", "none", lay="NA")]))

    result = seed_verdicts(conn, write_dataset(tmp_path / "new.csv", [row(CT_A, "cancelled", "incomplete")]))

    assert result.changed == 0
    assert result.failed == {CT_A: "differs from the row loaded from old.csv; left as it was"}
    assert stored(conn) == [(CT_A, "extended", "none", False, None, None)]


def test_file_without_label_columns_is_rejected(conn, tmp_path):
    path = tmp_path / "CTIS_trials_20251110.csv"
    path.write_text("Trial number,Trial results\n1999-000000-00-01,Yes\n", encoding="utf-8")
    with pytest.raises(ValueError, match="legal_compliance_assessment"):
        seed_verdicts(conn, path)
