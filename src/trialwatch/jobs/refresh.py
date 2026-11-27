"""The refresh (a job of kind refresh, started by the refresh schedule as TrialWatch itself): keeps
every trial's evidence, AI tags and judgement current without anyone starting it. One job, step after
step, each the same step a job of its own kind runs (jobs/steps.py):

1. sync every switched-on source that has a list
2. transfer the trials each source lists that are new, changed since their transfer (stale) or whose
   last transfer failed, the ones the list calls recruiting first (patient search, ADR-008); then OCR
   for files read before OCR ran (trials/ocr.py), for up to TRIALWATCH_OCR_MINUTES_PER_REFRESH, free
3. annotate the recruiting trials whose profile has no AI tags yet (new, or changed since). A changed
   prompt does not tag every trial again: `trialwatch search annotate --all --outdated-prompt`.
4. check the trials ready for a check that were never judged, whose judgement is outdated (a record or
   document it read changed, or OCR read more of it), or that were judged under other rules or another
   prompt than the current ones (checks/requirement_sets, checks/prompts: a change to either is checked
   again everywhere, undecided judgements first). A changed model or setup is not: that is an experiment
   a person runs (`check evidence --config`). Nothing changed: no check, no money spent.

Steps 3 and 4 call a paid model: the monthly spending cap holds the refresh before them, and it carries
on when the cap allows. A step that fails does not stop the others: its failure fails the attempt, and
the refresh is tried again later. Each step picks its work from what is stored now, so a new attempt
carries on where the last one stopped (a transferred trial is no longer stale, a tagged or judged one
no longer waits), and a source synced in an earlier attempt is not listed again.
"""

import traceback
from collections.abc import Callable

import psycopg

from trialwatch import config

from trialwatch.checks import requirements as requirement_sets
from trialwatch.checks import settings as check_settings
from trialwatch.checks.prompts import load_prompt
from trialwatch.db import checks as checks_db
from trialwatch.db import core
from trialwatch.jobs import steps
from trialwatch.jobs.retry import PermanentError, is_transient
from trialwatch.jobs.steps import Context, Outcome
from trialwatch.sources import registry
from trialwatch.trials import ocr, pages, readiness

# Which trials of a source it transfers (core.TRANSFER_CHOICES)
TRANSFER_WHICH = ("pending", "stale", "failed")

Step = Callable[[Context, psycopg.Connection, dict], Outcome]


def _sources(conn: psycopg.Connection, capability: str) -> list[core.RegisteredSource]:
    """The switched-on sources whose adapter can do this ("lists" or "transfers")."""
    found = []
    for source in core.registered_sources(conn):
        if not source.enabled or not source.adapter:
            continue
        adapter_class = registry.adapter_class(source.adapter)
        if getattr(adapter_class, capability):
            found.append(source)
    return found


def _transfer(ctx: Context, conn: psycopg.Connection, params: dict) -> Outcome:
    source = params["source"]
    # recruiting trials first (patient search, ADR-008)
    keys = core.keys_to_transfer_any(conn, source, TRANSFER_WHICH)
    ctx.add(f"{len(keys)} trials of {source} are {' or '.join(TRANSFER_WHICH)}")
    return steps.run_transfer(ctx, conn, {"source": source, "keys": keys})


def _ocr(ctx: Context, conn: psycopg.Connection, params: dict) -> Outcome:
    """OCR for the files read before OCR ran, of the kinds the check reads, for up to
    config.ocr_minutes_per_refresh(): the rest wait for the next refresh. Free: no model is called."""
    if not ocr.ocr_available():
        ctx.add("tesseract is not installed here: no OCR")
        return Outcome({"files": 0, "skipped": "tesseract not installed"})
    settings = check_settings.load(params["config"])
    kinds = requirement_sets.load(settings.requirements).document_kinds
    found = pages.ocr_missing(conn, None, kinds, progress=ctx.add, seconds=config.ocr_minutes_per_refresh() * 60,
                              should_stop=ctx.should_stop)
    ctx.add(f"{found.files} files looked at: {found.image_pages} picture pages, {found.read_pages} read by OCR; "
            f"page text changed in {len(found.trial_ids)} trials; {found.left} files left for a later refresh")
    return Outcome({"files": found.files, "picture_pages": found.image_pages, "read_pages": found.read_pages,
                    "trials_changed": len(found.trial_ids), "left": found.left}, dict(found.failed))


