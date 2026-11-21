"""The web interface (trialwatch.web): it answers only requests addressed to this machine and takes
form posts only from its own pages, serves stored files without letting them run as its pages, and
shows on its pages what the stages and checks decided."""

import hashlib
from urllib.parse import unquote

from fastapi.testclient import TestClient
from test_sources_ctis import CT, RECORD, FakeCtis, ctis_source, listed_row, transfer

from trialwatch.db import core
from trialwatch.sources import ctis, stages
from trialwatch.web import app as web
from trialwatch.web.jobs import JobRequest, check_request


def error_of(response):
    """Where a form post sent the browser back to, with its message readable."""
    return unquote(response.headers["location"])


def listed_only(conn):
    """A trial CTIS lists, whose evidence is not transferred yet; returns its id."""
    core.act_as(conn, "ctis-adapter", "process")
    listing = FakeCtis(RECORD, [listed_row(CT)])
    stages.sync_list(conn, ctis_source(conn, listing={"criteria": {}, "page_size": 10}), ctis.CtisAdapter(listing))
    return core.trials_with_scheme(conn, "eu-ct", [CT])[CT]


def store_file(conn, key, content, media_type):
    """A document's file, stored as a person's upload; returns its sha256."""
    sha256 = hashlib.sha256(content).digest()
    core.save_document_version(conn, source="manual", source_key=key, kind="other", title=key, language=None,
                               version="1", content=content, media_type=media_type, sha256=sha256)
    return sha256


def text_of_pdf(content):
    """The fake PDFs' page text: their bytes, one page."""
    return [content.decode()]


def test_quote_is_marked_across_line_breaks_and_case():
    pieces = web.highlighted("Overall 24\nparticipants were randomised.", "24 Participants were")
    assert pieces == [("Overall ", False), ("24\nparticipants were", True), (" randomised.", False)]
    assert web.highlighted("text", None) == [("text", False)]


def test_only_requests_addressed_to_this_machine_are_answered(monkeypatch):
    client = TestClient(web.app)
    # a page on another domain whose name points at this machine (DNS rebinding) is not answered
    for host in ("evil.example", "evil.example:8010", "127.0.0.1.evil.example", "localhost.evil.example"):
        assert client.get("/requirements", headers={"host": host}).status_code == 403, host
    refused = client.get("/requirements", headers={"host": "evil.example:8010"})
    assert refused.text == "refused: unknown host 'evil.example'"
    for host in ("127.0.0.1:8010", "localhost", "LOCALHOST:8010", "[::1]:8010", "testserver"):
        assert client.get("/requirements", headers={"host": host}).status_code == 200, host
    # names it is served under on a network are added by hand
    assert client.get("/requirements", headers={"host": "trialwatch.lan:8010"}).status_code == 403
    monkeypatch.setenv("TRIALWATCH_HOSTS", "trialwatch.lan, 192.168.1.20")
    assert client.get("/requirements", headers={"host": "trialwatch.lan:8010"}).status_code == 200
    assert client.get("/requirements", headers={"host": "192.168.1.20"}).status_code == 200


