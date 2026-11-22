"""What each kind of job does: the same functions as the CLI commands of the workflow steps, with the
job's params, its progress written to jobs.job_line and its Stop button (cancel) and the spending cap
asked between trials or pages.

A retry redoes only what is left: a transfer the keys not transferred since the job was created, a
check the trials not judged in the job's runs or whose call failed for a passing reason.
"""

from dataclasses import dataclass, field

import anthropic
import psycopg

from trialwatch import costs as pricing
from trialwatch.checks import evidence
from trialwatch.checks import requirements as requirement_sets
from trialwatch.checks import settings as check_settings
from trialwatch.checks.model import load_prompt
from trialwatch.checks.verify import has_readable_text
from trialwatch.db import core
from trialwatch.db import costs as costs_db
from trialwatch.db import jobs as jobs_db
from trialwatch.db import search as search_db
from trialwatch.db.jobs import Job
from trialwatch.jobs import CAPPED_KINDS
from trialwatch.jobs.retry import PermanentError, is_transient
from trialwatch.search import annotate
from trialwatch.sources import registry, stages

# Calls go to Anthropic's API itself, as from the CLI
ANTHROPIC_API = "https://api.anthropic.com"
CAP_REACHED = "held: this month's spend reached the monthly cap (/admin/spend)"

STEPS = {
    "sync": "Sync a source's list (source sync)",
    "transfer": "Transfer evidence (source transfer)",
    "check": "Evidence check (check evidence)",
    "annotate": "AI tags for patient search (search annotate)",
    "refresh": "Weekly refresh: sync, transfer what changed, check what changed",
}


class Context:
    """What a running job's step talks to: progress lines, and whether to stop (a person cancelled it,
    or an automatic check reached the spending cap)."""

    def __init__(self, queue: psycopg.Connection, job: Job) -> None:
        # the worker's own connection (as trialwatch): lines and the job's flags, not the job's work
        self.queue = queue
        self.job = job
        self.cancelled = False
        self.hold_reason: str | None = None
        # only automatic model calls (checks, AI tags) are held by the cap; a person's always run
        self.watch_cap = job.automatic and job.kind in CAPPED_KINDS

    def add(self, line: str) -> None:
        jobs_db.add_line(self.queue, self.job.id, self.job.attempts, line)

    def should_stop(self) -> bool:
        if jobs_db.cancel_requested(self.queue, self.job.id):
            if not self.cancelled:
                self.add("cancel asked: stopping after the current item")
            self.cancelled = True
            return True
        if self.watch_cap and costs_db.cap_reached(self.queue):
            if self.hold_reason is None:
                self.add(CAP_REACHED)
            self.hold_reason = CAP_REACHED
            return True
        return False


@dataclass
class Outcome:
    """How a step ended: what it did (jobs.job.result), what failed (item -> error; worth another
    attempt unless final), and whether it now waits for child jobs it added."""

    result: dict = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    wait: bool = False
    # its failures were tried already (a refresh's failed children): the job fails without a retry
    final: bool = False


# --- checking params before a job is queued ------------------------------------------------------

def params_problem(kind: str, params: dict) -> str | None:
    """Why a job of `kind` can't run with these params, or None. Checked when it is queued (so the
    form says so at once) and again when it runs."""
    if kind not in STEPS:
        return f"unknown kind of job {kind!r}"
    limit = params.get("limit")
    if limit is not None and limit < 1:
        return "the limit must be 1 or more"
    if kind in ("sync", "transfer") and not params.get("source"):
        return "choose a source"
    if kind == "transfer":
        keys = params.get("keys")
        which = params.get("which")
        if keys and which:
            return "choose trials, or which of the source's trials, not both"
        if not keys and not which:
            return "choose trials, or which of the source's trials to transfer"
        if which and which not in core.TRANSFER_CHOICES:
            return f"choose one of {', '.join(core.TRANSFER_CHOICES)}"
    if kind == "annotate" and not params.get("trial_ids") and not params.get("every"):
        return "choose trials, or every recruiting trial not tagged yet"
    if kind == "check":
        if not params.get("trial_ids") and not params.get("every"):
            return "choose trials, or tick 'every trial ready for a check'"
        try:
            check_settings.load(params.get("config", check_settings.DEFAULT))
        except (FileNotFoundError, ValueError) as exc:
            return str(exc)
    return None


