"""Queries on the core schema: the central, source-agnostic model of ADR-003 (see
db/migrations/*_init.sql).

Who writes: every connection names an actor (act_as, called by trialwatch.db.connect), and Postgres
triggers write every change to core.audit_log with that actor.

Trial identity: save_trial_record and save_listed match all identifiers a source gives for a trial,
one writer at a time (an advisory lock). Identifiers that point at different trials are never
merged automatically: the record goes to the oldest of them and the pair is recorded in
core.possible_duplicate for a person to decide.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb


# --- actors and sources --------------------------------------------------------------------------

def actor_id(conn: psycopg.Connection, name: str, kind: str = "person") -> int:
    """The id of the actor `name` (created if new), without changing who this session writes as."""
    # Creating an actor is not audited (there is nobody to record yet), so no actor is needed for it
    conn.execute(
        "INSERT INTO core.actor (kind, name) VALUES (%s, %s) ON CONFLICT (kind, name) DO NOTHING", (kind, name)
    )
    row = conn.execute("SELECT id FROM core.actor WHERE kind = %s AND name = %s", (kind, name)).fetchone()
    return row[0]


def act_as(conn: psycopg.Connection, name: str, kind: str = "person") -> int:
    """Make this session's writes count as `name`'s (created if new); returns the actor id."""
    actor_id_found = actor_id(conn, name, kind)
    # Session-wide (not just this transaction): the connections run in autocommit
    conn.execute("SELECT set_config('trialwatch.actor', %s, false)", (str(actor_id_found),))
    return actor_id_found


def current_actor(conn: psycopg.Connection) -> int | None:
    row = conn.execute("SELECT nullif(current_setting('trialwatch.actor', true), '')::bigint").fetchone()
    return row[0]


def source_id(conn: psycopg.Connection, code: str) -> int:
    row = conn.execute("SELECT id FROM core.source WHERE code = %s", (code,)).fetchone()
    if row is None:
        raise ValueError(f"no source {code!r} in core.source")
    return row[0]


# --- advisory locks ------------------------------------------------------------------------------
# Postgres advisory locks keep two processes (the CLI and the web interface, or two terminals) off the
# same work, and tell a live run from one whose process died: a session's locks end with it. A key is
# one bigint: what is locked in the top bits, which one below.

LOCK_INGEST_RUN = 1
LOCK_CHECK_RUN = 2
LOCK_SOURCE = 3
LOCK_TRIAL_IDENTITY = 4


def lock_key(kind: int, number: int) -> int:
    return (kind << 48) | number


def try_lock(conn: psycopg.Connection, kind: int, number: int) -> bool:
    """Take a session lock unless another session holds it; True if this session holds it now."""
    row = conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_key(kind, number),)).fetchone()
    return row[0]


def lock(conn: psycopg.Connection, kind: int, number: int) -> None:
    """Take a session lock, waiting for another session to let go of it."""
    conn.execute("SELECT pg_advisory_lock(%s)", (lock_key(kind, number),))


def unlock(conn: psycopg.Connection, kind: int, number: int) -> None:
    conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key(kind, number),))


# --- trials ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Identifier:
    scheme: str  # a core.identifier_scheme code, e.g. eu-ct, nct
    value: str


def normalised(identifier: Identifier) -> Identifier:
    """Upper case without spaces: how identifiers are stored, so the same one always matches."""
    value = identifier.value.strip().upper()
    value = value.replace(" ", "")
    return Identifier(identifier.scheme, value)


@dataclass
class SavedRecord:
    trial_id: int
    new_trial: bool
    # a new trial_record row: the source's content was new or had changed
    new_version: bool
    # other trials the identifiers also pointed at, recorded as possible duplicates
    possible_duplicates: list[int] = field(default_factory=list)
    # identifiers the source gave that are not valid identifiers of their scheme, so not stored
    skipped_identifiers: list[Identifier] = field(default_factory=list)


def resolved_trial(conn: psycopg.Connection, trial_id: int) -> int:
    """The trial itself, or the trial it was merged into (following the chain)."""
    current = trial_id
    while True:
        row = conn.execute("SELECT merged_into FROM core.trial WHERE id = %s", (current,)).fetchone()
        if row is None or row[0] is None:
            return current
        current = row[0]


def trials_with_identifiers(conn: psycopg.Connection, identifiers: Sequence[Identifier]) -> dict[Identifier, int]:
    """Identifier -> trial it belongs to (after merges), for the identifiers already known."""
    found = {}
    for identifier in identifiers:
        row = conn.execute(
            "SELECT trial_id FROM core.trial_identifier WHERE scheme = %s AND value = %s",
            (identifier.scheme, identifier.value),
        ).fetchone()
        if row is not None:
            found[identifier] = resolved_trial(conn, row[0])
    return found


def _trial_of_source_key(conn: psycopg.Connection, source: int, source_key: str) -> int | None:
    row = conn.execute(
        "SELECT trial_id FROM core.trial_source WHERE source_id = %s AND source_key = %s", (source, source_key)
    ).fetchone()
    if row is None:
        return None
    return resolved_trial(conn, row[0])


def _record_possible_duplicate(conn: psycopg.Connection, first: int, second: int, evidence: dict) -> None:
    trial_a = min(first, second)
    trial_b = max(first, second)
    conn.execute(
        "INSERT INTO core.possible_duplicate (trial_a, trial_b, evidence, created_by) "
        "VALUES (%s, %s, %s, current_setting('trialwatch.actor')::bigint) ON CONFLICT (trial_a, trial_b) DO NOTHING",
        (trial_a, trial_b, Jsonb(evidence)),
    )


def valid_identifiers(conn: psycopg.Connection, identifiers: Sequence[Identifier]) -> tuple[list[Identifier], list[Identifier]]:
    """(valid, invalid): which identifiers match their scheme's pattern. A source's free-text field
    (e.g. an NCT number given as "N/A") is not an identifier; core.check_identifier would refuse it."""
    valid = []
    invalid = []
    for identifier in identifiers:
        row = conn.execute(
            "SELECT %s ~ pattern FROM core.identifier_scheme WHERE code = %s", (identifier.value, identifier.scheme)
        ).fetchone()
        if row is not None and row[0]:
            valid.append(identifier)
        else:
            invalid.append(identifier)
    return valid, invalid


def _has_other_key(conn: psycopg.Connection, trial_id: int, source_number: int) -> bool:
    """The trial already has a key at this source. A source lists one trial once, so a new key of the
    same source is another trial, whatever identifiers they share (an extension study may name its
    parent's NCT number)."""
    row = conn.execute(
        "SELECT 1 FROM core.trial_source WHERE trial_id = %s AND source_id = %s LIMIT 1", (trial_id, source_number)
    ).fetchone()
    return row is not None


def _attach_trial(conn: psycopg.Connection, source: str, source_number: int, source_key: str,
                  wanted: Sequence[Identifier]) -> tuple[int, bool, list[int]]:
    """Inside a transaction: the trial of the source's key, else the oldest trial one of its
    identifiers names that has no other key at this source (see _has_other_key), else a new one; the
    source key and the identifiers not known yet are attached. Every other trial the identifiers name
    is recorded as a possible duplicate. Returns (trial id, new trial, possible duplicates)."""
    # One writer at a time decides identities, until its transaction ends: two processes bringing in
    # the same trial at once would otherwise both create it
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key(LOCK_TRIAL_IDENTITY, 0),))
    known_trial = _trial_of_source_key(conn, source_number, source_key)
    matched = trials_with_identifiers(conn, wanted)
    candidates = set()
    if known_trial is not None:
        candidates.add(known_trial)
    for trial_id in matched.values():
        candidates.add(trial_id)

    trial_id = known_trial
    if trial_id is None:
        for candidate in sorted(candidates):
            if not _has_other_key(conn, candidate, source_number):
                trial_id = candidate
                break
    new_trial = False
    if trial_id is None:
        row = conn.execute(
            "INSERT INTO core.trial (created_by, first_source) "
            "VALUES (current_setting('trialwatch.actor')::bigint, %s) RETURNING id",
            (source_number,),
        ).fetchone()
        trial_id = row[0]
        new_trial = True

    duplicates = []
    for other in sorted(candidates):
        if other == trial_id:
            continue
        evidence = {"source": source, "source_key": source_key, "kept_on": trial_id, "identifiers": {}}
        for identifier, matched_trial in matched.items():
            evidence["identifiers"][f"{identifier.scheme}:{identifier.value}"] = matched_trial
        _record_possible_duplicate(conn, trial_id, other, evidence)
        duplicates.append(other)

    if known_trial is None:
        conn.execute(
            "INSERT INTO core.trial_source (source_id, source_key, trial_id, created_by) "
            "VALUES (%s, %s, %s, current_setting('trialwatch.actor')::bigint)",
            (source_number, source_key, trial_id),
        )
    attached = []
    for identifier in wanted:
        if identifier in matched:
            # an identifier already on another trial stays there: that conflict is a possible duplicate
            continue
        if identifier in attached:
            # given twice by the source (CTIS can name a trial's EudraCT number in two places)
            continue
        conn.execute(
            "INSERT INTO core.trial_identifier (scheme, value, trial_id, source_id, created_by) "
            "VALUES (%s, %s, %s, %s, current_setting('trialwatch.actor')::bigint)",
            (identifier.scheme, identifier.value, trial_id, source_number),
        )
        attached.append(identifier)
    return trial_id, new_trial, duplicates


