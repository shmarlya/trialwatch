"""Jobs (src/trialwatch/jobs/): the queue in the database, the worker's attempts and retries, the
weekly refresh that syncs, transfers and checks what changed, and the spending cap that holds automatic
checks. Against a fake CTIS and a fake model."""

import contextlib
import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import psycopg
import pytest
from test_evidence import CT, CT_2, GOOD, PAGES, FakeClient, PageTexts, payload, seed
from test_sources_ctis import FakeCtis, listed_row

from trialwatch import db
from trialwatch.db import SESSION_OPTIONS, core
from trialwatch.db import costs as costs_db
from trialwatch.db import jobs as jobs_db
from trialwatch.jobs import MAX_ATTEMPTS, refresh, retry, steps, worker
from trialwatch.sources import ctis


def queue(conn, kind, params, actor="test-person", **options):
    core.act_as(conn, actor)
    if "max_attempts" not in options:
        options["max_attempts"] = MAX_ATTEMPTS[kind]
    # the prepared options are passed as keyword arguments to the library call
    return jobs_db.enqueue(conn, kind, params, **options)


def work(app_conn, monkeypatch):
    """Run the worker until nothing is due; returns the jobs it ran."""
    return worker.run(once=True)


def due_now(conn, job_id):
    conn.execute("UPDATE jobs.job SET run_after = now() WHERE id = %s", (job_id,))


def line_texts(conn, job_id):
    texts = []
    for line in jobs_db.lines(conn, job_id):
        texts.append(line.line)
    return texts


class ManyCtis(FakeCtis):
    """A CTIS that has a record for each listed trial (not one record for all)."""

    def __init__(self, records, listing):
        super().__init__(records[CT], listing)
        self.records = records

    def retrieve(self, ct_number):
        if self.retrieve_error is not None:
            raise self.retrieve_error
        return copy.deepcopy(self.records[ct_number])


class Model:
    """The fake model the worker's checks call: GOOD answers, as many as asked."""

    def __init__(self):
        self.client = FakeClient()
        self.client.replies = _endless_good()

    def __call__(self):
        return self.client


class _endless_good(list):
    def pop(self, index=-1):
        return GOOD


@pytest.fixture
def fake_world(app_conn, monkeypatch):
    """CTIS lists CT and CT_2, each with a results summary whose pages read as PAGES; the model answers GOOD."""
    fake = ManyCtis({CT: payload(CT), CT_2: payload(CT_2)}, [listed_row(CT), listed_row(CT_2)])

    @contextlib.contextmanager
    def open_fake(name):
        yield ctis.CtisAdapter(fake)

    monkeypatch.setattr(steps.registry, "open_adapter", open_fake)
    monkeypatch.setattr("trialwatch.trials.pages.extract_pages", PageTexts(PAGES))
    model = Model()
    monkeypatch.setattr(steps, "anthropic_client", model)
    # the weekly schedule is not due in these tests
    core.act_as(app_conn, "test-person")
    app_conn.execute("UPDATE jobs.schedule SET enabled = false")
    return fake, model


# --- the queue -----------------------------------------------------------------------------------

def test_a_job_is_claimed_once_and_its_attempt_counted(conn):
    first = queue(conn, "sync", {"source": "ctis"})
    second = queue(conn, "sync", {"source": "ctis"}, priority=10)
    claimed = jobs_db.claim_next(conn, "w1")
    # lower priority first
    assert (claimed.id, claimed.status, claimed.attempts, claimed.locked_by) == (second, "running", 1, "w1")
    assert jobs_db.claim_next(conn, "w2").id == first
    assert jobs_db.claim_next(conn, "w3") is None


