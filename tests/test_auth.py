"""Who may do what (ADR-006): the action → role chart, signing in and out, and every route of the web
interface checked for anyone not signed in, a moderator and an admin."""

from datetime import timedelta

import pytest
from conftest import TEST_PASSWORD, signed_in_client
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from test_web import listed_only

from trialwatch.auth import actions, passwords, sessions
from trialwatch.db import auth as auth_db
from trialwatch.db import core
from trialwatch.web import app as web
from trialwatch.web.jobs import Job, JobRequest

CHANGING = ("POST", "PUT", "PATCH", "DELETE")
# The routes that need no action: signing in and out
OPEN_POSTS = ("/login", "/logout")


# --- the chart -----------------------------------------------------------------------------------

def test_the_chart_gives_the_public_only_browsing():
    for action in actions.ACTIONS:
        if action.name == "view":
            assert actions.allowed(None, action.name)
        else:
            assert not actions.allowed(None, action.name), action.name


def test_an_admin_may_do_everything_and_a_moderator_curates():
    admin_only = ["check.run", "source.sync", "source.register", "source.configure", "job.stop.any", "user.manage"]
    for action in actions.ACTIONS:
        assert actions.allowed("admin", action.name), action.name
        if action.name in admin_only:
            assert not actions.allowed("moderator", action.name), action.name
        else:
            assert actions.allowed("moderator", action.name), action.name


def test_an_unknown_action_is_a_mistake_not_a_refusal():
    with pytest.raises(KeyError):
        actions.allowed("admin", "trial.delete")
    with pytest.raises(KeyError):
        web.Requires("no.such.action")


def guards_of(route: APIRoute) -> list[web.Requires]:
    found = []
    for dependency in route.dependant.dependencies:
        if isinstance(dependency.call, web.Requires):
            found.append(dependency.call)
    return found


def test_every_route_that_changes_something_names_its_action():
    unguarded = []
    for route in web.app.routes:
        if not isinstance(route, APIRoute):
            continue
        changes = False
        for method in route.methods:
            if method in CHANGING:
                changes = True
        if changes and route.path not in OPEN_POSTS and not guards_of(route):
            unguarded.append(route.path)
    assert unguarded == []


# --- signing in ----------------------------------------------------------------------------------

def add(conn, username, role):
    core.act_as(conn, "test-setup")
    auth_db.add_user(conn, username, passwords.hashed(TEST_PASSWORD), role)


def test_signing_in_needs_the_right_password_and_an_enabled_user(app_conn):
    add(app_conn, "mona", "moderator")
    assert sessions.sign_in(app_conn, "mona", "wrong password!") is None
    assert sessions.sign_in(app_conn, "nobody", TEST_PASSWORD) is None
    token = sessions.sign_in(app_conn, "MONA ", TEST_PASSWORD)
    assert sessions.user_of(app_conn, token).username == "mona"
    # only the token's hash is stored
    stored = app_conn.execute("SELECT token_sha256 FROM auth.session").fetchall()
    assert stored == [(sessions.token_hash(token),)]
    auth_db.set_disabled(app_conn, "mona", True)
    assert sessions.user_of(app_conn, token) is None
    assert sessions.sign_in(app_conn, "mona", TEST_PASSWORD) is None


def test_a_session_ends_at_sign_out_after_idling_and_on_a_role_change(app_conn, monkeypatch):
    add(app_conn, "mona", "moderator")
    token = sessions.sign_in(app_conn, "mona", TEST_PASSWORD)
    sessions.sign_out(app_conn, token)
    assert sessions.user_of(app_conn, token) is None

    token = sessions.sign_in(app_conn, "mona", TEST_PASSWORD)
    monkeypatch.setattr(sessions, "now", LaterBy(sessions.IDLE + timedelta(minutes=1)))
    assert sessions.user_of(app_conn, token) is None
    monkeypatch.undo()

    token = sessions.sign_in(app_conn, "mona", TEST_PASSWORD)
    auth_db.set_role(app_conn, "mona", "admin")
    auth_db.drop_sessions_of(app_conn, "mona")
    assert sessions.user_of(app_conn, token) is None


