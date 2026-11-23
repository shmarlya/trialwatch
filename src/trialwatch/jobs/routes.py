"""Jobs from the web interface (ADR-005): a form queues a job (jobs.job) as the signed-in person;
`trialwatch worker` runs it (jobs/worker.py), tries it again when it fails, and the job's page follows
its progress lines from the database. Also the admin pages for jobs, schedules and the monthly spend.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from trialwatch import db
from trialwatch.checks import evidence
from trialwatch.checks import requirements as requirement_sets
from trialwatch.checks import settings as check_settings
from trialwatch.db import core, overview
from trialwatch.db import costs as costs_db
from trialwatch.db import jobs as jobs_db
from trialwatch.jobs import PRIORITY_AUTOMATIC, PRIORITY_PERSON, tasks
from trialwatch.llm import dollars
from trialwatch.sources import registry
from trialwatch.db import auth as auth_db
from trialwatch.web.deps import TEMPLATES, Requires, is_whole_number, require, whole_number, with_error

router = APIRouter()

# The forms' step names and the kind of job each becomes
STEP_KINDS = {"list": "sync", "transfer": "transfer", "evidence": "check"}


@dataclass
class JobRequest:
    """What the form asked for."""

    step: str
    # list and transfer: the registered source (core.source code)
    source: str = ""
    # transfer: the source's keys of the chosen trials (e.g. EU CT numbers), or which of its trials
    # (core.TRANSFER_CHOICES: pending, failed, stale, recruiting, listed)
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


def job_params(request: JobRequest) -> tuple[str, dict]:
    """The kind of job and its params (jobs.job.params) for a form's request."""
    kind = STEP_KINDS[request.step]
    params = {}
    if request.limit is not None:
        params["limit"] = request.limit
    if kind in ("sync", "transfer"):
        params["source"] = request.source
    if kind == "transfer":
        if request.keys:
            params["keys"] = list(request.keys)
        if request.which:
            params["which"] = request.which
    if kind == "check":
        if request.trial_ids:
            params["trial_ids"] = list(request.trial_ids)
        if request.every:
            params["every"] = True
        params["config"] = request.config
        if request.dry_run:
            params["dry_run"] = True
    return kind, params


def check_request(request: JobRequest) -> None:
    """Raise ValueError when the request can't run."""
    if request.step not in STEP_KINDS:
        raise ValueError(f"unknown step {request.step!r}")
    kind, params = job_params(request)
    problem = tasks.problem(kind, params)
    if problem is not None:
        raise ValueError(problem)


def enqueue_request(conn: psycopg.Connection, request: JobRequest) -> int:
    """Check the request and queue it as the connection's actor (the signed-in person); returns the
    job's id. Raises ValueError."""
    check_request(request)
    kind, params = job_params(request)
    return jobs_db.enqueue(conn, kind, params, max_attempts=tasks.TASKS[kind].max_attempts, priority=PRIORITY_PERSON)


def redirect_back(back: str, message: str) -> RedirectResponse:
    """Back to the page the form was on (one of ours), with the message; else to the workflow page."""
    for prefix in ("/trials/", "/gold/", "/sources"):
        if back.startswith(prefix):
            return with_error(back, message)
    return with_error("/", message)


def job_action(step: str, dry_run: bool) -> str:
    """The chart's action for starting a job of `step`."""
    if step == "list":
        return "source.sync"
    if step == "evidence":
        if dry_run:
            return "check.dry_run"
        return "check.run"
    if step == "transfer":
        return "trial.transfer"
    raise HTTPException(400, f"unknown step {step!r}")


