"""Data sources in stages (ADR-005): the registry, syncing a source's list, transferring evidence,
and which trials a check may read. Against a fake CTIS."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from conftest import default_settings
from test_evidence import GOOD, PROMPT, SET, FakeClient
from test_sources_ctis import CT, RECORD, FakeCtis, ctis_source, listed_row, listing_rows, transfer

from trialwatch.checks import evidence as ev
from trialwatch.db import core, overview
from trialwatch.db.core import Identifier
from trialwatch.sources import ctis, manual, registry, stages
from trialwatch.sources.ctis_client import CtisClient
from trialwatch.web import app as web

OTHERS = ["1999-000000-00-12", "1999-000000-00-13"]
# what the evidence check reads
KINDS = SET.document_kinds


def sync_list(conn, fake, source=None, should_stop=None):
    """A list sync from the fake CTIS, two trials a page, with the criteria {"status": [8]}."""
    if source is None:
        source = ctis_source(conn, listing={"criteria": {"status": [8]}, "page_size": 2})
    return stages.sync_list(conn, source, ctis.CtisAdapter(fake), should_stop=should_stop)


def status_of(conn, key):
    row = conn.execute("SELECT status, delisted_at IS NOT NULL FROM core.evidence_status WHERE source_key = %s",
                       (key,)).fetchone()
    return row


def always():
    return True


def latest_run(conn, kind):
    return conn.execute("SELECT id, status, counts, errors FROM core.ingest_run WHERE kind = %s ORDER BY id DESC LIMIT 1",
                        (kind,)).fetchone()


def typed_in(conn, key, identifiers):
    """A trial record a person typed in (the manual source), with these identifiers."""
    return core.save_trial_record(conn, source="manual", source_key=key, identifiers=identifiers,
                                  payload={"typed": key}, registered={}, content_hash=bytes(32))


class WatchingCtis(FakeCtis):
    """Looks at the trial's evidence transfer in the database while its record is being fetched."""

    def __init__(self, record, conn):
        super().__init__(record)
        self.conn = conn
        self.seen = []

    def retrieve(self, ct_number):
        row = self.conn.execute("SELECT status, ingest_run_id, errors FROM core.evidence_transfer WHERE source_key = %s",
                                (ct_number,)).fetchone()
        self.seen.append(row)
        return super().retrieve(ct_number)


class InterruptedCtis(FakeCtis):
    """Ctrl-C while the second page of the list is read."""

    def search(self, criteria, page, size, direction="ASC"):
        if len(self.searches) == 1:
            raise KeyboardInterrupt
        return super().search(criteria, page, size, direction)


class OnePageOfCtis:
    """CTIS's search over HTTP (for httpx.MockTransport): keeps each request, answers one list page."""

    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"pagination": {"totalRecords": 1, "nextPage": False},
                                         "data": [listed_row(CT)]})


def test_the_registry_knows_ctis_and_manual(conn):
    codes = []
    for source in core.registered_sources(conn):
        codes.append((source.code, source.adapter))
    assert ("ctis", "ctis") in codes and ("manual", "manual") in codes
    adapter = ctis.CtisAdapter()
    assert adapter.config_problems(core.registered_source(conn, "ctis").config) == []
    problems = adapter.config_problems({"listing": {"page_size": 9000, "sort": "x"},
                                        "evidence": {"document_kinds": ["results-summary", "poems"]}})
    assert len(problems) == 3
    assert adapter.config_problems({"evidence": {"document_kinds": None}}) == []
    assert adapter.key_problem("NCT01234567") == "not an EU CT number: 'NCT01234567'"
    # the kinds each can give: CTIS's mapped types; a person may upload any kind
    assert registry.adapter_class("ctis").document_kinds == ("lay-summary", "other", "protocol", "results-summary",
                                                             "study-report")
    assert registry.adapter_class("manual").document_kinds is None
    with pytest.raises(ValueError, match="no adapter"):
        registry.adapter_class("clinicaltrials-gov")


