"""Bringing trials in from a registered source (ADR-005), the same for every adapter. An adapter
reads its source and says what it read (trialwatch.sources.registry); everything here saves it.

1. sync_list: every trial the source lists becomes a core.trial (matched by its source key and
   identifiers, or created), with what the list says about it in core.source_listing. A complete
   pass also marks the trials the source no longer lists. Nothing but the list is fetched.
2. transfer: for chosen trials, the adapter fetches the full record and reads it (a SourceTrial);
   `save` stores the record as a new version if it changed (the trial matched by all its identifiers
   or created), its sponsors, sites and profile, and every listed document of the configured kinds
   (downloaded when new or at a new version label; with recheck, every time, a new version when its
   bytes changed), and unlinks the ones the source no longer lists; then their page text is read, in
   child processes, while the next trial is fetched (pages.Reading). The trial's
   core.evidence_transfer row says 'complete' only when the record, every configured document and
   every page came in, else 'failed' and why. While a trial is being transferred its row says
   failed, so an interrupted transfer never leaves half-new evidence looking complete. Each trial is
   then indexed for patient search (ADR-008: its new sites geocoded, its search document built from
   the profile the adapter stored), so it is findable the moment it arrives; a failure there is
   reported but leaves the evidence complete.

Which trials a check may then read is trialwatch.trials.readiness.

Each stage is one core.ingest_run (kind 'list' or 'transfer') with the config it used, its counts and
errors. Both can be stopped between trials (or list pages): what was done so far is kept. One process
at a time reads a source (an advisory lock), and a run whose process died is closed by the next one.
"""

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

import psycopg

from trialwatch import telemetry
from trialwatch.db import core
from trialwatch.db import search as search_db
from trialwatch.db.core import RegisteredSource
from trialwatch.geo import load as geo_load
from trialwatch.hashing import storable
from trialwatch.progress import Progress, beat, report
from trialwatch.search import index as search_index
from trialwatch.sources import profile
from trialwatch.sources.registry import Adapter, DocumentRef, DownloadedFile, TrialNotFound
from trialwatch.trials import pages

ShouldStop = Callable[[], bool] | None
# Where a PDF's end-of-file marker is looked for: it ends the file, give or take trailing bytes
PDF_END_BYTES = 2048
# One line per trial transferred, with its errors when it failed, in the worker's log (Coolify shows it)
log = logging.getLogger("trialwatch.ingest")


class SourceBusy(ValueError):
    """Another process is syncing or transferring from the same source."""


def _stopping(should_stop: ShouldStop) -> bool:
    if should_stop is None:
        return False
    return should_stop()


def _usable(source: RegisteredSource, adapter: Adapter) -> None:
    if not source.enabled:
        raise ValueError(f"source {source.code} is switched off")
    problems = adapter.config_problems(source.config)
    if problems:
        raise ValueError(f"source {source.code}'s config: {'; '.join(problems)}")


def _hold_source(conn: psycopg.Connection, source: RegisteredSource) -> None:
    """Close the runs whose process died, then take the source's lock; SourceBusy if another process
    is reading the source."""
    core.close_abandoned_ingest_runs(conn)
    if not core.try_lock(conn, core.LOCK_SOURCE, source.id):
        raise SourceBusy(
            f"another process is reading source {source.code} (its ingest run is still running); "
            "try again when it has finished"
        )


# --- 1. the list -------------------------------------------------------------------------------


@dataclass
class ListResult:
    run_id: int
    listed: int = 0
    new_trials: int = 0
    possible_duplicates: int = 0
    # how many the source says it lists
    total: int | None = None
    # no longer listed (only marked after a complete pass)
    delisted: int = 0
    # every page was read
    complete: bool = False
    stopped: bool = False
    failed: dict[str, str] = field(default_factory=dict)

    def counts(self) -> dict:
        return {
            "listed": self.listed,
            "new_trials": self.new_trials,
            "total": self.total,
            "possible_duplicates": self.possible_duplicates,
            "delisted": self.delisted,
            "complete": self.complete,
            "stopped": self.stopped,
            "failed": len(self.failed),
        }

    def summary(self) -> str:
        state = "complete"
        if self.stopped:
            state = "stopped part-way"
        elif not self.complete:
            state = "not complete"
        return (
            f"list run {self.run_id} ({state}): {self.listed:,} listed, {self.new_trials:,} new trials, "
            f"{self.delisted:,} no longer listed, possible duplicates {self.possible_duplicates}"
        )


