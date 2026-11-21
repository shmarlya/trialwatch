"""Gold datasets as versioned files (ADR-003): the Bruckner converter, the lock, search, comparison,
and the web pages that put our judgements next to the items."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from conftest import default_settings
from test_evidence import CT, GOOD, PROMPT, FakeClient, seed, start_a_run

from trialwatch.checks import evidence as ev
from trialwatch.db import checks as checks_db
from trialwatch.db.core import Identifier
from trialwatch.gold import bruckner, datasets
from trialwatch.web import app as web

ROOT = Path(__file__).parent.parent
BRUCKNER = ROOT / "data" / "bruckner"
REPO_GOLD = ROOT / "data" / "gold"


def an_item(key=CT, label="full", parts=None, reviews=None, comment=None):
    if parts is None:
        parts = {"participants": True, "outcomes": True, "cancelled": False}
    if reviews is None:
        reviews = []
    return datasets.Item(
        key=key, identifiers=[Identifier("eu-ct", key)], title="A test trial", expected_parts=parts,
        expected_label=label, reviews=reviews, reviewers_agreed=None, comment=comment, evidence=[], details={},
    )


def a_meta(version="v1", as_of=None):
    return {
        "name": "test", "version": version, "title": "Test", "description": "For tests", "citation": None,
        "url": None, "requirements": "eu-ctr-results", "parts": ["participants", "outcomes", "cancelled"],
        "labels": ["full", "not full"], "evidence": {"document_kinds": [], "as_of": as_of, "description": ""},
        "built_from": [], "built_with": "tests", "items": 1,
    }


def test_the_released_bruckner_version_is_rebuilt_byte_for_byte():
    meta, items = bruckner.build(BRUCKNER)
    files = datasets.files_of(meta, items)
    lock = datasets.read_lock(REPO_GOLD)
    assert lock[f"{meta['name']}/{meta['version']}"] == datasets.file_hashes(files)
    for name, content in files.items():
        assert (REPO_GOLD / "bruckner" / "2026-03-16" / name).read_bytes() == content
    assert datasets.lock_problems(REPO_GOLD) == []


def test_bruckner_items_carry_the_consensus_and_their_label():
    dataset = datasets.load("bruckner/2026-03-16", REPO_GOLD)
    labelled = []
    for item in dataset.items:
        if item.expected_label is not None:
            labelled.append(item)
    assert (len(dataset.items), len(labelled)) == (277, 206)
    item = datasets.find_item(dataset, "2023-508777-91-00")
    assert item.expected_label == "not full"
    assert item.expected_parts == {"participants": True, "outcomes": False, "cancelled": False}
    assert item.identifiers == [Identifier("eu-ct", "2023-508777-91-00")]
    assert "\n" not in item.title


def test_a_released_version_cannot_change(tmp_path):
    ref, written = datasets.write_version(tmp_path, a_meta(), [an_item()])
    assert (ref, written) == ("test/v1", True)
    assert datasets.write_version(tmp_path, a_meta(), [an_item()]) == ("test/v1", False)
    with pytest.raises(datasets.VersionIsFixed):
        datasets.write_version(tmp_path, a_meta(), [an_item(label="not full")])
    assert datasets.write_version(tmp_path, a_meta("v2"), [an_item(label="not full")]) == ("test/v2", True)
    (tmp_path / "test" / "v1" / "items.jsonl").write_text("{}\n")
    assert datasets.lock_problems(tmp_path) == ["test/v1: items.jsonl changed since it was released"]


def test_refs_never_reach_outside_the_gold_folder(tmp_path):
    datasets.write_version(tmp_path, a_meta(), [an_item()])
    for ref in ("../test/v1", "test/../test", "test", "test/v1/x", "test/.hidden"):
        with pytest.raises(FileNotFoundError):
            datasets.load(ref, tmp_path)


def test_search_reads_identifiers_titles_and_every_comment():
    review = datasets.Review("first", {"participants": True}, None, "Terminated early with 5 patients")
    other = datasets.Review("second", {"participants": False}, None, None)
    item = an_item(reviews=[review, other], comment="final call")
    assert datasets.matches_text(item, "terminated 5 PATIENTS")
    assert datasets.matches_text(item, CT)
    assert not datasets.matches_text(item, "terminated nobody")
    assert datasets.reviewers_disagreed(item)
    assert not datasets.reviewers_disagreed(an_item(reviews=[review]))


def test_comparison_counts_a_false_accusation_and_leaves_open_answers_out():
    parts = ["participants", "outcomes", "cancelled"]
    gold = an_item(parts={"participants": True, "outcomes": True, "cancelled": None})
    ours = {"participants": True, "outcomes": False, "cancelled": False}
    result = datasets.compare(gold, parts, ours, "not full")
    assert result.parts == {"participants": "agree", "outcomes": "disagree", "cancelled": "no gold"}
    assert (result.label, result.false_accusation) == ("disagree", True)
    undecided = datasets.compare(gold, parts, {"participants": None}, "undecided")
    assert (undecided.parts["participants"], undecided.label) == ("ours open", "ours open")
    assert datasets.compare(an_item(label=None), parts, ours, "full").label == "no gold"


def test_the_gold_page_puts_our_latest_judgement_next_to_the_item(conn, tmp_path):
    trial = seed(conn)
    ev.run(conn, FakeClient(GOOD), [trial], default_settings({"method": "files"}), PROMPT)
    datasets.write_version(tmp_path, a_meta(), [an_item(), an_item(key="1999-000000-00-99", label=None)])
    rows = web._gold_rows(conn, datasets.load("test/v1", tmp_path))
    assert rows[0].trial_id == trial
    # GOOD says endpoint 12 was not analysed: "not full" where the gold label is "full"
    assert (rows[0].judgement.label, rows[0].comparison.label) == ("not full", "disagree")
    assert rows[0].comparison.false_accusation
    assert (rows[1].trial_id, rows[1].comparison) == (None, None)
    summary = web._gold_summary(rows)
    assert (summary.items, summary.labelled, summary.synced, summary.checked) == (2, 1, 1, 1)
    assert summary.false_accusations == 1


def test_a_judgement_that_read_a_document_posted_after_the_reviewers_day_is_set_aside(conn, tmp_path):
    trial = seed(conn)
    # the results summary we read was posted on the day the reviewers read theirs
    conn.execute("UPDATE core.document_version SET published_at = '2025-11-10T08:00:00Z'")
    ev.run(conn, FakeClient(GOOD), [trial], default_settings({"method": "files"}), PROMPT)
    datasets.write_version(tmp_path, a_meta(as_of="2025-11-10"), [an_item()])
    dataset = datasets.load("test/v1", tmp_path)
    rows = web._gold_rows(conn, dataset)
    assert (rows[0].trial_id, rows[0].set_aside, rows[0].comparison) == (trial, True, None)
    summary = web._gold_summary(rows)
    assert (summary.synced, summary.set_aside, summary.checked, summary.false_accusations) == (1, 1, 0, 0)
    assert not web.set_aside(dataset, None)
    assert not web.set_aside(dataset, datetime(2025, 11, 9, 23, 59, tzinfo=UTC))
    assert web.set_aside(dataset, datetime(2025, 11, 10, 0, 0, tzinfo=UTC))


def test_the_gold_pages_render_and_unknown_items_are_404(database_url, monkeypatch):
    monkeypatch.setenv("TRIALWATCH_GOLD_DIR", str(REPO_GOLD))
    client = TestClient(web.app)
    page = client.get("/gold", params={"q": "terminated", "label": "not full"})
    assert page.status_code == 200
    assert "2023-508777-91-00" in page.text
    item = client.get("/gold/bruckner/2026-03-16/2023-508777-91-00")
    assert item.status_code == 200
    assert "Transfer evidence from CTIS" in item.text
    assert client.get("/gold/bruckner/2026-03-16/nope").status_code == 404
    assert client.get("/gold/nope/v1/x").status_code == 404


def test_a_trial_page_lists_the_gold_items_naming_it(conn, tmp_path, monkeypatch):
    trial = seed(conn)
    ev.run(conn, FakeClient(GOOD), [trial], default_settings({"method": "files"}), PROMPT)
    # a newer run, still going: its judgement is not compared until it has ended
    running = start_a_run(conn)
    checks_db.save_judgement(conn, running, trial, parts={"participants": True, "outcomes": True, "cancelled": False},
                             label="full", trace=None, served_by=[], record_id=None, document_versions=[],
                             input_tokens=0, output_tokens=0, duration_ms=0, error=None)
    datasets.write_version(tmp_path, a_meta(), [an_item(), an_item(key="1999-000000-00-99")])
    monkeypatch.setenv("TRIALWATCH_GOLD_DIR", str(tmp_path))
    identifiers = web.overview.trial_identifiers(conn, trial)
    judgements = web.overview.trial_judgements(conn, trial)
    assert (judgements[0].run_id, judgements[0].run_status) == (running, "running")
    found = web._gold_items_for(identifiers, judgements)
    assert len(found) == 1
    dataset, item, comparison = found[0]
    assert (dataset.ref, item.key, comparison.label) == ("test/v1", CT, "disagree")