def test_syncing_the_list_brings_every_listed_trial_in_without_its_evidence(conn):
    core.act_as(conn, "ctis-adapter", "process")
    fake = FakeCtis(RECORD, listing_rows([CT, OTHERS[0], OTHERS[1]]))
    result = sync_list(conn, fake)
    assert (result.listed, result.new_trials, result.total, result.complete) == (3, 3, 3, True)
    # page by page, with the config's criteria
    assert fake.searches == [({"status": [8]}, 1, 2, "ASC"), ({"status": [8]}, 2, 2, "ASC")]
    assert fake.downloads == []
    trial_id = core.trials_with_scheme(conn, "eu-ct", [CT])[CT]
    assert status_of(conn, CT) == ("not transferred", False)
    rows = overview.source_trials(conn, "ctis", search="listed study")
    assert (rows[0].source_key, rows[0].title) == (CT, "A listed study")
    assert core.keys_to_transfer(conn, "ctis", "pending") == [CT, OTHERS[0], OTHERS[1]]
    # not transferred yet: no check
    assert stages.not_ready(conn, [trial_id], KINDS) == {trial_id: "ctis: evidence not transferred"}
    with pytest.raises(ev.NotReady):
        ev.run(conn, FakeClient(GOOD), [trial_id], default_settings({"method": "files"}), PROMPT)
    run = conn.execute("SELECT kind, status, args->'config'->'listing'->'criteria' FROM core.ingest_run WHERE id = %s",
                       (result.run_id,)).fetchone()
    assert run == ("list", "succeeded", {"status": [8]})


def test_a_listed_trial_is_matched_to_the_trial_already_here(conn):
    core.act_as(conn, "ctis-adapter", "process")
    transfer(conn, FakeCtis(RECORD))
    trial_id = core.trials_with_scheme(conn, "eu-ct", [CT])[CT]
    result = sync_list(conn, FakeCtis(RECORD, [listed_row(CT)]))
    assert (result.listed, result.new_trials) == (1, 0)
    assert status_of(conn, CT) == ("complete", False)
    assert overview.trials_of_keys(conn, "ctis", [CT]) == [trial_id]


def test_a_trial_two_complete_lists_missed_is_marked_no_longer_listed(conn):
    core.act_as(conn, "ctis-adapter", "process")
    sync_list(conn, FakeCtis(RECORD, listing_rows([CT, OTHERS[0]])))
    # one complete list without it: a list that shifts while it is paged can hide a trial from one pass
    once = sync_list(conn, FakeCtis(RECORD, listing_rows([CT])))
    assert once.delisted == 0 and status_of(conn, OTHERS[0]) == ("not transferred", False)
    # the next complete list misses it too: no longer listed, and kept
    twice = sync_list(conn, FakeCtis(RECORD, listing_rows([CT])))
    assert twice.delisted == 1 and status_of(conn, OTHERS[0]) == ("not transferred", True)
    assert core.keys_to_transfer(conn, "ctis", "listed") == [CT]
    # listed again: no longer marked
    sync_list(conn, FakeCtis(RECORD, listing_rows([CT, OTHERS[0]])))
    assert status_of(conn, OTHERS[0]) == ("not transferred", False)


def test_a_stopped_list_keeps_what_it_read_and_counts_as_no_miss(conn):
    core.act_as(conn, "ctis-adapter", "process")
    every_trial = listing_rows([CT, OTHERS[0], OTHERS[1]])
    sync_list(conn, FakeCtis(RECORD, every_trial))
    # a stop after the first page (of two): the third trial is not seen, and not marked as gone
    result = sync_list(conn, FakeCtis(RECORD, every_trial), should_stop=always)
    assert (result.listed, result.stopped, result.complete, result.delisted) == (2, True, False, 0)
    assert status_of(conn, OTHERS[1]) == ("not transferred", False)
    # the stopped list was not a miss: one complete list without it is still only one
    without = listing_rows([CT, OTHERS[0]])
    assert sync_list(conn, FakeCtis(RECORD, without)).delisted == 0
    assert sync_list(conn, FakeCtis(RECORD, without)).delisted == 1


