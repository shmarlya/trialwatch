"""Who may do what (ADR-006): the action → role chart, signing in and out, and every route of the web
interface checked for anyone not signed in, a moderator and an admin."""

from datetime import timedelta

import pytest
from conftest import TEST_PASSWORD, signed_in_client
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from test_web import listed_only

from trialwatch.auth import accounts, actions, passwords, sessions
from trialwatch.db import auth as auth_db
from trialwatch.db import core
from trialwatch.web import app as web
from trialwatch.db import jobs as jobs_db

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
    admin_only = ["check.run", "source.sync", "source.register", "source.configure", "job.stop.any", "job.retry",
                  "schedule.edit", "spend.cap.set", "user.manage"]
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

def add(conn, email, role):
    core.act_as(conn, "test-setup")
    auth_db.add_user(conn, email, passwords.hashed(TEST_PASSWORD), role)


def test_signing_in_needs_the_right_password_and_an_enabled_user(app_conn):
    add(app_conn, "mona@test.example", "moderator")
    assert sessions.sign_in(app_conn, "mona@test.example", "wrong password!") is None
    assert sessions.sign_in(app_conn, "nobody", TEST_PASSWORD) is None
    token = sessions.sign_in(app_conn, " Mona@Test.Example ", TEST_PASSWORD)
    assert sessions.user_of(app_conn, token).email == "mona@test.example"
    # only the token's hash is stored
    stored = app_conn.execute("SELECT token_sha256 FROM auth.session").fetchall()
    assert stored == [(sessions.token_hash(token),)]
    auth_db.set_disabled(app_conn, "mona@test.example", True)
    assert sessions.user_of(app_conn, token) is None
    assert sessions.sign_in(app_conn, "mona@test.example", TEST_PASSWORD) is None


def test_a_session_ends_at_sign_out_after_idling_and_on_a_role_change(app_conn, monkeypatch):
    add(app_conn, "mona@test.example", "moderator")
    token = sessions.sign_in(app_conn, "mona@test.example", TEST_PASSWORD)
    sessions.sign_out(app_conn, token)
    assert sessions.user_of(app_conn, token) is None

    token = sessions.sign_in(app_conn, "mona@test.example", TEST_PASSWORD)
    monkeypatch.setattr(sessions, "now", LaterBy(sessions.IDLE + timedelta(minutes=1)))
    assert sessions.user_of(app_conn, token) is None
    monkeypatch.undo()

    token = sessions.sign_in(app_conn, "mona@test.example", TEST_PASSWORD)
    auth_db.set_role(app_conn, "mona@test.example", "admin")
    auth_db.drop_sessions_of(app_conn, "mona@test.example")
    assert sessions.user_of(app_conn, token) is None


class LaterBy:
    """A stand-in for sessions.now: the time `delay` from now."""

    def __init__(self, delay: timedelta) -> None:
        self.moment = sessions.now() + delay

    def __call__(self):
        return self.moment


def test_the_sign_in_page_sets_a_cookie_and_goes_only_to_our_pages(app_conn):
    add(app_conn, "mona@test.example", "moderator")
    client = TestClient(web.app)
    wrong = client.post("/login", data={"email": "mona@test.example", "password": "nope-nope-nope"}, follow_redirects=False)
    assert "wrong email or password" in wrong.headers["location"].replace("%20", " ")
    right = client.post("/login", data={"email": "mona@test.example", "password": TEST_PASSWORD, "next": "//evil.example/"},
                        follow_redirects=False)
    assert (right.status_code, right.headers["location"]) == (303, "/")
    cookie = right.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "mona@test.example" in client.get("/").text
    assert client.post("/logout", follow_redirects=False).status_code == 303
    assert sessions.COOKIE not in client.cookies