def test_form_posts_are_taken_only_from_the_interfaces_own_pages(admin_client):
    client = admin_client
    # any page open in the browser could otherwise post a form here (cross-site request forgery)
    refused = client.post("/jobs/999/stop", headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert (refused.status_code, refused.text) == (403, "refused: a form post from another site ('evil.example')")
    from_referer = client.post("/jobs/999/stop", headers={"referer": "https://evil.example/a-page"},
                               follow_redirects=False)
    assert from_referer.status_code == 403
    # this machine on another port is another site
    other_port = client.post("/jobs/999/stop", headers={"host": "localhost:8010", "origin": "http://localhost:9999"},
                             follow_redirects=False)
    assert other_port.status_code == 403
    # its own pages; and a client that is not a browser page (no Origin, no Referer)
    # (taken, and answered: there is no job 999)
    own = client.post("/jobs/999/stop", headers={"origin": "http://testserver"}, follow_redirects=False)
    assert own.status_code == 404
    own_page = client.post("/jobs/999/stop", headers={"host": "localhost:8010", "referer": "http://localhost:8010/"},
                           follow_redirects=False)
    assert own_page.status_code == 404
    assert client.post("/jobs/999/stop", follow_redirects=False).status_code == 404
    # following a link from elsewhere changes nothing: reading is answered
    assert client.get("/requirements", headers={"referer": "https://evil.example/"}).status_code == 200
    # and a form only ever sends the browser back to one of the interface's own pages
    assert web._redirect_back("https://evil.example/x", "no").headers["location"] == "/?error=no"


def test_a_stored_pdf_is_shown_and_anything_else_is_downloaded(app_conn):
    core.act_as(app_conn, "test-person")
    pdf = store_file(app_conn, "upload-pdf", b"%PDF-1.7 results", "application/pdf")
    page = store_file(app_conn, "upload-html", b"<html><script>alert(document.cookie)</script></html>", "text/html")
    client = TestClient(web.app)
    shown = client.get(f"/files/{pdf.hex()}/raw")
    assert (shown.status_code, shown.content) == (200, b"%PDF-1.7 results")
    assert shown.headers["content-type"] == "application/pdf"
    # in the browser's PDF viewer, with nothing it could run as a page of this site
    assert shown.headers["x-content-type-options"] == "nosniff"
    assert shown.headers["content-security-policy"] == "sandbox"
    assert shown.headers["content-disposition"] == "inline"
    downloaded = client.get(f"/files/{page.hex()}/raw")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/octet-stream"
    assert downloaded.headers["content-disposition"] == f'attachment; filename="{page.hex()}"'
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/files/{'00' * 32}/raw").status_code == 404
    assert client.get("/files/not-a-hash/raw").status_code == 404


def test_a_trial_page_says_why_it_cannot_be_checked_yet(app_conn, admin_client):
    trial_id = listed_only(app_conn)
    client = admin_client
    page = client.get(f"/trials/{trial_id}")
    assert page.status_code == 200
    assert "It can't be checked yet: ctis: evidence not transferred." in page.text
    transfer(app_conn, FakeCtis(RECORD))
    page = client.get(f"/trials/{trial_id}")
    assert page.status_code == 200 and "It can't be checked yet" not in page.text
    # the workflow page offers it for a check now
    workflow = client.get("/")
    assert workflow.status_code == 200 and CT in workflow.text


def test_checking_a_trial_whose_evidence_is_not_complete_goes_back_with_why(app_conn, admin_client):
    trial_id = listed_only(app_conn)
    client = admin_client
    response = client.post("/jobs", data={"step": "evidence", "trial_ids": [str(trial_id)],
                                          "back": f"/trials/{trial_id}"}, follow_redirects=False)
    assert response.status_code == 303
    assert error_of(response) == (f"/trials/{trial_id}?error=not checked, their evidence is not complete: {CT} "
                                  "(ctis: evidence not transferred)")
    unknown_setup = client.post("/jobs", data={"step": "evidence", "trial_ids": [str(trial_id)],
                                               "config": "../default", "back": f"/trials/{trial_id}"},
                                follow_redirects=False)
    assert "no setup '../default'" in error_of(unknown_setup)
    assert web.jobs.running() is None


def test_a_bad_ct_number_goes_back_with_a_message(admin_client):
    client = admin_client
    response = client.post("/jobs", data={"step": "transfer", "source": "ctis",
                                          "key_text": "2022-500044-38-01, nonsense", "back": "/sources/ctis"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/sources/ctis?error=not%20an%20EU%20CT%20number")


def test_a_trial_typed_in_takes_valid_identifiers_and_pdf_files_only(app_conn, admin_client, monkeypatch):
    monkeypatch.setattr("trialwatch.sources.pages.extract_pages", text_of_pdf)
    core.act_as(app_conn, "test-person")
    client = admin_client
    created = client.post("/trials/new", data={"title": "A typed-in study", "schemes": ["nct"], "values": ["NCT09999970"]},
                          files=[("files", ("results.pdf", b"%PDF-1.7 24 participants", "application/pdf"))],
                          follow_redirects=False)
    assert created.status_code == 303 and created.headers["location"].startswith("/trials/")
    trial_id = int(created.headers["location"].removeprefix("/trials/"))
    assert core.trials_with_scheme(app_conn, "nct", ["NCT09999970"]) == {"NCT09999970": trial_id}
    refused = client.post(f"/trials/{trial_id}/documents", data={"kind": "results-summary"},
                          files={"file": ("page.html", b"<html>24 participants</html>", "text/html")},
                          follow_redirects=False)
    assert error_of(refused) == f"/trials/{trial_id}?error=page.html is not a PDF: only PDF files can be read"
    typo = client.post("/trials/new", data={"title": "A typo", "schemes": ["nct"], "values": ["NCT123"]},
                       follow_redirects=False)
    assert error_of(typo) == "/trials/new?error=NCT123 is not a valid nct identifier"


def test_job_requests_are_checked_before_anything_runs(monkeypatch):
    refused = [
        (JobRequest(step="sync"), "unknown step"),
        (JobRequest(step="list"), "choose a source"),
        (JobRequest(step="list", source="ctis", limit=0), "the limit must be 1 or more"),
        (JobRequest(step="transfer", source="ctis"), "choose trials, or which"),
        (JobRequest(step="transfer", source="ctis", which="everything"), "choose one of"),
        (JobRequest(step="transfer", source="ctis", which="pending", keys=["x"]), "not both"),
        (JobRequest(step="evidence", keys=["2022-500044-38-01"]), "choose trials"),
        (JobRequest(step="evidence", every=True, config="missing.v9", dry_run=True), "no setup 'missing.v9'"),
        (JobRequest(step="evidence", every=True, config="../default"), "no setup '../default'"),
    ]
    for request, message in refused:
        try:
            check_request(request)
        except ValueError as exc:
            assert message in str(exc), request
        else:
            raise AssertionError(f"{request} was accepted")
    # no API key is needed to ask for a check (a run without one stops at its first call)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    check_request(JobRequest(step="evidence", every=True))
    check_request(JobRequest(step="evidence", trial_ids=[1], dry_run=True))
    check_request(JobRequest(step="transfer", source="ctis", keys=["2022-500044-38-01"]))
    check_request(JobRequest(step="transfer", source="ctis", which="stale"))


def test_the_trace_names_files_by_their_document():
    steps = [web.overview.StepRow(1, "pages", None, None, {"documents": [{"doc": 1, "file": "ab", "title": "T"}]})]
    assert web._files_by_hex(steps) == {"ab": {"doc": 1, "file": "ab", "title": "T"}}