def numbers(count):
    found = []
    for number in range(count):
        found.append(f"1999-000000-01-{number:02d}")
    return found


def test_a_list_longer_than_one_query_is_read_from_both_ends(conn, monkeypatch):
    core.act_as(conn, "ctis-adapter", "process")
    # CTIS answers at most 10,000 rows per query; here 4, with 7 trials listed
    monkeypatch.setattr(ctis, "SEARCH_LIMIT", 4)
    fake = FakeCtis(RECORD, listing_rows(numbers(7)))
    result = sync_list(conn, fake)
    assert (result.listed, result.total, result.complete, result.failed) == (7, 7, True, {})
    directions = []
    for search in fake.searches:
        directions.append((search[1], search[3]))
    assert directions == [(1, "ASC"), (2, "ASC"), (1, "DESC"), (2, "DESC")]


def test_a_list_that_cannot_be_read_whole_marks_nothing_as_gone(conn, monkeypatch):
    core.act_as(conn, "ctis-adapter", "process")
    monkeypatch.setattr(ctis, "SEARCH_LIMIT", 4)
    keys = numbers(10)
    listing = listing_rows(keys)
    sync_list(conn, FakeCtis(RECORD, listing[:6]))
    sync_list(conn, FakeCtis(RECORD, listing))
    result = sync_list(conn, FakeCtis(RECORD, listing))
    # both ends give 8 of 10: kept, reported, and the two in the middle that two such passes missed
    # are not marked as no longer listed
    assert (result.listed, result.complete, result.delisted) == (8, False, 0)
    assert "read 8 of the 10" in result.failed["list"]
    assert status_of(conn, keys[4]) == ("not transferred", False)


def test_a_switched_off_source_is_not_read(conn):
    core.act_as(conn, "ctis-adapter", "process")
    core.set_source_config(conn, "ctis", core.registered_source(conn, "ctis").config, False)
    with pytest.raises(ValueError, match="switched off"):
        sync_list(conn, FakeCtis(RECORD), source=core.registered_source(conn, "ctis"))


def test_stale_evidence_is_not_checked_until_it_is_transferred_again(conn):
    core.act_as(conn, "ctis-adapter", "process")
    sync_list(conn, FakeCtis(RECORD, [listed_row(CT, updated="10/11/2026")]))
    first = transfer(conn, FakeCtis(RECORD), keys=core.keys_to_transfer(conn, "ctis", "pending"), why="pending")
    assert first.statuses == {CT: "complete"}
    trial_id = core.trials_with_scheme(conn, "eu-ct", [CT])[CT]
    assert stages.not_ready(conn, [trial_id], KINDS) == {}
    # CTIS says it changed the trial today, the day of our transfer: maybe after it
    today = datetime.now(UTC).strftime("%d/%m/%Y")
    sync_list(conn, FakeCtis(RECORD, [listed_row(CT, updated=today)]))
    assert status_of(conn, CT) == ("stale", False)
    assert core.keys_to_transfer(conn, "ctis", "stale") == [CT]
    # a check waits for the evidence as it is now
    assert stages.not_ready(conn, [trial_id], KINDS) == {trial_id: "ctis: changed since its transfer, transfer it again"}
    # CTIS gives only the day of a change, so a transfer on that same day still can't rule out a later one
    transfer(conn, FakeCtis(RECORD), keys=[CT])
    assert status_of(conn, CT) == ("stale", False)
    # a transfer on a later day can
    conn.execute("UPDATE core.evidence_transfer SET transferred_at = transferred_at + interval '1 day' "
                 "WHERE source_key = %s", (CT,))
    assert status_of(conn, CT) == ("complete", False)
    assert stages.not_ready(conn, [trial_id], KINDS) == {}


