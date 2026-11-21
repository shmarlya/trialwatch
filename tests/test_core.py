"""The central model (ADR-003): audit trail, trial identity and duplicates, records, document versions,
sponsors, and the advisory locks that keep processes apart."""

import hashlib
import time

import psycopg
import pytest

from trialwatch.db import SESSION_OPTIONS, core, overview
from trialwatch.db.core import Identifier

# Valid format but not real trials
EU = "1999-000000-00-01"
EU_2 = "1999-000000-00-02"
NCT = "NCT09999901"


def digest(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def save(conn, source_key, identifiers, content="v1", source="ctis", registered=None):
    if registered is None:
        registered = {}
    return core.save_trial_record(conn, source=source, source_key=source_key, identifiers=identifiers,
                                  payload={"content": content}, registered=registered, content_hash=digest(content))


def sponsors_of(conn, trial_id):
    """(name, primary, source, removal) of each of the trial's sponsors, current ones first."""
    found = []
    for sponsor in overview.trial_sponsors(conn, trial_id):
        found.append((sponsor.name, sponsor.is_primary, sponsor.source, sponsor.removal))
    return found


def test_writes_without_an_actor_are_refused(conn):
    with pytest.raises(psycopg.errors.RaiseException, match="who is writing"):
        conn.execute("INSERT INTO core.trial (created_by, first_source) VALUES (1, 1)")


def test_every_change_is_logged_with_who_did_it_and_the_log_never_changes(conn):
    core.act_as(conn, "test-person")
    saved = save(conn, EU, [Identifier("eu-ct", EU)])
    logged = []
    for entry in core.audit_entries(conn, "core.trial", {"id": saved.trial_id}):
        logged.append((entry.action, entry.actor, entry.actor_kind))
    assert logged == [("insert", "test-person", "person")]
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with conn.transaction():
            conn.execute("DELETE FROM core.audit_log")
    # TRUNCATE fires no row trigger: it is refused by a trigger of its own
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with conn.transaction():
            conn.execute("TRUNCATE core.audit_log")


def test_a_source_record_is_versioned_when_its_content_or_our_reading_of_it_changes(conn):
    core.act_as(conn, "ctis-adapter", "process")
    identifiers = [Identifier("eu-ct", EU)]
    first = save(conn, EU, identifiers)
    again = save(conn, EU, identifiers)
    changed = save(conn, EU, identifiers, content="v2")
    back = save(conn, EU, identifiers, content="v1")
    assert (first.new_trial, first.new_version) == (True, True)
    assert (again.new_trial, again.new_version, again.trial_id) == (False, False, first.trial_id)
    assert changed.new_version and back.new_version
    # the same content, read differently by the adapter (`registered`): its new reading is stored
    reread = save(conn, EU, identifiers, registered={"status": "Ended"})
    same = save(conn, EU, identifiers, registered={"status": "Ended"})
    assert reread.new_version and not same.new_version
    count = conn.execute("SELECT count(*) FROM core.trial_record WHERE source_key = %s", (EU,)).fetchone()
    assert count == (4,)
    assert core.trial_registered(conn, first.trial_id) == {"status": "Ended"}


def test_a_trials_current_record_is_the_latest_from_each_of_its_sources(conn):
    core.act_as(conn, "test-person")
    first = save(conn, EU, [Identifier("eu-ct", EU), Identifier("nct", NCT)])
    save(conn, EU, [Identifier("eu-ct", EU)], content="v2")
    save(conn, "entry-1", [Identifier("nct", NCT)], content="typed", source="manual")
    rows = conn.execute("SELECT source_key, payload FROM core.trial_record_current WHERE trial_id = %s "
                        "ORDER BY source_key", (first.trial_id,)).fetchall()
    assert rows == [(EU, {"content": "v2"}), ("entry-1", {"content": "typed"})]
    # what a check reads: the most recently fetched of them
    assert core.trial_current_record(conn, first.trial_id).source == "manual"


def test_the_same_trial_from_another_source_is_found_by_identifier(conn):
    core.act_as(conn, "test-person")
    from_ctis = save(conn, EU, [Identifier("eu-ct", EU), Identifier("nct", NCT)])
    by_hand = save(conn, "TW-1", [Identifier("nct", " nct09999901 ")], source="manual")
    assert by_hand.trial_id == from_ctis.trial_id and not by_hand.new_trial


def test_a_new_key_at_the_same_source_is_another_trial_even_with_a_shared_identifier(conn):
    core.act_as(conn, "ctis-adapter", "process")
    parent = save(conn, EU, [Identifier("eu-ct", EU), Identifier("nct", NCT)])
    # e.g. an extension study naming its parent's NCT number: CTIS lists one trial once, so this is another
    extension = save(conn, EU_2, [Identifier("eu-ct", EU_2), Identifier("nct", NCT)])
    assert extension.new_trial and extension.trial_id != parent.trial_id
    assert extension.possible_duplicates == [parent.trial_id]
    # the NCT number stays on the trial that had it; a person decides about the pair
    assert core.trial_identifiers(conn, extension.trial_id) == [Identifier("eu-ct", EU_2)]
    assert core.trial_identifiers(conn, parent.trial_id) == [Identifier("eu-ct", EU), Identifier("nct", NCT)]
    pair = conn.execute("SELECT trial_a, trial_b, status, evidence->'identifiers' FROM core.possible_duplicate"
                        ).fetchall()
    assert pair == [(parent.trial_id, extension.trial_id, "open", {f"nct:{NCT}": parent.trial_id})]
    again = save(conn, EU_2, [Identifier("eu-ct", EU_2), Identifier("nct", NCT)], content="v2")
    assert (again.trial_id, again.new_trial) == (extension.trial_id, False)


def test_identifiers_of_different_trials_make_a_possible_duplicate_not_a_merge(conn):
    core.act_as(conn, "test-person")
    first = save(conn, EU, [Identifier("eu-ct", EU)])
    second = save(conn, EU_2, [Identifier("eu-ct", EU_2), Identifier("nct", NCT)])
    both = save(conn, "TW-2", [Identifier("eu-ct", EU), Identifier("nct", NCT)], source="manual")
    assert both.trial_id == first.trial_id and both.possible_duplicates == [second.trial_id]
    status = conn.execute("SELECT status FROM core.possible_duplicate WHERE trial_a = %s AND trial_b = %s",
                          (first.trial_id, second.trial_id)).fetchone()
    assert status == ("open",)
    assert core.trial_identifiers(conn, second.trial_id) == [Identifier("eu-ct", EU_2), Identifier("nct", NCT)]


def test_identifiers_that_do_not_fit_their_scheme_are_skipped_and_reported(conn):
    core.act_as(conn, "test-person")
    given = [Identifier("eu-ct", EU), Identifier("nct", "N/A"), Identifier("nct", "12345"), Identifier("nct", NCT),
             Identifier("nct", NCT)]
    saved = save(conn, EU, given)
    assert saved.skipped_identifiers == [Identifier("nct", "N/A"), Identifier("nct", "12345")]
    # the valid ones are stored, an identifier given twice once
    assert core.trial_identifiers(conn, saved.trial_id) == [Identifier("eu-ct", EU), Identifier("nct", NCT)]
    valid, invalid = core.valid_identifiers(conn, [Identifier("who-utn", "U1111-1234-5678"),
                                                   Identifier("isrctn", "ISRCTN1")])
    assert (valid, invalid) == ([Identifier("who-utn", "U1111-1234-5678")], [Identifier("isrctn", "ISRCTN1")])
    # the database refuses them too
    with pytest.raises(psycopg.errors.RaiseException, match="not a valid nct"):
        with conn.transaction():
            core.add_identifier(conn, saved.trial_id, Identifier("nct", "12345"), "manual")


def test_a_file_is_stored_once_and_a_document_has_one_current_version(conn):
    actor = core.act_as(conn, "test-person")
    files = []
    for text in ("first", "second"):
        sha = digest(text)
        conn.execute("INSERT INTO core.file (sha256, size_bytes, content, created_by) VALUES (%s, %s, %s, %s)",
                     (sha, len(text), text.encode(), actor))
        files.append(sha)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            conn.execute("INSERT INTO core.file (sha256, size_bytes, content, created_by) VALUES (%s, 5, 'first', %s)",
                         (files[0], actor))
    row = conn.execute("INSERT INTO core.document (source_id, source_key, kind, created_by) "
                       "VALUES (1, 'uuid-1', 'results-summary', %s) RETURNING id", (actor,)).fetchone()
    document = row[0]
    conn.execute("INSERT INTO core.document_version (document_id, version, file_sha256, created_by) "
                 "VALUES (%s, '1', %s, %s)", (document, files[0], actor))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            conn.execute("INSERT INTO core.document_version (document_id, version, file_sha256, created_by) "
                         "VALUES (%s, '2', %s, %s)", (document, files[1], actor))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("UPDATE core.file SET content = 'changed' WHERE sha256 = %s", (files[0],))


def test_advisory_locks_keep_two_sessions_apart(conn, other_session):
    assert core.try_lock(conn, core.LOCK_SOURCE, 99)
    assert not core.try_lock(other_session, core.LOCK_SOURCE, 99)
    # the same number under another kind of lock is another lock
    assert core.try_lock(other_session, core.LOCK_INGEST_RUN, 99)
    core.unlock(other_session, core.LOCK_INGEST_RUN, 99)
    core.unlock(conn, core.LOCK_SOURCE, 99)
    assert core.try_lock(other_session, core.LOCK_SOURCE, 99)
    core.unlock(other_session, core.LOCK_SOURCE, 99)


def test_an_ingest_run_holds_its_lock_until_it_finishes(conn, other_session):
    core.act_as(conn, "ctis-adapter", "process")
    run_id = core.start_ingest(conn, "ctis", {"keys": []})
    assert not core.try_lock(other_session, core.LOCK_INGEST_RUN, run_id)
    # its writes are labelled with it in the audit log until then
    note = conn.execute("SELECT current_setting('trialwatch.note')").fetchone()
    assert note == (f"ingest run {run_id}",)
    core.finish_ingest(conn, run_id, counts={"trials": 0}, errors={})
    assert core.try_lock(other_session, core.LOCK_INGEST_RUN, run_id)
    core.unlock(other_session, core.LOCK_INGEST_RUN, run_id)
    row = conn.execute("SELECT status, finished_at IS NOT NULL FROM core.ingest_run WHERE id = %s",
                       (run_id,)).fetchone()
    assert row == ("succeeded", True)


def new_session(database_url, actor):
    """A session of its own, as another process opens one (its writes are committed)."""
    session = psycopg.connect(database_url, autocommit=True, options=SESSION_OPTIONS)
    core.act_as(session, actor, "process")
    return session


def close_abandoned_once_gone(session, run_id):
    """close_abandoned_ingest_runs, again until the closed session's server process has let go of the
    run's lock (it ends just after the client closes)."""
    closed = []
    for attempt in range(100):
        for closed_run in core.close_abandoned_ingest_runs(session):
            closed.append(closed_run)
        if run_id in closed:
            return closed
        time.sleep(0.05)
    return closed


def test_ingest_runs_whose_session_ended_are_closed_and_live_ones_are_not(database_url, other_session):
    # these runs are committed: other sessions only see committed rows
    dead = new_session(database_url, "abandoned-run-test")
    abandoned = core.start_ingest(dead, "ctis", {"test": "its process dies"})
    dead.close()
    core.act_as(other_session, "live-run-test", "process")
    alive = core.start_ingest(other_session, "ctis", {"test": "its process is alive"})
    closer = new_session(database_url, "closing-run-test")
    try:
        closed = close_abandoned_once_gone(closer, abandoned)
        rows = closer.execute("SELECT id, status, errors, finished_at IS NOT NULL FROM core.ingest_run "
                              "WHERE id = ANY(%s) ORDER BY id", ([abandoned, alive],)).fetchall()
    finally:
        core.finish_ingest(other_session, alive, counts={}, errors={})
        closer.close()
    assert abandoned in closed and alive not in closed
    assert rows == [(abandoned, "failed", {"run": core.ABANDONED}, True), (alive, "running", {}, False)]


def test_a_typed_name_finds_the_organisation_however_it_is_spaced_or_cased(conn):
    core.act_as(conn, "test-person")
    # names are compared by the database's own expression, which a stored name was keyed with
    first = core.save_organisation(conn, core.Sponsor("Example Pharma  A/S", True), "manual")
    again = core.save_organisation(conn, core.Sponsor(" example pharma a/s ", False), "manual")
    tabbed = core.save_organisation(conn, core.Sponsor("EXAMPLE PHARMA\tA/S", False), "manual")
    assert again == first and tabbed == first
    # one with another id under the same scheme is a different organisation that shares the name
    acme = core.save_organisation(conn, core.Sponsor("Acme", True, (Identifier("ema-org", "ORG-100001"),)), "ctis")
    other_acme = core.save_organisation(conn, core.Sponsor("ACME", True, (Identifier("ema-org", "ORG-100002"),)),
                                        "ctis")
    assert other_acme != acme


def test_a_source_leaves_other_sponsor_links_alone_and_a_primary_set_elsewhere(conn):
    core.act_as(conn, "test-person")
    trial_id = save(conn, EU, [Identifier("eu-ct", EU)]).trial_id
    pharma = core.Sponsor("Example Pharma", True, (Identifier("ema-org", "ORG-999000001"),))
    university = core.Sponsor("Example University", False)
    core.set_trial_sponsors(conn, trial_id, "ctis", [pharma, university])
    # a person names another primary sponsor
    core.add_trial_sponsor(conn, trial_id, core.Sponsor("Example Charity", True), "manual")
    changes = core.set_trial_sponsors(conn, trial_id, "ctis", [pharma])
    # the source still calls Example Pharma primary: the person's choice stays the only primary one,
    # and the person's link is not the source's to remove
    assert sponsors_of(conn, trial_id) == [("Example Charity", True, "manual", None),
                                           ("Example Pharma", False, "ctis", None),
                                           ("Example University", False, "ctis", "source")]
    assert (changes.added, changes.removed, changes.changed) == (0, 1, 0)


def test_a_sponsor_a_person_adds_becomes_theirs_and_the_source_leaves_it(conn):
    core.act_as(conn, "test-person")
    trial_id = save(conn, EU, [Identifier("eu-ct", EU)]).trial_id
    pharma = core.Sponsor("Example Pharma", False, (Identifier("ema-org", "ORG-999000001"),))
    core.set_trial_sponsors(conn, trial_id, "ctis", [pharma])
    listed_by_ctis = sponsors_of(conn, trial_id)
    # the person types the same organisation's name as the primary sponsor
    core.add_trial_sponsor(conn, trial_id, core.Sponsor("example pharma", True), "manual")
    assert sponsors_of(conn, trial_id) == [("Example Pharma", True, "manual", None)]
    # the source no longer lists it: the person's link stays
    core.set_trial_sponsors(conn, trial_id, "ctis", [])
    assert listed_by_ctis == [("Example Pharma", False, "ctis", None)]
    assert sponsors_of(conn, trial_id) == [("Example Pharma", True, "manual", None)]
