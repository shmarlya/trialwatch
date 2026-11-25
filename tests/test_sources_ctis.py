"""The CTIS adapter into the central model (ADR-003, ADR-005), against a fake CTIS."""

import copy
import dataclasses
from datetime import UTC, datetime

from trialwatch.db import core, overview
from trialwatch.db.core import Identifier
from trialwatch.hashing import storable
from trialwatch.sources import ctis
from trialwatch.trials import ingest, readiness
from trialwatch.sources.ctis_client import DownloadedFile
from trialwatch.trials.pages import extract_missing, text_fingerprint

CT = "1999-000000-00-11"
SUMMARY = {"uuid": "00000000-0000-4000-8000-000000000001", "documentType": "103", "fileType": "PDF",
           "title": "Results_summary", "systemVersion": "1", "languageCode": "en", "associatedEntityId": 5001}
REPORT = {"uuid": "00000000-0000-4000-8000-000000000004", "documentType": "45", "fileType": "PDF",
          "title": "Clinical study report", "systemVersion": "1"}
LAY = {"uuid": "00000000-0000-4000-8000-000000000002", "documentType": "46", "fileType": "PDF",
       "title": "Lay summary", "systemVersion": "1", "associatedEntityId": "5002"}
PROTOCOL = {"uuid": "00000000-0000-4000-8000-000000000003", "documentType": "104", "fileType": "PDF",
            "title": "Protocol", "systemVersion": "1"}
RECORD = {
    "ctNumber": CT, "ctStatus": "Ended", "startDateEU": "2021-06-25", "endDateEU": "2024-04-23",
    "documents": [SUMMARY, REPORT, LAY, PROTOCOL],
    "results": {
        "summaryResults": [{"id": 5001, "submissionDate": "2025-04-10T17:39:54"},
                           {"id": 5003, "submissionDate": "not a date"}],
        "laypersonResults": [{"id": "5002", "submissionDate": "2025-05-02T08:00:00"}],
    },
    "authorizedApplication": {"authorizedPartI": {
        "sponsors": [
            {"primary": True, "isCommercial": True,
             "organisation": {"name": "Example Pharma", "type": "Pharmaceutical company", "businessKey": "ORG-999000001"},
             "addresses": [{"address": {"countryName": "Denmark"}}]},
            {"primary": False, "isCommercial": False,
             "organisation": {"name": "Example  University", "businessKey": "LOC-1"}},
        ],
        "trialDetails": {
            "clinicalTrialIdentifiers": {"publicTitle": "A study", "secondaryIdentifyingNumbers": {
                "nctNumber": {"number": "NCT09999911"}, "whoUniversalTrialNumber": {"number": "U9999-9999-9911"}}},
            "trialInformation": {"endPoint": {"primaryEndPoints": [
                {"id": 7, "number": 1, "endPoint": "Change in  PVR\\nat 12 weeks"}]}},
        },
    }},
}
# Where CTIS puts every identifier it gives, as its records have them (values of other types happen)
EVERY_IDENTIFIER = {
    "ctNumber": "1999-000000-00-21",
    "documents": [],
    "authorizedApplication": {
        "eudraCt": {"eudraCtCode": "2019-001234-56", "isTransitioned": True},
        "authorizedPartI": {"trialDetails": {"clinicalTrialIdentifiers": {
            "publicTitle": "A transitioned trial",
            "secondaryIdentifyingNumbers": {
                "nctNumber": {"number": "N/A"},
                "whoUniversalTrialNumber": {"number": " u1111-1234-5678 "},
                "isrctnNumber": {"number": "ISRCTN12345678"},
                "additionalRegistries": [
                    {"otherRegistryName": "EudraCT number", "number": "2019-001234-56"},
                    {"otherRegistryName": "JapicCTI", "number": "JapicCTI-123456"},
                    {"otherRegistryName": "EudraCT", "number": {"value": "2019-009999-99"}},
                    "not a registry",
                ],
            },
        }}},
    },
}