def sync_list(
    conn: psycopg.Connection,
    source: RegisteredSource,
    adapter: Adapter,
    progress: Progress = None,
    should_stop: ShouldStop = None,
) -> ListResult:
    """Bring the source's whole list in (see the module's docstring)."""
    if not adapter.lists:
        raise ValueError(f"source {source.code} has no list to sync")
    _usable(source, adapter)
    _hold_source(conn, source)
    try:
        result = ListResult(run_id=core.start_ingest(conn, source.code, {"config": source.config}, kind="list"))
        try:
            _read_list(conn, source, adapter, result, progress, should_stop)
        except Exception as exc:  # e.g. the source stopped answering: the pages read so far are kept
            result.failed["list"] = f"{type(exc).__name__}: {exc}"
            report(progress, f"failed: {result.failed['list']}")
        except BaseException as exc:
            # e.g. Ctrl-C: the run is closed as failed, then the interruption goes on
            result.stopped = True
            result.failed["list"] = f"interrupted: {type(exc).__name__}"
            raise
        finally:
            if result.complete and not result.failed:
                result.delisted = core.mark_delisted(conn, source.code, result.run_id)
            core.finish_ingest(conn, result.run_id, counts=result.counts(), errors=result.failed)
    finally:
        core.unlock(conn, core.LOCK_SOURCE, source.id)
    return result


def _read_list(
    conn: psycopg.Connection,
    source: RegisteredSource,
    adapter: Adapter,
    result: ListResult,
    progress: Progress,
    should_stop: ShouldStop,
) -> None:
    page_number = 0
    for page in adapter.list_pages(source.config):
        page_number += 1
        if page.total is not None:
            result.total = page.total
        for listed in page.rows:
            try:
                saved = core.save_listed(conn, source.code, result.run_id, listed)
            except Exception as exc:  # recorded per trial, the list goes on
                result.failed[listed.key] = f"{type(exc).__name__}: {exc}"
                continue
            result.listed += 1
            if saved.new_trial:
                result.new_trials += 1
            result.possible_duplicates += len(saved.possible_duplicates)
        total_text = "?"
        if result.total is not None:
            total_text = f"{result.total:,}"
        report(progress, f"page {page_number}: {result.listed:,} of {total_text} listed, {result.new_trials:,} new")
        if not page.more:
            result.complete = True
            break
        if _stopping(should_stop):
            result.stopped = True
            report(progress, "stopped: the trials listed so far are kept")
            break
    if result.complete and result.total is not None and result.listed + len(result.failed) < result.total:
        # e.g. a source that answers only so many rows per query: what came is kept, nothing is delisted
        result.complete = False
        result.failed["list"] = (
            f"read {result.listed:,} of the {result.total:,} trials the source lists: "
            "not complete, so no trial is marked as no longer listed"
        )
        report(progress, f"failed: {result.failed['list']}")
    if result.complete and result.listed == 0 and not result.failed:
        # an empty answer (an outage, a query that no longer matches) is never taken as "the source lists
        # nothing": that would mark every trial as no longer listed
        result.complete = False
        result.failed["list"] = "the source listed no trials: not taken as complete, so no trial is delisted"
        report(progress, f"failed: {result.failed['list']}")


# --- 2. evidence -------------------------------------------------------------------------------


