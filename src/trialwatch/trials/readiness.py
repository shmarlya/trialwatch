"""Which trials a check may read (ADR-005): a trial whose evidence every source that holds it gave
completely and is still current (a stale transfer is transferred again first), including the document
kinds the check reads that the source can give at all."""

from collections.abc import Sequence

import psycopg

from trialwatch.db import core
from trialwatch.db.core import EvidenceRow
from trialwatch.sources.registry import adapter_class


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
    the trials not named may be."""
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
