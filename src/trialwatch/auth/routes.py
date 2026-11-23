"""Signing in and out, the users (admins), and the action → role chart (ADR-006)."""

import threading
import time

from typing import Annotated
from urllib.parse import quote as url_quote

import psycopg
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from trialwatch import db
from trialwatch.auth import accounts, actions, passwords, sessions
from trialwatch.db import auth as auth_db
from trialwatch.web.deps import (
    LOCAL_HOSTS,
    host_name,
    safe_next,
    signed_in,
    TEMPLATES,
    Requires,
    short_error,
    wants_page,
    with_error,
)

router = APIRouter()


class LoginThrottle:
    """Slows down password guessing: after FAILURES tries for a name or from an address within WINDOW
    seconds that did not sign in, sign-in for it is refused until the window has passed. Kept in memory,
    per process. A try counts before its password is checked, so tries sent at the same moment can't all
    get past the limit; one that signs in is forgotten again."""

    FAILURES = 5
    WINDOW = 15 * 60
    # past this many names and addresses, the ones without a recent try are forgotten
    SWEEP_AFTER = 10_000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        kept = []
        for moment in self._failures.get(key, []):
            if now - moment < self.WINDOW:
                kept.append(moment)
        if kept:
            self._failures[key] = kept
        elif key in self._failures:
            del self._failures[key]
        return kept

    def _sweep(self, now: float) -> None:
        for key in list(self._failures):
            self._recent(key, now)

    def try_counted(self, keys: list[str]) -> bool:
        """Count a try for each key; False (and nothing counted) when one of them has had too many."""
        now = time.monotonic()
        with self._lock:
            if len(self._failures) > self.SWEEP_AFTER:
                self._sweep(now)
            for key in keys:
                if len(self._recent(key, now)) >= self.FAILURES:
                    return False
            for key in keys:
                tries = self._recent(key, now)
                tries.append(now)
                self._failures[key] = tries
        return True

    def succeeded(self, keys: list[str]) -> None:
        with self._lock:
            for key in keys:
                if key in self._failures:
                    del self._failures[key]


login_throttle = LoginThrottle()


def throttle_keys(request: Request, email: str) -> list[str]:
    address = "unknown"
    if request.client is not None:
        address = request.client.host
    # an email longer than any allowed one is cut: a key must not keep whatever size was sent
    name = accounts.normalised_email(email[: accounts.LONGEST_EMAIL + 1])
    return [f"user:{name}", f"address:{address}"]


def secure_cookie(request: Request) -> bool:
    """Over a network the session cookie is sent only over HTTPS; on this machine plain http works."""
    return host_name(request.headers.get("host", "")) not in LOCAL_HOSTS


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str | None = None) -> Response:
    if signed_in(request) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return TEMPLATES.TemplateResponse(request, "login.html", {"next": safe_next(next), "error": error})