@dataclass
class TransferResult:
    run_id: int
    trials: int = 0
    complete: int = 0
    new_trials: int = 0
    documents: int = 0
    new_versions: int = 0
    unchanged: int = 0
    # of the unchanged: held at the version the source lists, so not downloaded (without recheck)
    known: int = 0
    unlinked: int = 0
    pages: int = 0
    possible_duplicates: int = 0
    # every document was downloaded and its bytes compared, whatever its version label
    recheck: bool = False
    stopped: bool = False
    # key -> 'complete' or 'failed'
    statuses: dict[str, str] = field(default_factory=dict)
    # the core.trial ids transferred, in order
    trial_ids: list[int] = field(default_factory=list)
    # "key" or "key/what" -> what went wrong
    failed: dict[str, str] = field(default_factory=dict)

    def counts(self) -> dict:
        return {
            "trials": self.trials,
            "complete": self.complete,
            "new_trials": self.new_trials,
            "documents": self.documents,
            "new_versions": self.new_versions,
            "unchanged": self.unchanged,
            "known": self.known,
            "recheck": self.recheck,
            "unlinked": self.unlinked,
            "pages": self.pages,
            "possible_duplicates": self.possible_duplicates,
            "stopped": self.stopped,
            "failed": len(self.failed),
        }

    def summary(self) -> str:
        return (
            f"transfer run {self.run_id}: {self.complete} of {self.trials} complete, {self.new_trials} new trials, "
            f"documents {self.documents} ({self.new_versions} new versions, {self.unchanged} unchanged{self._known()}, "
            f"{self.unlinked} no longer listed), {self.pages} pages read"
        )

    def _known(self) -> str:
        if self.recheck:
            return " (every document downloaded and compared)"
        if self.known:
            return f" ({self.known} held at their version, not downloaded)"
        return ""


def transfer(
    conn: psycopg.Connection,
    source: RegisteredSource,
    adapter: Adapter,
    keys: Iterable[str],
    *,
    why: str = "chosen",
    progress: Progress = None,
    should_stop: ShouldStop = None,
    extract: Callable[[bytes], list[str]] | None = None,
    recheck: bool = False,
) -> TransferResult:
    """Transfer the evidence of these trials (the source's keys), each once (see the module's
    docstring). `why` says how they were chosen, for the run's record. A document held at the version
    the source lists is not downloaded again, unless `recheck`: then every one is, and its bytes decide."""
    if not adapter.transfers:
        raise ValueError(f"source {source.code} has no evidence to transfer")
    _usable(source, adapter)
    wanted = []
    for key in keys:
        problem = adapter.key_problem(key)
        if problem is not None:
            raise ValueError(problem)
        if key not in wanted:
            wanted.append(key)
    kinds = adapter.evidence_kinds(source.config)
    run_args = {"keys": wanted, "why": why, "config": source.config, "recheck": recheck}
    _hold_source(conn, source)
    try:
        result = TransferResult(run_id=core.start_ingest(conn, source.code, run_args, kind="transfer"), recheck=recheck)
        run_started = time.monotonic()
        # The trial whose files are being read while the next one is fetched: its page text is saved,
        # and its status recorded, once that next trial's record and documents are in
        reading: _Reading | None = None
        try:
            for number, key in enumerate(wanted, start=1):
                if _stopping(should_stop):
                    result.stopped = True
                    report(progress, "stopped: the trials transferred so far are kept")
                    break
                started = time.monotonic()
                with telemetry.tracer.start_as_current_span("ingest.trial") as span:
                    span.set_attribute("trialwatch.source", source.code)
                    span.set_attribute("trialwatch.source.key", key)
                    fetched = _fetch_one(conn, source, adapter, key, kinds, result, recheck)
                if reading is not None:
                    _finish_and_tell(conn, source, kinds, result, reading, len(wanted), progress)
                    reading = None
                if fetched is None:
                    _tell(source, key, number, len(wanted), "failed", started, result, progress)
                else:
                    reading = _Reading(
                        key, number, started, fetched, pages.start_reading(conn, [fetched.trial_id], kinds, extract)
                    )
                _rate(number, len(wanted), run_started, progress)
            if reading is not None:
                _finish_and_tell(conn, source, kinds, result, reading, len(wanted), progress)
                reading = None
            if result.trial_ids:
                _refresh_search_words(conn, result)
        except BaseException as exc:
            # Ctrl-C or a database error: the run is closed as failed (the trials in hand stay failed:
            # marked so before their evidence changed), then the error goes on
            result.failed["run"] = f"interrupted: {type(exc).__name__}: {exc}"
            if reading is not None:
                reading.pages.cancel()
            raise
        finally:
            core.finish_ingest(conn, result.run_id, counts=result.counts(), errors=result.failed)
    finally:
        core.unlock(conn, core.LOCK_SOURCE, source.id)
    return result


