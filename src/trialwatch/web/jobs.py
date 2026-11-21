"""Workflow steps started from the web interface, run one at a time in a background thread: sync a
source's list, transfer trials' evidence, check trials (ADR-005).

Each job calls the same functions as the CLI command of that step, on its own database connection,
as the person running the web interface (core.actor), and collects their progress lines so the
browser can show them while the job runs. Jobs live in memory only: what they produce is in the
database (ingest runs, documents, pages, check runs with their traces), as with the CLI. A job that
dies with the server (a restart, --reload) leaves its run marked running; the next run closes it
(the run's advisory lock tells a live run from a dead one), and a source is read by one process at a
time, whether a job or the CLI.
"""

import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone

from trialwatch import db
from trialwatch.checks import evidence
from trialwatch.checks import requirements as requirement_sets
from trialwatch.checks import settings as check_settings
from trialwatch.checks.model import load_prompt
from trialwatch.checks.verify import has_readable_text
from trialwatch.db import core
from trialwatch.sources import registry, stages

# Calls go to Anthropic's API itself, as from the CLI
ANTHROPIC_API = "https://api.anthropic.com"

STEPS = {
    "list": "Sync a source's list (source sync)",
    "transfer": "Transfer evidence (source transfer)",
    "evidence": "Evidence check (check evidence)",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class JobRequest:
    """What the form asked for."""

    step: str
    # list and transfer: the registered source (core.source code)
    source: str = ""
    # transfer: the source's keys of the chosen trials (e.g. EU CT numbers), or which of its trials
    # (core.TRANSFER_CHOICES: pending, failed, stale, listed)
    keys: list[str] = field(default_factory=list)
    which: str = ""
    # core.trial ids, for checking trials chosen in the interface (e.g. "check again" on a trial page)
    trial_ids: list[int] = field(default_factory=list)
    # evidence: every trial ready for a check
    every: bool = False
    limit: int | None = None
    # evidence: the setup the check runs with (a file in checks/configs/)
    config: str = check_settings.DEFAULT
    dry_run: bool = False


@dataclass
class Job:
    id: int
    request: JobRequest
    actor: str
    status: str = "running"  # running | succeeded | failed | stopped
    lines: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=now)
    finished_at: datetime | None = None
    # the evidence run or ingest run this job created, to link to it
    run_id: int | None = None
    # set by the Stop button: the job stops after the trial (or list page) it is on
    stop_requested: bool = False

    def add(self, line: str) -> None:
        self.lines.append(line)

    def should_stop(self) -> bool:
        return self.stop_requested


class JobBusy(Exception):
    pass