def save_trial_record(
    conn: psycopg.Connection, *, source: str, source_key: str, identifiers: Sequence[Identifier], payload: dict,
    registered: dict, content_hash: bytes,
) -> SavedRecord:
    """Store what one source says about a trial, in one transaction: find the trial by the source's
    key or by any identifier (or create it), add identifiers not known yet, and add a record version
    unless both the content and its reading (`registered`) are the same as the latest one's: an
    adapter that now reads a record differently stores its new reading even when the source's
    content did not change."""
    source_number = source_id(conn, source)
    given = []
    for identifier in identifiers:
        given.append(normalised(identifier))
    with conn.transaction():
        wanted, skipped = valid_identifiers(conn, given)
        trial_id, new_trial, duplicates = _attach_trial(conn, source, source_number, source_key, wanted)
        latest = conn.execute(
            "SELECT content_hash, registered FROM core.trial_record WHERE source_id = %s AND source_key = %s "
            "ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (source_number, source_key),
        ).fetchone()
        if latest is None:
            new_version = True
        elif bytes(latest[0]) != content_hash:
            new_version = True
        else:
            new_version = latest[1] != registered
        if new_version:
            conn.execute(
                "INSERT INTO core.trial_record (source_id, source_key, content_hash, payload, registered, created_by) "
                "VALUES (%s, %s, %s, %s, %s, current_setting('trialwatch.actor')::bigint)",
                (source_number, source_key, content_hash, Jsonb(payload), Jsonb(registered)),
            )
    return SavedRecord(trial_id, new_trial, new_version, duplicates, skipped)


def trial_identifiers(conn: psycopg.Connection, trial_id: int) -> list[Identifier]:
    rows = conn.execute(
        "SELECT scheme, value FROM core.trial_identifier WHERE trial_id = %s ORDER BY scheme, value", (trial_id,)
    ).fetchall()
    found = []
    for scheme, value in rows:
        found.append(Identifier(scheme, value))
    return found


# --- audit -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AuditEntry:
    at: datetime
    actor: str
    actor_kind: str
    action: str
    table_name: str
    row_key: dict
    old_row: dict | None
    new_row: dict | None
    note: str | None


def audit_entries(conn: psycopg.Connection, table_name: str, row_key: dict) -> list[AuditEntry]:
    """Every recorded change to one row, oldest first: who did what, when."""
    with conn.cursor(row_factory=class_row(AuditEntry)) as cur:
        return cur.execute(
            """
            SELECT l.at, a.name AS actor, a.kind AS actor_kind, l.action, l.table_name, l.row_key,
                   l.old_row, l.new_row, l.note
            FROM core.audit_log l JOIN core.actor a ON a.id = l.actor_id
            WHERE l.table_name = %s AND l.row_key = %s
            ORDER BY l.id
            """,
            (table_name, Jsonb(row_key)),
        ).fetchall()


# --- ingest runs -------------------------------------------------------------------------------

def start_ingest(conn: psycopg.Connection, source: str, args: dict, kind: str = "transfer") -> int:
    """Open a run of a source adapter (kind 'list' or 'transfer') and label this session's audit
    entries with it. The session holds the run's lock until finish_ingest, so close_abandoned_ingest_runs
    can tell it is alive; the lock is taken before other sessions can see the run."""
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO core.ingest_run (source_id, actor_id, args, kind) "
            "VALUES (%s, current_setting('trialwatch.actor')::bigint, %s, %s) RETURNING id",
            (source_id(conn, source), Jsonb(args), kind),
        ).fetchone()
        run_id = row[0]
        lock(conn, LOCK_INGEST_RUN, run_id)
    set_note(conn, f"ingest run {run_id}")
    return run_id