def _log_trial(source: str, key: str, status: str, seconds: float, failed: dict[str, str]) -> None:
    problems = []
    for where, message in failed.items():
        if where.startswith(f"{key}/"):
            problems.append(f"{where.removeprefix(key + '/')}: {message}")
    if status == "failed":
        log.warning("transfer %s %s failed in %.0f s: %s", source, key, seconds, "; ".join(problems))
    elif problems:
        log.info("transfer %s %s %s in %.0f s, with warnings: %s", source, key, status, seconds, "; ".join(problems))
    else:
        log.info("transfer %s %s %s in %.0f s", source, key, status, seconds)


@dataclass
class _Reading:
    """A trial fetched and saved whose files are being read; its status comes when they are."""

    key: str
    number: int
    started: float
    fetched: "Saved"
    pages: "pages.Reading | pages.ReadingHere"


def _tell(
    source: RegisteredSource,
    key: str,
    number: int,
    total: int,
    status: str,
    started: float,
    result: TransferResult,
    progress: Progress,
) -> None:
    result.statuses[key] = status
    _log_trial(source.code, key, status, time.monotonic() - started, result.failed)
    report(progress, f"{number}/{total} {key}: {status}")


def _finish_and_tell(
    conn: psycopg.Connection,
    source: RegisteredSource,
    kinds: Sequence[str] | None,
    result: TransferResult,
    reading: _Reading,
    total: int,
    progress: Progress,
) -> None:
    with telemetry.tracer.start_as_current_span("ingest.trial.pages") as span:
        span.set_attribute("trialwatch.source", source.code)
        span.set_attribute("trialwatch.source.key", reading.key)
        status = _finish_one(conn, source, kinds, result, reading)
        span.set_attribute("trialwatch.ingest.status", status)
    _tell(source, reading.key, reading.number, total, status, reading.started, result, progress)


# A long transfer says how it goes every this many trials
RATE_EVERY = 50


def _rate(number: int, total: int, run_started: float, progress: Progress) -> None:
    """Every RATE_EVERY trials: how long a trial takes, and about how long is left."""
    if number % RATE_EVERY != 0 or number >= total:
        return
    per_trial = (time.monotonic() - run_started) / number
    left = per_trial * (total - number)
    report(progress, f"{number}/{total}: {per_trial:.0f} s a trial, about {_duration(left)} left")


def _duration(seconds: float) -> str:
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _fetch_one(
    conn: psycopg.Connection,
    source: RegisteredSource,
    adapter: Adapter,
    key: str,
    kinds: Sequence[str] | None,
    result: TransferResult,
    recheck: bool = False,
) -> "Saved | None":
    """The trial's record and documents, saved; None when even its record could not be (its failed
    transfer recorded then, if the trial is known)."""
    result.trials += 1
    known = core.source_key_known(conn, source.code, key)
    if known:
        core.mark_transfer_started(conn, source=source.code, source_key=key, run_id=result.run_id)
    try:
        fetched = save(conn, source.code, key, adapter, kinds, recheck)
    except Exception as exc:  # save reports errors itself; this keeps the run going if not
        fetched = Saved(trial_id=None, errors={"record": f"{type(exc).__name__}: {exc}"})
    if fetched.trial_id is None:
        for where, message in fetched.errors.items():
            result.failed[f"{key}/{where}"] = message
        if known:
            core.save_transfer(
                conn,
                source=source.code,
                source_key=key,
                run_id=result.run_id,
                documents=0,
                pages=0,
                errors=dict(fetched.errors),
                document_kinds=kinds,
            )
        # else: neither listed nor fetched before, so there is no trial to record the failed transfer on
        return None
    for where, message in fetched.warnings.items():
        result.failed[f"{key}/{where}"] = message
    if fetched.trial_id not in result.trial_ids:
        result.trial_ids.append(fetched.trial_id)
    if fetched.new_trial:
        result.new_trials += 1
    result.possible_duplicates += fetched.possible_duplicates
    result.documents += fetched.documents
    result.new_versions += fetched.new_versions
    result.unchanged += fetched.unchanged
    result.known += fetched.known
    result.unlinked += fetched.unlinked
    return fetched