def listed_row(ct_number, updated="10/11/2026", title="A listed\nstudy"):
    return {"ctNumber": ct_number, "ctTitle": title, "sponsor": "Example Pharma", "trialPhase": "Phase III",
            "resultsFirstReceived": "Yes", "lastUpdated": updated}


def ct_number_of(row):
    return row["ctNumber"]


class FakeCtis:
    """CTIS's search, retrieve and download, from a record and a list. Setting `retrieve_error`, or an
    error per document uuid in `download_errors`, makes those calls fail."""

    def __init__(self, record, listing=None):
        self.record = record
        self.listing = []
        if listing is not None:
            self.listing = listing
        self.downloads = []
        # uuid -> text added to that document's file, as if CTIS replaced it without a new systemVersion
        self.replaced = {}
        self.searches = []
        self.retrieve_error = None
        self.download_errors = {}

    def retrieve(self, ct_number):
        if self.retrieve_error is not None:
            raise self.retrieve_error
        return copy.deepcopy(self.record)

    def download_document(self, ct_number, uuid):
        if uuid in self.download_errors:
            raise self.download_errors[uuid]
        self.downloads.append(uuid)
        version = "1"
        for entry in self.record["documents"]:
            if entry["uuid"] == uuid:
                version = entry["systemVersion"]
        extra = ""
        if uuid in self.replaced:
            extra = self.replaced[uuid]
        return DownloadedFile(f"%PDF-1.7 {uuid} v{version}{extra}".encode(), "application/pdf", None)

    def search(self, criteria, page, size, direction="ASC"):
        """Like CTIS: sorted by EU CT number, and nothing past ctis.SEARCH_LIMIT rows of one query."""
        self.searches.append((criteria, page, size, direction))
        descending = direction == "DESC"
        ordered = sorted(self.listing, key=ct_number_of, reverse=descending)
        start = (page - 1) * size
        if start + size > ctis.SEARCH_LIMIT:
            return {"pagination": {"totalRecords": 0, "currentPage": page, "nextPage": False}, "data": []}
        rows = ordered[start:start + size]
        more = start + size < len(ordered)
        return {"pagination": {"totalRecords": len(ordered), "currentPage": page, "nextPage": more}, "data": rows}


class ShiftingCtis(FakeCtis):
    """A list that changes while it is read: after `searches_before` searches it is `then`."""

    def __init__(self, record, listing, searches_before, then):
        super().__init__(record, listing)
        self.searches_before = searches_before
        self.then = then

    def search(self, criteria, page, size, direction="ASC"):
        if len(self.searches) == self.searches_before:
            self.listing = self.then
        return super().search(criteria, page, size, direction)


def read_text(content):
    """The fake PDFs' "page text": their bytes, one page."""
    return [content.decode()]


def ctis_source(conn, **config):
    """The registered CTIS source, with parts of its config replaced."""
    source = core.registered_source(conn, "ctis")
    changed = copy.deepcopy(source.config)
    for name, value in config.items():
        changed[name] = value
    return dataclasses.replace(source, config=changed)


def transfer(conn, fake, keys=(CT,), source=None, extract=read_text, why="chosen", should_stop=None,
             recheck=False):
    """A transfer from the fake CTIS, as the registered source (or `source`) is configured."""
    if source is None:
        source = ctis_source(conn)
    return ingest.transfer(conn, source, ctis.CtisAdapter(fake), keys, why=why, should_stop=should_stop,
                           extract=extract, recheck=recheck)


def listing_rows(keys):
    rows = []
    for key in keys:
        rows.append(listed_row(key))
    return rows


def keys_of(page):
    keys = []
    for listed in page.rows:
        keys.append(listed.key)
    return keys


def link_of(conn, trial_id, uuid):
    """(removal, removed) of the trial's link to the document with this CTIS uuid."""
    return conn.execute(
        "SELECT l.removal, l.removed_at IS NOT NULL FROM core.trial_document l JOIN core.document d "
        "ON d.id = l.document_id WHERE l.trial_id = %s AND d.source_key = %s",
        (trial_id, uuid),
    ).fetchone()


