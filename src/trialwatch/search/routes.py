"""The public patient search page (/find, ADR-008)."""

import time

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from trialwatch import db, telemetry
from trialwatch.db import auth as auth_db
from trialwatch.db import search as search_db
from trialwatch.search import categories as search_categories
from trialwatch.search import find as finding
from trialwatch.search import query as search_query
from trialwatch.search.constants import AGE_GROUP_WORDS, DISTANCES, PHASE_WORDS
from trialwatch.web.deps import TEMPLATES, Requires

router = APIRouter()

# what the search page offers to try before anything is searched
FIND_EXAMPLES = (
    "diabetes Paris",
    "cancer du sein Lyon",
    "Alzheimer München",
    "healthy volunteers Berlin",
    "mucoviscidose enfant",
    "asthma 28046",
)


@router.get("/find", response_class=HTMLResponse)
def find(
    request: Request,
    q: str = "",
    what: str = "",
    where: str = "",
    km: str = "",
    age: str = "",
    sex: str = "",
    hv: str = "",
    phase: str = "",
    cat: str = "",
    page: int = 1,
    user: auth_db.User | None = Depends(Requires("view.recruiting")),
) -> HTMLResponse:
    """Search the trials that recruit now: a condition, a place, who can join. Nothing about a search is
    stored; the address it is at is never sent on in a Referer (Referrer-Policy), kept by the
    browser's cache, or indexed by search engines."""
    params = finding.params_from(q, what, where, km, age, sex, hv, phase, cat, page)
    result = None
    links = None
    urls = {}
    why = {}
    with db.connect_system() as conn:
        if params.given():
            result = _counted_find(conn, params)
            links = finding.links(params, result)
            urls = result.urls
            why = result.why
        indexed = search_db.recruiting_count(conn)
    examples = []
    for example in FIND_EXAMPLES:
        examples.append((example, finding.example_href(example)))
    context = {
        "params": params,
        "result": result,
        "links": links,
        "examples": examples,
        "urls": urls,
        "why": why,
        "distances": DISTANCES,
        "phase_words": PHASE_WORDS,
        "age_words": AGE_GROUP_WORDS,
        "categories": search_categories.all_categories(),
        "describe": search_query.describe,
        "indexed": indexed,
    }
    response = TEMPLATES.TemplateResponse(request, "find.html", context)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    return response


def _counted_find(conn: psycopg.Connection, params: finding.SearchParams) -> finding.SearchPage:
    """The search, counted by how it went (hits, empty, error) and timed, for Grafana."""
    started = time.monotonic()
    outcome = "error"
    try:
        result = finding.find(conn, params)
        outcome = "hits" if result.total else "empty"
        return result
    finally:
        labels = {"outcome": outcome}
        telemetry.search_requests.add(1, labels)
        telemetry.search_duration.record(time.monotonic() - started, labels)