def _finish_one(
    conn: psycopg.Connection,
    source: RegisteredSource,
    kinds: Sequence[str] | None,
    result: TransferResult,
    reading: _Reading,
) -> str:
    """Save the trial's page text as its reading ends, index it, and record its transfer's status."""
    key = reading.key
    trial_id = reading.fetched.trial_id
    errors = dict(reading.fetched.errors)
    read = reading.pages.finish(conn)
    result.pages += read.pages
    for sha, message in read.failed.items():
        errors[f"file {sha}"] = message
    unread = core.files_without_pages(conn, [trial_id], kinds=kinds)
    if unread and not read.failed:
        errors["pages"] = f"{len(unread)} files without page text"
    problem = _index(conn, trial_id)
    if problem is not None:
        # what search reads is not evidence: reported, the transfer can still complete
        result.failed[f"{key}/search"] = problem
    documents, page_count = core.evidence_counts(conn, trial_id, kinds)
    status = core.save_transfer(
        conn,
        source=source.code,
        source_key=key,
        run_id=result.run_id,
        documents=documents,
        pages=page_count,
        errors=errors,
        document_kinds=kinds,
    )
    for where, message in errors.items():
        result.failed[f"{key}/{where}"] = message
    if status == "complete":
        result.complete += 1
    return status


def _index(conn: psycopg.Connection, trial_id: int) -> str | None:
    """Make the trial findable by patient search now (ADR-008): find where its new sites are and build
    its search document from the profile just stored. Returns what went wrong, if anything."""
    try:
        with conn.transaction():
            geo_load.geocode_missing(conn)
            search_index.rebuild_trial(conn, trial_id)
    except Exception as exc:  # `trialwatch search reindex` builds it later
        return f"{type(exc).__name__}: {exc}"
    return None


def _refresh_search_words(conn: psycopg.Connection, result: TransferResult) -> None:
    """Once a run's trials are indexed: the words "did you mean" offers (it reads every document)."""
    try:
        with conn.transaction():
            search_db.refresh_terms(conn)
    except Exception as exc:
        result.failed["search words"] = f"{type(exc).__name__}: {exc}"


# --- saving one trial --------------------------------------------------------------------------


@dataclass
class Saved:
    """What saving one trial did."""

    # None when even the record could not be fetched or read
    trial_id: int | None
    new_trial: bool = False
    new_record: bool = False
    possible_duplicates: int = 0
    # documents of the wanted kinds the record lists
    documents: int = 0
    new_versions: int = 0
    # documents whose current file we already held, byte for byte or (known) by its version label
    unchanged: int = 0
    known: int = 0
    # links to documents the source no longer lists, now removed
    unlinked: int = 0
    # "record", or a document's key -> what went wrong: the evidence is not complete
    errors: dict[str, str] = field(default_factory=dict)
    # what went wrong besides the evidence (e.g. reading the sponsors): reported, not blocking
    warnings: dict[str, str] = field(default_factory=dict)


def _fetch(source: str, adapter: Adapter, key: str) -> dict:
    """The trial's raw record from its source, counted by how it went (trialwatch.source.fetches)."""
    try:
        payload = adapter.fetch(key)
    except TrialNotFound:
        telemetry.source_fetches.add(1, {"source": source, "outcome": "not_found"})
        raise
    except Exception:
        telemetry.source_fetches.add(1, {"source": source, "outcome": "error"})
        raise
    telemetry.source_fetches.add(1, {"source": source, "outcome": "ok"})
    return payload


