"""What the public sees (ADR-007): a judgement only once staff published it, with its answers and
quotes but not its trace; staff-only pages; people shown only as staff; files a person uploaded kept
from the public until a published judgement cites them."""

import psycopg
import pytest
from conftest import signed_in_client
from fastapi.testclient import TestClient
from test_evidence import SET, seed, start_a_run
from test_web import store_file

from trialwatch.db import checks as checks_db
from trialwatch.db import core, overview
from trialwatch.web import app as web

QUOTE = "Overall 24 participants were randomised and analysed."


def judged(conn, trial_id, label="not full", error=None):
    """A finished run that judged the trial, quoting its results summary; returns the run id."""
    run_id = start_a_run(conn)
    sha = conn.execute(
        "SELECT d.file_sha256 FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id "
        "WHERE l.trial_id = %s LIMIT 1", (trial_id,)).fetchone()[0]
    checks_db.save_question(conn, run_id, trial_id, "participants", "How many took part?", [], False)
    checks_db.save_evidence(conn, run_id, trial_id, "participants", found=True, kind="numeric_result", quote=QUOTE,
                            document_version_id=None, file_sha256=sha, page_no=2, note="a secret model note",
                            verified=True)
    if error is not None:
        label = None
    checks_db.save_judgement(conn, run_id, trial_id, parts={"participants": True}, label=label,
                             trace={"participants": {"value": True, "rule": "a verified count"}}, served_by=[],
                             record_id=None, document_versions=[], input_tokens=123456, output_tokens=7,
                             duration_ms=10, error=error)
    checks_db.finish_run(conn, run_id, status="succeeded", error=None, trials=1, errors=0, input_tokens=123456,
                         output_tokens=7)
    return run_id


@pytest.fixture
def trial(app_conn):
    return seed(app_conn)


def test_the_public_sees_a_judgement_only_once_published_and_never_its_trace(app_conn, trial):
    run_id = judged(app_conn, trial)
    public = TestClient(web.app)
    page = public.get(f"/trials/{trial}").text
    assert "not full" not in page and QUOTE not in page
    assert "No published result yet" in page
    assert public.get("/trials").text.count("not full") == 0

    moderator = signed_in_client(app_conn, "moderator", "mona@test.example")
    staff_page = moderator.get(f"/trials/{trial}").text
    assert "Publish" in staff_page and "not public" in staff_page and "Trace, start to finish" in staff_page
    assert moderator.post(f"/trials/{trial}/judgements/{run_id}/publish", follow_redirects=False).status_code == 303

    page = public.get(f"/trials/{trial}").text
    assert "not full" in page and QUOTE in page
    # the trace, the model's notes and the staff controls stay with staff
    assert "Trace, start to finish" not in page and "Publish" not in page and "Sources</h3>" not in page
    assert "mona@test.example" not in page
    assert "not full" in public.get("/trials").text
    assert f"/trials/{trial}?run={run_id}" in public.get(f"/runs/{run_id}").text

    moderator.post(f"/trials/{trial}/judgements/{run_id}/withdraw", data={"note": "quote misread"})
    page = public.get(f"/trials/{trial}").text
    assert QUOTE not in page and "No published result yet" in page
    assert f"/trials/{trial}?run={run_id}" not in public.get(f"/runs/{run_id}").text


def test_a_judgement_with_an_error_cannot_be_published(app_conn, trial):
    run_id = judged(app_conn, trial, error="the model timed out")
    moderator = signed_in_client(app_conn, "moderator")
    refused = moderator.post(f"/trials/{trial}/judgements/{run_id}/publish", follow_redirects=False)
    assert "error=" in refused.headers["location"]
    with pytest.raises(psycopg.errors.RaiseException):
        checks_db.publish_judgement(app_conn, run_id, trial)


def test_publishing_needs_staff(app_conn, trial):
    run_id = judged(app_conn, trial)
    public = TestClient(web.app)
    assert public.post(f"/trials/{trial}/judgements/{run_id}/publish").status_code == 401
    assert public.post(f"/trials/{trial}/judgements/{run_id}/withdraw").status_code == 401


def test_the_public_view_is_the_newest_live_publication_and_each_change_is_audited(app_conn, trial):
    first = judged(app_conn, trial, label="full")
    second = judged(app_conn, trial, label="not full")
    core.act_as(app_conn, "pat@test.example")
    assert checks_db.publish_judgement(app_conn, first, trial)
    assert not checks_db.publish_judgement(app_conn, first, trial)
    assert overview.trial_row(app_conn, trial, SET.name, published_only=True).label == "full"
    checks_db.publish_judgement(app_conn, second, trial)
    assert overview.trial_row(app_conn, trial, SET.name, published_only=True).label == "not full"
    assert checks_db.withdraw_publication(app_conn, second, trial, "wrong page")
    assert overview.trial_row(app_conn, trial, SET.name, published_only=True).label == "full"
    actions = app_conn.execute(
        "SELECT action FROM core.audit_log WHERE table_name = 'checks.publication' ORDER BY id").fetchall()
    assert [row[0] for row in actions] == ["insert", "insert", "update"]


def test_staff_only_pages_send_the_public_to_sign_in(app_conn, trial):
    public = TestClient(web.app)
    for path in ("/sources", "/sources/ctis", "/gold"):
        assert public.get(path).status_code == 401, path
        page = public.get(path, headers={"accept": "text/html"}, follow_redirects=False)
        assert page.headers["location"].startswith("/login?next="), path
    home = public.get("/").text
    assert "with a published result" in home and "Sources</a>" not in home and "Gold</a>" not in home
    moderator = signed_in_client(app_conn, "moderator")
    for path in ("/sources", "/sources/ctis", "/gold"):
        assert moderator.get(path).status_code == 200, path


def test_people_are_shown_to_the_public_only_as_staff(app_conn, trial):
    core.act_as(app_conn, "pat@test.example")
    core.add_trial_sponsor(app_conn, trial, core.Sponsor(name="Pat's Pharma", primary=False), "manual")
    public = TestClient(web.app).get(f"/trials/{trial}").text
    assert "pat@test.example" not in public and web.STAFF in public
    # a process keeps its name: the public may see which source's adapter brought something in
    staff = signed_in_client(app_conn, "admin").get(f"/trials/{trial}").text
    assert "pat@test.example" in staff


def test_a_file_a_person_uploaded_is_public_only_once_a_published_judgement_cites_it(app_conn, trial):
    core.act_as(app_conn, "test-person")
    uploaded = store_file(app_conn, "upload-pdf", b"%PDF-1.7 an internal note", "application/pdf")
    public = TestClient(web.app)
    assert public.get(f"/files/{uploaded.hex()}/raw").status_code == 404
    assert signed_in_client(app_conn, "moderator").get(f"/files/{uploaded.hex()}/raw").status_code == 200
    # a document CTIS published is public
    registry_file = app_conn.execute(
        "SELECT d.file_sha256 FROM core.trial_document l JOIN core.document_current d ON d.id = l.document_id "
        "WHERE l.trial_id = %s LIMIT 1", (trial,)).fetchone()[0]
    assert public.get(f"/files/{registry_file.hex()}").status_code == 200
    assert overview.file_is_public(app_conn, registry_file)
    assert not overview.file_is_public(app_conn, uploaded)
