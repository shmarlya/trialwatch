"""Scoring one check run against a gold dataset (gold/compare.py; ADR-001, ADR-003): part by part and
per trial, with false accusations, missed trials, and trials set aside because our check read a
document posted on or after the day the reviewers read theirs."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_evidence import start_a_run

from trialwatch.db import checks as checks_db
from trialwatch.db import core
from trialwatch.db.core import Identifier
from trialwatch.gold import compare, datasets

REPO_GOLD = Path(__file__).parent.parent / "data" / "gold"
FULL = {"participants": True, "outcomes": True, "cancelled": False}
NOT_FULL = {"participants": True, "outcomes": False, "cancelled": False}
UNDECIDED = {"participants": True, "outcomes": None, "cancelled": False}
# The reviewers read their documents on this day
AS_OF = "2025-11-10"
BEFORE = datetime(2025, 11, 9, 23, 59, 59, tzinfo=UTC)
ON_THE_DAY = datetime(2025, 11, 10, 0, 0, tzinfo=UTC)
# Items, by what happens to them
ACCUSED = "1999-000000-10-01"
MISSED = "1999-000000-10-02"
AGREED = "1999-000000-10-03"
LEFT_OPEN = "1999-000000-10-04"
LATER_DOCUMENT = "1999-000000-10-05"
FAILED = "1999-000000-10-06"
NO_LABEL = "1999-000000-10-07"
NOT_HERE = "1999-000000-10-08"


def an_item(key, label, parts=None, comment=None):
    expected = FULL
    if parts is not None:
        expected = parts
    return datasets.Item(key=key, identifiers=[Identifier("eu-ct", key)], title=f"Trial {key}",
                         expected_parts=dict(expected), expected_label=label, reviews=[], reviewers_agreed=True,
                         comment=comment, evidence=[], details={})


def a_dataset(items, requirements="eu-ctr-results"):
    return datasets.Dataset(name="test", version="v1", title="Test", description="For tests", citation=None, url=None,
                            requirements=requirements, parts=["participants", "outcomes", "cancelled"],
                            labels=["full", "not full"], evidence={"document_kinds": ["results-summary"], "as_of": AS_OF},
                            built_from=[], built_with="tests", items=items)


ITEMS = [
    an_item(ACCUSED, "full", comment="every outcome is in table 3"),
    an_item(MISSED, "not full", parts=NOT_FULL),
    an_item(AGREED, "full"),
    an_item(LEFT_OPEN, "full"),
    an_item(LATER_DOCUMENT, "full"),
    an_item(FAILED, "full"),
    an_item(NO_LABEL, None),
    an_item(NOT_HERE, "full"),
]


def a_trial(conn, ct_number, published_at):
    """A trial with one results summary, posted when its source says; returns (trial, document version)."""
    saved = core.save_trial_record(conn, source="ctis", source_key=ct_number, identifiers=[Identifier("eu-ct", ct_number)],
                                   payload={"ctNumber": ct_number}, registered={},
                                   content_hash=hashlib.sha256(ct_number.encode()).digest())
    content = f"%PDF-1.7 results of {ct_number}".encode()
    stored = core.save_document_version(conn, source="ctis", source_key=f"results-{ct_number}", kind="results-summary",
                                        title="Results", language=None, version="1", content=content,
                                        media_type="application/pdf", sha256=hashlib.sha256(content).digest(),
                                        published_at=published_at)
    row = conn.execute("SELECT id FROM core.document_version WHERE document_id = %s", (stored.document_id,)).fetchone()
    return saved.trial_id, row[0]


def judge(conn, run_id, ct_number, label, parts, published_at=BEFORE, error=None):
    trial_id, version_id = a_trial(conn, ct_number, published_at)
    checks_db.save_judgement(conn, run_id, trial_id, parts=parts, label=label, trace=None, served_by=[],
                             record_id=None, document_versions=[version_id], input_tokens=100, output_tokens=10,
                             duration_ms=5, error=error)


def a_judged_run(conn):
    """A finished run that judged every item's trial but NOT_HERE's (not in the database)."""
    core.act_as(conn, "test-person")
    run_id = start_a_run(conn)
    judge(conn, run_id, ACCUSED, "not full", NOT_FULL)
    judge(conn, run_id, MISSED, "full", FULL)
    judge(conn, run_id, AGREED, "full", FULL)
    judge(conn, run_id, LEFT_OPEN, "undecided", UNDECIDED)
    judge(conn, run_id, LATER_DOCUMENT, "not full", NOT_FULL, published_at=ON_THE_DAY)
    judge(conn, run_id, FAILED, None, {}, error="InternalServerError: overloaded")
    judge(conn, run_id, NO_LABEL, "full", FULL)
    checks_db.finish_run(conn, run_id, status="failed", error=None, trials=7, errors=1, input_tokens=700,
                         output_tokens=70)
    return run_id


def test_a_run_is_scored_part_by_part_and_per_trial(conn):
    run_id = a_judged_run(conn)
    scored = compare.compare_run(conn, run_id, a_dataset(ITEMS))
    assert (scored.dataset, scored.run_id, scored.model, scored.prompt, scored.requirements) == (
        "test/v1", run_id, "claude-test", "evidence", "eu-ctr-results")
    # items with a label: compared, not judged in the run, failed, or set aside
    assert (scored.labelled, scored.compared) == (7, 4)
    assert scored.not_in_run == [NOT_HERE]
    assert scored.errors == {FAILED: "InternalServerError: overloaded"}
    # our check read a document posted on the reviewers' day: it may not be the one they read
    assert scored.set_aside == [LATER_DOCUMENT]
    assert scored.parts == {"participants": [4, 4], "outcomes": [3, 1], "cancelled": [4, 4]}
    assert scored.parts_open == {"participants": 0, "outcomes": 1, "cancelled": 0}
    assert scored.labels == {("not full", "full"): 1, ("full", "not full"): 1, ("full", "full"): 1,
                             ("undecided", "full"): 1}
    # the costliest error: ours "not full" where theirs is "full"
    assert scored.false_accusations == [ACCUSED]
    assert scored.missed == [MISSED]
    disagreed = []
    for disagreement in scored.disagreements:
        disagreed.append((disagreement.key, disagreement.ours, disagreement.theirs, disagreement.comment))
    assert disagreed == [(ACCUSED, "not full", "full", "every outcome is in table 3"), (MISSED, "full", "not full", None)]
    # the tokens of every judgement of a labelled item
    assert (scored.input_tokens, scored.output_tokens) == (600, 60)


def test_the_score_reads_as_lines(conn):
    run_id = a_judged_run(conn)
    scored = compare.compare_run(conn, run_id, a_dataset(ITEMS))
    lines = compare.report_lines(scored)
    assert lines[0] == (f"run {run_id} against test/v1 · model claude-test, effort low, prompt evidence, "
                        "requirements eu-ctr-results")
    assert lines[1] == ("items with a label 7: compared 4, not judged in the run 1, errors 1, set aside (we read "
                        "documents posted after the reviewers' day) 1")
    assert lines[2:5] == ["  participants  4/4 agree with the reviewers, 0 left open",
                          "  outcomes      1/3 agree with the reviewers, 1 left open",
                          "  cancelled     4/4 agree with the reviewers, 0 left open"]
    assert lines[5:9] == ["  ours full      · theirs full     1", "  ours full      · theirs not full 1",
                          "  ours not full  · theirs full     1", "  ours undecided · theirs full     1"]
    assert lines[9:] == ["false accusations (ours not full, theirs full): 1; missed (ours full, theirs not full): 1",
                         "tokens in 600, out 60"]
    disagreements = compare.disagreement_lines(scored)
    assert len(disagreements) == 3
    assert disagreements[0].startswith(f"  {ACCUSED} ours not full (") and "outcomes=0" in disagreements[0]
    assert disagreements[0].endswith("theirs full (participants=1 outcomes=1 cancelled=0): every outcome is in table 3")
    assert disagreements[1].endswith("theirs not full (participants=1 outcomes=0 cancelled=0): ")
    assert disagreements[2] == f"  {FAILED}: InternalServerError: overloaded"


def test_a_run_is_only_scored_against_labels_of_its_requirement_set(conn):
    run_id = a_judged_run(conn)
    with pytest.raises(ValueError, match="judged against eu-ctr-results, but test/v1 answers another-set"):
        compare.compare_run(conn, run_id, a_dataset(ITEMS, requirements="another-set"))
    with pytest.raises(ValueError, match="no check run 987654321"):
        compare.compare_run(conn, 987654321, a_dataset(ITEMS))


def test_without_a_reading_day_nothing_is_set_aside(conn):
    run_id = a_judged_run(conn)
    dataset = a_dataset(ITEMS)
    dataset.evidence["as_of"] = None
    scored = compare.compare_run(conn, run_id, dataset)
    assert scored.set_aside == [] and scored.compared == 5
    assert compare.evidence_day(a_dataset(ITEMS)).isoformat() == AS_OF


def meta(version, requirements="eu-ctr-results"):
    return {"name": "test", "version": version, "title": "Test", "description": "For tests", "citation": None,
            "url": None, "requirements": requirements, "parts": ["participants", "outcomes", "cancelled"],
            "labels": ["full", "not full"], "evidence": {"document_kinds": [], "as_of": None, "description": ""},
            "built_from": [], "built_with": "tests", "items": 1}


def test_a_run_is_scored_by_default_against_the_newest_dataset_of_its_requirement_set(conn, tmp_path, monkeypatch):
    core.act_as(conn, "test-person")
    run_id = start_a_run(conn)
    assert compare.dataset_for_run(conn, run_id) == "bruckner/2026-03-16"
    monkeypatch.setenv("TRIALWATCH_GOLD_DIR", str(tmp_path))
    assert compare.dataset_for_run(conn, run_id) is None
    datasets.write_version(tmp_path, meta("v1"), [an_item(AGREED, "full")])
    datasets.write_version(tmp_path, meta("v2"), [an_item(AGREED, "full")])
    datasets.write_version(tmp_path, meta("v3", requirements="another-set"), [an_item(AGREED, "full")])
    assert compare.dataset_for_run(conn, run_id) == "test/v2"
    assert compare.dataset_for_run(conn, 987654321) is None