class LaterBy:
    """A stand-in for sessions.now: the time `delay` from now."""

    def __init__(self, delay: timedelta) -> None:
        self.moment = sessions.now() + delay

    def __call__(self):
        return self.moment


def test_the_sign_in_page_sets_a_cookie_and_goes_only_to_our_pages(app_conn):
    add(app_conn, "mona", "moderator")
    client = TestClient(web.app)
    wrong = client.post("/login", data={"username": "mona", "password": "nope-nope-nope"}, follow_redirects=False)
    assert "wrong username or password" in wrong.headers["location"].replace("%20", " ")
    right = client.post("/login", data={"username": "mona", "password": TEST_PASSWORD, "next": "//evil.example/"},
                        follow_redirects=False)
    assert (right.status_code, right.headers["location"]) == (303, "/")
    cookie = right.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "mona" in client.get("/").text
    assert client.post("/logout", follow_redirects=False).status_code == 303
    assert sessions.COOKIE not in client.cookies


def test_too_many_wrong_passwords_block_sign_in_for_a_while(app_conn, monkeypatch):
    add(app_conn, "mona", "moderator")
    monkeypatch.setattr(web, "login_throttle", web.LoginThrottle())
    client = TestClient(web.app)
    for attempt in range(web.LoginThrottle.FAILURES):
        client.post("/login", data={"username": "mona", "password": f"wrong-{attempt}-xxxxx"})
    blocked = client.post("/login", data={"username": "mona", "password": TEST_PASSWORD}, follow_redirects=False)
    assert "too many wrong tries" in blocked.headers["location"].replace("%20", " ")


# --- every route, for each role ------------------------------------------------------------------

def post_as(client, path, data=None):
    return client.post(path, data=data, follow_redirects=False)


def test_anyone_may_browse_but_not_act(app_conn):
    trial_id = listed_only(app_conn)
    client = TestClient(web.app)
    for path in ("/", "/trials", f"/trials/{trial_id}", "/sponsors", "/sources", "/sources/ctis", "/prompts",
                 "/requirements", "/gold"):
        assert client.get(path).status_code == 200, path
    # a browser is sent to sign in, and back to the page afterwards
    page = client.get("/trials/new", headers={"accept": "text/html"}, follow_redirects=False)
    assert (page.status_code, page.headers["location"]) == (303, "/login?next=/trials/new")
    # anything else (curl, a script) is refused
    for path, data in (("/jobs", {"step": "evidence", "dry_run": "true"}), ("/trials/new", {"title": "x"}),
                       (f"/trials/{trial_id}/sponsors", {"name": "x"}), ("/sources", {"adapter": "manual"}),
                       ("/sources/ctis/config", {"config": "{}"}), ("/jobs/1/stop", None),
                       ("/admin/users", {"username": "x", "password": "y", "role": "admin"})):
        assert post_as(client, path, data).status_code == 401, path
    assert client.get("/jobs/1/progress").status_code == 401
    assert client.get("/admin/roles").status_code == 401


