"""Manual source (ADR-003): trials and documents a person enters in TrialWatch, stored in the central
model like any source's, with the person as the actor of every change.

A trial entered by hand is matched to existing trials by its identifiers first, so the same trial is
never added twice. A trial without any registry identifier gets our own (TW-<trial id>). Its
registered information (titles, sponsor, status, primary endpoints) is what the person typed, in the
same `registered` shape the CTIS adapter fills, so checks ask about it the same way.

There is nothing to list or fetch (ADR-005): the evidence is what the person uploads, so the trial's
evidence transfer is recorded as they add it: complete when every uploaded document has its page
text read. Only PDF files are taken (page text is read from PDFs), and nothing is added while the
manual source is switched off.
"""

import hashlib
import uuid
from dataclasses import dataclass, field

import psycopg

from trialwatch.hashing import json_hash
from trialwatch.db import core
from trialwatch.db.core import Identifier
from trialwatch.trials.pages import extract_missing
from trialwatch.sources.registry import Adapter

SOURCE = "manual"


class ManualAdapter(Adapter):
    """Trials and documents a person enters: registered like any source, with no list and no transfer."""

    name = "manual"
    label = "Entered by a person in TrialWatch"
    kind = "manual"


@dataclass(frozen=True)
class UploadedFile:
    name: str
    content: bytes


@dataclass(frozen=True)
class TrialEntry:
    """What a person typed for a new trial."""

    identifiers: list[Identifier]
    public_title: str
    # sponsor names, the primary sponsor first
    sponsors: list[str] = field(default_factory=list)
    status: str | None = None
    # one primary endpoint per entry, in the registered order
    primary_endpoints: list[str] = field(default_factory=list)


def registered(entry: TrialEntry) -> dict:
    """The entry in the shape every adapter fills (see trialwatch.sources.ctis.registered)."""
    endpoints = []
    for number, text in enumerate(entry.primary_endpoints, start=1):
        # the endpoint's number doubles as its id: questions are keyed endpoint:<id>
        endpoints.append({"id": number, "number": number, "text": text})
    return {"public_title": entry.public_title, "full_title": None, "short_title": None,
            "status": entry.status, "start_date": None, "end_date": None, "estimated_global_end_date": None,
            "primary_endpoints": endpoints}


def sponsors(entry: TrialEntry) -> list[core.Sponsor]:
    """The typed names as sponsors: the first is the primary one; a name typed twice counts once."""
    found = []
    seen = []
    for name in entry.sponsors:
        cleaned = " ".join(name.split())
        if not cleaned or core.name_key(cleaned) in seen:
            continue
        seen.append(core.name_key(cleaned))
        is_first = not found
        found.append(core.Sponsor(name=cleaned, primary=is_first))
    return found


class AlreadyKnown(Exception):
    """The trial is already in the database (one of its identifiers belongs to it)."""

    def __init__(self, trial_id: int, identifier: Identifier) -> None:
        super().__init__(f"{identifier.scheme} {identifier.value} is already trial {trial_id}")
        self.trial_id = trial_id


def _refuse_when_switched_off(conn: psycopg.Connection) -> None:
    source = core.registered_source(conn, SOURCE)
    if source is None or not source.enabled:
        raise ValueError("the manual source is switched off (see Sources)")


def _refuse_non_pdf(file: UploadedFile) -> None:
    if not file.content:
        raise ValueError(f"{file.name} is empty")
    if not file.content.startswith(b"%PDF"):
        raise ValueError(f"{file.name} is not a PDF: only PDF files can be read")


def add_trial(conn: psycopg.Connection, entry: TrialEntry, files: list[UploadedFile], kind: str) -> int:
    """Add a trial by hand, with documents of one kind; returns its core.trial id. Raises
    AlreadyKnown when one of its identifiers is already a trial's, and ValueError on bad input."""
    _refuse_when_switched_off(conn)
    if not entry.public_title.strip():
        raise ValueError("a trial needs a title")
    for file in files:
        _refuse_non_pdf(file)
    wanted = []
    for identifier in entry.identifiers:
        wanted.append(core.normalised(identifier))
    valid, invalid = core.valid_identifiers(conn, wanted)
    for identifier in invalid:
        # typed by a person: refused with what is wrong, not stored without it (as a source's "N/A" is)
        raise ValueError(f"{identifier.value} is not a valid {identifier.scheme} identifier")
    known = core.trials_with_identifiers(conn, wanted)
    for identifier, trial_id in known.items():
        raise AlreadyKnown(trial_id, identifier)
    payload = {"entered_by_hand": True, "identifiers": [], "registered": registered(entry),
               "sponsors": entry.sponsors}
    for identifier in wanted:
        payload["identifiers"].append({"scheme": identifier.scheme, "value": identifier.value})
    # A manual record's key is its own; the trial's identifiers carry the registry numbers
    source_key = f"entry-{uuid.uuid4()}"
    with conn.transaction():
        saved = core.save_trial_record(conn, source=SOURCE, source_key=source_key, identifiers=wanted,
                                       payload=payload, registered=registered(entry),
                                       content_hash=json_hash(payload))
        if not wanted:
            core.add_identifier(conn, saved.trial_id, Identifier("trialwatch", f"TW-{saved.trial_id}"), SOURCE)
        core.set_trial_sponsors(conn, saved.trial_id, SOURCE, sponsors(entry))
        for file in files:
            add_document(conn, saved.trial_id, file, kind, title=None, extract=False)
    if files:
        extract_missing(conn, [saved.trial_id])
    record_transfer(conn, saved.trial_id)
    return saved.trial_id


def record_transfer(conn: psycopg.Connection, trial_id: int) -> None:
    """The trial's evidence as entered by hand: complete when every document linked to it has its
    page text read (recorded on each of its manual records)."""
    keys = core.source_keys_of_trial(conn, SOURCE, trial_id)
    if not keys:
        return
    errors = {}
    unread = core.files_without_pages(conn, [trial_id])
    if unread:
        errors["pages"] = f"{len(unread)} files without page text"
    documents, pages = core.evidence_counts(conn, trial_id, None)
    for key in keys:
        core.save_transfer(conn, source=SOURCE, source_key=key, run_id=None, documents=documents, pages=pages,
                           errors=errors, document_kinds=None)


def add_document(conn: psycopg.Connection, trial_id: int, file: UploadedFile, kind: str, title: str | None,
                 extract: bool = True) -> int:
    """Upload a document to a trial: stored once by its bytes, linked to the trial by hand, and its
    page text extracted. Returns the core.document id."""
    _refuse_when_switched_off(conn)
    _refuse_non_pdf(file)
    sha256 = hashlib.sha256(file.content).digest()
    # The same bytes twice on one trial would be the same document read twice
    existing = core.trial_document_with_file(conn, trial_id, sha256)
    if existing is not None:
        raise ValueError(f"{file.name} is already linked to this trial as {existing!r}")
    document_title = title
    if not document_title:
        document_title = file.name
    # One upload is one document; uploading the same bytes again adds a document pointing at the same file
    source_key = f"upload-{uuid.uuid4()}"
    saved = core.save_document_version(
        conn, source=SOURCE, source_key=source_key, kind=kind, title=document_title, language=None, version="1",
        content=file.content, media_type="application/pdf", sha256=sha256,
    )
    core.link_document(conn, trial_id=trial_id, document_id=saved.document_id, basis="manual", source=SOURCE)
    if extract:
        extract_missing(conn, [trial_id])
        record_transfer(conn, trial_id)
    return saved.document_id