def test_two_workers_never_take_the_same_job(database_url):
    one = psycopg.connect(database_url, autocommit=True, options=SESSION_OPTIONS)
    two = psycopg.connect(database_url, autocommit=True, options=SESSION_OPTIONS)
    created = []
    try:
        core.act_as(one, "test-person")
        core.act_as(two, "test-person")
        for _ in range(2):
            created.append(jobs_db.enqueue(one, "sync", {"source": "nowhere"}, max_attempts=1, priority=1))
        taken = [jobs_db.claim_next(one, "w1").id, jobs_db.claim_next(two, "w2").id]
        assert sorted(taken) == sorted(created)
    finally:
        one.execute("DELETE FROM jobs.job_line WHERE job_id = ANY(%s)", (created,))
        one.execute("DELETE FROM jobs.job WHERE id = ANY(%s)", (created,))
        one.close()
        two.close()


def test_a_failed_attempt_is_tried_later_then_left_failed(app_conn, monkeypatch):
    job_id = queue(app_conn, "sync", {"source": "ctis"}, max_attempts=2)

    def failing(ctx, conn, job):
        raise httpx.ConnectError("CTIS is down")

    monkeypatch.setitem(worker.RUNNERS, "sync", failing)
    assert work(app_conn, monkeypatch) == 1
    job = jobs_db.job(app_conn, job_id)
    assert (job.status, job.attempts, job.last_error) == ("queued", 1, "ConnectError: CTIS is down")
    # it waits about a minute before the next attempt
    waits = app_conn.execute("SELECT run_after - now() FROM jobs.job WHERE id = %s", (job_id,)).fetchone()[0]
    assert timedelta(seconds=50) < waits < timedelta(seconds=70)
    due_now(app_conn, job_id)
    work(app_conn, monkeypatch)
    job = jobs_db.job(app_conn, job_id)
    assert (job.status, job.attempts) == ("failed", 2)
    assert job.finished_at is not None and jobs_db.needing_attention(app_conn) == 1
    assert "— attempt" not in "".join(line_texts(app_conn, job_id))
    assert "attempt 2 of 2" in line_texts(app_conn, job_id)[-3]
    # an admin tries it once more
    assert jobs_db.retry_now(app_conn, job_id)
    job = jobs_db.job(app_conn, job_id)
    assert (job.status, job.max_attempts, job.finished_at) == ("queued", 3, None)


def test_a_permanent_error_fails_at_once(app_conn, monkeypatch):
    unknown = queue(app_conn, "sync", {"source": "nowhere"})
    bad_setup = queue(app_conn, "check", {"every": True, "config": "../default"})
    work(app_conn, monkeypatch)
    assert (jobs_db.job(app_conn, unknown).status, jobs_db.job(app_conn, unknown).attempts) == ("failed", 1)
    assert jobs_db.job(app_conn, unknown).last_error == "no source 'nowhere' is registered"
    assert jobs_db.job(app_conn, bad_setup).status == "failed"
    assert "no setup '../default'" in jobs_db.job(app_conn, bad_setup).last_error


def test_the_backoff_grows_and_only_passing_errors_are_retried():
    assert timedelta(minutes=1) <= retry.backoff(1) <= timedelta(minutes=1.1)
    assert timedelta(minutes=5) <= retry.backoff(2) <= timedelta(minutes=5.5)
    assert timedelta(hours=6) <= retry.backoff(9) <= timedelta(hours=6.6)
    assert retry.is_transient("InternalServerError: overloaded")
    assert retry.is_transient("widening: APIConnectionError: Connection error.")
    assert not retry.is_transient("no readable page text in its documents (scanned PDF?)")
    assert not retry.is_transient("stop_reason refusal (cyber)")
    assert not retry.is_transient(None)


def test_a_job_whose_worker_died_goes_back_to_the_queue(conn):
    job_id = queue(conn, "sync", {"source": "ctis"})
    jobs_db.claim_next(conn, "dead-worker")
    assert jobs_db.requeue_stalled(conn, timedelta(minutes=5)) == []
    conn.execute("UPDATE jobs.job SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s", (job_id,))
    assert jobs_db.requeue_stalled(conn, timedelta(minutes=5)) == [job_id]
    job = jobs_db.job(conn, job_id)
    assert (job.status, job.attempts, job.locked_by) == ("queued", 1, None)
    assert "stopped" in job.last_error