def test_a_check_waits_for_every_source_that_holds_the_trial(conn):
    core.act_as(conn, "test-person")
    trial_id = transfer(conn, FakeCtis(RECORD)).trial_ids[0]
    assert stages.not_ready(conn, [trial_id], KINDS) == {}
    # a person also typed it in (found by its NCT number), and nothing came from that source yet
    typed = typed_in(conn, "entry-test", [Identifier("nct", "NCT09999911")])
    assert typed.trial_id == trial_id
    assert stages.not_ready(conn, [trial_id], KINDS) == {trial_id: "manual: evidence not transferred"}
    core.save_transfer(conn, source="manual", source_key="entry-test", run_id=None, documents=1, pages=0,
                       errors={"pages": "1 files without page text"}, document_kinds=None)
    assert stages.not_ready(conn, [trial_id], KINDS) == {trial_id: "manual: its last transfer failed"}
    # every source's problem is named
    failing = FakeCtis(RECORD)
    failing.retrieve_error = RuntimeError("CTIS said 500")
    transfer(conn, failing)
    assert stages.not_ready(conn, [trial_id], KINDS) == {
        trial_id: "ctis: its last transfer failed; manual: its last transfer failed"}
    # complete at both (a person's documents are of any kind): ready
    transfer(conn, FakeCtis(RECORD))
    manual.record_transfer(conn, trial_id)
    assert stages.not_ready(conn, [trial_id], KINDS) == {}


def test_a_transfer_that_did_not_bring_a_kind_the_check_reads_holds_it_up(conn):
    core.act_as(conn, "ctis-adapter", "process")
    only_summaries = ctis_source(conn, evidence={"document_kinds": ["results-summary"]})
    trial_id = transfer(conn, FakeCtis(RECORD), source=only_summaries).trial_ids[0]
    assert status_of(conn, CT) == ("complete", False)
    # CTIS has study reports and the check reads them: it waits for a transfer that brings them
    assert stages.not_ready(conn, [trial_id], KINDS) == {trial_id: "ctis: its transfer did not bring study-report"}
    # CTIS has no publications: a check reading them does not wait for CTIS to bring them
    assert stages.not_ready(conn, [trial_id], ["results-summary", "publication"]) == {}
    every_kind = ctis_source(conn, evidence={"document_kinds": None})
    transfer(conn, FakeCtis(RECORD), source=every_kind)
    assert stages.not_ready(conn, [trial_id], KINDS) == {}


def test_the_evidence_of_a_trial_merged_into_another_counts_for_it(conn):
    core.act_as(conn, "test-person")
    kept = transfer(conn, FakeCtis(RECORD)).trial_ids[0]
    # a person confirmed this one is the same trial; its source has given nothing yet
    merged = typed_in(conn, "entry-merged", [Identifier("nct", "NCT09999988")]).trial_id
    conn.execute("UPDATE core.trial SET merged_into = %s WHERE id = %s", (kept, merged))
    assert stages.not_ready(conn, [kept], KINDS) == {kept: "manual: evidence not transferred"}
    assert stages.not_ready(conn, [987654321], KINDS) == {987654321: "no source has given its evidence"}


def test_ready_trials_have_page_text_and_nothing_holding_them_up(conn):
    core.act_as(conn, "ctis-adapter", "process")
    trial_id = transfer(conn, FakeCtis(RECORD)).trial_ids[0]
    assert stages.ready_trials(conn, KINDS) == [trial_id]
    assert stages.ready_trials(conn, ["protocol"]) == []
    # the list now names another trial (no evidence yet) and says this one changed: neither is ready
    sync_list(conn, FakeCtis(RECORD, [listed_row(CT, updated="31/12/2099"), listed_row(OTHERS[0])]))
    assert stages.ready_trials(conn, KINDS) == []


def test_the_config_chooses_the_documents_a_transfer_brings(conn):
    core.act_as(conn, "ctis-adapter", "process")
    fake = FakeCtis(RECORD)
    result = transfer(conn, fake, source=ctis_source(conn, evidence={"document_kinds": None}))
    assert len(fake.downloads) == 4 and result.statuses == {CT: "complete"}
    run = conn.execute("SELECT args->'config'->'evidence' FROM core.ingest_run WHERE id = %s",
                       (result.run_id,)).fetchone()
    assert run == ({"document_kinds": None},)
    # every kind came: the transfer says so
    kinds = conn.execute("SELECT document_kinds, documents FROM core.evidence_transfer WHERE source_key = %s",
                         (CT,)).fetchone()
    assert kinds == (None, 4)