def save(
    conn: psycopg.Connection,
    source: str,
    key: str,
    adapter: Adapter,
    kinds: Sequence[str] | None,
    recheck: bool = False,
) -> Saved:
    """Fetch one trial from its source and save what its record says: the record (a new version if it
    changed), its sponsors, sites and profile, and every listed document of these kinds (None: every
    kind); documents of these kinds the source no longer lists are unlinked. A document held at the
    listed version is linked without downloading it, unless `recheck`."""
    try:
        # text fields can hold characters Postgres can't store (a NUL): cleaned before anything else
        payload = storable(_fetch(source, adapter, key))
        listing = core.listing_summary(conn, source, key)
        trial = adapter.parse(payload, listing)
        record = core.save_trial_record(
            conn,
            source=source,
            source_key=key,
            identifiers=trial.identifiers,
            payload=payload,
            registered=trial.registered,
            content_hash=trial.content_hash,
        )
    except Exception as exc:  # recorded on the trial's transfer
        return Saved(trial_id=None, errors={"record": f"{type(exc).__name__}: {exc}"})
    saved = Saved(
        trial_id=record.trial_id,
        new_trial=record.new_trial,
        new_record=record.new_version,
        possible_duplicates=len(record.possible_duplicates),
    )
    for where, message in trial.warnings.items():
        saved.warnings[where] = message
    # sponsors, sites and profile are not evidence: a problem is reported, the transfer can still complete
    if trial.sponsors is not None:
        try:
            core.set_trial_sponsors(conn, record.trial_id, source, trial.sponsors)
        except Exception as exc:
            saved.warnings["sponsors"] = f"{type(exc).__name__}: {exc}"
    if trial.locations is not None:
        try:
            core.set_trial_locations(conn, record.trial_id, source, trial.locations)
        except Exception as exc:
            saved.warnings["locations"] = f"{type(exc).__name__}: {exc}"
    if trial.profile is not None:
        try:
            profile.store(conn, record.trial_id, source, key, trial.profile)
        except Exception as exc:
            saved.warnings["profile"] = f"{type(exc).__name__}: {exc}"
    if trial.documents is None:
        # a record without its list of documents says nothing about them: nothing is unlinked
        saved.errors["documents"] = "the record has no list of documents"
        return saved
    listed = []
    for document in trial.documents:
        if kinds is not None and document.kind not in kinds:
            continue
        saved.documents += 1
        listed.append(document.key)
        if document.problem is not None:
            saved.errors[f"document {document.key}"] = document.problem
            continue
        try:
            _save_document(conn, adapter, source, key, record.trial_id, document, saved, recheck)
            beat(f"{key}: document {document.key}")
        except Exception as exc:  # recorded per document, the others go on
            saved.errors[f"document {document.key}"] = f"{type(exc).__name__}: {exc}"
    saved.unlinked = core.unlink_documents_not_listed(
        conn,
        trial_id=record.trial_id,
        source=source,
        kinds=kinds,
        listed=core.document_ids(conn, source, listed),
    )
    return saved


def _check_file(document: DocumentRef, file: DownloadedFile) -> None:
    if file.size == 0:
        raise ValueError("empty file")
    if document.listed_as_pdf and not file.head(5).startswith(b"%PDF"):
        raise ValueError(f"listed as PDF but is not one (starts {file.head(20)!r})")


def _cut_short(document: DocumentRef, file: DownloadedFile) -> bool:
    """A PDF without its end-of-file marker near its end: most likely cut short on the way."""
    return document.listed_as_pdf and b"%%EOF" not in file.tail(PDF_END_BYTES)


def _download(adapter: Adapter, key: str, document: DocumentRef, saved: Saved) -> DownloadedFile:
    """The document's file, checked; a PDF that looks cut short is downloaded once more, and kept with a
    warning if it still does (pypdf may read it anyway; if not, its pages fail as a broken file)."""
    file = adapter.download(key, document)
    try:
        _check_file(document, file)
        if not _cut_short(document, file):
            return file
    except BaseException:
        file.close()
        raise
    file.close()
    file = adapter.download(key, document)
    try:
        _check_file(document, file)
    except BaseException:
        file.close()
        raise
    if _cut_short(document, file):
        saved.warnings[f"document {document.key}"] = "no %%EOF at its end: the file may be cut short"
    return file


def _save_document(
    conn: psycopg.Connection,
    adapter: Adapter,
    source: str,
    key: str,
    trial_id: int,
    document: DocumentRef,
    saved: Saved,
    recheck: bool = False,
) -> None:
    if not recheck:
        known = core.known_document(
            conn,
            source=source,
            source_key=document.key,
            kind=document.kind,
            title=document.title,
            language=document.language,
            version=document.version,
        )
        if known is not None:
            saved.unchanged += 1
            saved.known += 1
            core.link_document(conn, trial_id=trial_id, document_id=known, basis="listed-by-source", source=source)
            return
    with _download(adapter, key, document, saved) as file:
        stored = core.save_document_version(
            conn,
            source=source,
            source_key=document.key,
            kind=document.kind,
            title=document.title,
            language=document.language,
            version=document.version,
            # read (from its spool) only if the file is not stored yet
            content=lambda: file.content,
            media_type=file.media_type,
            sha256=file.sha256(),
            published_at=document.published_at,
        )
    if stored.new_version:
        saved.new_versions += 1
    else:
        saved.unchanged += 1
    core.link_document(conn, trial_id=trial_id, document_id=stored.document_id, basis="listed-by-source", source=source)