def test_cancelling_a_queued_job_ends_it_and_a_running_one_after_its_item(conn):
    waiting = queue(conn, "sync", {"source": "ctis"})
    assert jobs_db.request_cancel(conn, waiting) == "cancelled"
    assert jobs_db.job(conn, waiting).status == "cancelled"
    assert jobs_db.request_cancel(conn, waiting) is None
    running = queue(conn, "sync", {"source": "ctis"})
    jobs_db.claim_next(conn, "w")
    assert jobs_db.request_cancel(conn, running) == "running"
    job = jobs_db.job(conn, running)
    ctx = steps.Context(conn, job)
    assert ctx.should_stop() and ctx.cancelled


# --- the steps ----------------------------------------------------------------------------------

def test_a_check_job_judges_its_trials_as_its_creator_and_records_the_job(app_conn, fake_world, monkeypatch):
    trial = seed(app_conn)
    job_id = queue(app_conn, "check", {"trial_ids": [trial], "config": "default"}, actor="ada@test.example")
    work(app_conn, monkeypatch)
    job = jobs_db.job(app_conn, job_id)
    assert job.status == "succeeded", job.last_error
    run_id = job.result["check_run"]
    assert jobs_db.runs_of(app_conn, job_id).check_runs == [run_id]
    calls = app_conn.execute("SELECT job_id, cost_usd FROM core.api_call WHERE check_run_id = %s", (run_id,)).fetchall()
    assert calls == [(job_id, Decimal("0.000075"))]
    assert jobs_db.job_cost(app_conn, job_id) == Decimal("0.000075")
    assert job.result["cost"] == "$0.0001"


