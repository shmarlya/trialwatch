"""Publishing (ADR-007) and what the public sees: anyone not signed in sees the home page, which
lists the trials with a published judgement, each opening to how its evidence was collected
(with the run, prompts, requirement sets and quoted files); every other page is for staff. A run publishes
every judgement it made; staff may only withdraw one with a reason, and it stays public, marked;
the queries keep the view a public page uses."""

from datetime import datetime, timedelta, timezone

import pytest
from calls import recorded_call
from conftest import signed_in_client
from fastapi.testclient import TestClient
from test_evidence import CT, SET, seed, start_a_run
from test_web import store_file

from trialwatch.checks import publishing
from trialwatch.db import checks as checks_db
from trialwatch.db import core, overview
from trialwatch.db import jobs as jobs_db
from trialwatch.web import app as web

QUOTE = "Overall 24 participants were randomised and analysed."


def judged(conn, trial_id, label="not full", error=None, finished=True):
    """A run that judged the trial, quoting its results summary: finished, which makes what it judged
    public with its agreement, or still going (`finished` False); returns the run id."""
    run_id = start_a_run(conn)
    sha = conn.execute(
        "SELECT d.file_sha256 FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id "
        "WHERE l.trial_id = %s LIMIT 1", (trial_id,)).fetchone()[0]
    answer = checks_db.Answer(found=True, kind="numeric_result", quote=QUOTE, document_version_id=None, file_sha256=sha,
                              page_no=2, note="a secret model note", verified=True)
    checks_db.save_answer(conn, run_id, trial_id, "participants", "How many took part?", [], False, answer)
    if error is not None:
        label = None
    checks_db.save_judgement(conn, run_id, trial_id, parts={"participants": True}, label=label, served_by=[],
                             record_id=None, document_versions=[], duration_ms=10, error=error)
    call_id = recorded_call(conn, run_id, trial_id, input_tokens=123456, output_tokens=7, cost_usd="0.42")
    now = datetime.now(timezone.utc)
    checks_db.save_steps(conn, run_id, trial_id, [
        {"name": "call", "at": now, "data": {"call": 1, "model": "claude-test", "effort": "low", "pages": [],
                                             "questions": ["participants"], "llm_call": call_id}},
        {"name": "rules", "at": now, "data": {"requirements": SET.name, "label": label, "parts": {"participants": True},
                                              "trace": {"participants": {"value": True, "rule": "a verified count"}}}},
    ])
    if finished:
        finish(conn, run_id)
    return run_id


def finish(conn, run_id):
    """The run ends: its judgements are public now, each with how its kind agreed with gold labels."""
    checks_db.finish_run(conn, run_id, status="succeeded", error=None, trials=1, errors=0)
    publishing.publish_run(conn, run_id)


@pytest.fixture
def trial(app_conn):
    return seed(app_conn)


def test_a_run_publishes_what_it_judged_and_a_withdrawn_one_stays_public_marked(app_conn, trial):
    public = TestClient(web.app)
    assert "No published results yet." in public.get("/").text
    run_id = judged(app_conn, trial)
    # nobody published it by hand: its run finished, and recorded how judgements of its kind agreed with gold
    basis = app_conn.execute("SELECT agreement->>'basis' FROM checks.judgement WHERE run_id = %s", (run_id,)).fetchone()
    assert "gold items agreed" in basis[0]
    home = public.get("/").text
    assert CT in home and '<span class="badge not-full">not full</span>' in home and "So far" not in home
    assert f'href="/trials/{trial}?run={run_id}"' in home
    # the trial page shows how the evidence was collected, step by step, but nothing of staff's work
    page = public.get(f"/trials/{trial}").text
    assert "How the evidence was collected" in page and QUOTE in page and "System prompt" in page
    assert "Published by its run as it finished" in page and "gold items agreed" in page
    assert "Withdraw" not in page
    assert "$0.42" not in page  # what a call cost stays with staff who see spending
    assert public.get(f"/runs/{run_id}").status_code == 200

    moderator = signed_in_client(app_conn, "moderator", "mona@test.example")
    staff_page = moderator.get(f"/trials/{trial}").text
    assert "Withdraw" in staff_page and "Trace, start to finish" in staff_page
    assert ">Publish<" not in staff_page and "not public" not in staff_page
    # a withdrawal needs a reason, because the public sees it
    refused = moderator.post(f"/trials/{trial}/judgements/{run_id}/withdraw", data={"note": " "}, follow_redirects=False)
    assert "error=" in refused.headers["location"]
    assert overview.trial_judgements(app_conn, trial)[0].withdrawn_at is None
    moderator.post(f"/trials/{trial}/judgements/{run_id}/withdraw", data={"note": "quote misread"})
    # still public, marked withdrawn with the reason, and no staff name
    home = public.get("/").text
    assert CT in home and "withdrawn" in home and "quote misread" in home
    page = public.get(f"/trials/{trial}").text
    assert "Withdrawn" in page and "quote misread" in page and "TrialWatch staff" in page
    assert "mona@test.example" not in page and "mona@test.example" not in home
    assert public.get(f"/runs/{run_id}").status_code == 200