@router.post("/login")
def login(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    keys = throttle_keys(request, email)
    target = safe_next(next)
    if not login_throttle.try_counted(keys):
        return with_error(f"/login?next={url_quote(target)}", "too many wrong tries: wait a few minutes")
    token = None
    # a NUL can't be in any stored email, and the database refuses it in a query
    if "\x00" not in email:
        with db.connect_system() as conn:
            token = sessions.sign_in(conn, email, password)
    if token is None:
        return with_error(f"/login?next={url_quote(target)}", "wrong email or password")
    login_throttle.succeeded(keys)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        sessions.COOKIE,
        token,
        max_age=int(sessions.LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=secure_cookie(request),
        path="/",
    )
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    with db.connect_system() as conn:
        sessions.sign_out(conn, request.cookies.get(sessions.COOKIE))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(sessions.COOKIE, path="/")
    return response


# --- Grafana (admins): signed in through TrialWatch ------------------------------------------------

# The header Grafana takes as who is signed in (GF_AUTH_PROXY_HEADER_NAME, compose.yaml); the proxy
# removes any a browser sends and sets it only from this answer (ops/caddy/Caddyfile)
GRAFANA_USER_HEADER = "X-WEBAUTH-USER"


@router.get("/auth/grafana")
def grafana_access(request: Request) -> Response:
    """Asked by the proxy (Caddy's forward_auth) before every request to Grafana (/grafana/): 200 with the
    signed-in admin's email in GRAFANA_USER_HEADER, which Grafana believes (auth.proxy). A browser not
    signed in is sent to sign in and then back to Grafana; anyone else is refused."""
    user = signed_in(request)
    if user is None:
        if wants_page(request):
            target = request.headers.get("x-forwarded-uri", "")
            if not target.startswith("/grafana"):
                target = "/grafana/"
            return RedirectResponse(f"/login?next={url_quote(safe_next(target))}", status_code=303)
        return PlainTextResponse("sign in first", status_code=401)
    if not actions.allowed(user.role, "ops.view"):
        return PlainTextResponse(f"your role ({user.role}) may not open Grafana", status_code=403)
    return PlainTextResponse("ok", headers={GRAFANA_USER_HEADER: user.email})


# --- users and the chart (admins) ----------------------------------------------------------------


@router.get("/admin/users", response_class=HTMLResponse)
def users_page(
    request: Request, error: str | None = None, user: auth_db.User = Depends(Requires("user.manage"))
) -> HTMLResponse:
    with db.connect_system() as conn:
        rows = auth_db.users(conn)
    context = {"rows": rows, "roles": actions.ROLES, "error": error, "minimum": passwords.MINIMUM_LENGTH}
    return TEMPLATES.TemplateResponse(request, "admin_users.html", context)


def _is_last_admin(conn, email: str) -> bool:
    """Whether `email` is the only enabled admin (then they can't be demoted or disabled: nobody
    would be left to manage users)."""
    target = auth_db.user_by_email(conn, email)
    if target is None or target.role != actions.ADMIN or target.disabled_at is not None:
        return False
    return auth_db.active_admins(conn) <= 1


@router.post("/admin/users")
def add_user(
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()],
    user: auth_db.User = Depends(Requires("user.manage")),
) -> Response:
    if role not in actions.ROLES:
        return with_error("/admin/users", f"unknown role {role!r}")
    chosen = accounts.normalised_email(email)
    problem = accounts.email_problem(chosen)
    if problem is not None:
        return with_error("/admin/users", problem)
    try:
        password_hash = passwords.hashed(password)
        with db.connect(actor=user.email) as conn:
            auth_db.add_user(conn, chosen, password_hash, role)
    except ValueError as exc:
        return with_error("/admin/users", str(exc))
    except psycopg.Error as exc:  # an email already taken
        return with_error("/admin/users", short_error(exc))
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{email}/role")
def change_role(
    email: str, role: Annotated[str, Form()], user: auth_db.User = Depends(Requires("user.manage"))
) -> Response:
    if role not in actions.ROLES:
        return with_error("/admin/users", f"unknown role {role!r}")
    with db.connect(actor=user.email) as conn:
        if role != actions.ADMIN and _is_last_admin(conn, email):
            return with_error("/admin/users", "the last admin can't be given another role")
        if not auth_db.set_role(conn, email, role):
            raise HTTPException(404, f"no user {email}")
        auth_db.drop_sessions_of(conn, email)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{email}/password")
def reset_password(
    email: str, password: Annotated[str, Form()], user: auth_db.User = Depends(Requires("user.manage"))
) -> Response:
    try:
        password_hash = passwords.hashed(password)
    except ValueError as exc:
        return with_error("/admin/users", str(exc))
    with db.connect(actor=user.email) as conn:
        if not auth_db.set_password_hash(conn, email, password_hash):
            raise HTTPException(404, f"no user {email}")
        auth_db.drop_sessions_of(conn, email)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{email}/disable")
def disable_user(email: str, user: auth_db.User = Depends(Requires("user.manage"))) -> Response:
    with db.connect(actor=user.email) as conn:
        if _is_last_admin(conn, email):
            return with_error("/admin/users", "the last admin can't be disabled")
        if not auth_db.set_disabled(conn, email, True):
            raise HTTPException(404, f"no user {email}")
        auth_db.drop_sessions_of(conn, email)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{email}/enable")
def enable_user(email: str, user: auth_db.User = Depends(Requires("user.manage"))) -> Response:
    with db.connect(actor=user.email) as conn:
        if not auth_db.set_disabled(conn, email, False):
            raise HTTPException(404, f"no user {email}")
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/admin/roles", response_class=HTMLResponse)
def roles_page(request: Request, user: auth_db.User = Depends(Requires("user.manage"))) -> HTMLResponse:
    """The action → role chart, drawn from trialwatch.auth.actions itself."""
    columns = [actions.PUBLIC]
    for role in actions.ROLES:
        columns.append(role)
    return TEMPLATES.TemplateResponse(request, "admin_roles.html", {"chart": actions.ACTIONS, "columns": columns})
