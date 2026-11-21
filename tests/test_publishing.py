"""Publishing by measured confidence (checks/publishing.py, ADR-007): a judgement's confidence is how
often judgements of its kind agreed with gold labels; at 90% or more, over enough gold items, a run
publishes it by itself, any label; the rest wait for a person."""

import pytest
from conftest import signed_in_client
from fastapi.testclient import TestClient
from test_evidence import start_a_run
from test_gold_compare import FULL, NOT_FULL, UNDECIDED, an_item, judge, meta

from trialwatch.checks import publishing
from trialwatch.db import checks as checks_db
from trialwatch.db import core
from trialwatch.gold import datasets
from trialwatch.web import app as web


def ct(number):
    return f"1999-000000-20-{number:02d}"


@pytest.fixture
def gold(tmp_path, monkeypatch):
    """An empty gold folder, and a small minimum so a few items measure a kind."""
    monkeypatch.setenv("TRIALWATCH_GOLD_DIR", str(tmp_path))
    monkeypatch.setattr(publishing, "MIN_GOLD_ITEMS", 5)
    return tmp_path


def a_gold_run(conn, gold, agreeing, disagreeing):
    """A finished run that judged gold trials "full": `agreeing` the gold says full, `disagreeing` not full."""
    core.act_as(conn, "test-person")
    run_id = start_a_run(conn)
    items = []
    for number in range(agreeing + disagreeing):
        expected = "full" if number < agreeing else "not full"
        items.append(an_item(ct(number), expected))
        judge(conn, run_id, ct(number), "full", FULL)
    checks_db.finish_run(conn, run_id, status="succeeded", error=None, trials=len(items), errors=0, input_tokens=0,
                         output_tokens=0)
    datasets.write_version(gold, meta("v1"), items)
    return run_id


def a_new_run(conn, judged):
    """A finished run of the same check on trials no gold item names: (ct number, label, parts)."""
    run_id = start_a_run(conn)
    for number, label, parts in judged:
        judge(conn, run_id, ct(number), label, parts)
    checks_db.finish_run(conn, run_id, status="succeeded", error=None, trials=len(judged), errors=0, input_tokens=0,
                         output_tokens=0)
    return run_id


def test_a_kind_is_published_by_a_run_from_90_percent_agreement(app_conn, gold):
    a_gold_run(app_conn, gold, agreeing=9, disagreeing=1)
    run_id = a_new_run(app_conn, [(50, "full", FULL), (51, "not full", NOT_FULL), (52, "undecided", UNDECIDED)])
    calibration = publishing.calibrate(app_conn, "eu-ctr-results")
    assert calibration.datasets == ["test/v1"]
    published = publishing.auto_publish(app_conn, run_id)
    # "full" of this check agreed 9 of 10 times; "not full" has no gold yet; "undecided" is a person's
    trials = core.trials_with_scheme(app_conn, "eu-ct", [ct(50), ct(51), ct(52)])
    assert published == [trials[ct(50)]]
    row = app_conn.execute("SELECT confidence, basis FROM checks.publication WHERE run_id = %s", (run_id,)).fetchone()
    assert row[0] == pytest.approx(0.9) and "9 of 10 gold items agreed" in row[1] and "test/v1" in row[1]


def test_below_90_percent_or_too_few_items_a_person_decides(app_conn, gold):
    a_gold_run(app_conn, gold, agreeing=8, disagreeing=2)
    run_id = a_new_run(app_conn, [(50, "full", FULL)])
    assert publishing.auto_publish(app_conn, run_id) == []
    kind = checks_db.judgement_kinds_of_run(app_conn, run_id)[0]
    decision = publishing.decide(kind, publishing.calibrate(app_conn, "eu-ctr-results"))
    assert (decision.confidence, decision.automatic) == (0.8, False) and "below 90%" in decision.reason


def test_too_few_gold_items_measure_nothing(app_conn, gold):
    a_gold_run(app_conn, gold, agreeing=4, disagreeing=0)
    run_id = a_new_run(app_conn, [(50, "full", FULL)])
    kind = checks_db.judgement_kinds_of_run(app_conn, run_id)[0]
    decision = publishing.decide(kind, publishing.calibrate(app_conn, "eu-ctr-results"))
    assert decision.confidence is None and not decision.automatic and "at least 5 are needed" in decision.reason


def test_a_high_confidence_accusation_is_published_too(app_conn, gold):
    core.act_as(app_conn, "test-person")
    run_id = start_a_run(app_conn)
    items = []
    for number in range(10):
        items.append(an_item(ct(number), "not full", parts=NOT_FULL))
        judge(app_conn, run_id, ct(number), "not full", NOT_FULL)
    checks_db.finish_run(app_conn, run_id, status="succeeded", error=None, trials=10, errors=0, input_tokens=0,
                         output_tokens=0)
    datasets.write_version(gold, meta("v1"), items)
    later = a_new_run(app_conn, [(50, "not full", NOT_FULL)])
    assert len(publishing.auto_publish(app_conn, later)) == 1


def test_another_prompt_is_another_kind(app_conn, gold):
    a_gold_run(app_conn, gold, agreeing=10, disagreeing=0)
    run_id = a_new_run(app_conn, [(50, "full", FULL)])
    app_conn.execute("UPDATE checks.run SET prompt_sha256 = %s WHERE id = %s", (b"\x01" * 32, run_id))
    assert publishing.auto_publish(app_conn, run_id) == []


def test_the_review_page_lists_what_waits_and_how_each_kind_did(app_conn, gold):
    a_gold_run(app_conn, gold, agreeing=8, disagreeing=2)
    a_new_run(app_conn, [(50, "full", FULL)])
    assert TestClient(web.app).get("/review").status_code == 401
    page = signed_in_client(app_conn, "moderator").get("/review").text
    assert ct(50) in page and "80%" in page and "below 90%" in page
    trial_id = core.trials_with_scheme(app_conn, "eu-ct", [ct(50)])[ct(50)]
    trial = signed_in_client(app_conn, "admin", "ada@test.example").get(f"/trials/{trial_id}").text
    assert "Confidence:" in trial and "80%" in trial
