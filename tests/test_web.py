"""The web interface (trialwatch.web): it answers only requests addressed to this machine and takes
form posts only from its own pages, serves stored files without letting them run as its pages, and
shows on its pages what the stages and checks decided."""

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote

from fastapi.testclient import TestClient
from test_sources_ctis import CT, RECORD, FakeCtis, ctis_source, listed_row, transfer

from trialwatch.db import core
from trialwatch.sources import ctis, stages
from trialwatch.web import app as web
from trialwatch.db import jobs as jobs_db
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
        assert client.get("/login", headers={"host": host}).status_code == 403, host
    refused = client.get("/login", headers={"host": "evil.example:8010"})
    assert refused.text == "refused: unknown host 'evil.example'"
    for host in ("127.0.0.1:8010", "localhost", "LOCALHOST:8010", "[::1]:8010", "testserver"):
        assert client.get("/login", headers={"host": host}).status_code == 200, host
    # names it is served under on a network are added by hand
    assert client.get("/login", headers={"host": "trialwatch.lan:8010"}).status_code == 403
    monkeypatch.setenv("TRIALWATCH_HOSTS", "trialwatch.lan, 192.168.1.20")
    assert client.get("/login", headers={"host": "trialwatch.lan:8010"}).status_code == 200
    assert client.get("/login", headers={"host": "192.168.1.20"}).status_code == 200


def test_pages_carry_the_brand_and_its_styles_are_served(admin_client):
    client = TestClient(web.app)
    page = client.get("/login").text
    assert '<img src="/static/logo.svg"' in page and 'href="/static/brand.css"' in page
    staff_page = admin_client.get("/requirements").text
    assert '<a href="/requirements" aria-current="page">Requirements</a>' in staff_page
    for name, kind in (("brand.css", "text/css"), ("components.css", "text/css"), ("logo.svg", "image/svg+xml"),
                       ("favicon.svg", "image/svg+xml")):
        response = client.get(f"/static/{name}")
        assert response.status_code == 200 and response.headers["content-type"].startswith(kind), name


def test_brand_colours_meet_wcag_aa_contrast_in_both_themes():
    """Every text colour on the background it is drawn on reaches 4.5:1, light and dark (brand.css)."""
    css = (Path(web.__file__).parent / "static/brand.css").read_text()
    light, dark = css.split("@media (prefers-color-scheme: dark)")
    pairs = [("text", "bg"), ("text", "surface"), ("muted", "surface"), ("muted", "bg"), ("link", "surface"),
             ("link", "bg"), ("on-primary", "primary"), ("on-danger", "danger"), ("header-text", "header-bg"),
             ("header-link", "header-bg"), ("full", "full-bg"), ("notfull", "notfull-bg"),
             ("undecided", "undecided-bg"), ("info", "info-bg")]
    for name, block in (("light", light), ("dark", dark + light)):
        tokens = colour_tokens(block)
        for fore, back in pairs:
            ratio = contrast(tokens[fore], tokens[back])
            assert ratio >= 4.5, f"{name}: --{fore} on --{back} is {ratio:.2f}:1"


def colour_tokens(css):
    """--name: #rrggbb, following var(--other); the first definition wins (dark before light)."""
    tokens = {}
    for name, value in re.findall(r"--([\w-]+):\s*([^;]+);", css):
        tokens.setdefault(name, value.strip())
    resolved = {}
    for name in tokens:
        value = tokens[name]
        while value.startswith("var(--"):
            value = tokens[value[6:-1]]
        resolved[name] = value
    return resolved


def contrast(one, two):
    def luminance(colour):
        channels = []
        for start in (1, 3, 5):
            c = int(colour[start:start + 2], 16) / 255
            if c <= 0.03928:
                channels.append(c / 12.92)
            else:
                channels.append(((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
    high, low = sorted((luminance(one), luminance(two)), reverse=True)
    return (high + 0.05) / (low + 0.05)


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
    # an opaque origin (a sandboxed frame, or a page whose policy hides its origin) could be any site
    opaque = client.post("/jobs/999/stop", headers={"origin": "null"}, follow_redirects=False)
    assert opaque.status_code == 403
    # so our own pages must not hide theirs: under "no-referrer" a browser sends "Origin: null" with a
    # form post, and every form here (sign-in first) would be refused
    policy = client.get("/login").headers["referrer-policy"]
    assert policy not in ("no-referrer", "")
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


def test_a_stored_pdf_is_shown_and_anything_else_is_downloaded(app_conn, admin_client):
    core.act_as(app_conn, "test-person")
    pdf = store_file(app_conn, "upload-pdf", b"%PDF-1.7 results", "application/pdf")
    page = store_file(app_conn, "upload-html", b"<html><script>alert(document.cookie)</script></html>", "text/html")
    # uploads by a person are for staff until a published judgement cites them (ADR-007)
    client = admin_client
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
    assert jobs_db.recent_jobs(app_conn) == []


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


def test_a_browser_gets_an_error_page_and_a_script_the_same_answer_as_before(admin_client):
    page = admin_client.get("/no-such-page", headers={"accept": "text/html"})
    assert page.status_code == 404 and "text/html" in page.headers["content-type"]
    assert "Not found" in page.text and "There is no page at this address." in page.text
    script = admin_client.get("/no-such-page")
    assert (script.status_code, script.json()) == (404, {"detail": "Not Found"})


def fails_quoting_the_search(*args):
    # the first argument is the search box (q); the error's message quotes it, as a real one might
    raise RuntimeError(f"could not read {args[0]!r}")


def test_an_unhandled_error_is_logged_with_the_reference_the_page_shows(app_conn, monkeypatch, caplog):
    monkeypatch.setattr(web.web_search, "params_from", fails_quoting_the_search)
    client = TestClient(web.app, raise_server_exceptions=False)
    with caplog.at_level("ERROR", logger="trialwatch.web"):
        page = client.get("/find", params={"q": "private-marker"}, headers={"accept": "text/html"})
    assert page.status_code == 500 and "Something went wrong" in page.text
    reference = re.search(r"<code>([0-9a-f]{8})</code>", page.text).group(1)
    logged = caplog.text
    assert reference in logged and "RuntimeError" in logged and "/find" in logged
    # neither the search nor the error's message (which quotes it) reaches the log or the page
    assert "private-marker" not in logged and "private-marker" not in page.text