def test_a_record_that_cannot_be_fetched_fails_a_listed_trial(conn):
    core.act_as(conn, "ctis-adapter", "process")
    sync_list(conn, FakeCtis(RECORD, [listed_row(OTHERS[0])]))
    fake = FakeCtis(RECORD)
    fake.retrieve_error = RuntimeError("CTIS said 500")
    result = transfer(conn, fake, keys=[OTHERS[0], OTHERS[1]], should_stop=None)
    assert result.statuses == {OTHERS[0]: "failed", OTHERS[1]: "failed"}
    assert status_of(conn, OTHERS[0]) == ("failed", False)
    # never listed nor fetched: no trial to record it on
    assert status_of(conn, OTHERS[1]) is None
    assert core.keys_to_transfer(conn, "ctis", "failed") == [OTHERS[0]]


def test_while_a_trial_is_transferred_its_evidence_says_failed(conn):
    core.act_as(conn, "ctis-adapter", "process")
    transfer(conn, FakeCtis(RECORD))
    watching = WatchingCtis(RECORD, conn)
    again = transfer(conn, watching)
    # so a transfer cut short never leaves half-new evidence looking complete
    assert watching.seen == [("failed", again.run_id,
                              {"transfer": f"ingest run {again.run_id} started it and did not finish"})]
    assert status_of(conn, CT) == ("complete", False)


def test_an_interrupted_transfer_leaves_the_trial_failed_and_its_run_failed(conn, other_session):
    core.act_as(conn, "ctis-adapter", "process")
    trial_id = transfer(conn, FakeCtis(RECORD)).trial_ids[0]
    interrupted = FakeCtis(RECORD)
    interrupted.retrieve_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        transfer(conn, interrupted, keys=[CT, OTHERS[0]])
    assert status_of(conn, CT) == ("failed", False)
    assert stages.not_ready(conn, [trial_id], KINDS) == {trial_id: "ctis: its last transfer failed"}
    run_id, status, counts, errors = latest_run(conn, "transfer")
    assert (status, counts["trials"], errors) == ("failed", 1, {"run": "interrupted: KeyboardInterrupt: "})
    # the run and the source are let go of
    source = core.registered_source(conn, "ctis")
    assert core.try_lock(other_session, core.LOCK_SOURCE, source.id)
    assert core.try_lock(other_session, core.LOCK_INGEST_RUN, run_id)
    core.unlock(other_session, core.LOCK_SOURCE, source.id)
    core.unlock(other_session, core.LOCK_INGEST_RUN, run_id)


def test_an_interrupted_list_keeps_what_it_read_and_closes_its_run_as_failed(conn):
    core.act_as(conn, "ctis-adapter", "process")
    with pytest.raises(KeyboardInterrupt):
        sync_list(conn, InterruptedCtis(RECORD, listing_rows([CT, OTHERS[0], OTHERS[1]])))
    run_id, status, counts, errors = latest_run(conn, "list")
    assert (status, counts["listed"], counts["stopped"]) == ("failed", 2, True)
    assert errors == {"list": "interrupted: KeyboardInterrupt"}
    assert core.keys_to_transfer(conn, "ctis", "listed") == [CT, OTHERS[0]]


def test_a_source_another_process_is_reading_is_busy(conn, other_session):
    core.act_as(conn, "ctis-adapter", "process")
    source = ctis_source(conn, listing={"criteria": {}, "page_size": 2})
    runs_before = conn.execute("SELECT count(*) FROM core.ingest_run").fetchone()
    assert core.try_lock(other_session, core.LOCK_SOURCE, source.id)
    try:
        with pytest.raises(stages.SourceBusy, match="another process is reading source ctis"):
            sync_list(conn, FakeCtis(RECORD, [listed_row(CT)]), source=source)
        with pytest.raises(stages.SourceBusy):
            transfer(conn, FakeCtis(RECORD))
    finally:
        core.unlock(other_session, core.LOCK_SOURCE, source.id)
    # no run was started
    assert conn.execute("SELECT count(*) FROM core.ingest_run").fetchone() == runs_before
    # free again
    assert sync_list(conn, FakeCtis(RECORD, [listed_row(CT)]), source=source).listed == 1


