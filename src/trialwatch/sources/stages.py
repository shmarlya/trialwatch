"""The two stages of bringing trials in from a registered source (ADR-005), the same for every
adapter, and the rule for which trials a check may read:

1. sync_list: every trial the source lists becomes a core.trial (matched by its source key and
   identifiers, or created), with what the list says about it in core.source_listing. A complete
   pass also marks the trials the source no longer lists. Nothing but the list is fetched.
2. transfer: for chosen trials, the adapter fetches the full record and the configured documents
   (unlinking the ones the source no longer lists); then their page text is read. The trial's
   core.evidence_transfer row says 'complete' only when the record, every configured document and
   every page came in, else 'failed' and why. While a trial is being transferred its row says
   failed, so an interrupted transfer never leaves half-new evidence looking complete. Each trial is
   then indexed for patient search (ADR-008: its new sites geocoded, its search document built from
   the profile the adapter stored), so it is findable the moment it arrives; a failure there is
   reported but leaves the evidence complete.
3. not_ready: a check reads a trial only when every source that holds the trial gave its evidence
   completely and it is still current (a stale transfer is transferred again first), including the
   document kinds the check reads that the source can give at all.

Each stage is one core.ingest_run (kind 'list' or 'transfer') with the config it used, its counts and
errors. Both can be stopped between trials (or list pages): what was done so far is kept. One process
at a time reads a source (an advisory lock), and a run whose process died is closed by the next one.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from trialwatch.db import core
from trialwatch.db import search as search_db
from trialwatch.db.core import EvidenceRow, RegisteredSource
from trialwatch.geo import load as geo_load
from trialwatch.search import index as search_index
from trialwatch.progress import Progress, report
from trialwatch.sources.pages import extract_missing
from trialwatch.sources.registry import Adapter, Fetched, adapter_class

ShouldStop = Callable[[], bool] | None


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
        raise SourceBusy(f"another process is reading source {source.code} (its ingest run is still running); "
                         "try again when it has finished")


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
        return {"listed": self.listed, "new_trials": self.new_trials, "total": self.total,
                "possible_duplicates": self.possible_duplicates, "delisted": self.delisted,
                "complete": self.complete, "stopped": self.stopped, "failed": len(self.failed)}

    def summary(self) -> str:
        state = "complete"
        if self.stopped:
            state = "stopped part-way"
        elif not self.complete:
            state = "not complete"
        return (f"list run {self.run_id} ({state}): {self.listed:,} listed, {self.new_trials:,} new trials, "
                f"{self.delisted:,} no longer listed, possible duplicates {self.possible_duplicates}")


def sync_list(conn: psycopg.Connection, source: RegisteredSource, adapter: Adapter, progress: Progress = None,
              should_stop: ShouldStop = None) -> ListResult:
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


def _read_list(conn: psycopg.Connection, source: RegisteredSource, adapter: Adapter, result: ListResult,
               progress: Progress, should_stop: ShouldStop) -> None:
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
        result.failed["list"] = (f"read {result.listed:,} of the {result.total:,} trials the source lists: "
                                 "not complete, so no trial is marked as no longer listed")
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
    unlinked: int = 0
    pages: int = 0
    possible_duplicates: int = 0
    stopped: bool = False
    # key -> 'complete' or 'failed'
    statuses: dict[str, str] = field(default_factory=dict)
    # the core.trial ids transferred, in order
    trial_ids: list[int] = field(default_factory=list)
    # "key" or "key/what" -> what went wrong
    failed: dict[str, str] = field(default_factory=dict)

    def counts(self) -> dict:
        return {"trials": self.trials, "complete": self.complete, "new_trials": self.new_trials,
                "documents": self.documents, "new_versions": self.new_versions,
                "unchanged": self.unchanged, "unlinked": self.unlinked, "pages": self.pages,
                "possible_duplicates": self.possible_duplicates, "stopped": self.stopped,
                "failed": len(self.failed)}

    def summary(self) -> str:
        return (f"transfer run {self.run_id}: {self.complete} of {self.trials} complete, {self.new_trials} new trials, "
                f"documents {self.documents} ({self.new_versions} new versions, {self.unchanged} unchanged, "
                f"{self.unlinked} no longer listed), {self.pages} pages read")


def transfer(conn: psycopg.Connection, source: RegisteredSource, adapter: Adapter, keys: Iterable[str], *,
             why: str = "chosen", files_dir: Path | None = None, progress: Progress = None,
             should_stop: ShouldStop = None, extract: Callable[[bytes], list[str]] | None = None) -> TransferResult:
    """Transfer the evidence of these trials (the source's keys), each once (see the module's
    docstring). `why` says how they were chosen, for the run's record."""
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
    run_args = {"keys": wanted, "why": why, "config": source.config}
    if files_dir is not None:
        run_args["files_dir"] = str(files_dir)
    _hold_source(conn, source)
    try:
        result = TransferResult(run_id=core.start_ingest(conn, source.code, run_args, kind="transfer"))
        try:
            for number, key in enumerate(wanted, start=1):
                if _stopping(should_stop):
                    result.stopped = True
                    report(progress, "stopped: the trials transferred so far are kept")
                    break
                status = _transfer_one(conn, source, adapter, key, kinds, files_dir, extract, result)
                result.statuses[key] = status
                report(progress, f"{number}/{len(wanted)} {key}: {status}")
            if result.trial_ids:
                _refresh_search_words(conn, result)
        except BaseException as exc:
            # Ctrl-C or a database error: the run is closed as failed (the trial in hand stays failed),
            # then the error goes on
            result.failed["run"] = f"interrupted: {type(exc).__name__}: {exc}"
            raise
        finally:
            core.finish_ingest(conn, result.run_id, counts=result.counts(), errors=result.failed)
    finally:
        core.unlock(conn, core.LOCK_SOURCE, source.id)
    return result