def test_reads_identifiers_and_registered_information():
    found = []
    for identifier in ctis.identifiers(RECORD):
        found.append((identifier.scheme, identifier.value))
    assert found == [("eu-ct", CT), ("nct", "NCT09999911"), ("who-utn", "U9999-9999-9911")]
    registered = ctis.registered(RECORD)
    assert "sponsor" not in registered and registered["status"] == "Ended"
    pharma, university = ctis.sponsors(RECORD)
    assert pharma == core.Sponsor("Example Pharma", True, (Identifier("ema-org", "ORG-999000001"),),
                                  "Pharmaceutical company", "Denmark", True)
    # only EMA organisation ids (ORG-…) are identifiers; a location key is not
    assert (university.name, university.primary, university.identifiers) == ("Example  University", False, ())
    assert registered["primary_endpoints"] == [{"id": 7, "number": 1, "text": "Change in  PVR\\nat 12 weeks"}]


def test_reads_every_identifier_ctis_gives_and_skips_what_is_not_one(conn):
    found = ctis.identifiers(EVERY_IDENTIFIER)
    # the EudraCT number of a trial that moved over, the secondary numbers, and an EudraCT number among
    # other registries (text values only)
    assert found == [Identifier("eu-ct", "1999-000000-00-21"), Identifier("eudract", "2019-001234-56"),
                     Identifier("nct", "N/A"), Identifier("who-utn", "u1111-1234-5678"),
                     Identifier("isrctn", "ISRCTN12345678"), Identifier("eudract", "2019-001234-56")]
    core.act_as(conn, "ctis-adapter", "process")
    saved = core.save_trial_record(
        conn, source="ctis", source_key="1999-000000-00-21", identifiers=found, payload=EVERY_IDENTIFIER,
        registered=ctis.registered(EVERY_IDENTIFIER), content_hash=ctis.content_hash(EVERY_IDENTIFIER),
    )
    # "N/A" is not an NCT number: skipped, the rest stored (the EudraCT number given twice, once)
    assert saved.skipped_identifiers == [Identifier("nct", "N/A")]
    assert core.trial_identifiers(conn, saved.trial_id) == [
        Identifier("eu-ct", "1999-000000-00-21"), Identifier("eudract", "2019-001234-56"),
        Identifier("isrctn", "ISRCTN12345678"), Identifier("who-utn", "U1111-1234-5678")]


def test_a_number_that_is_not_text_is_not_an_identifier():
    record = copy.deepcopy(EVERY_IDENTIFIER)
    record["authorizedApplication"]["eudraCt"] = {"eudraCtCode": 2019001234}
    numbers = record["authorizedApplication"]["authorizedPartI"]["trialDetails"]["clinicalTrialIdentifiers"][
        "secondaryIdentifyingNumbers"]
    numbers["nctNumber"] = {"number": True}
    numbers["whoUniversalTrialNumber"] = "U1111-1234-5678"
    numbers["isrctnNumber"] = {"number": ""}
    numbers["additionalRegistries"] = {"otherRegistryName": "EudraCT", "number": "2019-001234-56"}
    assert ctis.identifiers(record) == [Identifier("eu-ct", "1999-000000-00-21")]


def test_ctis_document_types_are_our_kinds():
    assert ctis.document_kind({"documentType": "103"}) == "results-summary"
    assert ctis.document_kind({"documentType": 45}) == "study-report"
    assert ctis.document_kind({"documentType": "46"}) == "lay-summary"
    assert ctis.document_kind({"documentType": "999"}) == "other"
    assert ctis.document_kind({"documentType": "a report"}) == "other"
    assert ctis.document_kind({}) == "other"
    # what a transfer brings unless the source's config says otherwise: the kinds the check reads
    assert ctis.DEFAULT_EVIDENCE_KINDS == ("results-summary", "study-report")
    assert ctis.CtisAdapter().evidence_kinds({}) == ["results-summary", "study-report"]
    kinds = []
    for kind in ctis.DOCUMENT_KINDS.values():
        if kind not in kinds:
            kinds.append(kind)
    assert sorted(kinds) == list(ctis.CtisAdapter.document_kinds)