class Jobs:
    """The jobs of this server process. Only one runs at a time: CTIS asks for polite, one-at-a-time
    requests, and two model runs at once would only make their costs harder to follow."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: list[Job] = []

    def all(self) -> list[Job]:
        newest_first = []
        for job in reversed(self._jobs):
            newest_first.append(job)
        return newest_first

    def get(self, job_id: int) -> Job | None:
        for job in self._jobs:
            if job.id == job_id:
                return job
        return None

    def running(self) -> Job | None:
        for job in self._jobs:
            if job.status == "running":
                return job
        return None

    def stop(self, job_id: int) -> bool:
        """Ask a running job to stop; False when it is not running."""
        job = self.get(job_id)
        if job is None or job.status != "running":
            return False
        job.stop_requested = True
        job.add("stop asked: finishing the current trial")
        return True

    def start(self, request: JobRequest) -> Job:
        """Check the request, then run it in a background thread. Raises ValueError or JobBusy."""
        check_request(request)
        with self._lock:
            busy = self.running()
            if busy is not None:
                raise JobBusy(f"job {busy.id} ({STEPS[busy.request.step]}) is still running")
            job = Job(id=len(self._jobs) + 1, request=request, actor=db.default_actor())
            self._jobs.append(job)
        thread = threading.Thread(target=run_job, args=(job,), name=f"job-{job.id}", daemon=True)
        thread.start()
        return job


def check_request(request: JobRequest) -> None:
    if request.step not in STEPS:
        raise ValueError(f"unknown step {request.step!r}")
    if request.limit is not None and request.limit < 1:
        raise ValueError("the limit must be 1 or more")
    if request.step in ("list", "transfer") and not request.source:
        raise ValueError("choose a source")
    if request.step == "transfer":
        if request.keys and request.which:
            raise ValueError("choose trials, or which of the source's trials, not both")
        if not request.keys and not request.which:
            raise ValueError("choose trials, or which of the source's trials to transfer")
        if request.which and request.which not in core.TRANSFER_CHOICES:
            raise ValueError(f"choose one of {', '.join(core.TRANSFER_CHOICES)}")
    if request.step == "evidence":
        if not request.trial_ids and not request.every:
            raise ValueError("choose trials, or tick 'every trial ready for a check'")
        # a name that is not one of the files in checks/configs/ is refused, as is a file that can't be used
        try:
            check_settings.load(request.config)
        except FileNotFoundError as exc:
            raise ValueError(str(exc)) from exc


def run_job(job: Job) -> None:
    try:
        # Writes are recorded as the person who started the job
        with db.connect(actor=job.actor) as conn:
            if job.request.step == "list":
                _run_list(conn, job)
            elif job.request.step == "transfer":
                _run_transfer(conn, job)
            else:
                _run_evidence(conn, job)
    except Exception as exc:  # shown on the job's page; the thread must not die silently
        job.add(f"failed: {type(exc).__name__}: {exc}")
        job.add(traceback.format_exc())
        job.status = "failed"
    finally:
        job.finished_at = now()


def _finish(job: Job, failed: dict[str, str], summary: str) -> None:
    job.add(summary)
    for where, message in failed.items():
        job.add(f"  failed {where}: {message}")
    if failed:
        job.status = "failed"
    elif job.stop_requested:
        job.status = "stopped"
    else:
        job.status = "succeeded"


def _source(conn, code: str) -> core.RegisteredSource:
    source = core.registered_source(conn, code)
    if source is None:
        raise ValueError(f"no source {code!r} is registered")
    return source


def _run_list(conn, job: Job) -> None:
    source = _source(conn, job.request.source)
    with registry.open_adapter(source.adapter) as adapter:
        result = stages.sync_list(conn, source, adapter, progress=job.add, should_stop=job.should_stop)
    job.run_id = result.run_id
    _finish(job, result.failed, result.summary())


def transfer_keys(conn, request: JobRequest) -> list[str]:
    if request.which:
        keys = core.keys_to_transfer(conn, request.source, request.which)
    else:
        keys = list(request.keys)
    if request.limit is not None:
        keys = keys[:request.limit]
    return keys


def _run_transfer(conn, job: Job) -> None:
    source = _source(conn, job.request.source)
    keys = transfer_keys(conn, job.request)
    why = "chosen"
    if job.request.which:
        why = job.request.which
    job.add(f"{len(keys)} trials to transfer from {source.code} ({why})")
    with registry.open_adapter(source.adapter) as adapter:
        result = stages.transfer(conn, source, adapter, keys, why=why, progress=job.add, should_stop=job.should_stop)
    job.run_id = result.run_id
    _finish(job, result.failed, result.summary())


def _anthropic_client():
    import anthropic

    return anthropic.Anthropic(base_url=ANTHROPIC_API, max_retries=4)


def _trial_ids(conn, request: JobRequest, requirement_set) -> list[int]:
    """The trials chosen in the form (each from the database), or every trial a check of this
    requirement set may read now (sources/stages.py ready_trials)."""
    if request.every:
        return stages.ready_trials(conn, requirement_set.document_kinds)
    trial_ids = []
    for trial_id in request.trial_ids:
        if trial_id not in trial_ids:
            trial_ids.append(trial_id)
    return trial_ids


def _run_evidence(conn, job: Job) -> None:
    request = job.request
    settings = check_settings.load(request.config)
    prompt = load_prompt(settings.prompt)
    requirement_set = requirement_sets.load(settings.requirements)
    trial_ids = _trial_ids(conn, request, requirement_set)
    if request.limit is not None:
        trial_ids = trial_ids[:request.limit]
    evidence.refuse_unready(conn, trial_ids, requirement_set)
    names = core.trial_names(conn, trial_ids)
    job.add(f"{len(trial_ids)} trials · {settings.summary()}")
    if request.dry_run:
        for trial_id in trial_ids:
            questions, pages = evidence.preview_trial(conn, trial_id, requirement_set)
            readable = 0
            for page in pages:
                if has_readable_text(page):
                    readable += 1
            job.add(f"{names[trial_id]}: {len(questions)} questions, {len(pages)} pages ({readable} readable)")
        _finish(job, {}, "dry run: nothing was sent")
        return
    summary = evidence.run(conn, _anthropic_client(), trial_ids, settings, prompt, progress=job.add,
                           requirement_set=requirement_set, should_stop=job.should_stop)
    job.run_id = summary.run_id
    _finish(job, summary.failed, summary.summary())
