import csv
import io
from datetime import date

import pytest

from trialwatch.gold.bruckner_reviews import COLUMNS, seed_reviews

# Valid format but not real trials, so tests never collide with seeded or fetched data
CT_A, CT_B = "1999-000000-00-01", "1999-000000-00-02"
HEADER = [*COLUMNS, "reviewer_team"]


def row(ct_number, first=("1", "1", "0"), second=("1", "0", "0"), third=("NA", "NA", "NA"), agreed="TRUE",
        posted="12/09/2025", comments=("", "NA", "NA"), checks=("NA", "0", "NA"), final_comment=""):
    values = {"trial_number": ct_number, "results_pub_date_DDMMYYYY": posted, "agreement_flag": agreed,
              "comments_final": final_comment, "reviewer_team": "Someone_Else"}
    for suffix, parts, comment in zip(("till_christie", "team", "ghassan"), (first, second, third), comments):
        values |= dict(zip((f"participants_{suffix}", f"outcomes_{suffix}", f"cancelled_{suffix}"), parts))
        values[f"comments_{suffix}"] = comment
    values |= dict(zip(("check", "check_team", "check_ghassan"), checks))
    return [values[h] for h in HEADER]


def write_reviews(path, rows, broken=b""):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(HEADER)
    writer.writerows(rows)
    # The real file is not valid UTF-8: `broken` stands in for its mangled bytes
    path.write_bytes(buffer.getvalue().encode("utf-8").replace(b"@BROKEN@", broken))
    return path


def stored_reviews(conn):
    return conn.execute(
        """
        SELECT ct_number, reviewer, participants, outcomes, cancelled, asked_to_check, comment
        FROM gold.bruckner_review WHERE ct_number IN (%s, %s) ORDER BY ct_number, reviewer
        """,
        (CT_A, CT_B),
    ).fetchall()


def test_documents_and_each_reviewers_answers_stored_and_reload_adds_nothing(conn, tmp_path):
    path = write_reviews(tmp_path / "ctis_merged_reviews_consolidated_with_comments.csv", [
        row(CT_A, agreed="FALSE", third=("1", "1", "0"), comments=("Only 1/2\nprimary outcomes.", "NA", "NA"),
            checks=("1", "0", "0"), final_comment="Vague outcomes."),
        # The second reviewer left every part blank: no row for them
        row(CT_B, second=("NA", "NA", "NA"), checks=("NA", "1", "NA")),
    ])

    first = seed_reviews(conn, path)
    again = seed_reviews(conn, path)

    assert (first.fetched, first.changed, again.changed, again.failed) == (2, 2, 0, {})
    documents = conn.execute(
        "SELECT ct_number, results_posted_on, reviewers_agreed, final_comment FROM gold.bruckner_document "
        "WHERE ct_number IN (%s, %s) ORDER BY ct_number", (CT_A, CT_B),
    ).fetchall()
    assert documents == [(CT_A, date(2025, 9, 12), False, "Vague outcomes."), (CT_B, date(2025, 9, 12), True, None)]
    assert stored_reviews(conn) == [
        (CT_A, "first", True, True, False, True, "Only 1/2\nprimary outcomes."),
        (CT_A, "second", True, False, False, False, None),
        (CT_A, "third", True, True, False, False, None),
        (CT_B, "first", True, True, False, None, None),
    ]


def test_broken_bytes_are_replaced_and_counted(conn, tmp_path):
    path = write_reviews(tmp_path / "reviews.csv", [row(CT_A, comments=("Zeqmelit@BROKEN@ 6 mg", "NA", "NA"))],
                         broken=b"\x83\x9b")

    result = seed_reviews(conn, path)

    assert result.changed == 1
    comment = conn.execute("SELECT comment FROM gold.bruckner_review WHERE ct_number = %s AND reviewer = 'first'",
                           (CT_A,)).fetchone()[0]
    assert comment.startswith("Zeqmelit�") and comment.endswith(" 6 mg")
    args = conn.execute("SELECT args FROM ctis.fetch_run WHERE id = %s", (result.run_id,)).fetchone()[0]
    assert args["replaced_characters"] == comment.count("�") > 0


def test_malformed_rows_are_skipped_and_recorded(conn, tmp_path):
    path = write_reviews(tmp_path / "reviews.csv", [
        row(CT_A, agreed="maybe"),
        row(CT_B, second=("1", "2", "0")),
        row("not-a-number"),
    ])

    result = seed_reviews(conn, path)

    assert (result.fetched, result.changed) == (0, 0)
    assert list(result.failed) == [CT_A, CT_B, "row 3"]
    assert "outcomes_team" in result.failed[CT_B]
    assert stored_reviews(conn) == []


def test_changed_release_is_reported_and_its_reviews_not_mixed_in(conn, tmp_path):
    seed_reviews(conn, write_reviews(tmp_path / "old.csv", [row(CT_A)]))

    result = seed_reviews(conn, write_reviews(tmp_path / "new.csv", [row(CT_A, third=("0", "0", "1"))]))

    assert result.changed == 0
    assert result.failed == {CT_A: "differs from the row loaded from old.csv; left as it was"}
    assert [r[1] for r in stored_reviews(conn)] == ["first", "second"]


def test_file_without_review_columns_is_rejected(conn, tmp_path):
    path = tmp_path / "2026-03-16_ctis_study_final_dataset.csv"
    path.write_text("trial_number,legal_compliance_assessment\n1999-000000-00-01,extended\n", encoding="utf-8")
    with pytest.raises(ValueError, match="agreement_flag"):
        seed_reviews(conn, path)
