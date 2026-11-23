"""Trials and documents entered by a person (ADR-003): matched to trials already here by identifier,
PDF files only, nothing added while the manual source is switched off; and a trial from a fake CTIS."""

import pytest
from test_evidence import SET
from test_sources_ctis import RECORD, FakeCtis, transfer

from trialwatch.db import core, overview
from trialwatch.db.core import Identifier
from trialwatch.sources import manual
from trialwatch.trials import readiness
from trialwatch.sources.manual import TrialEntry, UploadedFile

ENTRY = TrialEntry(identifiers=[Identifier("nct", "NCT09999977")], public_title="A hand-entered study",
                   sponsors=["Example University", "", "Example Pharma", "example  university"], status="Completed",
                   primary_endpoints=["Change in pain score at 12 weeks", "Adverse events"])


def pdf(text, name="results.pdf"):
    return UploadedFile(name, f"%PDF-1.7 {text}".encode())


def text_of_pdf(content):
    """The fake PDFs' page text: their bytes, one page."""
    return [content.decode()]


def broken_pdf(content):
    raise ValueError("broken PDF")


@pytest.fixture
def no_pdf_reading(monkeypatch):
    monkeypatch.setattr("trialwatch.trials.pages.extract_pages", text_of_pdf)


def transfer_of(conn, trial_id):
    """(status, document kinds, documents, pages) of the trial's evidence transfer at the manual source."""
    return conn.execute(
        "SELECT e.status, e.document_kinds, e.documents, e.pages FROM core.evidence_transfer e "
        "JOIN core.trial_source s USING (source_id, source_key) WHERE s.trial_id = %s AND s.source_id = %s",
        (trial_id, core.source_id(conn, "manual")),
    ).fetchone()


def test_a_trial_added_by_hand_is_checked_like_any_other(conn, no_pdf_reading):
    core.act_as(conn, "test-person")
    trial_id = manual.add_trial(conn, ENTRY, [pdf("results page with 24 participants")], "results-summary")
    registered = core.trial_registered(conn, trial_id)
    assert registered["public_title"] == "A hand-entered study"
    endpoints = []
    for endpoint in registered["primary_endpoints"]:
        endpoints.append(endpoint["text"])
    assert endpoints == ENTRY.primary_endpoints
    pages = core.trial_pages(conn, trial_id, ["results-summary"])
    assert len(pages) == 1 and "24 participants" in pages[0].text
    link = conn.execute("SELECT basis FROM core.trial_document WHERE trial_id = %s", (trial_id,)).fetchone()
    assert link == ("manual",)
    stored = conn.execute("SELECT f.media_type, d.title FROM core.document_current d JOIN core.file f "
                          "ON f.sha256 = d.file_sha256 JOIN core.trial_document l ON l.document_id = d.id "
                          "WHERE l.trial_id = %s", (trial_id,)).fetchone()
    assert stored == ("application/pdf", "results.pdf")
    who = []
    for entry in core.audit_entries(conn, "core.trial", {"id": trial_id}):
        who.append((entry.action, entry.actor))
    assert who == [("insert", "test-person")]
    # its evidence is what the person gave, of any kind: complete, so a check may read it
    assert transfer_of(conn, trial_id) == ("complete", None, 1, 1)
    assert readiness.not_ready(conn, [trial_id], SET.document_kinds) == {}


def test_a_trial_without_identifiers_gets_our_own_number(conn):
    core.act_as(conn, "test-person")
    trial_id = manual.add_trial(conn, TrialEntry(identifiers=[], public_title="Unregistered study"), [], "other")
    assert core.trial_identifiers(conn, trial_id) == [Identifier("trialwatch", f"TW-{trial_id}")]


def test_an_identifier_that_does_not_fit_its_scheme_is_refused(conn):
    core.act_as(conn, "test-person")
    typo = TrialEntry(identifiers=[Identifier("nct", "NCT123")], public_title="A study with a typo")
    with pytest.raises(ValueError, match="NCT123 is not a valid nct identifier"):
        manual.add_trial(conn, typo, [], "results-summary")
    count = conn.execute("SELECT count(*) FROM core.trial").fetchone()
    assert count == (0,)


def test_a_known_trial_is_not_added_twice(conn):
    core.act_as(conn, "test-person")
    first = manual.add_trial(conn, ENTRY, [], "results-summary")
    with pytest.raises(manual.AlreadyKnown) as known:
        manual.add_trial(conn, TrialEntry(identifiers=[Identifier("nct", " nct09999977 ")], public_title="Again"),
                         [], "results-summary")
    assert known.value.trial_id == first


def test_the_same_file_twice_on_one_trial_is_refused(conn, no_pdf_reading):
    core.act_as(conn, "test-person")
    trial_id = manual.add_trial(conn, ENTRY, [pdf("x")], "results-summary")
    with pytest.raises(ValueError, match="already linked"):
        manual.add_document(conn, trial_id, pdf("x"), "results-summary", None)