@router.post("/jobs")
def start_job(
    step: Annotated[str, Form()],
    # transfer: the source, and its keys (ticked, or pasted), or which of its trials
    source: Annotated[str, Form()] = "",
    keys: Annotated[list[str], Form()] = [],
    key_text: Annotated[str, Form()] = "",
    which: Annotated[str, Form()] = "",
    # chosen in a dropdown of trials already in the database (several may be chosen)
    trial_ids: Annotated[list[str], Form()] = [],
    every: Annotated[bool, Form()] = False,
    limit: Annotated[str, Form()] = "",
    # the check's setup: a file in checks/configs/
    config: Annotated[str, Form()] = check_settings.DEFAULT,
    dry_run: Annotated[bool, Form()] = False,
    back: Annotated[str, Form()] = "",
    user: auth_db.User = Depends(Requires("source.sync", "trial.transfer", "check.dry_run", "check.run")),
) -> Response:
    require(user, job_action(step, dry_run))
    # Keys may be ticked, or pasted separated by spaces, commas or new lines
    given = []
    for key in keys:
        if key.strip() and key.strip() not in given:
            given.append(key.strip())
    for key in re.findall(r"\S+", key_text.replace(",", " ")):
        if key not in given:
            given.append(key)
    chosen_trials = []
    for value in trial_ids:
        if is_whole_number(value.strip()):
            chosen_trials.append(int(value.strip()))
    try:
        chosen_limit = whole_number(limit, "the limit")
    except ValueError as exc:
        return redirect_back(back, str(exc))
    if step == "evidence" and given:
        # trials ticked on a source's page: their keys at that source
        with db.connect_system() as conn:
            for trial_id in overview.trials_of_keys(conn, source, given):
                if trial_id not in chosen_trials:
                    chosen_trials.append(trial_id)
        given = []
    if step == "evidence" and chosen_trials:
        try:
            requirement_set = requirement_sets.load(check_settings.load(config.strip()).requirements)
            with db.connect_system() as conn:
                evidence.refuse_unready(conn, chosen_trials, requirement_set)
        except (FileNotFoundError, ValueError) as exc:
            # an unknown setup, or trials whose evidence is not complete (evidence.NotReady)
            return redirect_back(back, str(exc))
    if step == "transfer" and given:
        with db.connect_system() as conn:
            registered = core.registered_source(conn, source)
        if registered is not None and registered.adapter is not None:
            adapter = registry.adapter_class(registered.adapter)()
            for key in given:
                problem = adapter.key_problem(key)
                if problem is not None:
                    return redirect_back(back, problem)
    job_request = JobRequest(
        step=step,
        source=source.strip(),
        keys=given,
        which=which.strip(),
        trial_ids=chosen_trials,
        every=every,
        limit=chosen_limit,
        config=config.strip(),
        dry_run=dry_run,
    )
    try:
        with db.connect(actor=user.email) as conn:
            job_id = enqueue_request(conn, job_request)
    except ValueError as exc:
        return redirect_back(back, str(exc))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


def ids_of(found: list[jobs_db.Job]) -> list[int]:
    ids = []
    for job in found:
        ids.append(job.id)
    return ids