def test_the_public_home_page_filters_the_published_trials(app_conn, trial):
    other_ct = "2023-500099-11-00"
    other = seed(app_conn, ct_number=other_ct)
    unpublished_ct = "2023-500077-22-00"
    unpublished = seed(app_conn, ct_number=unpublished_ct)
    judged(app_conn, trial, label="full")
    judged(app_conn, other, label="not full")
    # as a run still going has
    judged(app_conn, unpublished, label="full", finished=False)
    public = TestClient(web.app)

    def shown(query=""):
        home = public.get("/" + query).text
        return {ct for ct in (CT, other_ct, unpublished_ct) if ct in home}

    assert shown() == {CT, other_ct}
    assert shown("?result=full") == {CT}
    assert shown("?result=not+full") == {other_ct}
    assert shown("?result=nonsense") == {CT, other_ct}
    assert shown(f"?q={other_ct}") == {other_ct}
    assert shown("?q=no-such-trial") == set()
    assert "No trials match." in public.get("/?q=no-such-trial").text
    today = app_conn.execute("SELECT current_date").fetchone()[0]
    assert shown(f"?checked_from={today}&checked_to={today}") == {CT, other_ct}
    assert shown(f"?checked_from={today + timedelta(days=1)}") == set()
    assert shown(f"?checked_to={today - timedelta(days=1)}") == set()
    assert shown("?checked_from=not-a-day") == {CT, other_ct}
    assert overview.published_trial_count(app_conn, SET.name) == 2
    for status in overview.published_trial_statuses(app_conn, SET.name):
        assert shown(f"?status={status}") <= {CT, other_ct}
    # staff still see the workflow, not the public table
    moderator = signed_in_client(app_conn, "moderator")
    staff_home = moderator.get("/").text
    assert "So far" in staff_home and "Checked trials" not in staff_home


def test_a_judgement_with_an_error_is_not_published(app_conn, trial):
    run_id = judged(app_conn, trial, error="the model timed out")
    assert overview.trial_judgements(app_conn, trial)[0].published is False
    assert not checks_db.set_agreement(app_conn, run_id, trial, None, "nothing to measure")
    assert not checks_db.withdraw_judgement(app_conn, run_id, trial, "nothing to withdraw")
    assert TestClient(web.app).get(f"/trials/{trial}").status_code == 404


def test_a_run_that_ended_without_publishing_is_found_so_the_next_run_records_its_agreement(app_conn, trial):
    run_id = judged(app_conn, trial, finished=False)
    assert checks_db.runs_without_agreement(app_conn) == []  # still going: not public yet
    # its process died, or its publishing failed, after the run was closed
    checks_db.finish_run(app_conn, run_id, status="failed", error="abandoned", trials=1, errors=0)
    assert checks_db.runs_without_agreement(app_conn) == [run_id]
    publishing.publish_run(app_conn, run_id)
    assert checks_db.runs_without_agreement(app_conn) == []
    basis = app_conn.execute("SELECT agreement->>'basis' FROM checks.judgement WHERE run_id = %s", (run_id,)).fetchone()
    assert "gold items agreed" in basis[0]


def test_nobody_publishes_by_hand_and_withdrawing_needs_staff(app_conn, trial):
    run_id = judged(app_conn, trial)
    public = TestClient(web.app)
    assert public.post(f"/trials/{trial}/judgements/{run_id}/withdraw", data={"note": "x"}).status_code == 401
    moderator = signed_in_client(app_conn, "moderator")
    assert moderator.post(f"/trials/{trial}/judgements/{run_id}/publish").status_code in (404, 405)