def test_a_check_retry_judges_only_trials_whose_call_failed_for_a_passing_reason(app_conn, fake_world, monkeypatch):
    first = seed(app_conn)
    second = seed(app_conn, ct_number=CT_2)
    fake, model = fake_world
    overloaded = httpx.Response(529, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    import anthropic

    replies = [GOOD, anthropic.InternalServerError("overloaded", response=overloaded, body=None)]
    model.client.replies = replies
    job_id = queue(app_conn, "check", {"trial_ids": [first, second], "config": "default"})
    work(app_conn, monkeypatch)
    job = jobs_db.job(app_conn, job_id)
    assert (job.status, job.attempts) == ("queued", 1)
    assert "InternalServerError" in job.last_error
    model.client.replies = _endless_good()
    model.client.requests = []
    due_now(app_conn, job_id)
    work(app_conn, monkeypatch)
    job = jobs_db.job(app_conn, job_id)
    assert job.status == "succeeded", job.last_error
    # only the trial whose call failed was asked again
    assert len(model.client.requests) == 1
    assert len(jobs_db.runs_of(app_conn, job_id).check_runs) == 2


def test_a_transfer_retry_takes_only_the_keys_not_transferred_yet(app_conn, fake_world, monkeypatch):
    fake, model = fake_world
    job_id = queue(app_conn, "transfer", {"source": "ctis", "keys": [CT, CT_2]})
    job = jobs_db.claim_next(app_conn, "w")
    app_conn.execute("UPDATE jobs.job SET attempts = 2 WHERE id = %s", (job_id,))
    worker.run_one(app_conn, jobs_db.job(app_conn, job_id), "w")
    assert jobs_db.job(app_conn, job_id).status == "succeeded"
    assert steps.transfer_keys(app_conn, jobs_db.job(app_conn, job_id)) == []
    assert job.attempts == 1


# --- the weekly refresh --------------------------------------------------------------------------

def refresh_job(conn):
    return queue(conn, "refresh", {}, actor=db.SYSTEM_ACTOR, automatic=True)


def checked_trials(model):
    return len(model.client.requests)


def test_the_refresh_syncs_transfers_and_checks_what_is_new(app_conn, fake_world, monkeypatch):
    fake, model = fake_world
    job_id = refresh_job(app_conn)
    work(app_conn, monkeypatch)
    job = jobs_db.job(app_conn, job_id)
    assert job.status == "succeeded", (job.last_error, line_texts(app_conn, job_id))
    kinds = []
    for child in jobs_db.children(app_conn, job_id):
        kinds.append((child.kind, child.status, child.automatic))
    assert kinds == [("sync", "succeeded", True), ("transfer", "succeeded", True), ("check", "succeeded", True)]
    assert job.result["to_check"] == 2 and job.result["children"]["check succeeded"] == 1
    assert checked_trials(model) == 2
    # the check runs are TrialWatch's own
    actors = app_conn.execute("SELECT DISTINCT a.name FROM checks.run r JOIN core.actor a ON a.id = r.actor_id "
                              "JOIN jobs.job j ON j.id = r.job_id WHERE j.parent_id = %s", (job_id,)).fetchall()
    assert actors == [(db.SYSTEM_ACTOR,)]

    # a week later nothing changed: nothing is transferred or checked, nothing spent
    second = refresh_job(app_conn)
    work(app_conn, monkeypatch)
    job = jobs_db.job(app_conn, second)
    assert job.status == "succeeded"
    assert job.result["to_transfer"] == {"ctis": 0} and job.result["to_check"] == 0
    assert checked_trials(model) == 2

    # CTIS changed one trial's results summary: only that trial is transferred and checked again
    fake.records[CT]["documents"][0]["systemVersion"] = "2"
    fake.listing = [listed_row(CT, updated=datetime.now(UTC).strftime("%d/%m/%Y")), listed_row(CT_2)]
    third = refresh_job(app_conn)
    work(app_conn, monkeypatch)
    job = jobs_db.job(app_conn, third)
    assert job.status == "succeeded", job.last_error
    assert job.result["to_transfer"] == {"ctis": 1} and job.result["to_check"] == 1
    assert checked_trials(model) == 3


def test_a_failed_transfer_fails_the_refresh_but_the_rest_is_still_checked(app_conn, fake_world, monkeypatch):
    fake, model = fake_world
    monkeypatch.setitem(MAX_ATTEMPTS, "transfer", 1)
    monkeypatch.setattr(refresh, "TRANSFER_BATCH", 1)
    original = fake.retrieve

    def broken_for_ct2(ct_number):
        if ct_number == CT_2:
            raise httpx.ConnectError("CTIS is down")
        return original(ct_number)

    fake.retrieve = broken_for_ct2
    job_id = refresh_job(app_conn)
    work(app_conn, monkeypatch)
    job = jobs_db.job(app_conn, job_id)
    assert job.status == "failed"
    assert "transfer" in job.last_error
    # the trial that came in was checked
    assert checked_trials(model) == 1
    assert job.result["children"]["transfer failed"] == 1


# --- the spending cap ----------------------------------------------------------------------------

def test_an_automatic_check_over_the_cap_is_held_and_a_persons_still_runs(app_conn, fake_world, monkeypatch):
    fake, model = fake_world
    trial = seed(app_conn)
    core.act_as(app_conn, "ada@test.example")
    costs_db.set_monthly_cap(app_conn, Decimal("0"))
    automatic = queue(app_conn, "check", {"trial_ids": [trial], "config": "default"}, actor=db.SYSTEM_ACTOR,
                      automatic=True)
    work(app_conn, monkeypatch)
    job = jobs_db.job(app_conn, automatic)
    assert (job.status, job.attempts) == ("held", 0)
    assert checked_trials(model) == 0
    assert jobs_db.needing_attention(app_conn) == 1
    manual = queue(app_conn, "check", {"trial_ids": [trial], "config": "default"}, actor="ada@test.example")
    work(app_conn, monkeypatch)
    assert jobs_db.job(app_conn, manual).status == "succeeded"
    assert checked_trials(model) == 1
    # the cap is raised: the worker resumes the held job by itself
    costs_db.set_monthly_cap(app_conn, Decimal("100"))
    work(app_conn, monkeypatch)
    assert jobs_db.job(app_conn, automatic).status == "succeeded"
    assert checked_trials(model) == 2


# --- automatic publishing is TrialWatch's --------------------------------------------------------

def test_a_finished_runs_judgements_are_public_with_their_agreement(conn, monkeypatch):
    from trialwatch.checks import publishing
    from test_evidence import start_a_run

    trial = seed(conn)
    run_id = start_a_run(conn)
    conn.execute("INSERT INTO checks.judgement (run_id, trial_id, label, parts) VALUES (%s, %s, 'full', '{}')",
                 (run_id, trial))
    conn.execute("UPDATE checks.run SET status = 'succeeded', finished_at = now() WHERE id = %s", (run_id,))
    person = core.current_actor(conn)
    assert publishing.publish_run(conn, run_id) == [trial]
    # nobody publishes: the run's finishing does; it records how its kind agreed with gold labels
    agreement = conn.execute("SELECT agreement->>'basis' FROM checks.judgement WHERE run_id = %s", (run_id,)).fetchone()
    assert "gold items agreed" in agreement[0]
    # the session writes as before afterwards
    assert core.current_actor(conn) == person
    name = conn.execute("SELECT kind, name FROM core.actor WHERE id = 1").fetchone()
    assert name == ("process", "trialwatch")


# --- the pages -----------------------------------------------------------------------------------

def test_staff_see_jobs_spend_and_costs_and_the_public_does_not(app_conn, fake_world, monkeypatch, admin_client):
    from fastapi.testclient import TestClient

    from trialwatch.web import app as web

    trial = seed(app_conn)
    done = queue(app_conn, "check", {"trial_ids": [trial], "config": "default"}, actor="admin@test.example")
    failed = queue(app_conn, "sync", {"source": "nowhere"}, actor="admin@test.example")
    work(app_conn, monkeypatch)
    assert jobs_db.job(app_conn, failed).status == "failed"
    run_id = jobs_db.job(app_conn, done).result["check_run"]
    page = admin_client.get("/admin/jobs")
    assert page.status_code == 200
    assert "1 job needs attention" in page.text and "no source &#39;nowhere&#39; is registered" in page.text and "Try again" in page.text
    assert "weekly-refresh" in page.text
    job_page = admin_client.get(f"/jobs/{done}")
    assert job_page.status_code == 200 and f"/runs/{run_id}" in job_page.text and "$0.0001" in job_page.text
    progress = admin_client.get(f"/jobs/{done}/progress").json()
    assert progress["status"] == "succeeded" and progress["cost"] == "$0.0001"
    spend = admin_client.get("/admin/spend")
    assert spend.status_code == 200 and "claude-haiku-5-5" in spend.text
    capped = admin_client.post("/admin/spend/cap", data={"cap": "$25"}, follow_redirects=False)
    assert capped.status_code == 303 and costs_db.monthly_cap(app_conn) == Decimal("25")
    bad = admin_client.post("/admin/spend/cap", data={"cap": "lots"}, follow_redirects=False)
    assert "not%20an%20amount" in bad.headers["location"]
    schedule_id = jobs_db.schedules(app_conn)[0].id
    moved = admin_client.post(f"/admin/schedules/{schedule_id}", follow_redirects=False,
                              data={"frequency": "weekly", "weekday": "3", "hour_utc": "5", "enabled": "true"})
    assert moved.status_code == 303
    schedule = jobs_db.schedule(app_conn, schedule_id)
    assert (schedule.frequency, schedule.weekday, schedule.hour_utc, schedule.enabled) == ("weekly", 3, 5, True)
    assert schedule.next_run_at.isoweekday() == 3 and schedule.next_run_at.hour == 5
    assert admin_client.get("/").status_code == 200
    assert "$0.0001" in admin_client.get(f"/runs/{run_id}").text
    # the public sees the run, not what it cost, and no jobs or spend
    public = TestClient(web.app)
    assert "$0.0001" not in public.get(f"/runs/{run_id}").text
    assert public.get("/admin/spend").status_code == 401 and public.get("/admin/jobs").status_code == 401


# --- schedules -----------------------------------------------------------------------------------

def test_schedules_start_switched_off(app_conn):
    """A fresh database never starts paid work by itself: an admin switches a schedule on."""
    assert [s.enabled for s in jobs_db.schedules(app_conn)] == [False]
    assert jobs_db.take_due_schedule(app_conn) is None


def test_a_schedule_runs_daily_weekly_or_monthly(app_conn):
    core.act_as(app_conn, "test-person")
    schedule_id = jobs_db.schedules(app_conn)[0].id
    now = app_conn.execute("SELECT now()").fetchone()[0]
    jobs_db.set_schedule(app_conn, schedule_id, frequency="daily", weekday=1, hour_utc=4, enabled=True)
    daily = jobs_db.schedule(app_conn, schedule_id).next_run_at
    assert daily.hour == 4 and timedelta(0) < daily - now <= timedelta(days=1)
    jobs_db.set_schedule(app_conn, schedule_id, frequency="weekly", weekday=5, hour_utc=4, enabled=True)
    weekly = jobs_db.schedule(app_conn, schedule_id).next_run_at
    assert weekly.isoweekday() == 5 and timedelta(0) < weekly - now <= timedelta(days=7)
    jobs_db.set_schedule(app_conn, schedule_id, frequency="monthly", weekday=5, hour_utc=4, enabled=True)
    monthly = jobs_db.schedule(app_conn, schedule_id).next_run_at
    assert (monthly.day, monthly.hour) == (1, 4) and timedelta(0) < monthly - now <= timedelta(days=31)
    # when it comes due, the next time moves on by its frequency
    app_conn.execute("UPDATE jobs.schedule SET frequency = 'daily', next_run_at = now() - interval '1 minute'")
    due = jobs_db.take_due_schedule(app_conn)
    assert due is not None and timedelta(0) < due.next_run_at - now <= timedelta(days=1)


def test_an_admin_starts_stops_and_reschedules_a_schedule(app_conn, admin_client):
    schedule_id = jobs_db.schedules(app_conn)[0].id
    page = admin_client.get("/admin/jobs").text
    assert "Start</button>" in page and 'name="frequency"' in page
    assert admin_client.post(f"/admin/schedules/{schedule_id}/switch", data={"enabled": "true"},
                             follow_redirects=False).status_code == 303
    assert jobs_db.schedule(app_conn, schedule_id).enabled
    assert "Stop</button>" in admin_client.get("/admin/jobs").text
    admin_client.post(f"/admin/schedules/{schedule_id}", data={"frequency": "daily", "weekday": "1", "hour_utc": "6",
                                                               "enabled": "true"})
    schedule = jobs_db.schedule(app_conn, schedule_id)
    assert (schedule.frequency, schedule.hour_utc, schedule.enabled) == ("daily", 6, True)
    assert "every day at 06:00" in admin_client.get("/admin/jobs").text
    bad = admin_client.post(f"/admin/schedules/{schedule_id}", follow_redirects=False,
                            data={"frequency": "hourly", "weekday": "1", "hour_utc": "6"})
    assert "error=" in bad.headers["location"]
    admin_client.post(f"/admin/schedules/{schedule_id}/switch")
    schedule = jobs_db.schedule(app_conn, schedule_id)
    assert not schedule.enabled and schedule.frequency == "daily"


def test_only_admins_change_schedules(app_conn):
    from conftest import signed_in_client
    from fastapi.testclient import TestClient

    from trialwatch.web import app as web

    schedule_id = jobs_db.schedules(app_conn)[0].id
    moderator = signed_in_client(app_conn, "moderator")
    assert moderator.post(f"/admin/schedules/{schedule_id}/switch", data={"enabled": "true"}).status_code == 403
    assert TestClient(web.app).post(f"/admin/schedules/{schedule_id}/switch").status_code == 401
    assert not jobs_db.schedule(app_conn, schedule_id).enabled
