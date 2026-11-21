"""TrialWatch's web interface on the central model (ADR-003): start the workflow steps (sync a
source's list → transfer trials' evidence: record, documents, page text → evidence check; ADR-005) as
background jobs, manage the registered sources, and browse the
trials, their identifiers, sources, documents and history, and every check run with its prompt and a
step-by-step trace of each trial it judged.

Started by `trialwatch web`. Pages are rendered on the server from templates/; the only scripts are
the job page's polling of its own progress and small conveniences (scroll to a quote). All SQL is in
trialwatch.db (db/overview.py for these pages).

It has no login: it is for the person running it, on their own machine. So it answers only requests
addressed to this machine (a web page can't reach it through a domain name pointing here) and takes
form posts only from its own pages (a web page elsewhere can't submit a form to it).
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote as url_quote
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from trialwatch import db
from trialwatch.checks import evidence, model
from trialwatch.checks import requirements as requirement_sets
from trialwatch.checks import settings as check_settings
from trialwatch.db import core, overview
from trialwatch.db.core import Identifier, normalised
from trialwatch.gold import datasets as gold_datasets
from trialwatch.sources import manual, registry, stages
from trialwatch.web.jobs import STEPS, JobBusy, JobRequest, Jobs

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
CT_NUMBER = re.compile(r"\d{4}-\d{6}-\d{2}-\d{2}")
# Names of the trace steps as the trial page shows them
STEP_TITLES = {
    "questions": "Questions: the requirements for this trial",
    "pages": "Documents and pages read",
    "retrieval": "Retrieval: what the model is shown",
    "call": "Model call",
    "widening": "Widening",
    "verification": "Quote verification",
    "rules": "Rules",
    "error": "Stopped",
}

app = FastAPI(title="TrialWatch")
jobs = Jobs()

# The names this machine answers to; TRIALWATCH_HOSTS adds others (comma-separated), e.g. when
# `trialwatch web --host` serves it on a network
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "testserver")
# Methods that change something: only accepted from the interface's own pages
CHANGING_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def default_settings() -> check_settings.Settings:
    """The check's default setup (checks/configs/default.json); pages show labels judged against its
    requirement set."""
    return check_settings.load(check_settings.DEFAULT)


def allowed_hosts() -> list[str]:
    hosts = []
    for host in LOCAL_HOSTS:
        hosts.append(host)
    configured = os.environ.get("TRIALWATCH_HOSTS", "")
    for host in configured.split(","):
        if host.strip():
            hosts.append(host.strip())
    return hosts


def host_name(netloc: str) -> str:
    """The host of a "host:port" (or "[::1]:port") without the port and brackets, lower case."""
    hostname = urlsplit(f"//{netloc}").hostname
    if hostname is None:
        return ""
    return hostname


def refusal(request: Request) -> str | None:
    """Why the request is refused, or None. A request must name one of this machine's names as its
    Host (else a page on another domain that points at this machine could read it: DNS rebinding);
    a form post must come from one of the interface's own pages (its Origin, or else its Referer, is
    this host), else any web page open in the browser could post forms here (cross-site request
    forgery). Requests without either header (curl, the tests) are not from a browser page."""
    host = host_name(request.headers.get("host", ""))
    if host not in allowed_hosts():
        return f"unknown host {host!r}"
    if request.method not in CHANGING_METHODS:
        return None
    sender = request.headers.get("origin")
    if not sender:
        sender = request.headers.get("referer")
    if not sender:
        return None
    sender_parts = urlsplit(sender)
    if sender_parts.netloc != request.headers.get("host", ""):
        return f"a form post from another site ({sender_parts.netloc or sender!r})"
    return None


@app.middleware("http")
async def only_this_machine_and_own_pages(request: Request, call_next):
    reason = refusal(request)
    if reason is not None:
        return PlainTextResponse(f"refused: {reason}", status_code=403)
    return await call_next(request)


# --- helpers for the pages -----------------------------------------------------------------------

def highlighted(text: str, quote: str | None) -> list[tuple[str, bool]]:
    """The page text cut into (piece, is the quote) pieces, so the template can mark the quote. Words
    are matched loosely (any spacing, dashes or punctuation between them), like quote verification."""
    if not quote:
        return [(text, False)]
    words = re.findall(r"\w+", quote)
    if not words:
        return [(text, False)]
    escaped = []
    for word in words:
        escaped.append(re.escape(word))
    pattern = re.compile(r"\W*".join(escaped), re.IGNORECASE)
    pieces = []
    position = 0
    for match in pattern.finditer(text):
        pieces.append((text[position:match.start()], False))
        pieces.append((match.group(0), True))
        position = match.end()
    pieces.append((text[position:], False))
    return pieces


def _back_with_error(message: str) -> RedirectResponse:
    """Back to the workflow page, which shows the message above the forms."""
    return RedirectResponse(f"/?error={url_quote(message)}", status_code=303)


def _file_hash(sha_hex: str) -> bytes:
    try:
        sha = bytes.fromhex(sha_hex)
    except ValueError as exc:
        raise HTTPException(404, "not a file hash") from exc
    if len(sha) != 32:
        raise HTTPException(404, "not a file hash")
    return sha


def _whole_number(text: str, name: str) -> int | None:
    """The form field as a number, None when empty; raises ValueError with the field's name."""
    stripped = text.strip()
    if not stripped:
        return None
    if not is_whole_number(stripped):
        raise ValueError(f"{name} must be a whole number")
    return int(stripped)