def test_a_moderator_curates_but_does_not_spend_or_configure(app_conn):
    trial_id = listed_only(app_conn)
    client = signed_in_client(app_conn, "moderator")
    # curating: allowed (each answers with its usual redirect)
    assert client.get("/trials/new").status_code == 200
    added = post_as(client, f"/trials/{trial_id}/sponsors", {"name": "Moderated Pharma"})
    assert added.status_code == 303
    dry = post_as(client, "/jobs", {"step": "evidence", "trial_ids": [str(trial_id)], "dry_run": "true",
                                    "back": f"/trials/{trial_id}"})
    assert dry.status_code == 303 and "error=not%20checked" in dry.headers["location"]
    # spending money, sources and users: refused
    assert post_as(client, "/jobs", {"step": "evidence", "trial_ids": [str(trial_id)]}).status_code == 403
    assert post_as(client, "/jobs", {"step": "list", "source": "ctis"}).status_code == 403
    assert post_as(client, "/sources", {"adapter": "manual"}).status_code == 403
    assert post_as(client, "/sources/ctis/config", {"config": "{}"}).status_code == 403
    assert client.get("/admin/users").status_code == 403
    assert client.get("/admin/roles").status_code == 403
    assert post_as(client, "/admin/users", {"username": "x", "password": "y", "role": "admin"}).status_code == 403
    # the pages offer only what the role allows
    page = client.get(f"/trials/{trial_id}").text
    assert "Add sponsor" in page
    source = client.get("/sources/ctis").text
    assert "Save config" not in source and "Sync list" not in source


def test_stopping_a_job_someone_else_started_needs_an_admin(app_conn, monkeypatch):
    moderator = signed_in_client(app_conn, "moderator", "mona")
    admin = signed_in_client(app_conn, "admin", "ada")
    jobs = web.Jobs()
    monkeypatch.setattr(web, "jobs", jobs)
    theirs = Job(id=1, request=JobRequest(step="transfer", source="ctis", keys=["x"]), actor="ada")
    mine = Job(id=2, request=JobRequest(step="transfer", source="ctis", keys=["x"]), actor="mona")
    jobs._jobs.extend([theirs, mine])
    assert post_as(moderator, "/jobs/1/stop").status_code == 403
    assert post_as(moderator, "/jobs/2/stop").status_code == 303 and mine.stop_requested
    assert post_as(admin, "/jobs/1/stop").status_code == 303 and theirs.stop_requested


def test_an_admin_manages_users_and_the_last_admin_stays(app_conn):
    admin = signed_in_client(app_conn, "admin", "ada")
    assert "user.manage" in admin.get("/admin/roles").text
    added = post_as(admin, "/admin/users", {"username": "mona", "password": TEST_PASSWORD, "role": "moderator"})
    assert added.headers["location"] == "/admin/users"
    short = post_as(admin, "/admin/users", {"username": "max", "password": "short", "role": "moderator"})
    assert "at least" in short.headers["location"].replace("%20", " ")
    assert auth_db.user_by_name(app_conn, "mona").role == "moderator"
    demoted = post_as(admin, "/admin/users/ada/role", {"role": "moderator"})
    assert "last admin" in demoted.headers["location"].replace("%20", " ")
    disabled = post_as(admin, "/admin/users/ada/disable")
    assert "last admin" in disabled.headers["location"].replace("%20", " ")
    assert post_as(admin, "/admin/users/mona/disable").headers["location"] == "/admin/users"
    assert auth_db.user_by_name(app_conn, "mona").disabled_at is not None


class RecordingConnect:
    """db.connect that notes which actor each connection was asked for."""

    def __init__(self, connect) -> None:
        self.connect = connect
        self.actors = []

    def __call__(self, actor=None, actor_kind="person"):
        self.actors.append(actor)
        return self.connect(actor, actor_kind)


def test_changes_are_recorded_as_the_signed_in_person(app_conn, monkeypatch):
    trial_id = listed_only(app_conn)
    client = signed_in_client(app_conn, "moderator", "mona")
    # the tests' connection keeps one actor; the route asks for the signed-in person's
    recording = RecordingConnect(web.db.connect)
    monkeypatch.setattr(web.db, "connect", recording)
    post_as(client, f"/trials/{trial_id}/sponsors", {"name": "Audited Pharma"})
    assert "mona" in recording.actors
    # a user's role changes are in the audit log, without the password hash
    row = app_conn.execute(
        "SELECT new_row FROM core.audit_log WHERE table_name = 'auth.app_user' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0]["username"] == "mona" and "password_hash" not in row[0]
