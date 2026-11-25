"""The data source registry (ADR-005): which adapters exist, and what each one gives.

A source is registered in core.source with the adapter that reads it and its config (what to list,
what evidence to transfer). The adapter is code; the config is data, one current config per source,
and every run records the config it used.

An adapter only reads its source and says what it read; it never writes to the database. Saving is
the same for every source (trialwatch.trials.ingest):

- its list, page by page: each trial's key, title, identifiers and the day the source last changed it;
- one trial: `fetch` its record as the source gives it, `parse` it into a SourceTrial (identifiers,
  the registered information checks ask about, sponsors, where it runs, its profile for patient
  search, its documents), and `download` each document the transfer wants.

`parse` reads a stored record too, so readings are rebuilt without fetching anything (`trialwatch
source profiles`, `source locations`). A source that is not read from anywhere (manual: a person types
trials in) has no list and no transfer.
"""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO

from trialwatch.db.core import Identifier, Listed, Location, Sponsor
from trialwatch.sources.profile import TrialProfile


class TrialNotFound(Exception):
    """The source does not know the trial."""


class DownloadedFile:
    """A file a source gave: its bytes in memory, or spooled to a temporary file as it was downloaded
    (with its sha256 and size counted on the way), so a large one is read into memory only if it must
    be stored, and then once. close() (or `with`) removes the spool."""

    def __init__(
        self,
        content: bytes | None = None,
        media_type: str | None = None,
        file_name: str | None = None,
        *,
        spool: IO[bytes] | None = None,
        sha256: bytes | None = None,
        size: int | None = None,
    ) -> None:
        if (content is None) == (spool is None):
            raise ValueError("a downloaded file is its content or its spool")
        if spool is not None and (sha256 is None or size is None):
            raise ValueError("a spooled file comes with its sha256 and size")
        self._content = content
        self._spool = spool
        self.media_type = media_type
        # the file name the source gives it, if any
        self.file_name = file_name
        self._sha256 = sha256
        self._size = size

    @property
    def content(self) -> bytes:
        """The bytes (read from the spool each time: hold them only while they are needed)."""
        if self._content is not None:
            return self._content
        self._spool.seek(0)
        return self._spool.read()

    @property
    def size(self) -> int:
        if self._content is not None:
            return len(self._content)
        return self._size

    def sha256(self) -> bytes:
        if self._sha256 is None:
            self._sha256 = hashlib.sha256(self._content).digest()
        return self._sha256

    def head(self, count: int) -> bytes:
        if self._content is not None:
            return self._content[:count]
        self._spool.seek(0)
        return self._spool.read(count)

    def tail(self, count: int) -> bytes:
        if self._content is not None:
            return self._content[-count:]
        self._spool.seek(max(0, self._size - count))
        return self._spool.read()

    def close(self) -> None:
        if self._spool is not None:
            self._spool.close()

    def __enter__(self) -> "DownloadedFile":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@dataclass
class ListingPage:
    """One page of a source's list."""

    rows: list[Listed]
    # how many trials the list holds in all (as the source says), when it says so
    total: int | None
    # False on the last page
    more: bool


@dataclass(frozen=True)
class DocumentRef:
    """A document a trial's record lists, as the source identifies it."""

    # the source's own id for the document (CTIS: its uuid)
    key: str
    # the source's version label (CTIS: its systemVersion)
    version: str
    # our document kind (core.document_kind)
    kind: str
    title: str | None = None
    # ISO 639-1, when the source says
    language: str | None = None
    # when the source says this version was submitted or published
    published_at: datetime | None = None
    # the source lists it as a PDF: a file that is not one is an error
    listed_as_pdf: bool = False
    # why the record's entry for it can't be used (it is listed, but can't be fetched)
    problem: str | None = None