def is_whole_number(text: str) -> bool:
    """Digits 0-9 only: str.isdigit() alone also takes "²", which int() refuses."""
    return text.isascii() and text.isdigit()


def _files_by_hex(steps: list[overview.StepRow]) -> dict[str, dict]:
    """File hash (hex) -> {doc, title} from the trace's pages step, to name files in other steps."""
    files = {}
    for step in steps:
        if step.name != "pages":
            continue
        for document in step.data.get("documents", []):
            files[document["file"]] = document
    return files


@dataclass
class CallView:
    """One model call of a trace, laid out for reading: what was sent and what came back."""

    number: int
    asked: list[dict]
    message: str | None
    schema: str | None
    settings: dict
    answer_text: str
    answers: list[dict]
    answer_error: str | None


def call_view(data: dict) -> CallView:
    """The call step's stored data as the trial page shows it. Runs before the request was stored
    have no message or schema."""
    request = data.get("request") or {}
    message = None
    messages = request.get("messages")
    if isinstance(messages, list) and messages:
        content = messages[0].get("content")
        if isinstance(content, str):
            message = content
        elif isinstance(content, list):
            # the files method: attached files (stored by their sha256), then the text
            pieces = []
            for block in content:
                if block.get("type") == "document":
                    source = block.get("source") or {}
                    pieces.append(f"[attached file] {block.get('title')} · sha256 {str(source.get('sha256', ''))[:12]} · "
                                  f"{source.get('bytes', '?')} bytes")
                elif block.get("type") == "text":
                    pieces.append(block.get("text", ""))
            message = "\n\n".join(pieces)
    schema = None
    output_config = request.get("output_config") or {}
    output_format = output_config.get("format") or {}
    if output_format.get("schema"):
        schema = json.dumps(output_format["schema"], indent=2, ensure_ascii=False)
    settings = {}
    for name in ("model", "max_tokens", "thinking", "betas", "fallbacks"):
        if name in request:
            settings[name] = request[name]
    if "effort" in output_config:
        settings["effort"] = output_config["effort"]
    raw = data.get("answer") or ""
    answer_text = raw
    answers = []
    answer_error = None
    try:
        parsed = json.loads(raw)
        answer_text = json.dumps(parsed, indent=2, ensure_ascii=False)
        for answer in parsed.get("answers", []):
            answers.append(answer)
    except (ValueError, AttributeError) as exc:
        if raw:
            answer_error = f"not valid JSON: {exc}"
    asked = data.get("asked")
    if not asked:
        asked = []
        for key in data.get("questions", []):
            asked.append({"key": key, "text": ""})
    return CallView(data.get("call", 0), asked, message, schema, settings, answer_text, answers, answer_error)


# --- workflow and jobs ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, error: str | None = None) -> HTMLResponse:
    defaults = default_settings()
    with db.connect() as conn:
        coverage = overview.coverage(conn)
        runs = overview.runs(conn)
        default_set = requirement_sets.load(defaults.requirements)
        kinds = list(default_set.document_kinds)
        choices = overview.trial_choices(conn, stages.ready_trials(conn, kinds), kinds, default_set.name)
        sources = overview.source_rows(conn)
    context = {"sources": sources, "coverage": coverage, "runs": runs[:10], "jobs": jobs.all(), "running": jobs.running(),
               "steps": STEPS, "error": error, "defaults": defaults, "actor": db.default_actor(),
               "choices": choices, "configs": check_settings.load_all()}
    return TEMPLATES.TemplateResponse(request, "dashboard.html", context)


def _redirect_back(back: str, message: str) -> RedirectResponse:
    """Back to the page the form was on (one of ours), with the message; else to the workflow page."""
    for prefix in ("/trials/", "/gold/", "/sources"):
        if back.startswith(prefix):
            joiner = "?"
            if "?" in back:
                joiner = "&"
            return RedirectResponse(f"{back}{joiner}error={url_quote(message)}", status_code=303)
    return _back_with_error(message)