def test_too_many_wrong_passwords_block_sign_in_for_a_while(app_conn, monkeypatch):
    add(app_conn, "mona@test.example", "moderator")
    monkeypatch.setattr(web, "login_throttle", web.LoginThrottle())
    client = TestClient(web.app)
    for attempt in range(web.LoginThrottle.FAILURES):
        client.post("/login", data={"email": "mona@test.example", "password": f"wrong-{attempt}-xxxxx"})
    blocked = client.post("/login", data={"email": "mona@test.example", "password": TEST_PASSWORD}, follow_redirects=False)
    assert "too many wrong tries" in blocked.headers["location"].replace("%20", " ")


# --- every route, for each role ------------------------------------------------------------------

def post_as(client, path, data=None):
    return client.post(path, data=data, follow_redirects=False)


def test_anyone_may_browse_but_not_act(app_conn):
    trial_id = listed_only(app_conn)
    client = TestClient(web.app)
    for path in ("/", "/trials", f"/trials/{trial_id}", "/sponsors", "/prompts", "/requirements"):
        assert client.get(path).status_code == 200, path
    # sources, transfers and gold datasets are for staff (ADR-007)
    for path in ("/sources", "/sources/ctis", "/gold"):
        assert client.get(path).status_code == 401, path
    # a browser is sent to sign in, and back to the page afterwards
    page = client.get("/trials/new", headers={"accept": "text/html"}, follow_redirects=False)
    assert (page.status_code, page.headers["location"]) == (303, "/login?next=/trials/new")
    # anything else (curl, a script) is refused
    for path, data in (("/jobs", {"step": "evidence", "dry_run": "true"}), ("/trials/new", {"title": "x"}),
                       (f"/trials/{trial_id}/sponsors", {"name": "x"}), ("/sources", {"adapter": "manual"}),
                       ("/sources/ctis/config", {"config": "{}"}), ("/jobs/1/stop", None),
                       ("/admin/users", {"email": "x", "password": "y", "role": "admin"}),
                       (f"/trials/{trial_id}/judgements/1/publish", None)):
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
    assert post_as(client, "/admin/users", {"email": "x", "password": "y", "role": "admin"}).status_code == 403
    # the pages offer only what the role allows
    page = client.get(f"/trials/{trial_id}").text
    assert "Add sponsor" in page
    source = client.get("/sources/ctis").text
    assert "Save config" not in source and "Sync list" not in source


def test_stopping_a_job_someone_else_started_needs_an_admin(app_conn):
    moderator = signed_in_client(app_conn, "moderator", "mona@test.example")
    admin = signed_in_client(app_conn, "admin", "ada@test.example")
    core.act_as(app_conn, "ada@test.example")
    theirs = jobs_db.enqueue(app_conn, "transfer", {"source": "ctis", "keys": ["x"]}, max_attempts=1)
    core.act_as(app_conn, "mona@test.example")
    mine = jobs_db.enqueue(app_conn, "transfer", {"source": "ctis", "keys": ["x"]}, max_attempts=1)
    assert post_as(moderator, f"/jobs/{theirs}/stop").status_code == 403
    assert post_as(moderator, f"/jobs/{mine}/stop").status_code == 303
    assert jobs_db.job(app_conn, mine).status == "cancelled"
    assert post_as(admin, f"/jobs/{theirs}/stop").status_code == 303
    assert jobs_db.job(app_conn, theirs).status == "cancelled"
    # trying it again is an admin's
    assert post_as(moderator, f"/jobs/{mine}/retry").status_code == 403
    assert post_as(admin, f"/jobs/{mine}/retry").status_code == 303
    assert jobs_db.job(app_conn, mine).status == "queued"


