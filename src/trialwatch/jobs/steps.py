"""What each kind of job does, with its params: the same step whether the worker runs it as a job, the
refresh runs it as one of its steps, or a command (`trialwatch source transfer`, `check evidence`, ...)
runs it here and now. A step writes its progress lines and asks whether to stop through its Context:
the command line's prints them and never stops, a job's (JobContext) writes them to jobs.job_line and
stops for its Stop button (cancel) and the spending cap, asked between trials or pages.

A retry of a job redoes only what is left: a transfer the keys not transferred since the job was
created, a check the trials not judged in the job's runs or whose call failed for a passing reason, a
tagging the trials still untagged.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg

from trialwatch import llm, progress
from trialwatch.checks import evidence
from trialwatch.checks import requirements as requirement_sets
from trialwatch.checks import settings as check_settings
from trialwatch.checks.prompts import load_prompt
from trialwatch.db import core
from trialwatch.db import costs as costs_db
from trialwatch.db import jobs as jobs_db
from trialwatch.db import search as search_db
from trialwatch.db.jobs import Job
from trialwatch.jobs.retry import PermanentError, is_transient
from trialwatch.search import annotate
from trialwatch.sources import profile, registry
from trialwatch.trials import ingest, readiness

log = logging.getLogger("trialwatch.jobs")
CAP_REACHED = "held: this month's spend reached the monthly cap (/admin/spend)"


class Context:
    """What a step talks to: where its progress lines go, and whether to stop. This one is the command
    line's: its lines are printed, and nothing but Ctrl-C stops it."""

    def __init__(self, echo: Callable[[str], None]) -> None:
        self.echo = echo
        # the job it runs for (None from the command line), and which attempt: a retry does what is left
        self.job: Job | None = None
        self.attempt = 1
        # started by TrialWatch itself: a check skips trials not ready instead of refusing them
        self.automatic = False
        self.cancelled = False
        self.hold_reason: str | None = None
        self.watch_cap = False

    def add(self, line: str) -> None:
        self.echo(line)

    def should_stop(self) -> bool:
        return False


class JobContext(Context):
    """A job's: its lines go to jobs.job_line (the job's page follows them), and it stops when a person
    cancels the job or, for TrialWatch's own model calls, when this month's spend reached the cap."""

    def __init__(self, queue: psycopg.Connection, job: Job, capped: bool = False) -> None:
        # the worker's own connection (as trialwatch): lines and the job's flags, not the job's work
        self.queue = queue
        self.job = job
        self.attempt = job.attempts
        self.automatic = job.automatic
        self.cancelled = False
        self.hold_reason = None
        # only automatic model calls (checks, AI tags; jobs/tasks.py says which kinds) are held by the
        # cap; a person's always run. The refresh turns it on before its paid steps.
        self.watch_cap = job.automatic and capped

    def add(self, line: str) -> None:
        progress.beat(line)
        try:
            jobs_db.add_line(self.queue, self.job.id, self.job.attempts, line)
        except psycopg.OperationalError as exc:  # the queue's database went away: the work goes on
            log.warning("job %s: progress line not written (%s): %s", self.job.id, exc, line)

    def should_stop(self) -> bool:
        try:
            return self._should_stop()
        except psycopg.OperationalError as exc:  # can't ask now: carry on, and ask again after the next item
            log.warning("job %s: could not ask whether to stop (%s)", self.job.id, exc)
            return False

    def _should_stop(self) -> bool:
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
    """How a step ended: what it did (jobs.job.result), what failed that is worth another attempt
    (item -> error), and what failed that another attempt can't fix, left to people (a check refused, a
    file the source publishes broken); the result names those too (left_to_people). A job whose items
    failed or were left ends `partial` (jobs/worker.py). `broken` says why the step failed as a whole
    (the source's list could not be read): the job ends `failed`, as if it had raised."""

    result: dict = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    left: dict[str, str] = field(default_factory=dict)
    broken: str | None = None


def _source(conn: psycopg.Connection, code: str) -> core.RegisteredSource:
    source = core.registered_source(conn, code)
    if source is None:
        raise PermanentError(f"no source {code!r} is registered")
    return source


# --- sync ----------------------------------------------------------------------------------------

def run_sync(ctx: Context, conn: psycopg.Connection, params: dict) -> Outcome:
    source = _source(conn, params["source"])
    with registry.open_adapter(source.adapter) as adapter:
        try:
            result = ingest.sync_list(conn, source, adapter, progress=ctx.add, should_stop=ctx.should_stop)
        except ingest.SourceBusy:
            raise
        except ValueError as exc:
            # switched off, or its config is not valid: the same next time
            raise PermanentError(str(exc)) from exc
    ctx.add(result.summary())
    # the list itself, not one of its trials: the sync failed as a whole
    broken = result.failed.get("list")
    return Outcome({"ingest_run": result.run_id, "summary": result.summary(), "listed": result.listed,
                    "new_trials": result.new_trials}, dict(result.failed), broken=broken)


# --- transfer ------------------------------------------------------------------------------------