def _job_or_404(conn: psycopg.Connection, job_id: int) -> jobs_db.Job:
    job = jobs_db.job(conn, job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return job


@router.post("/jobs/{job_id}/stop")
def stop_job(job_id: int, user: auth_db.User = Depends(Requires("job.stop.own", "job.stop.any"))) -> Response:
    """Cancel a job: one waiting in the queue ends now; a running one stops after the trial or page it
    is on, and what it did so far is kept."""
    with db.connect(actor=user.email) as conn:
        job = _job_or_404(conn, job_id)
        if job.actor == user.email:
            require(user, "job.stop.own")
        else:
            require(user, "job.stop.any")
        jobs_db.request_cancel(conn, job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, user: auth_db.User = Depends(Requires("job.retry"))) -> Response:
    """Queue a failed, cancelled or held job again now, with one more attempt."""
    with db.connect(actor=user.email) as conn:
        _job_or_404(conn, job_id)
        if not jobs_db.retry_now(conn, job_id):
            return with_error(f"/jobs/{job_id}", "only a failed, cancelled or held job can be tried again")
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(
    request: Request, job_id: int, error: str | None = None, user: auth_db.User = Depends(Requires("job.view"))
) -> HTMLResponse:
    with db.connect_system() as conn:
        job = _job_or_404(conn, job_id)
        lines = jobs_db.lines(conn, job_id)
        runs = jobs_db.runs_of(conn, job_id)
        cost = jobs_db.job_cost(conn, job_id)
    context = {
        "job": job,
        "lines": lines,
        "runs": runs,
        "cost": cost,
        "steps": tasks.LABELS,
        "error": error,
        "dollars": dollars,
    }
    return TEMPLATES.TemplateResponse(request, "job.html", context)


@router.get("/jobs/{job_id}/progress")
def job_progress(job_id: int, after: int = 0, user: auth_db.User = Depends(Requires("job.view"))) -> JSONResponse:
    """The job's status and its lines after line id `after`, for the job page to poll."""
    with db.connect_system() as conn:
        job = _job_or_404(conn, job_id)
        new_lines = jobs_db.lines(conn, job_id, after)
        cost = jobs_db.job_cost(conn, job_id)
    texts = []
    last = after
    for line in new_lines:
        texts.append(line.line)
        last = line.id
    return JSONResponse(
        {"status": job.status, "lines": texts, "last": last, "attempts": job.attempts, "cost": dollars(cost)}
    )


# --- jobs, schedules and spend (admin) -----------------------------------------------------------

JOB_TABS = {
    "attention": ("Need attention", ("failed", "held")),
    "active": ("Running", ("running",)),
    "queued": ("Queued", ("queued",)),
    "ended": ("Ended", ("succeeded", "cancelled", "failed")),
}


@router.get("/admin/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    tab: str = "attention",
    error: str | None = None,
    user: auth_db.User = Depends(Requires("job.view")),
) -> HTMLResponse:
    if tab not in JOB_TABS:
        tab = "attention"
    with db.connect_system() as conn:
        counts = jobs_db.status_counts(conn)
        found = jobs_db.jobs_with_status(conn, JOB_TABS[tab][1])
        costs = jobs_db.job_costs(conn, ids_of(found))
        schedules = jobs_db.schedules(conn)
    tab_counts = {}
    for name, tab_info in JOB_TABS.items():
        total = 0
        for status in tab_info[1]:
            total += counts.get(status, 0)
        tab_counts[name] = total
    context = {
        "tab": tab,
        "tabs": JOB_TABS,
        "tab_counts": tab_counts,
        "jobs": found,
        "costs": costs,
        "schedules": schedules,
        "steps": tasks.LABELS,
        "error": error,
        "dollars": dollars,
        "weekdays": WEEKDAYS,
        "frequencies": jobs_db.FREQUENCIES,
    }
    return TEMPLATES.TemplateResponse(request, "admin_jobs.html", context)


WEEKDAYS = ("", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@router.post("/admin/schedules/{schedule_id}")
def edit_schedule(
    schedule_id: int,
    weekday: Annotated[int, Form()],
    hour_utc: Annotated[int, Form()],
    frequency: Annotated[str, Form()] = "weekly",
    enabled: Annotated[bool, Form()] = False,
    user: auth_db.User = Depends(Requires("schedule.edit")),
) -> Response:
    """Change how often (daily, weekly, monthly) and when a schedule runs, and whether it is on."""
    if frequency not in jobs_db.FREQUENCIES or weekday < 1 or weekday > 7 or hour_utc < 0 or hour_utc > 23:
        message = "daily, weekly or monthly, a day from Monday to Sunday and an hour 0-23"
        return with_error("/admin/jobs", message)
    with db.connect(actor=user.email) as conn:
        if jobs_db.schedule(conn, schedule_id) is None:
            raise HTTPException(404, "no such schedule")
        jobs_db.set_schedule(
            conn, schedule_id, frequency=frequency, weekday=weekday, hour_utc=hour_utc, enabled=enabled
        )
    return RedirectResponse("/admin/jobs#schedules", status_code=303)


@router.post("/admin/schedules/{schedule_id}/switch")
def switch_schedule(
    schedule_id: int, enabled: Annotated[bool, Form()] = False, user: auth_db.User = Depends(Requires("schedule.edit"))
) -> Response:
    """Start or stop a schedule, keeping how often and when it runs. Stopping it does not stop a job it
    already started: that job has its own Stop on its page."""
    with db.connect(actor=user.email) as conn:
        if jobs_db.schedule(conn, schedule_id) is None:
            raise HTTPException(404, "no such schedule")
        jobs_db.switch_schedule(conn, schedule_id, enabled)
    return RedirectResponse("/admin/jobs#schedules", status_code=303)


@router.post("/admin/schedules/{schedule_id}/run")
def run_schedule_now(schedule_id: int, user: auth_db.User = Depends(Requires("schedule.edit"))) -> Response:
    """Start a schedule's job now (as TrialWatch, like the schedule would), its next time unchanged."""
    with db.connect(actor=user.email) as conn:
        schedule = jobs_db.schedule(conn, schedule_id)
    if schedule is None:
        raise HTTPException(404, "no such schedule")
    with db.connect_system() as conn:
        max_attempts = tasks.TASKS[schedule.kind].max_attempts
        job_id = jobs_db.enqueue(
            conn,
            schedule.kind,
            schedule.params,
            max_attempts=max_attempts,
            automatic=True,
            schedule_id=schedule.id,
            priority=PRIORITY_AUTOMATIC,
        )
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.get("/admin/spend", response_class=HTMLResponse)
def spend_page(
    request: Request, error: str | None = None, user: auth_db.User = Depends(Requires("spend.view"))
) -> HTMLResponse:
    with db.connect_system() as conn:
        totals = costs_db.spend_totals(conn)
        cap = costs_db.monthly_cap(conn)
        groups = {}
        for group in ("month", "model", "operation", "actor"):
            groups[group] = costs_db.spend_by(conn, group)
        costliest = costs_db.costliest_runs(conn)
        held = jobs_db.jobs_with_status(conn, ("held",))
    context = {
        "totals": totals,
        "cap": cap,
        "groups": groups,
        "costliest": costliest,
        "held": held,
        "error": error,
        "dollars": dollars,
    }
    return TEMPLATES.TemplateResponse(request, "admin_spend.html", context)


@router.post("/admin/spend/cap")
def set_spend_cap(
    cap: Annotated[str, Form()] = "", user: auth_db.User = Depends(Requires("spend.cap.set"))
) -> Response:
    """Set the monthly cap for automatic checks (empty: no cap). Held jobs resume when the worker next
    finds the cap allows spending."""
    text = cap.strip().removeprefix("$").replace(",", "")
    chosen = None
    if text:
        try:
            chosen = Decimal(text)
        except InvalidOperation:
            return with_error("/admin/spend", f"not an amount: {cap!r}")
        if chosen < 0 or not chosen.is_finite():
            return with_error("/admin/spend", "the cap must be 0 or more")
    with db.connect(actor=user.email) as conn:
        costs_db.set_monthly_cap(conn, chosen)
    return RedirectResponse("/admin/spend", status_code=303)
