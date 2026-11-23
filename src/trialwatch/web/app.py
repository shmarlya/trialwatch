"""TrialWatch's web interface on the central model (ADR-003): start the workflow steps (sync a
source's list → transfer trials' evidence: record, documents, page text → evidence check; ADR-005) as
background jobs, manage the registered sources, and browse the
trials, their identifiers, sources, documents and history, and every check run with its prompt and a
step-by-step trace of each trial it judged.

Started by `trialwatch web`. This module puts the app together: the requests it answers, the error
pages, health, and each part's routes (trials/routes.py, checks/routes.py, search/routes.py,
gold/routes.py, sources/routes.py, jobs/routes.py, auth/routes.py, web/home.py), which share
web/deps.py. Pages are rendered on the server from templates/; the only scripts are the job page's
polling of its own progress and small conveniences (scroll to a quote). All SQL is in trialwatch.db.

Who may do what is the action → role chart in trialwatch.auth.actions (ADR-006). Anyone not signed in
sees the home page (what TrialWatch is, the trials with a published result with filters, the last and
next scheduled run, and the money spent on model calls), the sign-in page, and what "view.published"
opens: a trial with a published judgement and its whole trace, that run, the prompts, the requirement
sets and the files a registry gave or a published judgement quotes (ADR-007); and the patient search
(/find, ADR-008) with the page of each trial that recruits now, judged or not. Every other page and
every action needs a signed-in admin or moderator whose role the chart allows. Those pages decide once
with staff_view() what to load (only published data for the public). Each route that
changes something names its action (`Depends(Requires(...))`), and its writes are recorded as the
signed-in person. It also answers only requests addressed to this machine's names (a web page can't
reach it through a domain name pointing here) and takes form posts only from its own pages (a web
page elsewhere can't submit a form to it).
"""

import logging
import secrets
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote as url_quote
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from trialwatch import config, db, telemetry
from trialwatch.auth import accounts
from trialwatch.auth import routes as auth_routes
from trialwatch.checks import routes as checks_routes
from trialwatch.gold import routes as gold_routes
from trialwatch.jobs import routes as jobs_routes
from trialwatch.search import routes as search_routes
from trialwatch.sources import routes as sources_routes
from trialwatch.trials import routes as trials_routes
from trialwatch.web import home
from trialwatch.web.deps import LOCAL_HOSTS, SignInNeeded, host_name, wants_page


@asynccontextmanager
async def lifespan(application: FastAPI):
    """On start: open the database connection pool, and add the admin named by TRIALWATCH_ADMIN_EMAIL /
    _PASSWORD if they are not a user yet (trialwatch.auth.accounts); a half-set or invalid seed stops the
    server with the reason. On stop: close the pool."""
    db.open_pool()
    try:
        accounts.seed_from_environment(db.connect)
        yield
    finally:
        db.close_pool()


# Methods that change something: only accepted from the interface's own pages
CHANGING_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def allowed_hosts() -> list[str]:
    hosts = []
    for host in LOCAL_HOSTS:
        hosts.append(host)
    for host in config.extra_hosts():
        hosts.append(host)
    return hosts


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


async def only_this_machine_and_own_pages(request: Request, call_next):
    reason = refusal(request)
    if reason is not None:
        return PlainTextResponse(f"refused: {reason}", status_code=403)
    response = await call_next(request)
    # A Referer never carries a page's address, only this site's origin: a search's address holds what
    # someone looked for, which can be about their health (ADR-008). Not "no-referrer": under that
    # policy browsers send "Origin: null" when one of our own pages posts a form, which refusal()
    # rightly treats as another site, so sign-in and every form would be refused.
    response.headers["Referrer-Policy"] = "strict-origin"
    return response


def sign_in_needed(request: Request, exc: SignInNeeded) -> Response:
    """A browser goes to the sign-in page (back to the page it wanted, for a GET); anything else
    (curl, a script, the job page's polling) gets 401."""
    if not wants_page(request):
        return PlainTextResponse("sign in first", status_code=401)
    target = "/"
    if request.method == "GET":
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
    return RedirectResponse(f"/login?next={url_quote(target)}", status_code=303)