def transfer_keys(conn: psycopg.Connection, ctx: Context, params: dict) -> list[str]:
    """The keys to transfer: those given, or the source's trials of a kind (pending, failed, ...);
    on a retry, without those transferred since the job was created."""
    earlier = {}
    if ctx.attempt > 1 and ctx.job is not None and ctx.job.result:
        earlier = ctx.job.result
    if params.get("which") and "chosen" in earlier:
        # a retry redoes what its first attempt chose: the trials it transferred no longer count as
        # `which` (pending, stale, ...), so choosing again would take other trials and skip the failures
        keys = list(earlier["chosen"])
    elif params.get("which"):
        try:
            keys = core.keys_to_transfer(conn, params["source"], params["which"])
        except ValueError as exc:
            raise PermanentError(str(exc)) from exc
    else:
        keys = list(params["keys"])
    if params.get("limit") is not None:
        keys = keys[:params["limit"]]
    if ctx.attempt > 1 and keys:
        done = jobs_db.keys_transferred_since(conn, params["source"], keys, ctx.job.created_at)
        left = []
        for key in keys:
            if key not in done:
                left.append(key)
        keys = left
    return keys


def run_transfer(ctx: Context, conn: psycopg.Connection, params: dict) -> Outcome:
    source = _source(conn, params["source"])
    keys = transfer_keys(conn, ctx, params)
    why = "chosen"
    if params.get("which"):
        why = params["which"]
    if ctx.automatic:
        why = f"{why} (job {ctx.job.id})"
    ctx.add(f"{len(keys)} trials to transfer from {source.code} ({why})")
    if not keys:
        return Outcome({"trials": 0})
    with registry.open_adapter(source.adapter) as adapter:
        try:
            result = ingest.transfer(conn, source, adapter, keys, why=why, progress=ctx.add,
                                     should_stop=ctx.should_stop, recheck=bool(params.get("recheck")))
        except ingest.SourceBusy:
            raise
        except ValueError as exc:
            raise PermanentError(str(exc)) from exc
    ctx.add(result.summary())
    for name, error in result.left.items():
        ctx.add(f"  left to a person, {name}: {error}")
    outcome = {"ingest_run": result.run_id, "summary": result.summary(), "trials": result.trials,
               "complete": result.complete, "new_versions": result.new_versions,
               "left_to_people": dict(result.left)}
    if params.get("which"):
        outcome["chosen"] = _chosen_keys(ctx, keys)
    return Outcome(outcome, dict(result.failed), dict(result.left))


def _chosen_keys(ctx: Context, keys: list[str]) -> list[str]:
    """The keys a transfer by `which` chose on its first attempt (kept in its result for a retry)."""
    if ctx.attempt > 1 and ctx.job is not None and ctx.job.result and "chosen" in ctx.job.result:
        return list(ctx.job.result["chosen"])
    return keys


# --- check ---------------------------------------------------------------------------------------

def anthropic_client() -> llm.MessagesClient:
    """The model client (tests replace this)."""
    return llm.client()


def _chosen_trials(conn: psycopg.Connection, ctx: Context, params: dict, requirement_set) -> list[int]:
    """The trials given, or every trial ready for a check of this set; on a retry, only those the job
    has not judged yet or whose call failed for a passing reason."""
    if params.get("every"):
        chosen = readiness.ready_trials(conn, requirement_set.document_kinds)
    else:
        chosen = []
        for trial_id in params["trial_ids"]:
            if trial_id not in chosen:
                chosen.append(trial_id)
    if params.get("limit") is not None:
        chosen = chosen[:params["limit"]]
    if ctx.attempt <= 1:
        return chosen
    judged = jobs_db.trial_errors_of(conn, ctx.job.id)
    left = []
    for trial_id in chosen:
        if trial_id not in judged or is_transient(judged[trial_id]):
            left.append(trial_id)
    return left