def finish_ingest(conn: psycopg.Connection, run_id: int, *, counts: dict, errors: dict[str, str]) -> None:
    if errors:
        status = "failed"
    else:
        status = "succeeded"
    with conn.transaction():
        conn.execute(
            "UPDATE core.ingest_run SET status = %s, finished_at = clock_timestamp(), counts = %s, errors = %s "
            "WHERE id = %s",
            (status, Jsonb(counts), Jsonb(errors), run_id),
        )
    unlock(conn, LOCK_INGEST_RUN, run_id)
    set_note(conn, "")


# Why a run that was still 'running' with no live process behind it was closed
ABANDONED = "its process stopped before the run finished (closed by a later run)"


def close_abandoned_ingest_runs(conn: psycopg.Connection) -> list[int]:
    """Close as failed the ingest runs still marked running whose process is gone (nobody holds their
    lock): a crash, a killed process or a restarted web server. Call it before this session starts a
    run of its own. Returns the closed runs."""
    rows = conn.execute("SELECT id FROM core.ingest_run WHERE status = 'running' ORDER BY id").fetchall()
    closed = []
    for row in rows:
        run_id = row[0]
        if not try_lock(conn, LOCK_INGEST_RUN, run_id):
            # its process is alive
            continue
        try:
            cursor = conn.execute(
                "UPDATE core.ingest_run SET status = 'failed', finished_at = clock_timestamp(), "
                "errors = errors || %s WHERE id = %s AND status = 'running'",
                (Jsonb({"run": ABANDONED}), run_id),
            )
            if cursor.rowcount == 1:
                closed.append(run_id)
        finally:
            unlock(conn, LOCK_INGEST_RUN, run_id)
    return closed


def set_note(conn: psycopg.Connection, note: str) -> None:
    """Why this session is writing; stored with each audit entry ('' for no note)."""
    conn.execute("SELECT set_config('trialwatch.note', %s, false)", (note,))


# --- documents ---------------------------------------------------------------------------------

@dataclass
class SavedVersion:
    document_id: int
    # the bytes were not stored before (under any document)
    new_file: bool
    # the document's current file changed (or it is new): a new version, the previous one marked replaced
    new_version: bool


def save_document_version(
    conn: psycopg.Connection, *, source: str, source_key: str, kind: str, title: str | None, language: str | None,
    version: str, content: bytes, media_type: str | None, sha256: bytes, published_at: datetime | None = None,
) -> SavedVersion:
    """Store what a source gives as a document's current version, in one transaction: the file once by
    its hash, the document by its source key, and a new version (with when the source says it was
    published, if it says) unless the current one has the same label and the same bytes; the previous
    current version is then marked replaced. A file replaced under an unchanged label is a new
    version too: the bytes decide, not only the source's label."""
    source_number = source_id(conn, source)
    with conn.transaction():
        file_cursor = conn.execute(
            "INSERT INTO core.file (sha256, size_bytes, media_type, content, created_by) "
            "VALUES (%s, %s, %s, %s, current_setting('trialwatch.actor')::bigint) ON CONFLICT (sha256) DO NOTHING",
            (sha256, len(content), media_type, content),
        )
        new_file = file_cursor.rowcount == 1
        # The source may retitle or reclassify a document; the audit log keeps the earlier values
        row = conn.execute(
            """
            INSERT INTO core.document (source_id, source_key, kind, title, language, created_by)
            VALUES (%s, %s, %s, %s, %s, current_setting('trialwatch.actor')::bigint)
            ON CONFLICT (source_id, source_key) DO UPDATE
                SET kind = EXCLUDED.kind, title = EXCLUDED.title, language = EXCLUDED.language
            RETURNING id
            """,
            (source_number, source_key, kind, title, language),
        ).fetchone()
        the_document = row[0]
        current = conn.execute(
            "SELECT version, file_sha256 FROM core.document_version WHERE document_id = %s AND replaced_at IS NULL",
            (the_document,),
        ).fetchone()
        if current is not None and current[0] == version and bytes(current[1]) == sha256:
            return SavedVersion(the_document, new_file, False)
        conn.execute(
            "UPDATE core.document_version SET replaced_at = clock_timestamp() "
            "WHERE document_id = %s AND replaced_at IS NULL",
            (the_document,),
        )
        conn.execute(
            "INSERT INTO core.document_version (document_id, version, file_sha256, published_at, created_by) "
            "VALUES (%s, %s, %s, %s, current_setting('trialwatch.actor')::bigint)",
            (the_document, version, sha256, published_at),
        )
    return SavedVersion(the_document, new_file, True)


def link_document(conn: psycopg.Connection, *, trial_id: int, document_id: int, basis: str, source: str,
                  confidence: float = 1.0) -> bool:
    """Link a document to a trial; True if the link is new or back. A link the source had dropped
    comes back when the source lists the document again; one a person removed stays removed: a source
    listing the document again does not undo a person's decision."""
    cursor = conn.execute(
        """
        INSERT INTO core.trial_document (trial_id, document_id, basis, source_id, confidence, created_by)
        VALUES (%s, %s, %s, %s, %s, current_setting('trialwatch.actor')::bigint)
        ON CONFLICT (trial_id, document_id) DO UPDATE SET removed_at = NULL, removed_by = NULL, removal = NULL
            WHERE core.trial_document.removal = 'source'
        """,
        (trial_id, document_id, basis, source_id(conn, source), confidence),
    )
    return cursor.rowcount == 1


def unlink_documents_not_listed(conn: psycopg.Connection, *, trial_id: int, source: str, kinds: Sequence[str] | None,
                                listed: Sequence[int]) -> int:
    """The trial's documents of these kinds (None: every kind) that the source linked but no longer
    lists (`listed`: the documents it lists now) are unlinked, kept with who and when, so a check no
    longer reads them. Returns how many."""
    wanted_kinds = None
    if kinds is not None:
        wanted_kinds = list(kinds)
    cursor = conn.execute(
        """
        UPDATE core.trial_document l
        SET removed_at = clock_timestamp(), removed_by = current_setting('trialwatch.actor')::bigint, removal = 'source'
        FROM core.document d
        WHERE d.id = l.document_id AND l.trial_id = %(trial)s AND l.source_id = %(source)s
          AND l.basis = 'listed-by-source' AND l.removed_at IS NULL
          AND (%(kinds)s::text[] IS NULL OR d.kind = ANY(%(kinds)s)) AND NOT (d.id = ANY(%(listed)s))
        """,
        {"trial": trial_id, "source": source_id(conn, source), "kinds": wanted_kinds, "listed": list(listed)},
    )
    return cursor.rowcount