def test_an_admin_manages_users_and_the_last_admin_stays(app_conn):
    admin = signed_in_client(app_conn, "admin", "ada@test.example")
    assert "user.manage" in admin.get("/admin/roles").text
    added = post_as(admin, "/admin/users", {"email": "mona@test.example", "password": TEST_PASSWORD, "role": "moderator"})
    assert added.headers["location"] == "/admin/users"
    short = post_as(admin, "/admin/users", {"email": "max@test.example", "password": "short", "role": "moderator"})
    assert "at least" in short.headers["location"].replace("%20", " ")
    assert auth_db.user_by_email(app_conn, "mona@test.example").role == "moderator"
    demoted = post_as(admin, "/admin/users/ada@test.example/role", {"role": "moderator"})
    assert "last admin" in demoted.headers["location"].replace("%20", " ")
    disabled = post_as(admin, "/admin/users/ada@test.example/disable")
    assert "last admin" in disabled.headers["location"].replace("%20", " ")
    assert post_as(admin, "/admin/users/mona@test.example/disable").headers["location"] == "/admin/users"
    assert auth_db.user_by_email(app_conn, "mona@test.example").disabled_at is not None


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
    client = signed_in_client(app_conn, "moderator", "mona@test.example")
    # the tests' connection keeps one actor; the route asks for the signed-in person's
    recording = RecordingConnect(web.db.connect)
    monkeypatch.setattr(web.db, "connect", recording)
    post_as(client, f"/trials/{trial_id}/sponsors", {"name": "Audited Pharma"})
    assert "mona@test.example" in recording.actors
    # a user's role changes are in the audit log, without the password hash
    row = app_conn.execute(
        "SELECT new_row FROM core.audit_log WHERE table_name = 'auth.app_user' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0]["email"] == "mona@test.example" and "password_hash" not in row[0]


# --- the admin seeded from the environment -------------------------------------------------------

SEED = {accounts.ADMIN_EMAIL: " Admin@Test.Example ", accounts.ADMIN_PASSWORD: TEST_PASSWORD}


def test_the_seeded_admin_is_added_once_and_never_changed(app_conn):
    core.act_as(app_conn, accounts.SEED_ACTOR, "process")
    assert accounts.seed_admin(app_conn, SEED) == "admin@test.example"
    assert auth_db.user_by_email(app_conn, "admin@test.example").role == "admin"
    assert sessions.sign_in(app_conn, "admin@test.example", TEST_PASSWORD) is not None
    # a later start with another password leaves the user as they are
    changed = {accounts.ADMIN_EMAIL: "admin@test.example", accounts.ADMIN_PASSWORD: "another-password-2"}
    assert accounts.seed_admin(app_conn, changed) is None
    assert sessions.sign_in(app_conn, "admin@test.example", "another-password-2") is None


def test_no_seed_settings_add_nobody_and_half_or_bad_ones_stop_the_start(app_conn):
    assert accounts.seed_admin(app_conn, {}) is None
    for settings in ({accounts.ADMIN_EMAIL: "admin@test.example"},
                     {accounts.ADMIN_PASSWORD: TEST_PASSWORD},
                     {accounts.ADMIN_EMAIL: "not-an-email", accounts.ADMIN_PASSWORD: TEST_PASSWORD},
                     {accounts.ADMIN_EMAIL: "admin@test.example", accounts.ADMIN_PASSWORD: "short"}):
        with pytest.raises(accounts.SeedProblem):
            accounts.seed_admin(app_conn, settings)


def test_the_server_seeds_the_admin_when_it_starts(app_conn, monkeypatch):
    monkeypatch.setenv(accounts.ADMIN_EMAIL, "starter@test.example")
    monkeypatch.setenv(accounts.ADMIN_PASSWORD, TEST_PASSWORD)
    # the tests' shared connection ignores the actor db.connect is asked for, so it is set here
    core.act_as(app_conn, accounts.SEED_ACTOR, "process")
    # entering the client runs the app's start-up
    with TestClient(web.app):
        pass
    assert auth_db.user_by_email(app_conn, "starter@test.example").role == "admin"


def test_users_are_added_by_email_only(app_conn):
    admin = signed_in_client(app_conn, "admin", "ada@test.example")
    refused = post_as(admin, "/admin/users", {"email": "mona", "password": TEST_PASSWORD, "role": "moderator"})
    assert "not an email address" in refused.headers["location"].replace("%20", " ")
    assert auth_db.user_by_email(app_conn, "mona") is None