def test_a_run_whose_process_died_is_closed_before_the_next_one_starts(conn):
    core.act_as(conn, "ctis-adapter", "process")
    dead = core.start_ingest(conn, "ctis", {"test": True}, kind="list")
    # its process died: nobody holds the run's lock any more
    core.unlock(conn, core.LOCK_INGEST_RUN, dead)
    result = sync_list(conn, FakeCtis(RECORD, [listed_row(CT)]))
    rows = conn.execute("SELECT id, status, errors FROM core.ingest_run WHERE id = ANY(%s) ORDER BY id",
                        ([dead, result.run_id],)).fetchall()
    assert rows == [(dead, "failed", {"run": core.ABANDONED}), (result.run_id, "succeeded", {})]


def test_a_run_says_in_one_line_what_it_did():
    listed = stages.ListResult(run_id=3, listed=1200, new_trials=12, delisted=2, complete=True, possible_duplicates=1)
    assert listed.summary() == ("list run 3 (complete): 1,200 listed, 12 new trials, 2 no longer listed, "
                                "possible duplicates 1")
    assert stages.ListResult(run_id=4, stopped=True).summary().startswith("list run 4 (stopped part-way):")
    assert stages.ListResult(run_id=5).summary().startswith("list run 5 (not complete):")
    transferred = stages.TransferResult(run_id=6, trials=3, complete=2, new_trials=1, documents=4, new_versions=2,
                                        unchanged=1, unlinked=1, pages=9)
    assert transferred.summary() == ("transfer run 6: 2 of 3 complete, 1 new trials, documents 4 (2 new versions, "
                                     "1 unchanged, 1 no longer listed), 9 pages read")


def test_a_check_runs_on_a_transferred_trial(conn):
    core.act_as(conn, "ctis-adapter", "process")
    transfer(conn, FakeCtis(RECORD))
    trial_id = core.trials_with_scheme(conn, "eu-ct", [CT])[CT]
    stopped = ev.run(conn, FakeClient(GOOD), [trial_id], default_settings({"method": "files"}), PROMPT, should_stop=always)
    status = conn.execute("SELECT status FROM checks.run WHERE id = %s", (stopped.run_id,)).fetchone()
    assert status == ("stopped",)


def test_the_client_searches_ctis_in_ct_number_order():
    handler = OnePageOfCtis()
    with CtisClient(min_interval=0, backoff=0, transport=httpx.MockTransport(handler)) as client:
        answer = client.search({"status": [8]}, 2, 50)
    assert answer["data"][0]["ctNumber"] == CT
    sent = handler.requests[0]
    body = json.loads(sent.content)
    assert sent.method == "POST" and sent.url.path == "/ctis-public-api/search"
    assert body["pagination"] == {"page": 2, "size": 50}
    assert body["sort"] == {"property": "ctNumber", "direction": "ASC"}
    assert body["searchCriteria"] == {"containAll": "", "containAny": "", "containNot": "", "status": [8]}
    listed = ctis.listed(listed_row(CT, updated="31/12/2025"))
    assert (listed.key, listed.title, listed.updated_on.isoformat()) == (CT, "A listed study", "2025-12-31")


def test_the_source_pages_render(database_url):
    client = TestClient(web.app)
    page = client.get("/sources")
    assert page.status_code == 200 and "Sync" in page.text and "/sources/ctis" in page.text
    source = client.get("/sources/ctis", params={"state": "failed"})
    assert source.status_code == 200 and "Transfer ticked" in source.text and "listing" in source.text
    assert client.get("/sources/nope").status_code == 404
    trials = client.get("/trials", params={"evidence_state": "complete"})
    assert trials.status_code == 200