def document_ids(conn: psycopg.Connection, source: str, source_keys: Sequence[str]) -> list[int]:
    """The documents we hold under these source keys."""
    rows = conn.execute(
        "SELECT id FROM core.document WHERE source_id = %s AND source_key = ANY(%s) ORDER BY id",
        (source_id(conn, source), list(source_keys)),
    ).fetchall()
    found = []
    for row in rows:
        found.append(row[0])
    return found


# --- sponsors ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Sponsor:
    """One sponsor of a trial as a source names it: an organisation and whether it is the main one."""

    name: str
    primary: bool
    # e.g. EMA's organisation id (scheme ema-org); none for a name typed by hand
    identifiers: tuple[Identifier, ...] = ()
    kind: str | None = None
    country: str | None = None
    commercial: bool | None = None


def name_key(name: str) -> str:
    """Lower case, single-spaced, for telling typed names apart in Python. The database compares names
    with its own expression (core.organisation.name_key), which differs for a few characters (e.g. a
    no-break space), so lookups in the database never use this."""
    return " ".join(name.lower().split())


def resolved_organisation(conn: psycopg.Connection, organisation_id: int) -> int:
    """The organisation itself, or the one it was merged into (following the chain)."""
    current = organisation_id
    while True:
        row = conn.execute("SELECT merged_into FROM core.organisation WHERE id = %s", (current,)).fetchone()
        if row is None or row[0] is None:
            return current
        current = row[0]


def _organisation_by_identifiers(conn: psycopg.Connection, identifiers: Sequence[Identifier]) -> int | None:
    for identifier in identifiers:
        row = conn.execute(
            "SELECT organisation_id FROM core.organisation_identifier WHERE scheme = %s AND value = %s",
            (identifier.scheme, identifier.value),
        ).fetchone()
        if row is not None:
            return resolved_organisation(conn, row[0])
    return None


def _organisation_by_name(conn: psycopg.Connection, name: str, schemes: Sequence[str]) -> int | None:
    """An organisation with the same name and no identifier in these schemes (one that has another id
    under the same scheme is a different organisation that happens to share the name)."""
    row = conn.execute(
        """
        SELECT o.id FROM core.organisation o
        WHERE o.name_key = lower(regexp_replace(btrim(%s), '\\s+', ' ', 'g')) AND o.merged_into IS NULL
          AND NOT EXISTS (SELECT 1 FROM core.organisation_identifier i
                          WHERE i.organisation_id = o.id AND i.scheme = ANY(%s))
        ORDER BY o.id LIMIT 1
        """,
        (name, list(schemes)),
    ).fetchone()
    if row is None:
        return None
    return row[0]


def save_organisation(conn: psycopg.Connection, sponsor: Sponsor, source: str) -> int:
    """The organisation a source names: found by identifier, else by name, else created. Its
    identifiers not known yet are added, and what the source says now (name, kind, country,
    commercial) replaces what we had when it came by identifier."""
    source_number = source_id(conn, source)
    wanted = []
    schemes = []
    for identifier in sponsor.identifiers:
        wanted.append(normalised(identifier))
        schemes.append(identifier.scheme)
    with conn.transaction():
        organisation_id = _organisation_by_identifiers(conn, wanted)
        if organisation_id is not None:
            conn.execute(
                """
                UPDATE core.organisation SET name = %s, kind = coalesce(%s, kind), country = coalesce(%s, country),
                       commercial = coalesce(%s, commercial)
                WHERE id = %s AND (name, kind, country, commercial) IS DISTINCT FROM
                      (%s, coalesce(%s, kind), coalesce(%s, country), coalesce(%s, commercial))
                """,
                (sponsor.name, sponsor.kind, sponsor.country, sponsor.commercial, organisation_id,
                 sponsor.name, sponsor.kind, sponsor.country, sponsor.commercial),
            )
        else:
            organisation_id = _organisation_by_name(conn, sponsor.name, schemes)
        if organisation_id is None:
            organisation_id = conn.execute(
                "INSERT INTO core.organisation (name, kind, country, commercial, created_by, first_source) "
                "VALUES (%s, %s, %s, %s, current_setting('trialwatch.actor')::bigint, %s) RETURNING id",
                (sponsor.name, sponsor.kind, sponsor.country, sponsor.commercial, source_number),
            ).fetchone()[0]
        for identifier in wanted:
            conn.execute(
                "INSERT INTO core.organisation_identifier (scheme, value, organisation_id, source_id, created_by) "
                "VALUES (%s, %s, %s, %s, current_setting('trialwatch.actor')::bigint) "
                "ON CONFLICT (scheme, value) DO NOTHING",
                (identifier.scheme, identifier.value, organisation_id, source_number),
            )
    return organisation_id


@dataclass
class SponsorChanges:
    added: int = 0
    removed: int = 0
    changed: int = 0


