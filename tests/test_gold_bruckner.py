"""Reading Bruckner et al.'s files (gold/converters/bruckner.py): their verdicts, their reviewers' answers, the
CTIS export, and the gold dataset version built from them."""

import csv
import hashlib

import pytest

from trialwatch.db.core import Identifier
from trialwatch.gold.converters import bruckner
from trialwatch.gold.datasets import Review

# Valid format but not real trials
CT_A = "1999-000000-00-01"
CT_B = "1999-000000-00-02"
VERDICT_COLUMNS = ["trial_number", "Title.of.the.trial", "Overall.trial.status", "Trial.phase", "Sponsor.Co.Sponsors",
                   "Sponsor.type", "Global.end.of.the.trial", "regulatory_status", "completion_date",
                   "extended_not_due_yet", "days_to_results", "laypersons_results", "no_scientific_results_document",
                   "incomplete_scientific_results", "full_scientific_results", "legal_compliance_assessment", "comments"]
REVIEW_COLUMNS = ["trial_number", "regulatory_framework", "results_pub_date_DDMMYYYY", "cancelled_final_data",
                  "agreement_flag", "participants_till_christie", "outcomes_till_christie", "cancelled_till_christie",
                  "check", "participants_team", "outcomes_team", "cancelled_team", "check_team", "participants_ghassan",
                  "outcomes_ghassan", "cancelled_ghassan", "check_ghassan", "comments_till_christie", "comments_team",
                  "comments_ghassan", "for_team_discussion", "comments_final"]


def verdict_row(ct_number, verdict, document, lay="1", days="NA", comment="NA"):
    """A row of their final dataset, written by R: 'NA' for empty, flags '1' or 'NA'."""
    flags = {"full": "NA", "incomplete": "NA", "none": "NA"}
    flags[document] = "1"
    return {"trial_number": ct_number, "Title.of.the.trial": "A trial\nof something", "Overall.trial.status": "Ended",
            "Trial.phase": "Phase III", "Sponsor.Co.Sponsors": "Example Pharma", "Sponsor.type": "Pharmaceutical company",
            "Global.end.of.the.trial": "NA", "regulatory_status": "adult", "completion_date": "2024-01-31",
            "extended_not_due_yet": "0", "days_to_results": days, "laypersons_results": lay,
            "no_scientific_results_document": flags["none"], "incomplete_scientific_results": flags["incomplete"],
            "full_scientific_results": flags["full"], "legal_compliance_assessment": verdict, "comments": comment}


def review_row(ct_number, first=("1", "0", "0"), second=("NA", "NA", "NA"), third=("1", "1", "0"), agreed="FALSE"):
    """A row of their merged reviews: each reviewer's participants, outcomes and cancelled answers."""
    return {"trial_number": ct_number, "regulatory_framework": "CTR", "results_pub_date_DDMMYYYY": "15/03/2025",
            "cancelled_final_data": "0", "agreement_flag": agreed,
            "participants_till_christie": first[0], "outcomes_till_christie": first[1],
            "cancelled_till_christie": first[2], "check": "0",
            "participants_team": second[0], "outcomes_team": second[1], "cancelled_team": second[2], "check_team": "NA",
            "participants_ghassan": third[0], "outcomes_ghassan": third[1], "cancelled_ghassan": third[2],
            "check_ghassan": "1", "comments_till_christie": "Outcome 2 is only a chart", "comments_team": "NA",
            "comments_ghassan": "NA", "for_team_discussion": "NA", "comments_final": "Table 3 has outcome 2"}


def write_csv(path, columns, rows, byte_order_mark=False):
    encoding = "utf-8"
    if byte_order_mark:
        encoding = "utf-8-sig"
    with path.open("w", newline="", encoding=encoding) as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def test_a_verdict_row_is_read_with_its_labels_typed():
    late = bruckner.verdict_of(verdict_row(CT_A, "reported fully late", "full", days="400",
                                           comment="Results posted late."))
    assert (late.ct_number, late.verdict, late.results_document, late.lay_summary, late.days_to_results) == (
        CT_A, "reported fully late", "full", True, 400)
    assert (late.comment, late.regulatory_status, late.completion_date, late.extended) == (
        "Results posted late.", "adult", "2024-01-31", False)
    missing = bruckner.verdict_of(verdict_row(CT_B, "not fully reported", "incomplete", lay="NA"))
    assert (missing.results_document, missing.lay_summary, missing.days_to_results, missing.comment) == (
        "incomplete", False, None, None)


def test_a_verdict_row_whose_labels_contradict_each_other_is_refused():
    with pytest.raises(ValueError, match="unknown verdict 'late'"):
        bruckner.verdict_of(verdict_row(CT_A, "late", "full", days="400"))
    two_flags = verdict_row(CT_A, "not fully reported", "none")
    two_flags["full_scientific_results"] = "1"
    with pytest.raises(ValueError, match="expected one results-document flag, found 2"):
        bruckner.verdict_of(two_flags)
    # only a trial reported fully has a number of days to its results, and it always has one
    with pytest.raises(ValueError, match="days_to_results 'NA' doesn't fit verdict 'reported fully on time'"):
        bruckner.verdict_of(verdict_row(CT_A, "reported fully on time", "full"))
    with pytest.raises(ValueError, match="days_to_results '90' doesn't fit verdict 'not fully reported'"):
        bruckner.verdict_of(verdict_row(CT_A, "not fully reported", "none", days="90"))


