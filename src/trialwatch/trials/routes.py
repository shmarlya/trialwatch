"""The trials' pages: the list (staff, or the published ones), one trial with everything we hold on it
(its judgements and their traces from checks, what someone looking for a trial to join sees from
search, the gold items naming it), adding a trial and its documents by hand, its sponsors, and the
stored files."""

import re

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trialwatch import db
from trialwatch.checks import requirements as requirement_sets
from trialwatch.checks import settings as check_settings
from trialwatch.checks.views import STEP_TITLES, call_view, files_by_hex
from trialwatch.db import core, overview
from trialwatch.db import search as search_db
from trialwatch.db.core import Identifier
from trialwatch.gold.views import gold_items_for
from trialwatch.jobs.routes import JobRequest, enqueue_request
from trialwatch.search.views import patient_view
from trialwatch.sources import manual, registry
from trialwatch.trials import readiness
from trialwatch.db import auth as auth_db
from trialwatch.web.deps import (
    TEMPLATES,
    Requires,
    default_settings,
    or_none,
    running,
    short_error,
    staff_view,
    with_error,
)

router = APIRouter()

TRIALS_PER_PAGE = 100


@router.get("/trials", response_class=HTMLResponse)
def trials_page(
    request: Request,
    q: str = "",
    evidence_state: str = "",
    page: int = 1,
    user: auth_db.User = Depends(Requires("view")),
) -> HTMLResponse:
    staff = staff_view(request)
    search = q.strip()
    if not search:
        search = None
    state = None
    # the evidence state is the workflow's, for staff only
    if staff and evidence_state in overview.EVIDENCE_FILTERS:
        state = evidence_state
    page = max(page, 1)
    with db.connect_system() as conn:
        total = overview.trial_count(conn, search, state)
        rows = overview.trial_rows(
            conn,
            default_settings().requirements,
            search,
            state,
            offset=(page - 1) * TRIALS_PER_PAGE,
            limit=TRIALS_PER_PAGE,
            published_only=not staff,
        )
    context = {
        "rows": rows,
        "q": q,
        "evidence_state": evidence_state,
        "filters": overview.EVIDENCE_FILTERS,
        "total": total,
        "page": page,
        "pages": max(1, -(-total // TRIALS_PER_PAGE)),
    }
    return TEMPLATES.TemplateResponse(request, "trials.html", context)


@router.get("/trials/new", response_class=HTMLResponse)
def new_trial_page(
    request: Request, error: str | None = None, user: auth_db.User = Depends(Requires("trial.create"))
) -> HTMLResponse:
    with db.connect_system() as conn:
        schemes = core.identifier_schemes(conn)
        kinds = core.document_kinds(conn)
    context = {"schemes": schemes, "kinds": kinds, "error": error}
    return TEMPLATES.TemplateResponse(request, "new_trial.html", context)


def uploaded_files(files: list[UploadFile]) -> list[manual.UploadedFile]:
    """The files a form sent, without the empty entry a browser sends when none was chosen."""
    uploads = []
    for upload in files:
        if not upload.filename:
            continue
        content = upload.file.read()
        uploads.append(manual.UploadedFile(upload.filename, content))
    return uploads


@router.post("/trials/new")
def add_trial(
    title: Annotated[str, Form()],
    schemes: Annotated[list[str], Form()] = [],
    values: Annotated[list[str], Form()] = [],
    sponsors: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "",
    endpoints: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "results-summary",
    files: Annotated[list[UploadFile], File()] = [],
    user: auth_db.User = Depends(Requires("trial.create")),
) -> Response:
    identifiers = []
    for index, value in enumerate(values):
        if value.strip() and index < len(schemes):
            identifiers.append(Identifier(schemes[index], value.strip()))
    endpoint_lines = []
    for line in endpoints.splitlines():
        if line.strip():
            endpoint_lines.append(line.strip())
    entry = manual.TrialEntry(
        identifiers=identifiers,
        public_title=title.strip(),
        sponsors=sponsors.splitlines(),
        status=or_none(status),
        primary_endpoints=endpoint_lines,
    )
    try:
        # Recorded as the signed-in person
        with db.connect(actor=user.email) as conn:
            trial_id = manual.add_trial(conn, entry, uploaded_files(files), kind)
    except manual.AlreadyKnown as exc:
        return with_error(f"/trials/{exc.trial_id}", f"Already in the database ({exc}). Add documents here instead.")
    except Exception as exc:  # e.g. an identifier that does not fit its scheme: shown on the form
        return with_error("/trials/new", short_error(exc))
    return RedirectResponse(f"/trials/{trial_id}", status_code=303)


@router.post("/trials/{trial_id}/documents")
def upload_document(
    trial_id: int,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str, Form()] = "results-summary",
    title: Annotated[str, Form()] = "",
    user: auth_db.User = Depends(Requires("trial.document.upload")),
) -> Response:
    uploads = uploaded_files([file])
    if not uploads:
        return with_error(f"/trials/{trial_id}", "choose a file to upload")
    try:
        with db.connect(actor=user.email) as conn:
            manual.add_document(conn, trial_id, uploads[0], kind, or_none(title))
    except Exception as exc:  # shown on the trial page
        return with_error(f"/trials/{trial_id}", short_error(exc))
    return RedirectResponse(f"/trials/{trial_id}#documents", status_code=303)


@router.post("/trials/{trial_id}/transfer")
def transfer_trial(
    trial_id: int, source: Annotated[str, Form()], user: auth_db.User = Depends(Requires("trial.transfer"))
) -> Response:
    """Transfer this trial's evidence again from a source (its record, new document versions, page text).
    Every document is downloaded again (recheck): a person asking for one trial again may suspect a file."""
    with db.connect_system() as conn:
        keys = overview.trial_source_keys(conn, trial_id, source)
    if not keys:
        return with_error(f"/trials/{trial_id}", f"this trial has no key at {source} to transfer")
    try:
        with db.connect(actor=user.email) as conn:
            job_id = enqueue_request(conn, JobRequest(step="transfer", source=source, keys=keys, recheck=True))
    except ValueError as exc:
        return with_error(f"/trials/{trial_id}", str(exc))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/trials/{trial_id}/sponsors")
def add_sponsor(
    trial_id: int,
    name: Annotated[str, Form()],
    primary: Annotated[bool, Form()] = False,
    user: auth_db.User = Depends(Requires("trial.sponsor.edit")),
) -> Response:
    """A person adds a sponsor by name: an organisation we hold with that name, else a new one."""
    cleaned = " ".join(name.split())
    if not cleaned:
        return with_error(f"/trials/{trial_id}", "a sponsor needs a name", "#sponsors")
    with db.connect(actor=user.email) as conn:
        core.add_trial_sponsor(conn, trial_id, core.Sponsor(name=cleaned, primary=primary), manual.SOURCE)
    return RedirectResponse(f"/trials/{trial_id}#sponsors", status_code=303)


@router.post("/trials/{trial_id}/sponsors/{organisation_id}/remove")
def remove_sponsor(
    trial_id: int, organisation_id: int, user: auth_db.User = Depends(Requires("trial.sponsor.edit"))
) -> Response:
    """A person removes a sponsor from the trial: kept as removed, and a source listing it again won't bring it back."""
    with db.connect(actor=user.email) as conn:
        core.remove_trial_sponsor(conn, trial_id, organisation_id)
    return RedirectResponse(f"/trials/{trial_id}#sponsors", status_code=303)


@router.get("/sponsors", response_class=HTMLResponse)
def sponsors_page(request: Request, q: str = "", user: auth_db.User = Depends(Requires("view"))) -> HTMLResponse:
    search = q.strip()
    if not search:
        search = None
    with db.connect_system() as conn:
        rows = overview.organisation_rows(conn, search)
    return TEMPLATES.TemplateResponse(request, "sponsors.html", {"rows": rows, "q": q})


@router.get("/sponsors/{organisation_id}", response_class=HTMLResponse)
def sponsor_page(
    request: Request, organisation_id: int, user: auth_db.User = Depends(Requires("view"))
) -> HTMLResponse:
    with db.connect_system() as conn:
        row = overview.organisation_row(conn, organisation_id)
        if row is None:
            raise HTTPException(404, f"no organisation {organisation_id}")
        trials = overview.sponsored_trials(
            conn, organisation_id, default_settings().requirements, published_only=not staff_view(request)
        )
    return TEMPLATES.TemplateResponse(request, "sponsor.html", {"row": row, "trials": trials})


@router.get("/trials/{trial_id}", response_class=HTMLResponse)
def trial_page(
    request: Request,
    trial_id: int,
    run: int | None = None,
    error: str | None = None,
    user: auth_db.User | None = Depends(Requires("view.published", "view.recruiting")),
) -> HTMLResponse:
    staff = staff_view(request)
    with db.connect_system() as conn:
        row = overview.trial_row(conn, trial_id, default_settings().requirements, published_only=not staff)
        if row is None:
            raise HTTPException(404, f"no trial {trial_id}")
        # the public sees only published judgements, each with its whole trace, and only trials that have
        # one, or that recruit now (ADR-008: what someone looking for a trial to join needs)
        judgements = overview.trial_judgements(conn, trial_id, published_only=not staff)
        recruiting = search_db.public_recruiting(conn, trial_id)
        if not staff and not judgements and not recruiting:
            raise HTTPException(404, f"no published result for trial {trial_id}")
        patient = patient_view(conn, trial_id)
        if not staff and not judgements:
            context = {
                "row": row,
                "identifiers": overview.trial_identifiers(conn, trial_id),
                "sponsors": overview.trial_sponsors(conn, trial_id),
                "locations": overview.by_country(overview.trial_locations(conn, trial_id)),
                "patient": patient,
            }
            return TEMPLATES.TemplateResponse(request, "recruiting.html", context)
        registered = overview.trial_registered(conn, trial_id)
        identifiers = overview.trial_identifiers(conn, trial_id)
        documents = overview.trial_documents(conn, trial_id)
        history = overview.trial_history(conn, trial_id)
        sponsors = overview.trial_sponsors(conn, trial_id)
        locations = overview.by_country(overview.trial_locations(conn, trial_id))
        sources = []
        transfers = []
        kinds = []
        gold_items = []
        blocked = {}
        if staff:
            sources = overview.trial_sources(conn, trial_id)
            transfers = overview.trial_transfers(conn, trial_id)
            kinds = core.document_kinds(conn)
            gold_items = gold_items_for(identifiers, judgements)
            default_set = requirement_sets.load(default_settings().requirements)
            blocked = readiness.not_ready(conn, [trial_id], default_set.document_kinds)
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
        "row": row,
        "registered": registered,
        "identifiers": identifiers,
        "sources": sources,
        "documents": documents,
        "judgements": judgements,
        "judgement": chosen,
        "questions": questions,
        "steps": steps,
        "step_titles": STEP_TITLES,
        "calls": calls,
        "prompt_text": prompt_text,
        "files": files_by_hex(steps),
        "history": history,
        "defaults": default_settings(),
        "running": running(),
        "error": error,
        "transfers": transfers,
        "ready": trial_id not in blocked,
        "not_ready": blocked.get(trial_id),
        "transferable": transferable(transfers),
        "kinds": kinds,
        "configs": check_settings.load_all(),
        "judgement_config": judgement_config,
        "gold_items": gold_items,
        "sponsors": sponsors,
        "locations": locations,
        "patient": patient,
    }
    return TEMPLATES.TemplateResponse(request, "trial.html", context)