def test_the_seeded_source_transfers_the_results_documents(conn):
    config = core.registered_source(conn, "ctis").config
    assert config["evidence"] == {"document_kinds": ["results-summary", "study-report"]}
    assert config == ctis.CtisAdapter().default_config()


def test_ingests_the_trial_and_its_result_documents_once(conn):
    core.act_as(conn, "ctis-adapter", "process")
    fake = FakeCtis(RECORD)
    first = transfer(conn, fake)
    second = transfer(conn, fake)
    assert (first.trials, first.new_trials, first.new_versions, first.failed) == (1, 1, 2, {})
    assert first.statuses == {CT: "complete"} and first.pages == 2
    # the summary and the study report, not the lay summary or the protocol: downloaded once, the
    # second transfer finds them held at the version CTIS lists
    assert sorted(fake.downloads) == [SUMMARY["uuid"], REPORT["uuid"]]
    assert (second.new_trials, second.new_versions, second.unchanged, second.known) == (0, 0, 2, 2)
    assert "2 held at their version, not downloaded" in second.summary()
    records = conn.execute("SELECT count(*) FROM core.trial_record WHERE source_key = %s", (CT,)).fetchone()
    assert records == (1,)  # the same record again is not a new version
    links = conn.execute("SELECT count(*) FROM core.trial_document l JOIN core.trial_source s USING (trial_id) "
                         "WHERE s.source_key = %s AND l.basis = 'listed-by-source'", (CT,)).fetchone()
    assert links == (2,)
    kinds = conn.execute("SELECT d.kind FROM core.document d WHERE d.source_key = %s", (REPORT["uuid"],)).fetchone()
    assert kinds == ("study-report",)
    run = conn.execute("SELECT status, counts->>'new_versions' FROM core.ingest_run WHERE id = %s",
                       (first.run_id,)).fetchone()
    assert run == ("succeeded", "2")
    note = conn.execute("SELECT DISTINCT note FROM core.audit_log WHERE table_name = 'core.document'").fetchall()
    assert (f"ingest run {first.run_id}",) in note
    transferred = conn.execute("SELECT status, document_kinds, documents, pages FROM core.evidence_transfer "
                               "WHERE source_key = %s", (CT,)).fetchone()
    assert transferred == ("complete", ["results-summary", "study-report"], 2, 2)


def test_a_document_is_stored_with_the_day_its_results_were_submitted(conn):
    submitted = ctis.submission_dates(RECORD)
    # CTIS gives the submission time in UTC, without a zone; a date it can't read is left out
    assert submitted == {"5001": datetime(2025, 4, 10, 17, 39, 54, tzinfo=UTC),
                         "5002": datetime(2025, 5, 2, 8, 0, tzinfo=UTC)}
    assert ctis.published_at(SUMMARY, submitted) == datetime(2025, 4, 10, 17, 39, 54, tzinfo=UTC)
    assert ctis.published_at(LAY, submitted) == datetime(2025, 5, 2, 8, 0, tzinfo=UTC)
    assert ctis.published_at(REPORT, submitted) is None
    assert ctis.published_at({"associatedEntityId": 5003}, submitted) is None
    core.act_as(conn, "ctis-adapter", "process")
    transfer(conn, FakeCtis(RECORD))
    rows = conn.execute(
        "SELECT d.source_key, v.published_at FROM core.document_version v JOIN core.document d ON d.id = v.document_id "
        "ORDER BY d.source_key"
    ).fetchall()
    assert rows == [(SUMMARY["uuid"], datetime(2025, 4, 10, 17, 39, 54, tzinfo=UTC)), (REPORT["uuid"], None)]


def test_a_replaced_file_becomes_a_new_version_and_the_old_one_is_kept(conn):
    core.act_as(conn, "ctis-adapter", "process")
    record = copy.deepcopy(RECORD)
    transfer(conn, FakeCtis(record))
    record["documents"][0]["systemVersion"] = "2"
    result = transfer(conn, FakeCtis(record))
    assert result.new_versions == 1
    records = conn.execute("SELECT count(*) FROM core.trial_record WHERE source_key = %s", (CT,)).fetchone()
    assert records == (2,)
    versions = conn.execute(
        "SELECT v.version, v.replaced_at IS NULL FROM core.document_version v JOIN core.document d "
        "ON d.id = v.document_id WHERE d.source_key = %s ORDER BY v.version", (SUMMARY["uuid"],)).fetchall()
    assert versions == [("1", False), ("2", True)]