@app.post("/jobs")
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
) -> Response:
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
        chosen_limit = _whole_number(limit, "the limit")
    except ValueError as exc:
        return _redirect_back(back, str(exc))
    if step == "evidence" and given:
        # trials ticked on a source's page: their keys at that source
        with db.connect() as conn:
            for trial_id in overview.trials_of_keys(conn, source, given):
                if trial_id not in chosen_trials:
                    chosen_trials.append(trial_id)
        given = []
    if step == "evidence" and chosen_trials:
        try:
            requirement_set = requirement_sets.load(check_settings.load(config.strip()).requirements)
            with db.connect() as conn:
                evidence.refuse_unready(conn, chosen_trials, requirement_set)
        except (FileNotFoundError, ValueError) as exc:
            # an unknown setup, or trials whose evidence is not complete (evidence.NotReady)
            return _redirect_back(back, str(exc))
    if step == "transfer" and given:
        with db.connect() as conn:
            registered = core.registered_source(conn, source)
        if registered is not None and registered.adapter is not None:
            adapter = registry.adapter_class(registered.adapter)()
            for key in given:
                problem = adapter.key_problem(key)
                if problem is not None:
                    return _redirect_back(back, problem)
    job_request = JobRequest(step=step, source=source.strip(), keys=given, which=which.strip(),
                             trial_ids=chosen_trials, every=every, limit=chosen_limit, config=config.strip(),
                             dry_run=dry_run)
    try:
        job = jobs.start(job_request)
    except (ValueError, JobBusy) as exc:
        return _redirect_back(back, str(exc))
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.post("/jobs/{job_id}/stop")
def stop_job(job_id: int) -> Response:
    """Ask a running job to stop after the trial it is on; what it did so far is kept."""
    jobs.stop(job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: int) -> HTMLResponse:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job (jobs are kept until the server restarts)")
    # one copy of the lines, so the page shows exactly the lines it then counts as seen while the job
    # thread appends more
    lines = list(job.lines)
    return TEMPLATES.TemplateResponse(request, "job.html", {"job": job, "lines": lines, "steps": STEPS})


@app.get("/jobs/{job_id}/progress")
def job_progress(job_id: int, after: int = 0) -> JSONResponse:
    """The job's status and its lines from number `after` on, for the job page to poll."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    # one copy, so the lines sent and their count agree while the job thread appends more
    lines = list(job.lines)
    return JSONResponse({"status": job.status, "lines": lines[after:], "total": len(lines),
                         "run_id": job.run_id, "step": job.request.step})


# --- trials --------------------------------------------------------------------------------------

TRIALS_PER_PAGE = 100


@app.get("/trials", response_class=HTMLResponse)
def trials_page(request: Request, q: str = "", evidence_state: str = "", page: int = 1) -> HTMLResponse:
    search = q.strip()
    if not search:
        search = None
    state = None
    if evidence_state in overview.EVIDENCE_FILTERS:
        state = evidence_state
    page = max(page, 1)
    with db.connect() as conn:
        total = overview.trial_count(conn, search, state)
        rows = overview.trial_rows(conn, default_settings().requirements, search, state,
                                   offset=(page - 1) * TRIALS_PER_PAGE, limit=TRIALS_PER_PAGE)
    context = {"rows": rows, "q": q, "evidence_state": evidence_state, "filters": overview.EVIDENCE_FILTERS,
               "total": total, "page": page, "pages": max(1, -(-total // TRIALS_PER_PAGE))}
    return TEMPLATES.TemplateResponse(request, "trials.html", context)


@app.get("/trials/new", response_class=HTMLResponse)
def new_trial_page(request: Request, error: str | None = None) -> HTMLResponse:
    with db.connect() as conn:
        schemes = core.identifier_schemes(conn)
        kinds = core.document_kinds(conn)
    context = {"schemes": schemes, "kinds": kinds, "error": error}
    return TEMPLATES.TemplateResponse(request, "new_trial.html", context)


def _uploads(files: list[UploadFile]) -> list[manual.UploadedFile]:
    """The files a form sent, without the empty entry a browser sends when none was chosen."""
    uploads = []
    for upload in files:
        if not upload.filename:
            continue
        content = upload.file.read()
        uploads.append(manual.UploadedFile(upload.filename, content))
    return uploads


@app.post("/trials/new")
def add_trial(
    title: Annotated[str, Form()],
    schemes: Annotated[list[str], Form()] = [],
    values: Annotated[list[str], Form()] = [],
    sponsors: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "",
    endpoints: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "results-summary",
    files: Annotated[list[UploadFile], File()] = [],
) -> Response:
    identifiers = []
    for index, value in enumerate(values):
        if value.strip() and index < len(schemes):
            identifiers.append(Identifier(schemes[index], value.strip()))
    endpoint_lines = []
    for line in endpoints.splitlines():
        if line.strip():
            endpoint_lines.append(line.strip())
    entry = manual.TrialEntry(identifiers=identifiers, public_title=title.strip(), sponsors=sponsors.splitlines(),
                              status=_or_none(status), primary_endpoints=endpoint_lines)
    try:
        # Recorded as the person running the web interface
        with db.connect() as conn:
            trial_id = manual.add_trial(conn, entry, _uploads(files), kind)
    except manual.AlreadyKnown as exc:
        message = f"Already in the database ({exc}). Add documents here instead."
        return RedirectResponse(f"/trials/{exc.trial_id}?error={url_quote(message)}", status_code=303)
    except Exception as exc:  # e.g. an identifier that does not fit its scheme: shown on the form
        return RedirectResponse(f"/trials/new?error={url_quote(_short_error(exc))}", status_code=303)
    return RedirectResponse(f"/trials/{trial_id}", status_code=303)


def _or_none(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    return stripped


def _short_error(exc: Exception) -> str:
    """The first line of an error, e.g. a database check's message without its context lines."""
    lines = str(exc).splitlines()
    if not lines:
        return type(exc).__name__
    return lines[0]