# Error pages: rendered without auth_context, so they need no database (it may be what failed)
ERROR_TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
ERROR_TITLES = {403: "Not allowed", 404: "Not found", 405: "Not allowed", 500: "Something went wrong"}
log = logging.getLogger("trialwatch.web")


def error_page(request: Request, status_code: int, message: str, reference: str | None = None) -> Response:
    title = "Error"
    if status_code in ERROR_TITLES:
        title = ERROR_TITLES[status_code]
    context = {"title": title, "message": message, "reference": reference}
    return ERROR_TEMPLATES.TemplateResponse(request, "error.html", context, status_code=status_code)


async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
    """A browser gets a page saying what went wrong; anything else gets FastAPI's JSON as before."""
    if not wants_page(request):
        return await http_exception_handler(request, exc)
    message = str(exc.detail)
    if exc.status_code == 404 and message == "Not Found":
        message = "There is no page at this address."
    return error_page(request, exc.status_code, message)


async def server_error(request: Request, exc: Exception) -> Response:
    """An error nobody handled: logged with a reference the page shows, so the two can be matched (its
    trace's id when it is traced: an admin finds both in Grafana). The log names the path only: a
    search's query string can be about someone's health (ADR-008)."""
    reference = telemetry.trace_id()
    if reference is None:
        reference = secrets.token_hex(4)
    if request.url.path in telemetry.PRIVATE_PATHS:
        # where it was raised, but not its message: the message may quote what someone searched for
        frames = "".join(traceback.format_tb(exc.__traceback__))
        log.error(
            "unhandled error, reference %s, on %s %s: %s\n%s",
            reference,
            request.method,
            request.url.path,
            type(exc).__name__,
            frames,
        )
    else:
        log.error(
            "unhandled error, reference %s, on %s %s",
            reference,
            request.method,
            request.url.path,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    message = "TrialWatch could not answer this request. It has been logged."
    if not wants_page(request):
        return PlainTextResponse(f"{message} Reference {reference}.", status_code=500)
    return error_page(request, 500, message, reference)


# --- health, for Docker and the proxy in front ----------------------------------------------------

health = APIRouter()


@health.get("/healthz", response_class=PlainTextResponse)
def healthz() -> PlainTextResponse:
    """The server is up. Asks nothing of the database, so a database outage doesn't get the server
    restarted (/readyz says whether it can answer)."""
    return PlainTextResponse("ok")


@health.get("/readyz")
def readyz() -> JSONResponse:
    """The server can answer: the database is reachable, at this migration. Says nothing about why
    not, to anyone: the log has it."""
    try:
        version = db.migration_version()
    except psycopg.Error:
        log.warning("not ready: the database can't be reached", exc_info=True)
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok", "migration": version})


# --- the app ----------------------------------------------------------------------------------------

PARTS = (home, trials_routes, checks_routes, search_routes, gold_routes, sources_routes, jobs_routes, auth_routes)


def create_app() -> FastAPI:
    """The web interface: its pages, the checks every request passes, its error pages and health; traced
    when OpenTelemetry is set up (telemetry.py)."""
    application = FastAPI(title="TrialWatch", lifespan=lifespan)
    # The brand and UI components: static/brand.css (tokens), static/components.css, the logo and favicon
    application.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    application.middleware("http")(only_this_machine_and_own_pages)
    application.add_exception_handler(SignInNeeded, sign_in_needed)
    application.add_exception_handler(StarletteHTTPException, http_error)
    application.add_exception_handler(Exception, server_error)
    application.include_router(health)
    for part in PARTS:
        application.include_router(part.router)
    telemetry.instrument_app(application)
    return application


# Sent to Grafana when OTEL_EXPORTER_OTLP_ENDPOINT is set (compose); otherwise nothing is recorded
telemetry.setup("trialwatch-web")
app = create_app()