def _transfer_one(conn: psycopg.Connection, source: RegisteredSource, adapter: Adapter, key: str,
                  kinds: Sequence[str] | None, files_dir: Path | None, extract: Callable[[bytes], list[str]] | None,
                  result: TransferResult) -> str:
    result.trials += 1
    known = core.source_key_known(conn, source.code, key)
    if known:
        core.mark_transfer_started(conn, source=source.code, source_key=key, run_id=result.run_id)
    try:
        fetched = adapter.fetch(conn, source.code, key, source.config, files_dir)
    except Exception as exc:  # an adapter should report errors itself; this keeps the run going if not
        fetched = Fetched(trial_id=None, errors={"record": f"{type(exc).__name__}: {exc}"})
    errors = {}
    for where, message in fetched.errors.items():
        errors[where] = message
    if fetched.trial_id is None:
        for where, message in errors.items():
            result.failed[f"{key}/{where}"] = message
        if known:
            core.save_transfer(conn, source=source.code, source_key=key, run_id=result.run_id, documents=0, pages=0,
                               errors=errors, document_kinds=kinds)
        # else: neither listed nor fetched before, so there is no trial to record the failed transfer on
        return "failed"
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
    result.unlinked += fetched.unlinked
    pages = extract_missing(conn, [fetched.trial_id], kinds=kinds, extract=extract)
    result.pages += pages.pages
    for sha, message in pages.failed.items():
        errors[f"file {sha}"] = message
    unread = core.files_without_pages(conn, [fetched.trial_id], kinds=kinds)
    if unread and not pages.failed:
        errors["pages"] = f"{len(unread)} files without page text"
    problem = _index(conn, fetched.trial_id)
    if problem is not None:
        # what search reads is not evidence: reported, the transfer can still complete
        result.failed[f"{key}/search"] = problem
    documents, page_count = core.evidence_counts(conn, fetched.trial_id, kinds)
    status = core.save_transfer(conn, source=source.code, source_key=key, run_id=result.run_id,
                                documents=documents, pages=page_count, errors=errors, document_kinds=kinds)
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


# --- 3. which trials a check may read ----------------------------------------------------------

def _kinds_provided(adapter: str) -> tuple[str, ...] | None:
    """The document kinds a source read by this adapter can give (None: any kind, or unknown)."""
    try:
        return adapter_class(adapter).document_kinds
    except ValueError:
        return None


def _kinds_missing(row: EvidenceRow, kinds: Sequence[str]) -> list[str]:
    """The kinds a check reads that this source can give but its last transfer did not bring."""
    if row.document_kinds is None:
        return []
    provided = _kinds_provided(row.adapter)
    missing = []
    for kind in kinds:
        if provided is not None and kind not in provided:
            continue
        if kind not in row.document_kinds:
            missing.append(kind)
    return missing


def _why_not_ready(rows: list[EvidenceRow], kinds: Sequence[str]) -> str | None:
    if not rows:
        return "no source has given its evidence"
    problems = []
    for row in rows:
        if row.status == "not transferred":
            problems.append(f"{row.source}: evidence not transferred")
        elif row.status == "failed":
            problems.append(f"{row.source}: its last transfer failed")
        elif row.status == "stale":
            problems.append(f"{row.source}: changed since its transfer, transfer it again")
        else:
            missing = _kinds_missing(row, kinds)
            if missing:
                problems.append(f"{row.source}: its transfer did not bring {', '.join(missing)}")
    if problems:
        return "; ".join(problems)
    return None


def not_ready(conn: psycopg.Connection, trial_ids: Sequence[int], kinds: Sequence[str]) -> dict[int, str]:
    """Why each of these trials may not be checked yet by a check that reads these document kinds;
    the trials not named may be (see the module's docstring)."""
    rows_by_trial: dict[int, list[EvidenceRow]] = {}
    for row in core.evidence_rows(conn, trial_ids):
        if row.trial_id not in rows_by_trial:
            rows_by_trial[row.trial_id] = []
        rows_by_trial[row.trial_id].append(row)
    reasons = {}
    for trial_id in trial_ids:
        rows = []
        if trial_id in rows_by_trial:
            rows = rows_by_trial[trial_id]
        reason = _why_not_ready(rows, kinds)
        if reason is not None:
            reasons[trial_id] = reason
    return reasons


def ready_trials(conn: psycopg.Connection, kinds: Sequence[str]) -> list[int]:
    """Every trial a check reading these document kinds may read now: one with page text in linked
    documents of those kinds, and nothing in not_ready."""
    candidates = core.trials_with_pages(conn, kinds)
    blocked = not_ready(conn, candidates, kinds)
    ready = []
    for trial_id in candidates:
        if trial_id not in blocked:
            ready.append(trial_id)
    return ready
