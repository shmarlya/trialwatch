"""The data sources' pages (ADR-005): registering a source read by an adapter, its config, its trials
and runs."""

import json

from typing import Annotated
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trialwatch import db
from trialwatch.db import core, overview
from trialwatch.jobs import tasks
from trialwatch.sources import registry
from trialwatch.db import auth as auth_db
from trialwatch.web.deps import TEMPLATES, Requires, default_settings, running, short_error, with_error

router = APIRouter()

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


@router.get("/sources", response_class=HTMLResponse)
def sources_page(
    request: Request, error: str | None = None, user: auth_db.User = Depends(Requires("view.internal"))
) -> HTMLResponse:
    with db.connect_system() as conn:
        rows = overview.source_rows(conn)
    context = {
        "rows": rows,
        "free_adapters": _unregistered_adapters(rows),
        "error": error,
        "running": running(),
        "steps": tasks.LABELS,
    }
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


@router.post("/sources")
def register_source(
    adapter: Annotated[str, Form()],
    code: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    user: auth_db.User = Depends(Requires("source.register")),
) -> Response:
    """Register a source read by an adapter, with the adapter's default config (edited on its page)."""
    try:
        adapter_type = registry.adapter_class(adapter)
        chosen_code = code.strip() or adapter_type.name
        with db.connect(actor=user.email) as conn:
            core.register_source(
                conn,
                code=chosen_code,
                kind=adapter_type.kind,
                name=name.strip() or adapter_type.label,
                url=adapter_type.url,
                adapter=adapter,
                config=adapter_type().default_config(),
            )
    except Exception as exc:  # e.g. a code already used: shown on the page
        return with_error("/sources", short_error(exc))
    return RedirectResponse(f"/sources/{chosen_code}", status_code=303)


@router.get("/sources/{code}", response_class=HTMLResponse)
def source_page(
    request: Request,
    code: str,
    state: str = "",
    q: str = "",
    page: int = 1,
    error: str | None = None,
    saved: bool = False,
    user: auth_db.User = Depends(Requires("view.internal")),
) -> HTMLResponse:
    chosen_state = None
    if state in overview.SOURCE_TRIAL_FILTERS:
        chosen_state = state
    search = q.strip() or None
    page = max(page, 1)
    with db.connect_system() as conn:
        row = overview.source_row(conn, code)
        if row is None:
            raise HTTPException(404, f"no source {code}")
        total = overview.source_trial_count(conn, code, chosen_state, search)
        trials = overview.source_trials(
            conn, code, chosen_state, search, offset=(page - 1) * SOURCE_TRIALS_PER_PAGE, limit=SOURCE_TRIALS_PER_PAGE
        )
        runs = overview.source_runs(conn, code)
    adapter = None
    if row.adapter is not None:
        adapter = registry.adapter_class(row.adapter)()
    here = f"/sources/{code}?state={url_quote(state)}&q={url_quote(q)}&page={page}"
    context = {
        "row": row,
        "adapter": adapter,
        "config_text": json.dumps(row.config, indent=2),
        "trials": trials,
        "total": total,
        "page": page,
        "pages": max(1, -(-total // SOURCE_TRIALS_PER_PAGE)),
        "state": state,
        "q": q,
        "filters": overview.SOURCE_TRIAL_FILTERS,
        "choices": core.TRANSFER_CHOICES,
        "runs": runs,
        "running": running(),
        "steps": tasks.LABELS,
        "error": error,
        "saved": saved,
        "here": here,
        "defaults": default_settings(),
    }
    return TEMPLATES.TemplateResponse(request, "source.html", context)


@router.post("/sources/{code}/config")
def save_source_config(
    code: str,
    config: Annotated[str, Form()],
    enabled: Annotated[bool, Form()] = False,
    user: auth_db.User = Depends(Requires("source.configure")),
) -> Response:
    """Replace the source's config (one current config; every run records the one it used)."""
    with db.connect(actor=user.email) as conn:
        source = core.registered_source(conn, code)
        if source is None:
            raise HTTPException(404, f"no source {code}")
        try:
            chosen = source.config
            if source.adapter is not None:
                chosen = _config_from_text(config, registry.adapter_class(source.adapter)())
            core.set_source_config(conn, code, chosen, enabled)
        except ValueError as exc:
            return with_error(f"/sources/{code}", str(exc), "#config")
    return RedirectResponse(f"/sources/{code}?saved=true#config", status_code=303)