def test_a_failed_download_fails_the_transfer_and_the_run_goes_on(conn):
    core.act_as(conn, "ctis-adapter", "process")
    fake = FakeCtis(RECORD)
    fake.download_errors[REPORT["uuid"]] = RuntimeError("storage said no")
    result = transfer(conn, fake)
    assert list(result.failed) == [f"{CT}/document {REPORT['uuid']}"] and result.new_versions == 1
    assert result.statuses == {CT: "failed"}
    status = conn.execute("SELECT status FROM core.ingest_run WHERE id = %s", (result.run_id,)).fetchone()
    assert status == ("failed",)
    trial_id = core.trials_with_scheme(conn, "eu-ct", [CT])[CT]
    kinds = ["results-summary", "study-report"]
    assert readiness.not_ready(conn, [trial_id], kinds) == {trial_id: "ctis: its last transfer failed"}
    # the next transfer gets the file: complete, and the trial can be checked
    again = transfer(conn, FakeCtis(RECORD))
    assert again.statuses == {CT: "complete"} and readiness.not_ready(conn, [trial_id], kinds) == {}


def test_a_record_without_its_list_of_documents_fails_and_unlinks_nothing(conn):
    core.act_as(conn, "ctis-adapter", "process")
    first = transfer(conn, FakeCtis(RECORD))
    trial_id = first.trial_ids[0]
    without_list = copy.deepcopy(RECORD)
    del without_list["documents"]
    result = transfer(conn, FakeCtis(without_list))
    # it says nothing about the documents: not "none any more"
    assert result.statuses == {CT: "failed"} and result.unlinked == 0
    assert result.failed == {f"{CT}/documents": "the record has no list of documents"}
    assert link_of(conn, trial_id, SUMMARY["uuid"]) == (None, False)
    assert link_of(conn, trial_id, REPORT["uuid"]) == (None, False)


def test_a_document_no_longer_listed_is_unlinked_and_linked_again_when_it_is_listed(conn):
    core.act_as(conn, "ctis-adapter", "process")
    first = transfer(conn, FakeCtis(RECORD))
    trial_id = first.trial_ids[0]
    without_report = copy.deepcopy(RECORD)
    without_report["documents"] = [copy.deepcopy(SUMMARY), copy.deepcopy(LAY), copy.deepcopy(PROTOCOL)]
    second = transfer(conn, FakeCtis(without_report))
    assert (second.unlinked, second.statuses) == (1, {CT: "complete"})
    assert "1 no longer listed" in second.summary()
    assert link_of(conn, trial_id, REPORT["uuid"]) == ("source", True)
    # a check no longer reads it
    titles = []
    for page in core.trial_pages(conn, trial_id, ["results-summary", "study-report"]):
        titles.append(page.title)
    assert titles == ["Results_summary"]
    transferred = conn.execute("SELECT documents FROM core.evidence_transfer WHERE source_key = %s", (CT,)).fetchone()
    assert transferred == (1,)
    # listed again: linked again, without downloading it again
    third = transfer(conn, FakeCtis(RECORD))
    assert (third.unlinked, third.unchanged) == (0, 2)
    assert link_of(conn, trial_id, REPORT["uuid"]) == (None, False)


def test_a_document_a_person_unlinked_stays_unlinked(conn):
    actor = core.act_as(conn, "ctis-adapter", "process")
    first = transfer(conn, FakeCtis(RECORD))
    trial_id = first.trial_ids[0]
    conn.execute(
        "UPDATE core.trial_document l SET removed_at = clock_timestamp(), removed_by = %s, removal = 'person' "
        "FROM core.document d WHERE d.id = l.document_id AND l.trial_id = %s AND d.source_key = %s",
        (actor, trial_id, REPORT["uuid"]),
    )
    again = transfer(conn, FakeCtis(RECORD))
    assert again.unlinked == 0
    assert link_of(conn, trial_id, REPORT["uuid"]) == ("person", True)
    document = conn.execute("SELECT id FROM core.document WHERE source_key = %s", (REPORT["uuid"],)).fetchone()[0]
    assert core.link_document(conn, trial_id=trial_id, document_id=document, basis="listed-by-source",
                              source="ctis") is False