@app.post("/trials/{trial_id}/documents")
def upload_document(
    trial_id: int,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str, Form()] = "results-summary",
    title: Annotated[str, Form()] = "",
) -> Response:
    uploads = _uploads([file])
    if not uploads:
        return RedirectResponse(f"/trials/{trial_id}?error={url_quote('choose a file to upload')}", status_code=303)
    try:
        with db.connect() as conn:
            manual.add_document(conn, trial_id, uploads[0], kind, _or_none(title))
    except Exception as exc:  # shown on the trial page
        return RedirectResponse(f"/trials/{trial_id}?error={url_quote(_short_error(exc))}", status_code=303)
    return RedirectResponse(f"/trials/{trial_id}#documents", status_code=303)


@app.post("/trials/{trial_id}/transfer")
def transfer_trial(trial_id: int, source: Annotated[str, Form()]) -> Response:
    """Transfer this trial's evidence again from a source (its record, new document versions, page text)."""
    with db.connect() as conn:
        keys = overview.trial_source_keys(conn, trial_id, source)
    if not keys:
        message = f"this trial has no key at {source} to transfer"
        return RedirectResponse(f"/trials/{trial_id}?error={url_quote(message)}", status_code=303)
    try:
        job = jobs.start(JobRequest(step="transfer", source=source, keys=keys))
    except (ValueError, JobBusy) as exc:
        return RedirectResponse(f"/trials/{trial_id}?error={url_quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.post("/trials/{trial_id}/sponsors")
def add_sponsor(trial_id: int, name: Annotated[str, Form()], primary: Annotated[bool, Form()] = False) -> Response:
    """A person adds a sponsor by name: an organisation we hold with that name, else a new one."""
    cleaned = " ".join(name.split())
    if not cleaned:
        return RedirectResponse(f"/trials/{trial_id}?error={url_quote('a sponsor needs a name')}#sponsors",
                                status_code=303)
    with db.connect() as conn:
        core.add_trial_sponsor(conn, trial_id, core.Sponsor(name=cleaned, primary=primary), manual.SOURCE)
    return RedirectResponse(f"/trials/{trial_id}#sponsors", status_code=303)


@app.post("/trials/{trial_id}/sponsors/{organisation_id}/remove")
def remove_sponsor(trial_id: int, organisation_id: int) -> Response:
    """A person removes a sponsor from the trial: kept as removed, and a source listing it again won't bring it back."""
    with db.connect() as conn:
        core.remove_trial_sponsor(conn, trial_id, organisation_id)
    return RedirectResponse(f"/trials/{trial_id}#sponsors", status_code=303)


@app.get("/sponsors", response_class=HTMLResponse)
def sponsors_page(request: Request, q: str = "") -> HTMLResponse:
    search = q.strip()
    if not search:
        search = None
    with db.connect() as conn:
        rows = overview.organisation_rows(conn, search)
    return TEMPLATES.TemplateResponse(request, "sponsors.html", {"rows": rows, "q": q})


@app.get("/sponsors/{organisation_id}", response_class=HTMLResponse)
def sponsor_page(request: Request, organisation_id: int) -> HTMLResponse:
    with db.connect() as conn:
        row = overview.organisation_row(conn, organisation_id)
        if row is None:
            raise HTTPException(404, f"no organisation {organisation_id}")
        trials = overview.sponsored_trials(conn, organisation_id, default_settings().requirements)
    return TEMPLATES.TemplateResponse(request, "sponsor.html", {"row": row, "trials": trials})


@app.get("/trials/{trial_id}", response_class=HTMLResponse)
def trial_page(request: Request, trial_id: int, run: int | None = None, error: str | None = None) -> HTMLResponse:
    with db.connect() as conn:
        row = overview.trial_row(conn, trial_id, default_settings().requirements)
        if row is None:
            raise HTTPException(404, f"no trial {trial_id}")
        registered = overview.trial_registered(conn, trial_id)
        identifiers = overview.trial_identifiers(conn, trial_id)
        sources = overview.trial_sources(conn, trial_id)
        documents = overview.trial_documents(conn, trial_id)
        judgements = overview.trial_judgements(conn, trial_id)
        history = overview.trial_history(conn, trial_id)
        transfers = overview.trial_transfers(conn, trial_id)
        sponsors = overview.trial_sponsors(conn, trial_id)
        kinds = core.document_kinds(conn)
        gold_items = _gold_items_for(identifiers, judgements)
        default_set = requirement_sets.load(default_settings().requirements)
        blocked = stages.not_ready(conn, [trial_id], default_set.document_kinds)
        chosen = None
        for judgement in judgements:
            if run is None or judgement.run_id == run:
                chosen = judgement
                break
        questions = []
        steps = []
        prompt_text = None
        prompt_params = {}
        if chosen is not None:
            questions = overview.run_questions(conn, chosen.run_id, trial_id)
            steps = overview.run_steps(conn, chosen.run_id, trial_id)
            prompt_text, prompt_params = overview.run_prompt(conn, chosen.run_id)
    calls = {}
    for step in steps:
        if step.name == "call":
            calls[step.seq] = call_view(step.data)
    # a check again uses the setup of the judgement shown, by default
    judgement_config = None
    if prompt_params:
        judgement_config = prompt_params.get("config")
    context = {
        "row": row, "registered": registered, "identifiers": identifiers, "sources": sources,
        "documents": documents, "judgements": judgements, "judgement": chosen, "questions": questions,
        "steps": steps, "step_titles": STEP_TITLES, "calls": calls, "prompt_text": prompt_text, "files": _files_by_hex(steps), "history": history,
        "defaults": default_settings(), "running": jobs.running(), "error": error, "transfers": transfers,
        "ready": trial_id not in blocked, "not_ready": blocked.get(trial_id), "transferable": _transferable(transfers),
        "kinds": kinds, "configs": check_settings.load_all(), "judgement_config": judgement_config,
        "gold_items": gold_items, "sponsors": sponsors,
    }
    return TEMPLATES.TemplateResponse(request, "trial.html", context)


def _transferable(transfers: list[overview.TransferRow]) -> list[overview.TransferRow]:
    """The trial's keys at sources whose adapter can transfer evidence."""
    found = []
    for transfer in transfers:
        if transfer.adapter is None:
            continue
        if registry.adapter_class(transfer.adapter).transfers:
            found.append(transfer)
    return found


# --- data sources (ADR-005) ----------------------------------------------------------------------

SOURCE_TRIALS_PER_PAGE = 100


def _unregistered_adapters(rows: list[overview.SourceRow]) -> list[str]:
    used = []
    for row in rows:
        used.append(row.adapter)
    free = []
    for name in registry.ADAPTERS:
        if name not in used:
            free.append(name)
    return free


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, error: str | None = None) -> HTMLResponse:
    with db.connect() as conn:
        rows = overview.source_rows(conn)
    context = {"rows": rows, "free_adapters": _unregistered_adapters(rows), "error": error, "running": jobs.running(),
               "steps": STEPS}
    return TEMPLATES.TemplateResponse(request, "sources.html", context)