@dataclass
class SourceTrial:
    """What a source says about one trial, read from its record."""

    # sha256 of the record without what changes on every fetch: the same hash, the same record
    content_hash: bytes
    identifiers: list[Identifier]
    # the registered information checks ask about, the same shape for every source: titles, status,
    # dates, primary endpoints [{id, number, text}]
    registered: dict
    # None when the record's sponsors, sites or profile could not be read (see `warnings`): what is
    # stored for them is then left as it is
    sponsors: list[Sponsor] | None
    locations: list[Location] | None
    profile: TrialProfile | None
    # None: the record has no list of documents, so it says nothing about them (nothing is unlinked)
    documents: list[DocumentRef] | None
    # what could not be read besides the evidence, e.g. {"sponsors": "ValueError: …"}: reported, not blocking
    warnings: dict[str, str] = field(default_factory=dict)


class Adapter:
    """What saving needs from a source. Subclasses set the class attributes and override what they
    support."""

    name = ""
    label = ""
    # the core.source kind of a source read by this adapter
    kind = "registry"
    url: str | None = None
    lists = False
    transfers = False
    # the document kinds (core.document_kind) the source can give at all; None: any kind (e.g. a
    # person uploading). A check that reads a kind the source can give waits for a transfer of it.
    document_kinds: tuple[str, ...] | None = None
    # columns the source's page shows from a listed trial's summary: (shown name, summary field)
    listing_columns: tuple[tuple[str, str], ...] = ()
    # what one key looks like, for forms: e.g. an EU CT number
    key_label = "key"
    # the identifier scheme of the source's own keys (core.identifier_scheme), e.g. eu-ct: a gold dataset's
    # items are read through the source whose keys they name
    key_scheme: str | None = None

    @classmethod
    @contextmanager
    def connected(cls) -> Iterator["Adapter"]:
        """An adapter ready to reach its source; adapters that need a client override this."""
        yield cls()

    def default_config(self) -> dict:
        return {}

    def config_problems(self, config: dict) -> list[str]:
        """What is wrong with a config, in words; empty when it can be used."""
        if config:
            return ["this source takes no config"]
        return []

    def evidence_kinds(self, config: dict) -> list[str] | None:
        """The document kinds a transfer brings in and reads (None: every kind)."""
        return None

    def key_problem(self, key: str) -> str | None:
        """Why `key` cannot be one of this source's keys, or None."""
        return None

    def public_url(self, key: str) -> str | None:
        """The trial's page at the source, for people to read it there (e.g. its contacts)."""
        return None

    def list_pages(self, config: dict) -> Iterator[ListingPage]:
        raise NotImplementedError(f"{self.name} has no list to sync")

    def fetch(self, key: str) -> dict:
        """The trial's record as the source gives it; TrialNotFound when the source doesn't know it."""
        raise NotImplementedError(f"{self.name} has no evidence to transfer")

    def parse(self, payload: dict, listing: dict | None) -> SourceTrial:
        """What a record says (`listing`: the source's list row for the trial, None when not listed).
        Reads only the record: no network, no database."""
        raise NotImplementedError(f"{self.name} has no records to read")

    def download(self, key: str, document: DocumentRef) -> DownloadedFile:
        """The current file of a document the trial's record lists."""
        raise NotImplementedError(f"{self.name} has no documents to download")


def _ctis_adapter():
    from trialwatch.sources.ctis import CtisAdapter

    return CtisAdapter


def _manual_adapter():
    from trialwatch.sources.manual import ManualAdapter

    return ManualAdapter


# Adapter name -> its class (imported when needed)
ADAPTERS = {
    "ctis": _ctis_adapter,
    "manual": _manual_adapter,
}


def adapter_class(name: str) -> type[Adapter]:
    if name not in ADAPTERS:
        raise ValueError(f"no adapter {name!r}; there are {', '.join(ADAPTERS)}")
    return ADAPTERS[name]()


@contextmanager
def open_adapter(name: str) -> Iterator[Adapter]:
    """The adapter with what it needs to reach its source (e.g. an HTTP client), closed afterwards."""
    adapter_type = adapter_class(name)
    with adapter_type.connected() as adapter:
        yield adapter