def set_trial_sponsors(conn: psycopg.Connection, trial_id: int, source: str, sponsors: Sequence[Sponsor]
                       ) -> SponsorChanges:
    """Make the trial's sponsors from this source what the source lists now: new ones are linked,
    a changed primary flag is updated, and the ones it no longer lists are marked removed (kept,
    with who and when). A sponsor a person removed stays removed; links from other sources are not
    touched."""
    source_number = source_id(conn, source)
    changes = SponsorChanges()
    listed = []
    with conn.transaction():
        # a primary sponsor a person (or another source) set stays the only primary one
        row = conn.execute(
            "SELECT 1 FROM core.trial_sponsor WHERE trial_id = %s AND source_id <> %s AND is_primary "
            "AND removed_at IS NULL LIMIT 1",
            (trial_id, source_number),
        ).fetchone()
        primary_set_elsewhere = row is not None
        for sponsor in sponsors:
            organisation_id = save_organisation(conn, sponsor, source)
            primary = sponsor.primary and not primary_set_elsewhere
            if organisation_id in listed:
                continue
            listed.append(organisation_id)
            row = conn.execute(
                "SELECT is_primary, removal, source_id FROM core.trial_sponsor WHERE trial_id = %s AND organisation_id = %s",
                (trial_id, organisation_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO core.trial_sponsor (trial_id, organisation_id, is_primary, source_id, created_by) "
                    "VALUES (%s, %s, %s, %s, current_setting('trialwatch.actor')::bigint)",
                    (trial_id, organisation_id, primary, source_number),
                )
                changes.added += 1
                continue
            was_primary = row[0]
            removal = row[1]
            link_source = row[2]
            if removal == "person" or link_source != source_number:
                # a person's decision, or a link another source (or a person) made, is not this source's to change
                continue
            if removal == "source":
                conn.execute(
                    "UPDATE core.trial_sponsor SET removed_at = NULL, removed_by = NULL, removal = NULL, "
                    "is_primary = %s WHERE trial_id = %s AND organisation_id = %s",
                    (primary, trial_id, organisation_id),
                )
                changes.added += 1
            elif was_primary != primary:
                conn.execute(
                    "UPDATE core.trial_sponsor SET is_primary = %s WHERE trial_id = %s AND organisation_id = %s",
                    (primary, trial_id, organisation_id),
                )
                changes.changed += 1
        cursor = conn.execute(
            """
            UPDATE core.trial_sponsor
            SET removed_at = clock_timestamp(), removed_by = current_setting('trialwatch.actor')::bigint,
                removal = 'source'
            WHERE trial_id = %s AND source_id = %s AND removed_at IS NULL AND NOT (organisation_id = ANY(%s))
            """,
            (trial_id, source_number, listed),
        )
        changes.removed = cursor.rowcount
    return changes


def add_trial_sponsor(conn: psycopg.Connection, trial_id: int, sponsor: Sponsor, source: str) -> int:
    """A person adds one sponsor to a trial (also one that was removed before); returns the organisation id.
    A new primary sponsor makes the trial's other sponsors co-sponsors."""
    source_number = source_id(conn, source)
    with conn.transaction():
        organisation_id = save_organisation(conn, sponsor, source)
        if sponsor.primary:
            conn.execute(
                "UPDATE core.trial_sponsor SET is_primary = false "
                "WHERE trial_id = %s AND organisation_id <> %s AND is_primary",
                (trial_id, organisation_id),
            )
        conn.execute(
            """
            INSERT INTO core.trial_sponsor (trial_id, organisation_id, is_primary, source_id, created_by)
            VALUES (%s, %s, %s, %s, current_setting('trialwatch.actor')::bigint)
            ON CONFLICT (trial_id, organisation_id) DO UPDATE
            SET is_primary = EXCLUDED.is_primary, source_id = EXCLUDED.source_id, removed_at = NULL, removed_by = NULL,
                removal = NULL
            """,
            (trial_id, organisation_id, sponsor.primary, source_number),
        )
    return organisation_id


def remove_trial_sponsor(conn: psycopg.Connection, trial_id: int, organisation_id: int) -> bool:
    """A person removes a sponsor from a trial; True if it was linked. A source listing it again won't bring it back."""
    cursor = conn.execute(
        """
        UPDATE core.trial_sponsor
        SET removed_at = clock_timestamp(), removed_by = current_setting('trialwatch.actor')::bigint,
            removal = 'person'
        WHERE trial_id = %s AND organisation_id = %s AND (removal IS NULL OR removal = 'source')
        """,
        (trial_id, organisation_id),
    )
    return cursor.rowcount == 1


# --- data sources: the registry, their lists, evidence transfers (ADR-005) ---------------------

@dataclass(frozen=True)
class RegisteredSource:
    id: int
    code: str
    kind: str
    name: str
    url: str | None
    adapter: str | None
    enabled: bool
    config: dict


_SOURCE_COLUMNS = "id, code, kind, name, url, adapter, enabled, config"


def registered_sources(conn: psycopg.Connection) -> list[RegisteredSource]:
    with conn.cursor(row_factory=class_row(RegisteredSource)) as cur:
        return cur.execute(f"SELECT {_SOURCE_COLUMNS} FROM core.source ORDER BY id").fetchall()


def registered_source(conn: psycopg.Connection, code: str) -> RegisteredSource | None:
    with conn.cursor(row_factory=class_row(RegisteredSource)) as cur:
        return cur.execute(f"SELECT {_SOURCE_COLUMNS} FROM core.source WHERE code = %s", (code,)).fetchone()


def register_source(conn: psycopg.Connection, *, code: str, kind: str, name: str, url: str | None, adapter: str,
                    config: dict) -> int:
    """Add a source to the registry; the adapter must have checked the config. Returns its id."""
    row = conn.execute(
        "INSERT INTO core.source (code, kind, name, url, adapter, config, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, current_setting('trialwatch.actor')::bigint) RETURNING id",
        (code, kind, name, url, adapter, Jsonb(config)),
    ).fetchone()
    return row[0]


def set_source_config(conn: psycopg.Connection, code: str, config: dict, enabled: bool) -> None:
    """Replace a source's config (checked by its adapter first) and switch it on or off."""
    conn.execute("UPDATE core.source SET config = %s, enabled = %s WHERE code = %s", (Jsonb(config), enabled, code))


@dataclass(frozen=True)
class Listed:
    """One trial as a source's list gives it."""

    key: str  # the source's own id for the trial, e.g. the EU CT number for CTIS
    title: str | None
    identifiers: list[Identifier]
    updated_on: date | None  # the day the source last changed the trial
    summary: dict  # the list's row as given


@dataclass
class SavedListing:
    trial_id: int
    new_trial: bool
    possible_duplicates: list[int] = field(default_factory=list)


def save_listed(conn: psycopg.Connection, source: str, run_id: int, listed: Listed) -> SavedListing:
    """A listed trial: matched to a trial by its source key or identifiers (or created, as for a
    record), and what the list says about it stored in core.source_listing."""
    source_number = source_id(conn, source)
    given = []
    for identifier in listed.identifiers:
        given.append(normalised(identifier))
    with conn.transaction():
        wanted, _skipped = valid_identifiers(conn, given)
        trial_id, new_trial, duplicates = _attach_trial(conn, source, source_number, listed.key, wanted)
        conn.execute(
            """
            INSERT INTO core.source_listing (source_id, source_key, title, source_updated_on, summary, last_run)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id, source_key) DO UPDATE SET
                title = EXCLUDED.title, source_updated_on = EXCLUDED.source_updated_on, summary = EXCLUDED.summary,
                last_listed_at = clock_timestamp(), last_run = EXCLUDED.last_run, delisted_at = NULL
            """,
            (source_number, listed.key, listed.title, listed.updated_on, Jsonb(listed.summary), run_id),
        )
    return SavedListing(trial_id, new_trial, duplicates)


def source_keys_of_trial(conn: psycopg.Connection, source: str, trial_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT ts.source_key FROM core.trial_source ts JOIN core.source s ON s.id = ts.source_id "
        "WHERE s.code = %s AND ts.trial_id = %s ORDER BY ts.source_key",
        (source, trial_id),
    ).fetchall()
    keys = []
    for row in rows:
        keys.append(row[0])
    return keys


def source_key_known(conn: psycopg.Connection, source: str, source_key: str) -> bool:
    """The key is a trial's at that source (listed, or fetched before)."""
    row = conn.execute(
        "SELECT 1 FROM core.trial_source ts JOIN core.source s ON s.id = ts.source_id "
        "WHERE s.code = %s AND ts.source_key = %s",
        (source, source_key),
    ).fetchone()
    return row is not None


def mark_delisted(conn: psycopg.Connection, source: str, run_id: int) -> int:
    """After a complete listing run: the source's listed trials that neither this run nor the complete
    run before it saw are no longer listed (kept, with when). Two misses in a row, because a list that
    shifts while it is paged (one trial leaving, another arriving) can hide a trial from one pass that
    still adds up to the source's total. Returns how many."""
    cursor = conn.execute(
        """
        UPDATE core.source_listing SET delisted_at = clock_timestamp()
        WHERE source_id = %(source)s AND delisted_at IS NULL
          AND last_run < (SELECT coalesce(max(r.id), 0) FROM core.ingest_run r
                          WHERE r.source_id = %(source)s AND r.kind = 'list' AND r.status = 'succeeded'
                            AND (r.counts ->> 'complete')::boolean AND r.id < %(run)s)
        """,
        {"source": source_id(conn, source), "run": run_id},
    )
    return cursor.rowcount


def save_transfer(conn: psycopg.Connection, *, source: str, source_key: str, run_id: int | None, documents: int,
                  pages: int, errors: dict[str, str], document_kinds: Sequence[str] | None) -> str:
    """Record how a trial's evidence transfer from a source ended: 'complete' without errors, else
    'failed', with the document kinds it brought in (None: every kind). Replaces the previous
    transfer's row (its history is in the audit log)."""
    status = "complete"
    if errors:
        status = "failed"
    kinds = None
    if document_kinds is not None:
        kinds = sorted(document_kinds)
    conn.execute(
        """
        INSERT INTO core.evidence_transfer (source_id, source_key, status, transferred_by, ingest_run_id, document_kinds,
                                            source_updated_on, documents, pages, errors)
        VALUES (%(source)s, %(key)s, %(status)s, current_setting('trialwatch.actor')::bigint, %(run)s, %(kinds)s,
                (SELECT source_updated_on FROM core.source_listing WHERE source_id = %(source)s AND source_key = %(key)s),
                %(documents)s, %(pages)s, %(errors)s)
        ON CONFLICT (source_id, source_key) DO UPDATE SET
            status = EXCLUDED.status, transferred_at = clock_timestamp(), transferred_by = EXCLUDED.transferred_by,
            ingest_run_id = EXCLUDED.ingest_run_id, document_kinds = EXCLUDED.document_kinds,
            source_updated_on = EXCLUDED.source_updated_on, documents = EXCLUDED.documents, pages = EXCLUDED.pages,
            errors = EXCLUDED.errors
        """,
        {"source": source_id(conn, source), "key": source_key, "status": status, "run": run_id, "kinds": kinds,
         "documents": documents, "pages": pages, "errors": Jsonb(errors)},
    )
    return status


# Which of a source's trials a transfer takes, by their state in core.evidence_status
TRANSFER_CHOICES = {
    "pending": "listed, evidence not transferred yet",
    "failed": "the last transfer failed",
    "stale": "transferred, but the source changed the trial since",
    "listed": "every trial the source lists",
}


def keys_to_transfer(conn: psycopg.Connection, source: str, which: str) -> list[str]:
    """The source keys of `which` trials (see TRANSFER_CHOICES), in key order."""
    if which not in TRANSFER_CHOICES:
        raise ValueError(f"choose one of {', '.join(TRANSFER_CHOICES)}")
    conditions = {
        "pending": "e.listed AND e.delisted_at IS NULL AND e.status = 'not transferred'",
        "failed": "e.status = 'failed'",
        "stale": "e.status = 'stale'",
        "listed": "e.listed AND e.delisted_at IS NULL",
    }
    rows = conn.execute(
        "SELECT e.source_key FROM core.evidence_status e JOIN core.source s ON s.id = e.source_id "
        f"WHERE s.code = %s AND {conditions[which]} ORDER BY e.source_key",
        (source,),
    ).fetchall()
    keys = []
    for row in rows:
        keys.append(row[0])
    return keys


@dataclass(frozen=True)
class EvidenceRow:
    """A trial's evidence state at one source (core.evidence_status), for deciding whether it may be
    checked. Trials merged into it count as the trial."""

    trial_id: int
    source: str
    adapter: str
    # not transferred | failed | stale | complete
    status: str
    # the document kinds its last transfer brought in (None: every kind)
    document_kinds: list[str] | None


def evidence_rows(conn: psycopg.Connection, trial_ids: Sequence[int]) -> list[EvidenceRow]:
    """Every source's evidence state of these trials and of the trials merged into them."""
    with conn.cursor(row_factory=class_row(EvidenceRow)) as cur:
        return cur.execute(
            """
            WITH RECURSIVE member (trial_id, root) AS (
                SELECT id, id FROM core.trial WHERE id = ANY(%s)
                UNION
                SELECT t.id, m.root FROM core.trial t JOIN member m ON t.merged_into = m.trial_id
            )
            SELECT m.root AS trial_id, s.code AS source, s.adapter, e.status, e.document_kinds
            FROM member m
            JOIN core.evidence_status e ON e.trial_id = m.trial_id
            JOIN core.source s ON s.id = e.source_id
            ORDER BY m.root, s.code, e.source_key
            """,
            (list(trial_ids),),
        ).fetchall()


def mark_transfer_started(conn: psycopg.Connection, *, source: str, source_key: str, run_id: int) -> None:
    """Before a transfer changes a trial's record or documents, its earlier transfer stops counting:
    if the transfer is interrupted (Ctrl-C, a crash), the trial is left 'failed' instead of looking
    complete with half-new evidence. A trial the source never gave us has no row to mark."""
    conn.execute(
        """
        UPDATE core.evidence_transfer
        SET status = 'failed', transferred_at = clock_timestamp(),
            transferred_by = current_setting('trialwatch.actor')::bigint, ingest_run_id = %s, errors = %s
        WHERE source_id = %s AND source_key = %s
        """,
        (run_id, Jsonb({"transfer": f"ingest run {run_id} started it and did not finish"}), source_id(conn, source),
         source_key),
    )


def evidence_counts(conn: psycopg.Connection, trial_id: int, kinds: Sequence[str] | None) -> tuple[int, int]:
    """(documents, pages read) of the trial's current documents of these kinds (None: every kind)."""
    wanted_kinds = None
    if kinds is not None:
        wanted_kinds = list(kinds)
    row = conn.execute(
        """
        SELECT count(DISTINCT d.id)::int, count(p.page_no)::int
        FROM core.trial_document l
        JOIN core.document_current d ON d.id = l.document_id
        LEFT JOIN core.page p ON p.file_sha256 = d.file_sha256
        WHERE l.trial_id = %s AND l.removed_at IS NULL AND (%s::text[] IS NULL OR d.kind = ANY(%s))
        """,
        (trial_id, wanted_kinds, wanted_kinds),
    ).fetchone()
    return row[0], row[1]


# --- page text ---------------------------------------------------------------------------------

def files_without_pages(conn: psycopg.Connection, trial_ids: Sequence[int] | None = None,
                        results_only: bool = True, kinds: Sequence[str] | None = None) -> list[bytes]:
    """Files of the current document versions (of these trials, or all) whose text is not extracted
    yet; by default only document kinds a results check reads, or only these `kinds`."""
    if trial_ids is None:
        wanted_trials = None
    else:
        wanted_trials = list(trial_ids)
    wanted_kinds = None
    if kinds is not None:
        wanted_kinds = list(kinds)
    rows = conn.execute(
        """
        SELECT DISTINCT d.file_sha256
        FROM core.document_current d
        JOIN core.document_kind k ON k.code = d.kind
        JOIN core.trial_document l ON l.document_id = d.id AND l.removed_at IS NULL
        JOIN core.file f ON f.sha256 = d.file_sha256 AND f.pages_extracted_at IS NULL
        WHERE (k.has_results OR NOT %(results_only)s)
          AND (%(kinds)s::text[] IS NULL OR d.kind = ANY(%(kinds)s))
          AND (%(trials)s::bigint[] IS NULL OR l.trial_id = ANY(%(trials)s))
        ORDER BY d.file_sha256
        """,
        {"results_only": results_only, "trials": wanted_trials, "kinds": wanted_kinds},
    ).fetchall()
    files = []
    for row in rows:
        files.append(bytes(row[0]))
    return files


def file_content(conn: psycopg.Connection, sha256: bytes) -> bytes:
    row = conn.execute("SELECT content FROM core.file WHERE sha256 = %s", (sha256,)).fetchone()
    return bytes(row[0])


def save_pages(conn: psycopg.Connection, sha256: bytes, texts: Sequence[str], extractor: str,
               fingerprint: bytes | None) -> list[bytes]:
    """Store a file's page text and text fingerprint (None for a file without text, e.g. scanned);
    returns other files with the same text."""
    with conn.transaction():
        with conn.cursor() as cur, cur.copy(
            "COPY core.page (file_sha256, page_no, text, extractor) FROM STDIN"
        ) as copy:
            for page_no, text in enumerate(texts, start=1):
                copy.write_row((sha256, page_no, text, extractor))
        conn.execute(
            "UPDATE core.file SET text_fingerprint = %s, pages_extracted_at = clock_timestamp() WHERE sha256 = %s",
            (fingerprint, sha256),
        )
        if fingerprint is None:
            return []
        rows = conn.execute(
            "SELECT sha256 FROM core.file WHERE text_fingerprint = %s AND sha256 <> %s", (fingerprint, sha256)
        ).fetchall()
    same = []
    for row in rows:
        same.append(bytes(row[0]))
    return same


# --- what checks read --------------------------------------------------------------------------

def trials_with_scheme(conn: psycopg.Connection, scheme: str, values: Sequence[str]) -> dict[str, int]:
    """Identifier value -> trial id (after merges) for the values known under one scheme."""
    found = {}
    for value in values:
        row = conn.execute(
            "SELECT trial_id FROM core.trial_identifier WHERE scheme = %s AND value = %s",
            (scheme, normalised(Identifier(scheme, value)).value),
        ).fetchone()
        if row is not None:
            found[value] = resolved_trial(conn, row[0])
    return found


def trial_names(conn: psycopg.Connection, trial_ids: Sequence[int]) -> dict[int, str]:
    """A readable name per trial: its EU CT number, else its first identifier, else "trial <id>"."""
    names = {}
    for trial_id in trial_ids:
        row = conn.execute(
            "SELECT value FROM core.trial_identifier WHERE trial_id = %s ORDER BY scheme <> 'eu-ct', scheme, value "
            "LIMIT 1",
            (trial_id,),
        ).fetchone()
        if row is None:
            names[trial_id] = f"trial {trial_id}"
        else:
            names[trial_id] = row[0]
    return names


def trial_registered(conn: psycopg.Connection, trial_id: int) -> dict | None:
    """The trial's registered information from its most recently fetched record (any source), or
    None if no source has given a record for it."""
    row = conn.execute(
        "SELECT registered FROM core.trial_record_current WHERE trial_id = %s ORDER BY fetched_at DESC LIMIT 1",
        (trial_id,),
    ).fetchone()
    if row is None:
        return None
    return row[0]


@dataclass(frozen=True)
class Page:
    file: bytes
    doc: int  # 1-based document number within the trial, as shown to the model
    title: str
    page_no: int
    text: str
    document_version_id: int
    kind: str


# The current version of each document of some kinds linked to a trial, numbered 1, 2, ... in a fixed
# order. Pages and files are numbered from this, so "document 2" is the same document either way.
_TRIAL_DOCUMENTS = """
    SELECT d.version_id, d.file_sha256, d.kind, coalesce(d.title, '') AS title, d.id,
           (dense_rank() OVER (ORDER BY d.kind, coalesce(d.title, ''), d.id))::int AS doc
    FROM core.document_current d
    JOIN core.trial_document l ON l.document_id = d.id AND l.removed_at IS NULL
    WHERE l.trial_id = %(trial)s AND d.kind = ANY(%(kinds)s)
"""


def trial_pages(conn: psycopg.Connection, trial_id: int, kinds: Sequence[str]) -> list[Page]:
    """Every page of the current version of each document of these kinds linked to the trial (and
    not unlinked), numbered by document."""
    with conn.cursor(row_factory=class_row(Page)) as cur:
        return cur.execute(
            f"""
            WITH docs AS ({_TRIAL_DOCUMENTS})
            SELECT p.file_sha256 AS file, docs.doc, docs.title, p.page_no, p.text,
                   docs.version_id AS document_version_id, docs.kind
            FROM docs JOIN core.page p ON p.file_sha256 = docs.file_sha256
            ORDER BY docs.doc, p.page_no
            """,
            {"trial": trial_id, "kinds": list(kinds)},
        ).fetchall()


@dataclass(frozen=True)
class TrialFile:
    """A document's current file, for sending it whole to the model."""

    doc: int
    title: str
    kind: str
    document_version_id: int
    sha256: bytes
    media_type: str | None
    size_bytes: int
    content: bytes


def trial_files(conn: psycopg.Connection, trial_id: int, kinds: Sequence[str]) -> list[TrialFile]:
    """The current file of each document of these kinds linked to the trial, numbered as trial_pages."""
    with conn.cursor(row_factory=class_row(TrialFile)) as cur:
        return cur.execute(
            f"""
            WITH docs AS ({_TRIAL_DOCUMENTS})
            SELECT docs.doc, docs.title, docs.kind, docs.version_id AS document_version_id, f.sha256,
                   f.media_type, f.size_bytes::int AS size_bytes, f.content
            FROM docs JOIN core.file f ON f.sha256 = docs.file_sha256
            ORDER BY docs.doc
            """,
            {"trial": trial_id, "kinds": list(kinds)},
        ).fetchall()


def unread_documents(conn: psycopg.Connection, trial_id: int, kinds: Sequence[str]) -> list[str]:
    """Titles of linked documents of these kinds whose text has not been extracted yet (so a check
    would not see them; it must not judge as if it had read everything)."""
    rows = conn.execute(
        """
        SELECT coalesce(d.title, d.source_key)
        FROM core.document_current d
        JOIN core.trial_document l ON l.document_id = d.id AND l.removed_at IS NULL
        JOIN core.file f ON f.sha256 = d.file_sha256
        WHERE l.trial_id = %s AND d.kind = ANY(%s) AND f.pages_extracted_at IS NULL
        ORDER BY 1
        """,
        (trial_id, list(kinds)),
    ).fetchall()
    titles = []
    for row in rows:
        titles.append(row[0])
    return titles


def rank_pages(conn: psycopg.Connection, files: Sequence[bytes], terms: Sequence[str], limit: int
               ) -> list[tuple[bytes, int, float]]:
    """(file, page, rank) of the pages matching any of the terms, best first (full-text rank)."""
    # "|" is OR in a tsquery: a page matches if it has any of the terms
    query_text = " | ".join(terms)
    rows = conn.execute(
        """
        SELECT file_sha256, page_no, ts_rank(tsv, q)
        FROM core.page, to_tsquery('simple', %s) q
        WHERE file_sha256 = ANY(%s) AND tsv @@ q
        ORDER BY ts_rank(tsv, q) DESC, file_sha256, page_no
        LIMIT %s
        """,
        (query_text, list(files), limit),
    ).fetchall()
    ranked = []
    for sha, page_no, rank in rows:
        ranked.append((bytes(sha), page_no, float(rank)))
    return ranked


def trials_with_pages(conn: psycopg.Connection, kinds: Sequence[str]) -> list[int]:
    """Trials (not merged into another) with at least one linked document of these kinds whose text is extracted."""
    rows = conn.execute(
        """
        SELECT DISTINCT l.trial_id
        FROM core.trial_document l
        JOIN core.trial t ON t.id = l.trial_id AND t.merged_into IS NULL
        JOIN core.document_current d ON d.id = l.document_id AND d.kind = ANY(%s)
        JOIN core.file f ON f.sha256 = d.file_sha256 AND f.pages_extracted_at IS NOT NULL
        WHERE l.removed_at IS NULL
        ORDER BY l.trial_id
        """,
        (list(kinds),),
    ).fetchall()
    trial_ids = []
    for row in rows:
        trial_ids.append(row[0])
    return trial_ids


def add_identifier(conn: psycopg.Connection, trial_id: int, identifier: Identifier, source: str) -> bool:
    """Give a trial one more identifier; True if it is new. An identifier of another trial is refused."""
    wanted = normalised(identifier)
    owner = trials_with_identifiers(conn, [wanted])
    if wanted in owner:
        if owner[wanted] != resolved_trial(conn, trial_id):
            raise ValueError(f"{wanted.scheme} {wanted.value} belongs to trial {owner[wanted]}")
        return False
    conn.execute(
        "INSERT INTO core.trial_identifier (scheme, value, trial_id, source_id, created_by) "
        "VALUES (%s, %s, %s, %s, current_setting('trialwatch.actor')::bigint)",
        (wanted.scheme, wanted.value, trial_id, source_id(conn, source)),
    )
    return True


def identifier_schemes(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """(code, label) of every trial identifier scheme, for forms."""
    rows = conn.execute(
        "SELECT code, label FROM core.identifier_scheme WHERE identifies = 'trial' ORDER BY code = 'eu-ct' DESC, code"
    ).fetchall()
    schemes = []
    for code, label in rows:
        schemes.append((code, label))
    return schemes


def document_kinds(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """(code, label) of every document kind, results kinds first, for forms."""
    rows = conn.execute(
        "SELECT code, label FROM core.document_kind ORDER BY has_results DESC, code = 'results-summary' DESC, code"
    ).fetchall()
    kinds = []
    for code, label in rows:
        kinds.append((code, label))
    return kinds


def trial_document_with_file(conn: psycopg.Connection, trial_id: int, sha256: bytes) -> str | None:
    """Title (or source key) of a document linked to the trial whose current file has these bytes."""
    row = conn.execute(
        """
        SELECT coalesce(d.title, d.source_key) FROM core.document_current d
        JOIN core.trial_document l ON l.document_id = d.id AND l.removed_at IS NULL
        WHERE l.trial_id = %s AND d.file_sha256 = %s LIMIT 1
        """,
        (trial_id, sha256),
    ).fetchone()
    if row is None:
        return None
    return row[0]


@dataclass(frozen=True)
class CurrentRecord:
    """The trial record a check reads at this moment: the most recently fetched one, from any source."""

    id: int
    source: str
    fetched_at: datetime
    registered: dict


def trial_current_record(conn: psycopg.Connection, trial_id: int) -> CurrentRecord | None:
    with conn.cursor(row_factory=class_row(CurrentRecord)) as cur:
        return cur.execute(
            """
            SELECT r.id, s.code AS source, r.fetched_at, r.registered
            FROM core.trial_record_current r JOIN core.source s ON s.id = r.source_id
            WHERE r.trial_id = %s ORDER BY r.fetched_at DESC LIMIT 1
            """,
            (trial_id,),
        ).fetchone()