def transferable(transfers: list[overview.TransferRow]) -> list[overview.TransferRow]:
    """The trial's keys at sources whose adapter can transfer evidence."""
    found = []
    for transfer in transfers:
        if transfer.adapter is None:
            continue
        if registry.adapter_class(transfer.adapter).transfers:
            found.append(transfer)
    return found


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
        pieces.append((text[position : match.start()], False))
        pieces.append((match.group(0), True))
        position = match.end()
    pieces.append((text[position:], False))
    return pieces


def file_hash(sha_hex: str) -> bytes:
    try:
        sha = bytes.fromhex(sha_hex)
    except ValueError as exc:
        raise HTTPException(404, "not a file hash") from exc
    if len(sha) != 32:
        raise HTTPException(404, "not a file hash")
    return sha


@router.get("/files/{sha_hex}", response_class=HTMLResponse)
def file_page(
    request: Request,
    sha_hex: str,
    page: int | None = None,
    quote: str | None = None,
    user: auth_db.User | None = Depends(Requires("view.published")),
) -> HTMLResponse:
    """The page text of one stored file, as retrieval and quote checks see it, with a quote marked."""
    sha = file_hash(sha_hex)
    with db.connect_system() as conn:
        owner = overview.file_owner(conn, sha)
        if owner is None or not (staff_view(request) or overview.file_is_public(conn, sha)):
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


@router.get("/files/{sha_hex}/raw")
def file_raw(
    request: Request, sha_hex: str, user: auth_db.User | None = Depends(Requires("view.published"))
) -> Response:
    """The stored bytes, to open the document itself in the browser."""
    sha = file_hash(sha_hex)
    with db.connect_system() as conn:
        stored = None
        if staff_view(request) or overview.file_is_public(conn, sha):
            stored = overview.file_content(conn, sha)
    if stored is None:
        raise HTTPException(404, "no such file")
    content = stored[0]
    if content.startswith(b"%PDF"):
        # shown in the browser's PDF viewer, with nothing it could run as a page of this site
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Content-Disposition": "inline",
        }
        return Response(content, media_type="application/pdf", headers=headers)
    # anything else (a DOCX a source listed, say) is downloaded, never shown as a page of this site
    headers = {"X-Content-Type-Options": "nosniff", "Content-Disposition": f'attachment; filename="{sha_hex}"'}
    return Response(content, media_type="application/octet-stream", headers=headers)