def _source(conn: psycopg.Connection, code: str) -> core.RegisteredSource:
    source = core.registered_source(conn, code)
    if source is None:
        raise PermanentError(f"no source {code!r} is registered")
    return source


# --- sync ----------------------------------------------------------------------------------------

def run_sync(ctx: Context, conn: psycopg.Connection, job: Job) -> Outcome:
    source = _source(conn, job.params["source"])
    with registry.open_adapter(source.adapter) as adapter:
        try:
            result = stages.sync_list(conn, source, adapter, progress=ctx.add, should_stop=ctx.should_stop)
        except stages.SourceBusy:
            raise
        except ValueError as exc:
            # switched off, or its config is not valid: the same next time
            raise PermanentError(str(exc)) from exc
    ctx.add(result.summary())
    return Outcome({"ingest_run": result.run_id, "summary": result.summary(), "listed": result.listed,
                    "new_trials": result.new_trials}, dict(result.failed))


# --- transfer ------------------------------------------------------------------------------------

def transfer_keys(conn: psycopg.Connection, job: Job) -> list[str]:
    """The keys to transfer: those given, or the source's trials of a kind (pending, failed, ...);
    on a retry, without those transferred since the job was created."""
    params = job.params
    if params.get("which"):
        keys = core.keys_to_transfer(conn, params["source"], params["which"])
    else:
        keys = list(params["keys"])
    if params.get("limit") is not None:
        keys = keys[:params["limit"]]
    if job.attempts > 1 and keys:
        done = jobs_db.keys_transferred_since(conn, params["source"], keys, job.created_at)
        left = []
        for key in keys:
            if key not in done:
                left.append(key)
        keys = left
    return keys


def run_transfer(ctx: Context, conn: psycopg.Connection, job: Job) -> Outcome:
    source = _source(conn, job.params["source"])
    keys = transfer_keys(conn, job)
    why = "chosen"
    if job.params.get("which"):
        why = job.params["which"]
    if job.automatic:
        why = f"{why} (job {job.id})"
    ctx.add(f"{len(keys)} trials to transfer from {source.code} ({why})")
    if not keys:
        return Outcome({"trials": 0})
    with registry.open_adapter(source.adapter) as adapter:
        try:
            result = stages.transfer(conn, source, adapter, keys, why=why, progress=ctx.add,
                                     should_stop=ctx.should_stop)
        except stages.SourceBusy:
            raise
        except ValueError as exc:
            raise PermanentError(str(exc)) from exc
    ctx.add(result.summary())
    return Outcome({"ingest_run": result.run_id, "summary": result.summary(), "trials": result.trials,
                    "complete": result.complete, "new_versions": result.new_versions}, dict(result.failed))


# --- check ---------------------------------------------------------------------------------------

def anthropic_client():
    """The model client (tests replace this)."""
    return anthropic.Anthropic(base_url=ANTHROPIC_API, max_retries=4)


def _chosen_trials(conn: psycopg.Connection, job: Job, requirement_set) -> list[int]:
    """The trials given, or every trial ready for a check of this set; on a retry, only those the job
    has not judged yet or whose call failed for a passing reason."""
    if job.params.get("every"):
        chosen = stages.ready_trials(conn, requirement_set.document_kinds)
    else:
        chosen = []
        for trial_id in job.params["trial_ids"]:
            if trial_id not in chosen:
                chosen.append(trial_id)
    if job.params.get("limit") is not None:
        chosen = chosen[:job.params["limit"]]
    if job.attempts <= 1:
        return chosen
    judged = jobs_db.trial_errors_of(conn, job.id)
    left = []
    for trial_id in chosen:
        if trial_id not in judged or is_transient(judged[trial_id]):
            left.append(trial_id)
    return left


def _without_unready(ctx: Context, conn: psycopg.Connection, trial_ids: list[int], requirement_set) -> list[int]:
    """An automatic check skips trials whose evidence changed or broke since they were chosen (the next
    refresh takes them); a person's check refuses them, as on the command line."""
    reasons = stages.not_ready(conn, trial_ids, requirement_set.document_kinds)
    if not reasons:
        return trial_ids
    if not ctx.job.automatic:
        try:
            evidence.refuse_unready(conn, trial_ids, requirement_set)
        except evidence.NotReady as exc:
            raise PermanentError(str(exc)) from exc
    names = core.trial_names(conn, list(reasons))
    ready = []
    for trial_id in trial_ids:
        if trial_id in reasons:
            ctx.add(f"skipped {names[trial_id]}: {reasons[trial_id]}")
        else:
            ready.append(trial_id)
    return ready