def test_only_pdf_files_are_taken(conn, no_pdf_reading):
    core.act_as(conn, "test-person")
    page = UploadedFile("results.html", b"<html><body>24 participants</body></html>")
    with pytest.raises(ValueError, match="results.html is not a PDF"):
        manual.add_trial(conn, ENTRY, [pdf("fine"), page], "results-summary")
    # nothing of the refused trial was added
    assert core.trials_with_identifiers(conn, [Identifier("nct", "NCT09999977")]) == {}
    trial_id = manual.add_trial(conn, ENTRY, [], "results-summary")
    with pytest.raises(ValueError, match="is not a PDF"):
        manual.add_document(conn, trial_id, UploadedFile("results.docx", b"PK\x03\x04 a word file"), "results-summary",
                            None)
    with pytest.raises(ValueError, match="empty.pdf is empty"):
        manual.add_document(conn, trial_id, UploadedFile("empty.pdf", b""), "results-summary", None)
    document = manual.add_document(conn, trial_id, pdf("fine"), "study-report", "The study report")
    stored = conn.execute("SELECT d.kind, d.title, f.media_type FROM core.document_current d JOIN core.file f "
                          "ON f.sha256 = d.file_sha256 WHERE d.id = %s", (document,)).fetchone()
    assert stored == ("study-report", "The study report", "application/pdf")


def test_nothing_is_added_while_the_manual_source_is_switched_off(conn, no_pdf_reading):
    core.act_as(conn, "test-person")
    trial_id = manual.add_trial(conn, ENTRY, [], "results-summary")
    core.set_source_config(conn, "manual", {}, False)
    other = TrialEntry(identifiers=[Identifier("nct", "NCT09999976")], public_title="Another study")
    with pytest.raises(ValueError, match="the manual source is switched off"):
        manual.add_trial(conn, other, [], "results-summary")
    with pytest.raises(ValueError, match="the manual source is switched off"):
        manual.add_document(conn, trial_id, pdf("results"), "results-summary", None)
    documents = conn.execute("SELECT count(*) FROM core.trial_document WHERE trial_id = %s", (trial_id,)).fetchone()
    assert documents == (0,)
    assert core.trials_with_identifiers(conn, [Identifier("nct", "NCT09999976")]) == {}


def test_a_transfer_brings_a_trial_in_ready_for_a_check(conn, no_pdf_reading):
    core.act_as(conn, "ctis-adapter", "process")
    result = transfer(conn, FakeCtis(RECORD), extract=None)
    assert len(result.trial_ids) == 1 and result.pages == 2
    assert result.trial_ids[0] in core.trials_with_pages(conn, ["results-summary"])
    assert readiness.ready_trials(conn, SET.document_kinds) == result.trial_ids


def test_a_trial_added_by_hand_is_ready_once_its_documents_are_read(conn, no_pdf_reading, monkeypatch):
    core.act_as(conn, "test-person")
    without_documents = manual.add_trial(conn, ENTRY, [], "results-summary")
    assert readiness.not_ready(conn, [without_documents], SET.document_kinds) == {}
    monkeypatch.setattr("trialwatch.trials.pages.extract_pages", broken_pdf)
    other = TrialEntry(identifiers=[Identifier("nct", "NCT09999979")], public_title="Unreadable")
    broken = manual.add_trial(conn, other, [pdf("y")], "results-summary")
    assert readiness.not_ready(conn, [broken], SET.document_kinds) == {broken: "manual: its last transfer failed"}
    assert transfer_of(conn, broken)[0] == "failed"


def test_typed_sponsors_reuse_an_organisation_with_the_same_name(conn, no_pdf_reading):
    core.act_as(conn, "test-person")
    first = manual.add_trial(conn, ENTRY, [], "results-summary")
    other = TrialEntry(identifiers=[Identifier("nct", "NCT09999978")], public_title="Another study",
                       sponsors=["EXAMPLE pharma"])
    second = manual.add_trial(conn, other, [], "results-summary")
    sponsors = overview.trial_sponsors(conn, first)
    # the name typed twice counts once; the first typed is the primary sponsor
    named = []
    for sponsor in sponsors:
        named.append((sponsor.name, sponsor.is_primary))
    assert named == [("Example University", True), ("Example Pharma", False)]
    shared = overview.trial_sponsors(conn, second)[0]
    assert shared.organisation_id == sponsors[1].organisation_id
    sponsored = []
    for trial in overview.sponsored_trials(conn, shared.organisation_id, SET.name):
        sponsored.append(trial.trial_id)
    assert sponsored == [second, first]
    # a person adds a new primary sponsor: the previous primary becomes a co-sponsor
    core.add_trial_sponsor(conn, first, core.Sponsor("Example Pharma", True), "manual")
    named = []
    for sponsor in overview.trial_sponsors(conn, first):
        named.append((sponsor.name, sponsor.is_primary))
    assert named == [("Example Pharma", True), ("Example University", False)]