def test_their_final_dataset_is_read_from_its_file(tmp_path):
    write_csv(tmp_path / bruckner.VERDICT_FILE, VERDICT_COLUMNS,
              [verdict_row(CT_A, "extended", "none", lay="NA"), verdict_row(CT_B, "cancelled", "none", lay="NA")],
              byte_order_mark=True)
    read = []
    for verdict in bruckner.verdicts(tmp_path):
        read.append((verdict.ct_number, verdict.verdict))
    assert read == [(CT_A, "extended"), (CT_B, "cancelled")]
    (tmp_path / bruckner.VERDICT_FILE).write_text("Trial number,Trial results\n1999-000000-00-01,Yes\n")
    with pytest.raises(ValueError, match="has no trial_number, legal_compliance_assessment"):
        bruckner.verdicts(tmp_path)


def test_a_review_row_keeps_each_reviewer_who_answered():
    document = bruckner.reviewed_document_of(review_row(CT_A))
    assert (document.ct_number, document.posted_on.isoformat(), document.reviewers_agreed) == (CT_A, "2025-03-15", False)
    assert (document.final_comment, document.cancelled) == ("Table 3 has outcome 2", False)
    # the second reviewer left every part empty: they did not review this document
    assert document.reviews == [
        Review("first", {"participants": True, "outcomes": False, "cancelled": False}, False, "Outcome 2 is only a chart"),
        Review("third", {"participants": True, "outcomes": True, "cancelled": False}, True, None)]
    with pytest.raises(ValueError, match="agreement_flag is 'maybe', expected TRUE or FALSE"):
        bruckner.reviewed_document_of(review_row(CT_A, agreed="maybe"))
    with pytest.raises(ValueError, match="participants_team is 'yes', expected 1, 0 or NA"):
        bruckner.reviewed_document_of(review_row(CT_A, second=("yes", "NA", "NA")))


def test_the_third_reviewer_settles_a_part_else_the_first_two_must_agree():
    first = Review("first", {"participants": True, "outcomes": False, "cancelled": None}, None, None)
    second = Review("second", {"participants": True, "outcomes": True, "cancelled": None}, None, None)
    third = Review("third", {"participants": None, "outcomes": True, "cancelled": False}, None, None)
    assert bruckner.consensus([first, second], "participants") is True
    assert bruckner.consensus([first, second], "outcomes") is None
    assert bruckner.consensus([first, second, third], "outcomes") is True
    assert bruckner.consensus([first, second, third], "participants") is True
    assert bruckner.consensus([first, second, third], "cancelled") is False
    assert bruckner.consensus([first], "participants") is None


def test_an_export_is_read_by_trial_number_whatever_its_bytes(tmp_path):
    # a byte-order mark, and text that is not valid UTF-8 (as in their reviews file)
    path = tmp_path / "export.csv"
    path.write_bytes(b"\xef\xbb\xbfTrial number,Title of the trial\n 1999-000000-00-01 ,Caf\xe9 study\n")
    assert bruckner.export_rows(path) == {CT_A: {"Trial number": " 1999-000000-00-01 ",
                                                 "Title of the trial": "Caf� study"}}


def test_a_dataset_version_is_built_from_their_files(tmp_path):
    write_csv(tmp_path / bruckner.VERDICT_FILE, VERDICT_COLUMNS,
              [verdict_row(CT_A, "not fully reported", "incomplete"), verdict_row(CT_B, "extended", "none", lay="NA")])
    write_csv(tmp_path / bruckner.REVIEWS_FILE, REVIEW_COLUMNS, [review_row(CT_A)])
    meta, items = bruckner.build(tmp_path)
    assert (meta["name"], meta["version"], meta["requirements"], meta["items"]) == (
        "bruckner", "2026-03-16", "eu-ctr-results", 2)
    assert meta["evidence"]["as_of"] == "2025-11-10"
    verdict_hash = hashlib.sha256((tmp_path / bruckner.VERDICT_FILE).read_bytes()).hexdigest()
    assert meta["built_from"][0] == {"file": f"data/bruckner/{bruckner.VERDICT_FILE}", "sha256": verdict_hash}
    reviewed = items[0]
    assert (reviewed.key, reviewed.identifiers, reviewed.title) == (CT_A, [Identifier("eu-ct", CT_A)],
                                                                    "A trial of something")
    # the consensus of their reviewers; "not full" since their document was not full
    assert reviewed.expected_parts == {"participants": True, "outcomes": True, "cancelled": False}
    assert reviewed.expected_label == "not full"
    assert reviewed.evidence == [{"kind": "results-summary", "posted_on": "2025-03-15"}]
    assert reviewed.details["verdict"] == "not fully reported"
    assert reviewed.details["reviewers' final comment"] == "Table 3 has outcome 2"
    # no results document was read: nothing to compare a check with, but the item stays searchable
    unread = items[1]
    assert (unread.key, unread.expected_label, unread.reviews) == (CT_B, None, [])
    assert unread.expected_parts == {"participants": None, "outcomes": None, "cancelled": None}