def run_check(ctx: Context, conn: psycopg.Connection, params: dict) -> Outcome:
    try:
        settings = check_settings.load(params.get("config", check_settings.DEFAULT))
    except (FileNotFoundError, ValueError) as exc:
        raise PermanentError(str(exc)) from exc
    requirement_set = requirement_sets.load(settings.requirements)
    trial_ids = _chosen_trials(conn, ctx, params, requirement_set)
    # an automatic check skips trials whose evidence changed or broke since they were chosen (the next
    # refresh takes them); a person's check refuses them
    unready = evidence.REFUSE
    if ctx.automatic:
        unready = evidence.SKIP
    prompt = load_prompt(settings.prompt)
    ctx.add(f"{len(trial_ids)} trials · {settings.summary()} (prompt sha256 {prompt.sha256.hex()[:12]})")
    if not trial_ids:
        return Outcome({"trials": 0})
    if params.get("dry_run"):
        if unready == evidence.REFUSE:
            try:
                evidence.refuse_unready(conn, trial_ids, requirement_set)
            except evidence.NotReady as exc:
                raise PermanentError(str(exc)) from exc
        for preview in evidence.preview(conn, trial_ids, settings):
            ctx.add(f"{preview.name}: {len(preview.questions)} questions, {len(preview.pages)} pages "
                    f"({preview.readable} readable)")
            for question in preview.questions:
                ctx.add(f"  {question.key}")
        ctx.add("dry run: nothing was sent")
        return Outcome({"trials": len(trial_ids), "dry_run": True})
    # an automatic check never starts over the cap; it is held before anything is sent
    if ctx.should_stop():
        return Outcome({"trials": 0})
    try:
        summary = evidence.run(conn, anthropic_client(), trial_ids, settings, progress=ctx.add,
                               should_stop=ctx.should_stop, unready=unready)
    except evidence.NotReady as exc:
        raise PermanentError(str(exc)) from exc
    except llm.BAD_CREDENTIALS as exc:
        # the same for every trial and every attempt: someone has to fix the API key
        raise PermanentError(f"{type(exc).__name__}: {exc}") from exc
    if summary.run_id is None:
        return Outcome({"trials": 0, "skipped": len(summary.skipped)})
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
              "cost_usd": str(summary.cost_usd), "cost": llm.dollars(summary.cost_usd)}
    return Outcome(result, passing, left_to_people)


# --- annotate (patient search, ADR-008) ----------------------------------------------------------

def _trials_to_tag(conn: psycopg.Connection, ctx: Context, params: dict, settings: annotate.Settings) -> list[int]:
    """The trials given, or every recruiting trial not tagged for its current profile (with
    `outdated_prompt`, also those tagged by an earlier prompt text); on a retry, only those still untagged."""
    prompt_sha = None
    if params.get("outdated_prompt"):
        prompt_sha = load_prompt(settings.prompt).sha256
    waiting = None
    if params.get("every") or ctx.attempt > 1:
        untagged = search_db.trials_to_annotate(conn, prompt_sha)
        # a trial whose tagging of this profile failed for a lasting reason (a refusal, an invalid answer)
        # would fail the same way and be paid for again: it waits for its profile to change
        failed_before = search_db.annotation_errors(conn, untagged)
        waiting = []
        for trial_id in untagged:
            if trial_id in failed_before and not is_transient(failed_before[trial_id]):
                continue
            waiting.append(trial_id)
    if params.get("every"):
        chosen = waiting
    else:
        chosen = []
        for trial_id in params["trial_ids"]:
            if trial_id in chosen:
                continue
            if waiting is not None and trial_id not in waiting:
                continue
            chosen.append(trial_id)
    if params.get("limit") is not None:
        chosen = chosen[:params["limit"]]
    return chosen


def _show_annotate_request(ctx: Context, conn: psycopg.Connection, settings: annotate.Settings, trial_id: int) -> None:
    """What would be sent for one trial (a dry run)."""
    stored = profile.current(conn, trial_id)
    if stored is None:
        raise PermanentError(f"trial {trial_id} has no profile")
    read = stored.read
    fields = annotate.fields_for(read)
    languages = annotate.languages_for(conn, read, settings.max_languages)
    candidates = annotate.candidates_for(conn, read, settings.max_candidates)
    request = annotate.build_request(settings, load_prompt(settings.prompt), fields, languages, candidates)
    ctx.add(request["messages"][0]["content"])


def run_annotate(ctx: Context, conn: psycopg.Connection, params: dict) -> Outcome:
    """Tag trials with AI for patient search."""
    settings = annotate.load_settings()
    chosen = _trials_to_tag(conn, ctx, params, settings)
    ctx.add(f"{len(chosen)} trials to tag with {settings.model} (effort {settings.effort})")
    if not chosen:
        return Outcome({"trials": 0})
    if params.get("dry_run"):
        _show_annotate_request(ctx, conn, settings, chosen[0])
        ctx.add("dry run: nothing was sent")
        return Outcome({"trials": len(chosen), "dry_run": True})
    # an automatic run never starts over the cap; it is held before anything is sent
    if ctx.should_stop():
        return Outcome({"trials": 0})
    try:
        summary = annotate.run(conn, anthropic_client(), chosen, settings, progress=ctx.add,
                               should_stop=ctx.should_stop)
    except llm.BAD_CREDENTIALS as exc:
        raise PermanentError(f"{type(exc).__name__}: {exc}") from exc
    # only a failure that passes (an outage, a rate limit) is worth another attempt
    passing = {}
    for trial_id, error in summary.failed.items():
        if is_transient(error):
            passing[f"trial {trial_id}"] = error
        else:
            ctx.add(f"  left to a person, trial {trial_id}: {error}")
    ctx.add(f"annotation run {summary.run_id}: {summary.tagged} of {summary.trials} tagged, {summary.kept} tags kept, "
            f"{summary.dropped} dropped, {llm.dollars(summary.cost_usd)}")
    return Outcome({"annotation_run": summary.run_id, "trials": summary.trials, "tagged": summary.tagged,
                    "kept": summary.kept, "dropped": summary.dropped, "cost_usd": str(summary.cost_usd)}, passing)