def _check(ctx: Context, conn: psycopg.Connection, params: dict) -> Outcome:
    config_name = params["config"]
    settings = check_settings.load(config_name)
    requirement_set = requirement_sets.load(settings.requirements)
    ready = readiness.ready_trials(conn, requirement_set.document_kinds)
    open_labels = [label.name for label in requirement_set.labels if label.means == "open"]
    needed = checks_db.trials_needing_check(conn, ready, requirement_set.name, requirement_set.text,
                                            load_prompt(settings.prompt).sha256, open_labels)
    ctx.add(f"of {len(ready)} trials ready for a check: {needed.summary()}")
    if ocr.ocr_available():
        # a trial judged under other rules whose files OCR has yet to look at waits for it: checked now, it
        # would be checked again (and paid for again) once OCR reads its pictures
        waiting = core.trials_waiting_for_ocr(conn, needed.other_rules_undecided + needed.other_rules_decided,
                                              requirement_set.document_kinds, config.ocr_max_pages(),
                                              ocr.MIN_LETTERS)
        if waiting:
            needed.other_rules_undecided = [t for t in needed.other_rules_undecided if t not in waiting]
            needed.other_rules_decided = [t for t in needed.other_rules_decided if t not in waiting]
            ctx.add(f"{len(waiting)} judged under other rules wait for OCR of their files (a later refresh)")
    # what was never judged or changed first, then what earlier rules left undecided: the spending cap
    # may stop the refresh partway, and the next one carries on
    needing = needed.ordered()
    # a check that failed for a lasting reason (a refusal, an answer cut short) on evidence that has not
    # changed since would fail the same way and be paid for again: it stays for a person
    failed_before = checks_db.failed_on_unchanged_evidence(conn, needing, requirement_set.name)
    changed = []
    for trial_id in needing:
        if trial_id in failed_before and not is_transient(failed_before[trial_id]):
            continue
        changed.append(trial_id)
    left = len(needing) - len(changed)
    ctx.add(f"{len(changed)} of {len(ready)} trials ready for a check need one"
            f" ({left} left to a person: their last check failed on the same evidence)")
    outcome = steps.run_check(ctx, conn, {"trial_ids": changed, "config": config_name})
    outcome.result["ready"] = len(ready)
    return outcome


def _run(ctx: Context, conn: psycopg.Connection, name: str, step: Step, params: dict, result: dict,
         failed: dict[str, str], left: dict[str, str] | None = None) -> bool:
    """Run one step, as the refresh (its runs name the refresh's job, its retries are the refresh's).
    What failed in it is one of the refresh's failures, not the end of the refresh; what it left to
    people is the refresh's too (`left`). Its result goes in the refresh's under `name`; True when
    nothing in it failed."""
    ctx.add(f"— {name}")
    try:
        outcome = step(ctx, conn, params)
    except PermanentError as exc:
        error = str(exc)
    except Exception as exc:  # a network error, a busy source: tried again with the refresh
        error = f"{type(exc).__name__}: {exc}"
        ctx.add(traceback.format_exc())
    else:
        result[name] = outcome.result
        for item, item_error in outcome.failed.items():
            failed[f"{name}, {item}"] = item_error
        if left is not None:
            for item, item_error in outcome.left.items():
                left[f"{name}, {item}"] = item_error
        return not outcome.failed
    ctx.add(f"{name} failed: {error}")
    failed[name] = error
    return False


def run_refresh(ctx: Context, conn: psycopg.Connection, params: dict) -> Outcome:
    result = {}
    failed: dict[str, str] = {}
    left: dict[str, str] = {}
    # the sources an earlier attempt of this refresh synced without a failure
    earlier = {}
    if ctx.job is not None:
        earlier = ctx.job.result
    synced = list(earlier.get("synced", []))
    result["synced"] = synced
    for source in _sources(conn, "lists"):
        if ctx.should_stop():
            return Outcome(result, failed, left)
        name = f"sync {source.code}"
        if source.code in synced:
            ctx.add(f"— {name}: synced in an earlier attempt")
            continue
        if _run(ctx, conn, name, steps.run_sync, {"source": source.code}, result, failed):
            synced.append(source.code)
    for source in _sources(conn, "transfers"):
        if ctx.should_stop():
            return Outcome(result, failed, left)
        _run(ctx, conn, f"transfer {source.code}", _transfer, {"source": source.code}, result, failed, left)
    # OCR before the checks, so they read what it finds (free: no model is called)
    if ctx.should_stop():
        return Outcome(result, failed, left)
    config_name = params.get("config", check_settings.DEFAULT)
    _run(ctx, conn, "ocr", _ocr, {"config": config_name}, result, failed, left)
    # the steps that call a paid model: an automatic refresh is held by the spending cap from here on
    ctx.watch_cap = ctx.automatic
    if ctx.should_stop():
        return Outcome(result, failed, left)
    _run(ctx, conn, "annotate", steps.run_annotate, {"every": True}, result, failed, left)
    if ctx.should_stop():
        return Outcome(result, failed, left)
    _run(ctx, conn, "check", _check, {"config": config_name}, result, failed, left)
    return Outcome(result, failed, left)