def test_the_public_sees_the_newest_judgement_withdrawn_or_not_and_a_withdrawal_is_audited(app_conn, trial):
    judged(app_conn, trial, label="full")
    second = judged(app_conn, trial, label="not full")
    assert overview.trial_row(app_conn, trial, SET.name, published_only=True).label == "not full"
    core.act_as(app_conn, "pat@test.example")
    assert checks_db.withdraw_judgement(app_conn, second, trial, "wrong page")
    assert not checks_db.withdraw_judgement(app_conn, second, trial, "again")
    # the newest check is still the one shown, marked; the older one doesn't come back in its place
    assert overview.trial_row(app_conn, trial, SET.name, published_only=True).label == "not full"
    shown = overview.published_trial_rows(app_conn, SET.name)[0]
    assert (shown.run_id, shown.withdrawn_note) == (second, "wrong page") and shown.withdrawn_at is not None
    # the withdrawal is audited, with who did it; a judgement's other columns are written once, with its run
    audited = app_conn.execute(
        "SELECT l.action, a.name FROM core.audit_log l JOIN core.actor a ON a.id = l.actor_id"
        " WHERE l.table_name = 'checks.judgement' ORDER BY l.id").fetchall()
    assert audited == [("update", "pat@test.example")]


def test_the_public_opens_only_published_results_and_staff_pages_need_sign_in(app_conn, trial):
    run_id = judged(app_conn, trial, finished=False)
    sha = app_conn.execute(
        "SELECT d.file_sha256 FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id "
        "WHERE l.trial_id = %s LIMIT 1", (trial,)).fetchone()[0].hex()
    core.act_as(app_conn, "test-person")
    core.add_trial_sponsor(app_conn, trial, core.Sponsor(name="Acme Pharma", primary=True), "manual")
    sponsor = app_conn.execute("SELECT max(id) FROM core.organisation").fetchone()[0]
    public = TestClient(web.app)
    staff_only = ("/trials", "/sponsors", f"/sponsors/{sponsor}", "/sources", "/sources/ctis", "/gold")
    for path in staff_only:
        assert public.get(path).status_code == 401, path
        page = public.get(path, headers={"accept": "text/html"}, follow_redirects=False)
        assert page.headers["location"].startswith("/login?next="), path
    # not published yet: the trial and its run are not there for the public
    for path in (f"/trials/{trial}", f"/runs/{run_id}"):
        assert public.get(path).status_code == 404, path
    for path in ("/", "/login", "/prompts", "/requirements", f"/files/{sha}", f"/files/{sha}/raw"):
        assert public.get(path).status_code == 200, path
    home = public.get("/").text
    assert "Checked trials" in home
    for link in ("Trials</a>", "Sponsors</a>", "Prompts</a>", "Requirements</a>", "Sources</a>", "Gold</a>"):
        assert link not in home, link
    finish(app_conn, run_id)
    page = public.get(f"/trials/{trial}").text
    assert "Acme Pharma" in page and f"/sponsors/{sponsor}" not in page
    assert public.get(f"/runs/{run_id}").status_code == 200
    moderator = signed_in_client(app_conn, "moderator")
    for path in staff_only + (f"/trials/{trial}", f"/runs/{run_id}", "/prompts", "/requirements", f"/files/{sha}"):
        assert moderator.get(path).status_code == 200, path


def test_files_staff_see_are_marked_public_only_when_a_published_judgement_or_the_registry_has_them(app_conn, trial):
    core.act_as(app_conn, "test-person")
    uploaded = store_file(app_conn, "upload-pdf", b"%PDF-1.7 an internal note", "application/pdf")
    assert signed_in_client(app_conn, "moderator").get(f"/files/{uploaded.hex()}/raw").status_code == 200
    registry_file = app_conn.execute(
        "SELECT d.file_sha256 FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id "
        "WHERE l.trial_id = %s LIMIT 1", (trial,)).fetchone()[0]
    assert overview.file_is_public(app_conn, registry_file)
    assert not overview.file_is_public(app_conn, uploaded)


def test_the_home_page_shows_the_schedule_and_what_was_spent(app_conn, trial):
    """Anyone sees when the last scheduled run was, when the next is due, and the money spent so far."""
    home = TestClient(web.app).get("/").text
    assert "Last scheduled run" in home and "No scheduled run yet." in home
    # schedules start switched off
    assert "Next scheduled run" in home and "No schedule is switched on." in home
    core.act_as(app_conn, "test-person")
    schedule_id = jobs_db.schedules(app_conn)[0].id
    jobs_db.switch_schedule(app_conn, schedule_id, True)
    assert "weekly-refresh" in TestClient(web.app).get("/").text
    assert "Spent on AI so far" in home and "$0.00" in home
    staff = signed_in_client(app_conn, "moderator").get("/").text
    assert "Next scheduled run" in staff and "Spent on AI so far" in staff
