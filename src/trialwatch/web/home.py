"""The home page: for the public, what TrialWatch is, the patient search and the trials with a
published result (ADR-007); for staff, the workflow (sync → transfer → check) with the runs and jobs."""

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from trialwatch import db
from trialwatch.checks import requirements as requirement_sets
from trialwatch.checks import settings as check_settings
from trialwatch.db import costs as costs_db
from trialwatch.db import jobs as jobs_db
from trialwatch.db import overview
from trialwatch.jobs import tasks
from trialwatch.jobs.routes import ids_of
from trialwatch.trials import readiness
from trialwatch.web.deps import TEMPLATES, default_settings, running, staff_view

router = APIRouter()
TRIALS_PER_PAGE = 100
# A page further than any list goes (an address someone typed): its offset must still fit the database
LAST_PAGE = 10_000


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    error: str | None = None,
    q: str = "",
    result: str = "",
    status: str = "",
    checked_from: str = "",
    checked_to: str = "",
    country: str = "",
    city: str = "",
    page: int = 1,
) -> HTMLResponse:
    if staff_view(request):
        return workflow(request, error)
    return public_home(request, q, result, status, checked_from, checked_to, country, city, page)


def public_home(
    request: Request,
    q: str,
    result: str,
    status: str,
    checked_from: str,
    checked_to: str,
    country: str,
    city: str,
    page: int,
) -> HTMLResponse:
    """The public's front page: the trials with a published result (ADR-007), nothing of the workflow."""
    defaults = default_settings()
    # an address someone typed may ask for any page: its offset must still fit the database
    page = min(max(page, 1), LAST_PAGE)
    # NUL characters can't be in any text the database holds, and it refuses them in a query
    q = q.replace("\x00", "")
    city = city.replace("\x00", "")
    with db.connect_system() as conn:
        statuses = overview.published_trial_statuses(conn, defaults.requirements)
        countries = overview.published_trial_countries(conn, defaults.requirements)
        found = overview.PublicFilter(
            country=country if country in countries else None,
            city=city.strip() or None,
            search=q.strip() or None,
            label=result if result in overview.PUBLIC_LABELS else None,
            status=status if status in statuses else None,
            checked_from=_day(checked_from),
            checked_to=_day(checked_to),
        )
        total = overview.published_trial_count(conn, defaults.requirements, found)
        rows = overview.published_trial_rows(
            conn, defaults.requirements, found, offset=(page - 1) * TRIALS_PER_PAGE, limit=TRIALS_PER_PAGE
        )
        times = jobs_db.schedule_times(conn)
        spend = costs_db.spend_totals(conn)
    shown = {
        "q": q,
        "result": found.label or "",
        "status": found.status or "",
        "checked_from": found.checked_from.isoformat() if found.checked_from else "",
        "checked_to": found.checked_to.isoformat() if found.checked_to else "",
        "country": found.country or "",
        "city": found.city or "",
    }
    context = {
        "rows": rows,
        "shown": shown,
        "results": overview.PUBLIC_LABELS,
        "statuses": statuses,
        "countries": countries,
        "filtered": found != overview.PublicFilter(),
        "total": total,
        "page": page,
        "pages": max(1, -(-total // TRIALS_PER_PAGE)),
        "defaults": defaults,
        "times": times,
        "spend": spend,
    }
    return TEMPLATES.TemplateResponse(request, "home.html", context)


def workflow(request: Request, error: str | None) -> HTMLResponse:
    """Staff's front page: the workflow's forms, what came in so far, the latest runs and jobs."""
    defaults = default_settings()
    with db.connect_system() as conn:
        coverage = overview.coverage(conn)
        runs = overview.runs(conn)
        default_set = requirement_sets.load(defaults.requirements)
        kinds = list(default_set.document_kinds)
        choices = overview.trial_choices(conn, readiness.ready_trials(conn, kinds), kinds, default_set.name)
        sources = overview.source_rows(conn)
        recent = jobs_db.recent_jobs(conn, 15)
        job_costs = jobs_db.job_costs(conn, ids_of(recent))
        times = jobs_db.schedule_times(conn)
        spend = costs_db.spend_totals(conn)
    context = {
        "times": times,
        "spend": spend,
        "sources": sources,
        "coverage": coverage,
        "runs": runs[:10],
        "jobs": recent,
        "job_costs": job_costs,
        "running": running(),
        "steps": tasks.LABELS,
        "error": error,
        "defaults": defaults,
        "choices": choices,
        "configs": check_settings.load_all(),
    }
    return TEMPLATES.TemplateResponse(request, "workflow.html", context)


def _day(text: str) -> date | None:
    """A YYYY-MM-DD day from a form, or None (empty or not a day: the filter is then not applied)."""
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None