def test_page_text_is_extracted_once_and_fingerprinted(conn, monkeypatch):
    core.act_as(conn, "ctis-adapter", "process")
    result = transfer(conn, FakeCtis(RECORD), extract=unreadable)
    assert result.statuses == {CT: "failed"}  # files without page text: the evidence is not complete
    monkeypatch.setattr("trialwatch.trials.pages.extract_pages", two_pages)
    first = extract_missing(conn)
    again = extract_missing(conn)
    assert (first.files, first.pages, again.files) == (2, 4, 0)
    assert len(first.same_text) == 1  # the two fake PDFs give the same text: flagged as the same content
    assert text_fingerprint(["same text.", "page  two"]) == text_fingerprint(["Same  text.", "Page two"])
    assert text_fingerprint(["", "  "]) is None  # scanned: no fingerprint, so never "the same" as another


def test_the_worker_reads_a_trials_files_in_children_several_at_once(conn, monkeypatch):
    # extract_missing's own path in the worker: sizes from the database, read_in_children, each saved
    core.act_as(conn, "ctis-adapter", "process")
    transfer(conn, FakeCtis(RECORD), extract=unreadable)
    monkeypatch.setattr("trialwatch.trials.pages.READ_IN_CHILD", True)
    read = []

    def read_in_child(sha256, stop=None):  # in this process: a child can't see the test's transaction
        read.append(sha256)
        return ["page one", "page two"]

    monkeypatch.setattr("trialwatch.trials.pages.read_in_child", read_in_child)
    result = extract_missing(conn, readers=2)
    assert (result.files, result.pages, result.failed) == (2, 4, {})
    assert len(read) == 2


def unreadable(content):
    raise ValueError("not read in this test")


def two_pages(content):
    return ["Same  text.", "Page two"]


def test_sponsors_are_organisations_linked_to_the_trial_and_follow_the_source(conn):
    core.act_as(conn, "ctis-adapter", "process")
    fake = FakeCtis(copy.deepcopy(RECORD))
    transfer(conn, fake)
    trial_id = core.trials_with_scheme(conn, "eu-ct", [CT])[CT]
    sponsors = overview.trial_sponsors(conn, trial_id)
    shown = []
    for sponsor in sponsors:
        shown.append((sponsor.name, sponsor.is_primary, sponsor.identifiers))
    assert shown == [("Example Pharma", True, ["ema-org ORG-999000001"]), ("Example  University", False, [])]
    pharma = sponsors[0].organisation_id
    # the source drops the co-sponsor and renames the primary one: one organisation, renamed; the
    # co-sponsor kept as removed
    renamed = copy.deepcopy(RECORD["authorizedApplication"]["authorizedPartI"]["sponsors"][0])
    renamed["organisation"] = {"name": "Example Pharma GmbH", "businessKey": "ORG-999000001"}
    fake.record["authorizedApplication"]["authorizedPartI"]["sponsors"] = [renamed]
    transfer(conn, fake)
    after = []
    for sponsor in overview.trial_sponsors(conn, trial_id):
        after.append((sponsor.organisation_id, sponsor.name, sponsor.removal))
    assert after == [(pharma, "Example Pharma GmbH", None), (sponsors[1].organisation_id, "Example  University", "source")]
    # a person removes the primary sponsor: the next transfer does not bring it back
    core.remove_trial_sponsor(conn, trial_id, pharma)
    transfer(conn, fake)
    assert overview.trial_sponsors(conn, trial_id)[0].removal == "person"
    changes = conn.execute("SELECT count(*) FROM core.audit_log WHERE table_name = 'core.trial_sponsor'").fetchone()[0]
    assert changes >= 4