def _config_from_text(text: str, adapter: registry.Adapter) -> dict:
    """A config typed as JSON, checked by the adapter; ValueError with what is wrong."""
    try:
        config = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"the config is not JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("the config is a JSON object")
    problems = adapter.config_problems(config)
    if problems:
        raise ValueError("; ".join(problems))
    return config


@app.post("/sources")
def register_source(adapter: Annotated[str, Form()], code: Annotated[str, Form()] = "",
                    name: Annotated[str, Form()] = "") -> Response:
    """Register a source read by an adapter, with the adapter's default config (edited on its page)."""
    try:
        adapter_type = registry.adapter_class(adapter)
        chosen_code = code.strip() or adapter_type.name
        with db.connect() as conn:
            core.register_source(conn, code=chosen_code, kind=adapter_type.kind, name=name.strip() or adapter_type.label,
                                 url=adapter_type.url, adapter=adapter, config=adapter_type().default_config())
    except Exception as exc:  # e.g. a code already used: shown on the page
        return RedirectResponse(f"/sources?error={url_quote(_short_error(exc))}", status_code=303)
    return RedirectResponse(f"/sources/{chosen_code}", status_code=303)


@app.get("/sources/{code}", response_class=HTMLResponse)
def source_page(request: Request, code: str, state: str = "", q: str = "", page: int = 1,
                error: str | None = None, saved: bool = False) -> HTMLResponse:
    chosen_state = None
    if state in overview.SOURCE_TRIAL_FILTERS:
        chosen_state = state
    search = q.strip() or None
    page = max(page, 1)
    with db.connect() as conn:
        row = overview.source_row(conn, code)
        if row is None:
            raise HTTPException(404, f"no source {code}")
        total = overview.source_trial_count(conn, code, chosen_state, search)
        trials = overview.source_trials(conn, code, chosen_state, search, offset=(page - 1) * SOURCE_TRIALS_PER_PAGE,
                                        limit=SOURCE_TRIALS_PER_PAGE)
        runs = overview.source_runs(conn, code)
    adapter = None
    if row.adapter is not None:
        adapter = registry.adapter_class(row.adapter)()
    here = f"/sources/{code}?state={url_quote(state)}&q={url_quote(q)}&page={page}"
    context = {
        "row": row, "adapter": adapter, "config_text": json.dumps(row.config, indent=2), "trials": trials,
        "total": total, "page": page, "pages": max(1, -(-total // SOURCE_TRIALS_PER_PAGE)), "state": state, "q": q,
        "filters": overview.SOURCE_TRIAL_FILTERS, "choices": core.TRANSFER_CHOICES, "runs": runs,
        "running": jobs.running(), "steps": STEPS, "error": error, "saved": saved, "here": here,
        "defaults": default_settings(),
    }
    return TEMPLATES.TemplateResponse(request, "source.html", context)


@app.post("/sources/{code}/config")
def save_source_config(code: str, config: Annotated[str, Form()], enabled: Annotated[bool, Form()] = False) -> Response:
    """Replace the source's config (one current config; every run records the one it used)."""
    with db.connect() as conn:
        source = core.registered_source(conn, code)
        if source is None:
            raise HTTPException(404, f"no source {code}")
        try:
            chosen = source.config
            if source.adapter is not None:
                chosen = _config_from_text(config, registry.adapter_class(source.adapter)())
            core.set_source_config(conn, code, chosen, enabled)
        except ValueError as exc:
            return RedirectResponse(f"/sources/{code}?error={url_quote(str(exc))}#config", status_code=303)
    return RedirectResponse(f"/sources/{code}?saved=true#config", status_code=303)


@app.get("/files/{sha_hex}", response_class=HTMLResponse)
def file_page(request: Request, sha_hex: str, page: int | None = None, quote: str | None = None) -> HTMLResponse:
    """The page text of one stored file, as retrieval and quote checks see it, with a quote marked."""
    sha = _file_hash(sha_hex)
    with db.connect() as conn:
        owner = overview.file_owner(conn, sha)
        if owner is None:
            raise HTTPException(404, "no such file")
        pages = overview.file_pages(conn, sha)
    shown = []
    for page_text in pages:
        if page_text.page_no == page:
            pieces = highlighted(page_text.text, quote)
        else:
            pieces = [(page_text.text, False)]
        shown.append({"page_no": page_text.page_no, "pieces": pieces, "blank": not page_text.text.strip()})
    context = {"sha": sha_hex, "owner": owner, "pages": shown, "page": page, "quote": quote}
    return TEMPLATES.TemplateResponse(request, "file.html", context)


@app.get("/files/{sha_hex}/raw")
def file_raw(sha_hex: str) -> Response:
    """The stored bytes, to open the document itself in the browser."""
    sha = _file_hash(sha_hex)
    with db.connect() as conn:
        stored = overview.file_content(conn, sha)
    if stored is None:
        raise HTTPException(404, "no such file")
    content = stored[0]
    if content.startswith(b"%PDF"):
        # shown in the browser's PDF viewer, with nothing it could run as a page of this site
        headers = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox",
                   "Content-Disposition": "inline"}
        return Response(content, media_type="application/pdf", headers=headers)
    # anything else (a DOCX a source listed, say) is downloaded, never shown as a page of this site
    headers = {"X-Content-Type-Options": "nosniff", "Content-Disposition": f'attachment; filename="{sha_hex}"'}
    return Response(content, media_type="application/octet-stream", headers=headers)


# --- prompts -------------------------------------------------------------------------------------

@app.get("/prompts", response_class=HTMLResponse)
def prompts_page(request: Request, show: str | None = None) -> HTMLResponse:
    """Every prompt file as it is now, and every prompt text check runs used (an edited file is a
    different text: the runs keep the one they sent)."""
    with db.connect() as conn:
        uses = overview.prompt_uses(conn)
        used_texts = {}
        for use in uses:
            key = use.prompt_sha256.hex()
            used_texts[key] = overview.run_prompt_text(conn, use.prompt_sha256)
    current = []
    current_hashes = set()
    for name in model.prompt_names():
        prompt = model.load_prompt(name)
        current.append(prompt)
        current_hashes.add(prompt.sha256.hex())
    context = {"current": current, "current_hashes": current_hashes, "uses": uses, "used_texts": used_texts,
               "show": show, "default_prompt": default_settings().prompt}
    return TEMPLATES.TemplateResponse(request, "prompts.html", context)


@app.get("/requirements", response_class=HTMLResponse)
def requirements_page(request: Request, show: str | None = None) -> HTMLResponse:
    """Every requirement set as it is now: what a trial must report and how "enough" is decided."""
    sets = []
    for name in requirement_sets.set_names():
        sets.append(requirement_sets.load(name))
    context = {"sets": sets, "show": show, "default_set": default_settings().requirements,
               "rule_types": requirement_sets.RULE_TYPES}
    return TEMPLATES.TemplateResponse(request, "requirements.html", context)


# --- check runs ----------------------------------------------------------------------------------

@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int) -> HTMLResponse:
    with db.connect() as conn:
        run = overview.run_row(conn, run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        prompt_text, params = overview.run_prompt(conn, run_id)
        judgements = overview.run_judgements(conn, run_id)
    context = {"run": run, "prompt_text": prompt_text, "params": params, "judgements": judgements}
    return TEMPLATES.TemplateResponse(request, "run.html", context)


# --- gold datasets (ADR-003): files under data/gold/, searched here, never stored in the database ---

@dataclass(frozen=True)
class GoldRow:
    item: gold_datasets.Item
    trial_id: int | None
    judgement: overview.LatestJudgement | None
    comparison: gold_datasets.Comparison | None
    reviewers_disagreed: bool
    # our judgement read a document posted on or after the day the reviewers read theirs: not compared
    set_aside: bool


@dataclass
class GoldSummary:
    items: int = 0
    labelled: int = 0
    synced: int = 0
    checked: int = 0
    agree: int = 0
    disagree: int = 0
    ours_open: int = 0
    false_accusations: int = 0
    set_aside: int = 0


def _stored_identifiers(item: gold_datasets.Item) -> list[tuple[str, str]]:
    found = []
    for identifier in item.identifiers:
        stored = normalised(identifier)
        found.append((stored.scheme, stored.value))
    return found


def set_aside(dataset: gold_datasets.Dataset, published_at: datetime | None) -> bool:
    """Our judgement read a document posted on or after the day the dataset's reviewers read theirs,
    so ours and theirs may be different documents (as gold/compare.py sets them aside)."""
    as_of = dataset.evidence.get("as_of")
    if not as_of or published_at is None:
        return False
    return published_at.date() >= date.fromisoformat(as_of)


def _gold_rows(conn, dataset: gold_datasets.Dataset) -> list[GoldRow]:
    """Every item of the dataset with the trial we hold for it (if any) and our newest judgement of
    that trial under the dataset's requirement set, compared with the item's expected answers."""
    wanted = []
    for item in dataset.items:
        wanted.extend(_stored_identifiers(item))
    trial_by_identifier = overview.trials_for_identifiers(conn, wanted)
    trial_ids = []
    for trial_id in trial_by_identifier.values():
        trial_ids.append(trial_id)
    judgement_by_trial = overview.latest_judgements(conn, trial_ids, dataset.requirements)
    rows = []
    for item in dataset.items:
        trial_id = None
        for key in _stored_identifiers(item):
            if key in trial_by_identifier:
                trial_id = trial_by_identifier[key]
                break
        judgement = None
        comparison = None
        aside = False
        if trial_id is not None and trial_id in judgement_by_trial:
            judgement = judgement_by_trial[trial_id]
            aside = set_aside(dataset, judgement.latest_published_at)
            if not aside:
                comparison = gold_datasets.compare(item, dataset.parts, judgement.parts, judgement.label)
        disagreed = gold_datasets.reviewers_disagreed(item)
        rows.append(GoldRow(item, trial_id, judgement, comparison, disagreed, aside))
    return rows


def _gold_summary(rows: list[GoldRow]) -> GoldSummary:
    summary = GoldSummary()
    for row in rows:
        summary.items += 1
        if row.item.expected_label is not None:
            summary.labelled += 1
        if row.trial_id is not None:
            summary.synced += 1
        if row.set_aside:
            summary.set_aside += 1
        if row.comparison is None:
            continue
        summary.checked += 1
        if row.comparison.label == gold_datasets.AGREE:
            summary.agree += 1
        elif row.comparison.label == gold_datasets.DISAGREE:
            summary.disagree += 1
        elif row.comparison.label == gold_datasets.OURS_OPEN:
            summary.ours_open += 1
        if row.comparison.false_accusation:
            summary.false_accusations += 1
    return summary


# What the "ours" filter keeps
GOLD_OURS_FILTERS = {
    "": "any",
    "not-synced": "not in our database",
    "synced": "in our database",
    "checked": "checked by us",
    "agree": "our label agrees",
    "disagree": "our label disagrees",
    "false-accusation": "false accusation (ours not full, theirs full)",
}


def _keeps(row: GoldRow, text: str, label_filter: str, reviewers: str, ours: str) -> bool:
    if text and not gold_datasets.matches_text(row.item, text):
        return False
    if label_filter == "none" and row.item.expected_label is not None:
        return False
    if label_filter and label_filter != "none" and row.item.expected_label != label_filter:
        return False
    if reviewers == "disagreed" and not row.reviewers_disagreed:
        return False
    if ours == "not-synced" and row.trial_id is not None:
        return False
    if ours == "synced" and row.trial_id is None:
        return False
    if ours == "checked" and row.comparison is None:
        return False
    if ours in ("agree", "disagree"):
        if row.comparison is None or row.comparison.label != ours:
            return False
    if ours == "false-accusation":
        if row.comparison is None or not row.comparison.false_accusation:
            return False
    return True


def _load_dataset(ref: str) -> gold_datasets.Dataset:
    try:
        return gold_datasets.load(ref)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/gold", response_class=HTMLResponse)
def gold_page(request: Request, dataset: str = "", q: str = "", label: str = "", reviewers: str = "",
              ours: str = "") -> HTMLResponse:
    refs = gold_datasets.dataset_refs()
    context = {"refs": refs, "dataset": None, "rows": [], "summary": None, "q": q, "label_filter": label,
               "reviewers": reviewers, "ours": ours, "ours_filters": GOLD_OURS_FILTERS,
               "lock_problems": gold_datasets.lock_problems(), "gold_dir": gold_datasets.gold_dir()}
    if not refs:
        return TEMPLATES.TemplateResponse(request, "gold.html", context)
    chosen_ref = dataset
    if chosen_ref not in refs:
        # by default the last in name order: the newest version of the last-named dataset
        chosen_ref = refs[-1]
    chosen = _load_dataset(chosen_ref)
    with db.connect() as conn:
        rows = _gold_rows(conn, chosen)
    shown = []
    for row in rows:
        if _keeps(row, q.strip(), label, reviewers, ours):
            shown.append(row)
    context["dataset"] = chosen
    context["rows"] = shown
    context["summary"] = _gold_summary(rows)
    return TEMPLATES.TemplateResponse(request, "gold.html", context)


@app.get("/gold/{name}/{version}/{key}", response_class=HTMLResponse)
def gold_item_page(request: Request, name: str, version: str, key: str, error: str | None = None) -> HTMLResponse:
    dataset = _load_dataset(f"{name}/{version}")
    item = gold_datasets.find_item(dataset, key)
    if item is None:
        raise HTTPException(404, f"no item {key} in {dataset.ref}")
    with db.connect() as conn:
        found = overview.trials_for_identifiers(conn, _stored_identifiers(item))
        trial_id = None
        for identifier in _stored_identifiers(item):
            if identifier in found:
                trial_id = found[identifier]
                break
        judgements = []
        documents = []
        ready = False
        if trial_id is not None:
            judgements = overview.trial_judgements(conn, trial_id)
            documents = overview.trial_documents(conn, trial_id)
            kinds = requirement_sets.load(dataset.requirements).document_kinds
            ready = trial_id not in stages.not_ready(conn, [trial_id], kinds)
    compared = []
    for judgement in judgements:
        if not _comparable(judgement, dataset):
            continue
        comparison = None
        if not set_aside(dataset, judgement.latest_published_at):
            comparison = gold_datasets.compare(item, dataset.parts, judgement.parts, judgement.label)
        compared.append((judgement, comparison))
    ct_numbers = []
    for identifier in item.identifiers:
        if identifier.scheme == "eu-ct":
            ct_numbers.append(identifier.value)
    context = {"dataset": dataset, "item": item, "trial_id": trial_id, "compared": compared,
               "documents": documents, "ct_numbers": ct_numbers, "running": jobs.running(), "error": error,
               "ready": ready,
               "defaults": default_settings(), "reviewers_disagreed": gold_datasets.reviewers_disagreed(item)}
    return TEMPLATES.TemplateResponse(request, "gold_item.html", context)


def _gold_items_for(identifiers: list, judgements: list) -> list[tuple]:
    """(dataset, item, comparison with our newest judgement under its requirement set) for every
    gold item naming one of the trial's identifiers."""
    wanted = []
    for identifier in identifiers:
        wanted.append(Identifier(identifier.scheme, identifier.value))
    found = []
    for dataset, item in gold_datasets.items_for_identifiers(wanted):
        comparison = None
        for judgement in judgements:
            if _comparable(judgement, dataset):
                if not set_aside(dataset, judgement.latest_published_at):
                    comparison = gold_datasets.compare(item, dataset.parts, judgement.parts, judgement.label)
                break
        found.append((dataset, item, comparison))
    return found


def _comparable(judgement: overview.JudgementRow, dataset: gold_datasets.Dataset) -> bool:
    """A judgement whose run has ended, without an error, under the requirement set the dataset's
    labels answer."""
    if judgement.error or judgement.run_status == "running":
        return False
    return judgement.requirements == dataset.requirements
