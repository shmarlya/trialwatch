"""Publishing every judgement (checks/publishing.py, ADR-007): a run publishes every judgement it made
without an error, any label, whatever its kind's agreement with gold labels; the publication records
that agreement (the confidence once enough gold items were compared, the basis always)."""

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
    checks_db.finish_run(conn, run_id, status="succeeded", error=None, trials=len(items), errors=0)
    datasets.write_version(gold, meta("v1"), items)
    return run_id


def a_new_run(conn, judged):
    """A finished run of the same check on trials no gold item names: (ct number, label, parts)."""
    run_id = start_a_run(conn)
    for number, label, parts in judged:
        judge(conn, run_id, ct(number), label, parts)
    checks_db.finish_run(conn, run_id, status="succeeded", error=None, trials=len(judged), errors=0)
    return run_id


def publication(conn, run_id, trial_id):
    """The agreement recorded on the judgement as its run published it: (confidence, basis)."""
    return conn.execute("SELECT (agreement->>'confidence')::real, agreement->>'basis' FROM checks.judgement"
                        " WHERE run_id = %s AND trial_id = %s", (run_id, trial_id)).fetchone()


def test_a_run_publishes_every_judgement_with_its_kinds_gold_agreement(app_conn, gold):
    a_gold_run(app_conn, gold, agreeing=9, disagreeing=1)
    run_id = a_new_run(app_conn, [(50, "full", FULL), (51, "not full", NOT_FULL), (52, "undecided", UNDECIDED)])
    calibration = publishing.calibrate(app_conn, "eu-ctr-results")
    assert calibration.datasets == ["test/v1"]
    trials = core.trials_with_scheme(app_conn, "eu-ct", [ct(50), ct(51), ct(52)])
    assert sorted(publishing.publish_run(app_conn, run_id)) == sorted(trials.values())
    # "full" of this check agreed 9 of 10 times: measured
    confidence, basis = publication(app_conn, run_id, trials[ct(50)])
    assert confidence == pytest.approx(0.9) and "9 of 10 gold items agreed" in basis and "test/v1" in basis
    # "not full" and "undecided" have no gold yet: published all the same, unmeasured, and the basis says so
    for number in (51, 52):
        confidence, basis = publication(app_conn, run_id, trials[ct(number)])
        assert confidence is None and "0 of 0 gold items agreed" in basis and "at least 5 are needed" in basis


def test_low_agreement_is_published_too_and_says_so(app_conn, gold):
    a_gold_run(app_conn, gold, agreeing=2, disagreeing=8)
    run_id = a_new_run(app_conn, [(50, "full", FULL)])
    trial_id = core.trials_with_scheme(app_conn, "eu-ct", [ct(50)])[ct(50)]
    assert publishing.publish_run(app_conn, run_id) == [trial_id]
    confidence, basis = publication(app_conn, run_id, trial_id)
    assert confidence == pytest.approx(0.2) and "2 of 10 gold items agreed" in basis and "(20%)" in basis
    # anyone sees it, with that agreement, on the trial page; there is nothing to review
    page = TestClient(web.app).get(f"/trials/{trial_id}").text
    assert "20%" in page and "2 of 10 gold items agreed" in page
    assert signed_in_client(app_conn, "moderator").get("/review").status_code == 404


def test_too_few_gold_items_measure_nothing(app_conn, gold):
    a_gold_run(app_conn, gold, agreeing=4, disagreeing=0)
    run_id = a_new_run(app_conn, [(50, "full", FULL)])
    kind = checks_db.judgement_kinds_of_run(app_conn, run_id)[0]
    agreement = publishing.measure(kind, publishing.calibrate(app_conn, "eu-ctr-results"))
    assert agreement.confidence is None and "at least 5 are needed" in agreement.reason


def test_another_prompt_is_another_kind(app_conn, gold):
    a_gold_run(app_conn, gold, agreeing=10, disagreeing=0)
    run_id = a_new_run(app_conn, [(50, "full", FULL)])
    app_conn.execute("UPDATE checks.run SET prompt_sha256 = %s WHERE id = %s", (b"\x01" * 32, run_id))
    trial_id = core.trials_with_scheme(app_conn, "eu-ct", [ct(50)])[ct(50)]
    assert publishing.publish_run(app_conn, run_id) == [trial_id]
    assert publication(app_conn, run_id, trial_id)[0] is None


def test_a_run_never_counts_its_own_judgements_towards_its_agreement(app_conn, gold):
    # a run that checks gold trials is measured only by runs started before it: here there are none
    run_id = a_gold_run(app_conn, gold, agreeing=9, disagreeing=1)
    trial_id = core.trials_with_scheme(app_conn, "eu-ct", [ct(0)])[ct(0)]
    assert trial_id in publishing.publish_run(app_conn, run_id)
    confidence, basis = publication(app_conn, run_id, trial_id)
    assert confidence is None and "0 of 0 gold items agreed" in basis
    # the next run of the same check is measured by it
    later = a_new_run(app_conn, [(50, "full", FULL)])
    publishing.publish_run(app_conn, later)
    later_trial = core.trials_with_scheme(app_conn, "eu-ct", [ct(50)])[ct(50)]
    assert publication(app_conn, later, later_trial)[0] == pytest.approx(0.9)