def test_text_postgres_cannot_store_is_cleaned():
    # a NUL, and an unpaired UTF-16 surrogate (as PDF and JSON decoders can produce), in values and keys
    assert storable("Results\x00 of\ud800 the trial") == "Results of� the trial"
    assert storable({"title\x00": ["a\x00b", {"c": "\udc80"}], "count": 3, "flag": None}) == {
        "title": ["ab", {"c": "�"}], "count": 3, "flag": None}
    # a pair of surrogates is one character, and is kept as it
    assert storable("😀") == "\U0001f600"


def test_a_record_with_text_postgres_cannot_store_is_stored_cleaned(conn):
    core.act_as(conn, "ctis-adapter", "process")
    record = copy.deepcopy(RECORD)
    identifiers = record["authorizedApplication"]["authorizedPartI"]["trialDetails"]["clinicalTrialIdentifiers"]
    identifiers["publicTitle"] = "A\x00 study of\ud800 things"
    result = transfer(conn, FakeCtis(record))
    assert result.statuses == {CT: "complete"}
    trial_id = result.trial_ids[0]
    assert core.trial_registered(conn, trial_id)["public_title"] == "A study of� things"
    stored = conn.execute("SELECT payload FROM core.trial_record WHERE source_key = %s", (CT,)).fetchone()[0]
    assert stored["authorizedApplication"]["authorizedPartI"]["trialDetails"]["clinicalTrialIdentifiers"][
        "publicTitle"] == "A study of� things"
    # a listed trial's row too, its title included
    sync = ingest.sync_list(conn, ctis_source(conn), ctis.CtisAdapter(FakeCtis(RECORD, [
        listed_row(CT, title="A listed\x00 study\ud800")])))
    assert (sync.listed, sync.failed) == (1, {})
    listing = conn.execute("SELECT title, summary->>'ctTitle' FROM core.source_listing WHERE source_key = %s",
                           (CT,)).fetchone()
    assert listing == ("A listed study�", "A listed study�")


def test_the_adapter_reads_a_record_without_the_database_or_the_network():
    # no client and no connection: reading a record is the adapter's whole part; trials/ingest.py saves it
    read = ctis.CtisAdapter().parse(RECORD, None)
    schemes = [identifier.scheme for identifier in read.identifiers]
    assert "eu-ct" in schemes and read.registered == ctis.registered(RECORD)
    by_key = {document.key: document for document in read.documents}
    summary = by_key[SUMMARY["uuid"]]
    assert (summary.kind, summary.version, summary.listed_as_pdf, summary.problem) == ("results-summary", "1", True, None)
    assert read.warnings == {} and read.content_hash == ctis.content_hash(RECORD)


def test_a_trial_repeated_on_the_next_page_when_the_list_shifts_is_given_once():
    keys = ["1999-000000-02-00", "1999-000000-02-01", "1999-000000-02-02", "1999-000000-02-03"]
    arriving = "1999-000000-01-99"
    shifted = listing_rows([arriving])
    for row in listing_rows(keys):
        shifted.append(row)
    # after the first page a trial arrives at the start of the list: the second page repeats one
    fake = ShiftingCtis(RECORD, listing_rows(keys), 1, shifted)
    pages = list(ctis.CtisAdapter(fake).list_pages({"listing": {"criteria": {}, "page_size": 2}}))
    given = []
    more = []
    for page in pages:
        given.append(keys_of(page))
        more.append(page.more)
    assert given == [keys[0:2], keys[2:3], keys[3:4]]
    assert more == [True, True, False]


