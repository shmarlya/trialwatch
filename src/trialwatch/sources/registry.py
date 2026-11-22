"""The data source registry (ADR-005): which adapters exist, and what each one must do.

A source is registered in core.source with the adapter that reads it and its config (what to list,
what evidence to transfer). The adapter is code; the config is data, one current config per source,
and every run records the config it used. The two stages that use an adapter are generic
(trialwatch.sources.stages):

- syncing the list: the adapter gives the source's trials page by page (key, title, identifiers,
  the day the source last changed it), each matched to or created as a core.trial;
- transferring evidence: the adapter fetches one trial's full record and the configured documents
  into the central model; the stage then reads their page text and records whether all of it came in.

A source that is not read from anywhere (manual: a person types trials in) has no list and no
transfer; it records its own transfers as the person adds documents.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from trialwatch.db.core import Listed, Location
from trialwatch.sources.profile import TrialProfile


@dataclass
class ListingPage:
    """One page of a source's list."""

    rows: list[Listed]
    # how many trials the list holds in all (as the source says), when it says so
    total: int | None
    # False on the last page
    more: bool


@dataclass
class Fetched:
    """What an adapter brought in for one trial."""

    # None when even the record could not be fetched
    trial_id: int | None
    new_trial: bool = False
    new_record: bool = False
    possible_duplicates: int = 0
    # documents of the configured kinds the record lists
    documents: int = 0
    new_versions: int = 0
    # documents whose current file we already held, byte for byte
    unchanged: int = 0
    # links to documents the source no longer lists, now removed
    unlinked: int = 0
    written: int = 0
    # "record", or a document's key -> what went wrong: the evidence is not complete
    errors: dict[str, str] = field(default_factory=dict)
    # what went wrong besides the evidence (e.g. reading the sponsors): reported, not blocking
    warnings: dict[str, str] = field(default_factory=dict)


class Adapter:
    """What the stages need from a source. Subclasses set the class attributes and override what
    they support."""

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

    def locations(self, payload: dict) -> list[Location]:
        """Where the trial of a stored record runs (sites and countries), read from its payload; a
        transfer stores them, and `trialwatch source locations` reads them again from stored records."""
        return []

    def profile(self, payload: dict, listing: dict | None) -> TrialProfile | None:
        """What a person looking for a trial needs (ADR-008), read from a stored record and the source's
        list row for it (None: not listed); None when the source has no such reading. A transfer stores
        it, and `trialwatch source profiles` reads it again from stored records."""
        return None

    def public_url(self, key: str) -> str | None:
        """The trial's page at the source, for people to read it there (e.g. its contacts)."""
        return None

    def list_pages(self, config: dict) -> Iterator[ListingPage]:
        raise NotImplementedError(f"{self.name} has no list to sync")

    def fetch(self, conn: psycopg.Connection, source: str, key: str, config: dict,
              files_dir: Path | None = None) -> Fetched:
        raise NotImplementedError(f"{self.name} has no evidence to transfer")


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