def run_check(ctx: Context, conn: psycopg.Connection, job: Job) -> Outcome:
    params = job.params
    try:
        settings = check_settings.load(params.get("config", check_settings.DEFAULT))
    except (FileNotFoundError, ValueError) as exc:
        raise PermanentError(str(exc)) from exc
    prompt = load_prompt(settings.prompt)
    requirement_set = requirement_sets.load(settings.requirements)
    trial_ids = _chosen_trials(conn, job, requirement_set)
    trial_ids = _without_unready(ctx, conn, trial_ids, requirement_set)
    ctx.add(f"{len(trial_ids)} trials · {settings.summary()}")
    if not trial_ids:
        return Outcome({"trials": 0})
    if params.get("dry_run"):
        names = core.trial_names(conn, trial_ids)
        for trial_id in trial_ids:
            questions, pages = evidence.preview_trial(conn, trial_id, requirement_set)
            readable = 0
            for page in pages:
                if has_readable_text(page):
                    readable += 1
            ctx.add(f"{names[trial_id]}: {len(questions)} questions, {len(pages)} pages ({readable} readable)")
        ctx.add("dry run: nothing was sent")
        return Outcome({"trials": len(trial_ids), "dry_run": True})
    # an automatic check never starts over the cap; it is held before anything is sent
    if ctx.should_stop():
        return Outcome({"trials": 0})
    try:
        summary = evidence.run(conn, anthropic_client(), trial_ids, settings, prompt, progress=ctx.add,
                               requirement_set=requirement_set, should_stop=ctx.should_stop)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        # the same for every trial and every attempt: someone has to fix the API key
        raise PermanentError(f"{type(exc).__name__}: {exc}") from exc
    ctx.add(summary.summary())
    passing = {}
    left_to_people = {}
    for name, error in summary.failed.items():
        if is_transient(error):
            passing[name] = error
        else:
            left_to_people[name] = error
    for name, error in left_to_people.items():
        ctx.add(f"  left to a person, {name}: {error}")
    result = {"check_run": summary.run_id, "summary": summary.summary(), "trials": len(trial_ids),
              "labels": summary.labels, "left_to_people": left_to_people, "published": len(summary.published),
              "cost_usd": str(summary.cost_usd), "cost": pricing.dollars(summary.cost_usd)}
    return Outcome(result, passing)


# --- annotate (patient search, ADR-008) ----------------------------------------------------------

def run_annotate(ctx: Context, conn: psycopg.Connection, job: Job) -> Outcome:
    """Tag the trials given (or every recruiting trial not tagged for its current profile) with AI;
    on a retry, only those still untagged."""
    if job.params.get("every") or job.attempts > 1:
        waiting = search_db.trials_to_annotate(conn)
    else:
        waiting = None
    chosen = []
    if job.params.get("every"):
        chosen = waiting
    else:
        for trial_id in job.params["trial_ids"]:
            if trial_id in chosen:
                continue
            if waiting is not None and trial_id not in waiting:
                continue
            chosen.append(trial_id)
    if job.params.get("limit") is not None:
        chosen = chosen[:job.params["limit"]]
    ctx.add(f"{len(chosen)} trials to tag")
    if not chosen:
        return Outcome({"trials": 0})
    # an automatic run never starts over the cap; it is held before anything is sent
    if ctx.should_stop():
        return Outcome({"trials": 0})
    try:
        summary = annotate.run(conn, anthropic_client(), chosen, progress=ctx.add, should_stop=ctx.should_stop)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise PermanentError(f"{type(exc).__name__}: {exc}") from exc
    passing = {}
    for trial_id, error in summary.failed.items():
        passing[f"trial {trial_id}"] = error
    ctx.add(f"annotation run {summary.run_id}: {summary.tagged} of {summary.trials} tagged, {summary.kept} tags kept, "
            f"{summary.dropped} dropped, {pricing.dollars(summary.cost_usd)}")
    return Outcome({"annotation_run": summary.run_id, "trials": summary.trials, "tagged": summary.tagged,
                    "kept": summary.kept, "dropped": summary.dropped, "cost_usd": str(summary.cost_usd)}, passing)