def test_the_descending_pass_stops_only_at_a_trial_the_ascending_pass_read(conn, monkeypatch):
    # CTIS answers at most 6 rows per query here: 3 pages of 2 from each end
    monkeypatch.setattr(ctis, "SEARCH_LIMIT", 6)
    keys = []
    for number in range(10):
        keys.append(f"1999-000000-03-{number:02d}")
    arriving = listing_rows(keys)
    for row in listing_rows(["1999-000000-03-10"]):
        arriving.append(row)
    # after the first descending page a trial arrives at its end: the next page repeats one of that pass
    fake = ShiftingCtis(RECORD, listing_rows(keys), 4, arriving)
    pages = list(ctis.CtisAdapter(fake).list_pages({"listing": {"criteria": {}, "page_size": 2}}))
    given = []
    for page in pages:
        given.append(keys_of(page))
    assert given == [keys[0:2], keys[2:4], keys[4:6], [keys[9], keys[8]], [keys[7]], [keys[6]]]
    directions = []
    for search in fake.searches:
        directions.append((search[1], search[3]))
    assert directions == [(1, "ASC"), (2, "ASC"), (3, "ASC"), (1, "DESC"), (2, "DESC"), (3, "DESC")]
    # the stage counts them all against the total CTIS gave
    core.act_as(conn, "ctis-adapter", "process")
    fake = ShiftingCtis(RECORD, listing_rows(keys), 4, arriving)
    source = ctis_source(conn, listing={"criteria": {}, "page_size": 2})
    result = ingest.sync_list(conn, source, ctis.CtisAdapter(fake))
    assert (result.listed, result.total, result.complete, result.failed) == (10, 10, True, {})


def versions_of(conn, uuid):
    """(version label, current?) of each version of the document with this CTIS uuid, oldest first."""
    return conn.execute(
        "SELECT v.version, v.replaced_at IS NULL FROM core.document_version v JOIN core.document d "
        "ON d.id = v.document_id WHERE d.source_key = %s ORDER BY v.id",
        (uuid,),
    ).fetchall()


def test_a_file_replaced_without_a_new_system_version_is_a_new_version(conn):
    core.act_as(conn, "ctis-adapter", "process")
    fake = FakeCtis(RECORD)
    trial_id = transfer(conn, fake).trial_ids[0]
    fake.replaced[SUMMARY["uuid"]] = " corrected"
    # the label is the same: only a transfer that downloads every document again finds the new bytes
    unchecked = transfer(conn, fake)
    assert (unchecked.new_versions, unchecked.known) == (0, 2)
    again = transfer(conn, fake, recheck=True)
    # the label is the same, the bytes are not: kept apart, the newer one current
    assert (again.new_versions, again.unchanged) == (1, 1)
    assert versions_of(conn, SUMMARY["uuid"]) == [(SUMMARY["systemVersion"], False), (SUMMARY["systemVersion"], True)]
    pages = conn.execute(
        "SELECT p.text FROM core.document_current d JOIN core.page p ON p.file_sha256 = d.file_sha256 "
        "WHERE d.source_key = %s", (SUMMARY["uuid"],)
    ).fetchall()
    assert "corrected" in pages[0][0]
    # nothing new the next time
    third = transfer(conn, fake, recheck=True)
    assert (third.new_versions, third.unchanged) == (0, 2)
    assert trial_id == third.trial_ids[0]


def test_a_document_at_a_new_version_is_downloaded_and_a_retry_downloads_only_what_failed(conn):
    core.act_as(conn, "ctis-adapter", "process")
    fake = FakeCtis(RECORD)
    fake.download_errors[REPORT["uuid"]] = OSError("connection reset")
    first = transfer(conn, fake)
    assert first.statuses == {CT: "failed"}
    fake.download_errors.clear()
    fake.downloads.clear()
    retry = transfer(conn, fake)
    assert retry.statuses == {CT: "complete"}
    assert fake.downloads == [REPORT["uuid"]]  # the summary was held already
    bumped = copy.deepcopy(RECORD)
    for document in bumped["documents"]:
        if document["uuid"] == SUMMARY["uuid"]:
            document["systemVersion"] = "9.0"
    fake.record = bumped
    fake.downloads.clear()
    third = transfer(conn, fake)
    assert fake.downloads == [SUMMARY["uuid"]]
    assert versions_of(conn, SUMMARY["uuid"])[-1] == ("9.0", True)
    assert third.known == 1


def test_a_recheck_is_recorded_on_the_run(conn):
    core.act_as(conn, "ctis-adapter", "process")
    result = transfer(conn, FakeCtis(RECORD), recheck=True)
    args = conn.execute("SELECT args->>'recheck' FROM core.ingest_run WHERE id = %s", (result.run_id,)).fetchone()
    assert args == ("true",)
