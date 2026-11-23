"""What every page and route shares: the templates, who is signed in and what they may do (the
action → role chart, ADR-006), and the small helpers forms use. The routes themselves live with their
part (trials/routes.py, checks/routes.py, search/routes.py, gold/routes.py, sources/routes.py,
jobs/routes.py, auth/routes.py, web/home.py); web/app.py puts them together."""

import json
from pathlib import Path
from urllib.parse import quote as url_quote
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from trialwatch import db
from trialwatch.auth import actions, sessions
from trialwatch.checks import settings as check_settings
from trialwatch.db import auth as auth_db
from trialwatch.db import jobs as jobs_db
from trialwatch.db import overview
from trialwatch.geo import countries
from trialwatch.llm import dollars

# The names this machine answers to; TRIALWATCH_HOSTS adds others (comma-separated), e.g. when
# `trialwatch web --host` serves it on a network
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "testserver")


def auth_context(request: Request) -> dict:
    """What every page gets: who is signed in, can(action) to show only the forms they may use, and
    who(name) to show a writer's name (to the public, a person is only "TrialWatch staff")."""
    user = signed_in(request)
    attention = 0
    if user is not None and actions.allowed(user.role, "job.view"):
        with db.connect_system() as conn:
            attention = jobs_db.needing_attention(conn)
    return {
        "user": user,
        "can": Permissions(user),
        "chart_roles": actions.ROLES,
        "who": Who(staff_view(request)),
        "jobs_needing_attention": attention,
    }


TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates", context_processors=[auth_context])
# `dollars(amount)` in a template: money as pages show it ($1.23, $0.0042)
TEMPLATES.env.globals["dollars"] = dollars


def country_names(codes: list[str]) -> list[str]:
    """Countries by code (as search keeps them) as pages name them."""
    return [countries.name_of(code) for code in codes]


TEMPLATES.env.filters["country_names"] = country_names

# Where Grafana is served (compose's lgtm behind Caddy, for admins), and its trace store's data source
GRAFANA = "/grafana"
TEMPO = "tempo"


def trace_link(trace_id: str) -> str:
    """A trace in Grafana's Explore (Tempo), for admins."""
    query = {"refId": "A", "datasource": {"type": "tempo", "uid": TEMPO}, "queryType": "traceql", "query": trace_id}
    panes = {"trace": {"datasource": TEMPO, "queries": [query], "range": {"from": "now-7d", "to": "now"}}}
    return f"{GRAFANA}/explore?schemaVersion=1&orgId=1&panes={url_quote(json.dumps(panes, separators=(',', ':')))}"


TEMPLATES.env.globals["trace_link"] = trace_link
TEMPLATES.env.globals["grafana"] = GRAFANA


def host_name(netloc: str) -> str:
    """The host of a "host:port" (or "[::1]:port") without the port and brackets, lower case."""
    hostname = urlsplit(f"//{netloc}").hostname
    if hostname is None:
        return ""
    return hostname


def default_settings() -> check_settings.Settings:
    """The check's default setup (checks/configs/default.json); pages show labels judged against its
    requirement set."""
    return check_settings.load(check_settings.DEFAULT)


# --- who is signed in, and what they may do (ADR-006) ---------------------------------------------


class SignInNeeded(Exception):
    """Raised by Requires when nobody is signed in; answered by sign_in_needed below."""


class Permissions:
    """`can("trial.create")` in a template: whether the viewer may do the action. Hiding a form is only
    a convenience: every route checks again on the server."""

    def __init__(self, user: auth_db.User | None) -> None:
        self.user = user

    def __call__(self, action: str) -> bool:
        role = None
        if self.user is not None:
            role = self.user.role
        return actions.allowed(role, action)


# How the public sees a person who wrote something (ADR-007): never their email
STAFF = "TrialWatch staff"
# How everyone sees TrialWatch's own writes (db.SYSTEM_ACTOR)
SYSTEM = "TrialWatch (automatic)"


class Who:
    """`who(row.created_by)` in a template: the actor's name for staff; for the public a process
    (a source's adapter, the migration) keeps its name and a person is shown as STAFF."""

    def __init__(self, staff: bool) -> None:
        self.staff = staff
        self._processes: set[str] | None = None

    def __call__(self, name: str | None) -> str | None:
        if name == db.SYSTEM_ACTOR:
            return SYSTEM
        if self.staff or name is None:
            return name
        if self._processes is None:
            with db.connect_system() as conn:
                self._processes = overview.process_actor_names(conn)
        if name in self._processes:
            return name
        return STAFF


def staff_view(request: Request) -> bool:
    """Whether the viewer sees what only staff see (the chart's view.internal)."""
    user = signed_in(request)
    role = None
    if user is not None:
        role = user.role
    return actions.allowed(role, "view.internal")


def signed_in(request: Request) -> auth_db.User | None:
    """The user of the request's session cookie, looked up once per request."""
    if "user_looked_up" in request.scope:
        return request.scope["user"]
    user = None
    token = request.cookies.get(sessions.COOKIE)
    if token:
        with db.connect_system() as conn:
            user = sessions.user_of(conn, token)
    request.scope["user_looked_up"] = True
    request.scope["user"] = user
    return user


class Requires:
    """A route's guard: `Depends(Requires("trial.create"))`. The user must be signed in and their role
    must allow one of the actions (a route whose action depends on its form, such as POST /jobs, names
    them all and checks the exact one itself). Gives the signed-in user, or None for someone not signed
    in when the chart gives one of the actions to the public."""

    def __init__(self, *names: str) -> None:
        for name in names:
            # an unknown name raises here, when the app starts, not when someone first uses the route
            actions.action_named(name)
        self.names = names

    def __call__(self, request: Request) -> auth_db.User | None:
        user = signed_in(request)
        if user is None:
            # an action the chart gives the public needs no sign-in; the user is then None
            for name in self.names:
                if actions.allowed(None, name):
                    return None
            raise SignInNeeded()
        for name in self.names:
            if actions.allowed(user.role, name):
                return user
        raise HTTPException(403, f"your role ({user.role}) may not do this: {', '.join(self.names)}")


def require(user: auth_db.User, name: str) -> None:
    """The check inside a route, for an action only its form tells (refused with 403)."""
    if not actions.allowed(user.role, name):
        raise HTTPException(403, f"your role ({user.role}) may not do this: {name}")


def wants_page(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def safe_next(target: str) -> str:
    """Where to go after signing in: a path on this site only, never another site."""
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


# --- small helpers for routes ----------------------------------------------------------------------


def with_error(path: str, message: str, anchor: str = "") -> RedirectResponse:
    """Back to one of our pages after a form post, with a message it shows above its forms (?error=)."""
    joiner = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{joiner}error={url_quote(message)}{anchor}", status_code=303)


def running() -> jobs_db.Job | None:
    """The job a worker runs now, for pages that say so."""
    with db.connect_system() as conn:
        return jobs_db.running_job(conn)


def or_none(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    return stripped


def short_error(exc: Exception) -> str:
    """The first line of an error, e.g. a database check's message without its context lines."""
    lines = str(exc).splitlines()
    if not lines:
        return type(exc).__name__
    return lines[0]


def whole_number(text: str, name: str) -> int | None:
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
