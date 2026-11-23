"""The public pages of checks (ADR-007): a run with its setup and judgements, the prompts and the
requirement sets as they are now, and a moderator withdrawing a published judgement."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trialwatch import db
from trialwatch.checks import prompts
from trialwatch.checks import requirements as requirement_sets
from trialwatch.db import checks as checks_db
from trialwatch.db import overview
from trialwatch.db import auth as auth_db
from trialwatch.web.deps import TEMPLATES, Requires, default_settings, or_none, staff_view, with_error

router = APIRouter()


@router.post("/trials/{trial_id}/judgements/{run_id}/withdraw")
def withdraw_judgement(
    trial_id: int,
    run_id: int,
    note: Annotated[str, Form()] = "",
    user: auth_db.User = Depends(Requires("judgement.withdraw")),
) -> Response:
    """Withdraw a published judgement, with a reason (ADR-007): it stays public, marked withdrawn with
    the reason (audited)."""
    reason = or_none(note)
    if reason is None:
        return with_error(f"/trials/{trial_id}?run={run_id}", "say why it is withdrawn: the public sees the reason")
    with db.connect(actor=user.email) as conn:
        checks_db.withdraw_judgement(conn, run_id, trial_id, reason)
    return RedirectResponse(f"/trials/{trial_id}?run={run_id}", status_code=303)


@router.get("/prompts", response_class=HTMLResponse)
def prompts_page(
    request: Request, show: str | None = None, user: auth_db.User | None = Depends(Requires("view.published"))
) -> HTMLResponse:
    """Every prompt file as it is now, and every prompt text check runs used (an edited file is a
    different text: the runs keep the one they sent)."""
    with db.connect_system() as conn:
        uses = overview.prompt_uses(conn)
        used_texts = {}
        for use in uses:
            key = use.prompt_sha256.hex()
            used_texts[key] = overview.run_prompt_text(conn, use.prompt_sha256)
    current = []
    current_hashes = set()
    for name in prompts.prompt_names():
        prompt = prompts.load_prompt(name)
        current.append(prompt)
        current_hashes.add(prompt.sha256.hex())
    context = {
        "current": current,
        "current_hashes": current_hashes,
        "uses": uses,
        "used_texts": used_texts,
        "show": show,
        "default_prompt": default_settings().prompt,
    }
    return TEMPLATES.TemplateResponse(request, "prompts.html", context)


@router.get("/requirements", response_class=HTMLResponse)
def requirements_page(
    request: Request, show: str | None = None, user: auth_db.User | None = Depends(Requires("view.published"))
) -> HTMLResponse:
    """Every requirement set as it is now: what a trial must report and how "enough" is decided."""
    sets = []
    for name in requirement_sets.set_names():
        sets.append(requirement_sets.load(name))
    context = {
        "sets": sets,
        "show": show,
        "default_set": default_settings().requirements,
        "rule_types": requirement_sets.RULE_TYPES,
    }
    return TEMPLATES.TemplateResponse(request, "requirements.html", context)


# --- check runs ----------------------------------------------------------------------------------


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(
    request: Request, run_id: int, user: auth_db.User | None = Depends(Requires("view.published"))
) -> HTMLResponse:
    with db.connect_system() as conn:
        run = overview.run_row(conn, run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        prompt_text, params = overview.run_prompt(conn, run_id)
        # the run's setup is public; its judgements only once published, and its tallies only for staff;
        # the public opens only a run with a published judgement
        judgements = overview.run_judgements(conn, run_id, published_only=not staff_view(request))
        if not judgements and not staff_view(request):
            raise HTTPException(404, f"no run {run_id}")
    context = {"run": run, "prompt_text": prompt_text, "params": params, "judgements": judgements}
    return TEMPLATES.TemplateResponse(request, "run.html", context)
