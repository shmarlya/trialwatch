"""What TrialWatch records about itself (telemetry.py), for admins in Grafana: a search leaves no trace
of what was searched for in any span, metric or log (ADR-008); a job's attempt is a trace its page
names; logs are JSON lines carrying their trace; the worker's heartbeat. Recorded into memory here,
set up once for the whole test session (OpenTelemetry's providers are set once per process)."""

import json
import logging
import sys

import pytest
from conftest import signed_in_client
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_jobs import queue

from trialwatch import telemetry
from trialwatch.db import jobs as jobs_db
from trialwatch.jobs import tasks, worker
from trialwatch.jobs.steps import Outcome
from trialwatch.search import find as finding
from trialwatch.web import app as web
from trialwatch.web import privacy

SPANS = InMemorySpanExporter()
METRICS = InMemoryMetricReader()
LOGS = InMemoryLogRecordExporter()
# What someone searched for: it must appear nowhere TrialWatch records itself
SECRET = "glioblastomaxq"


@pytest.fixture(scope="module")
def traced_app():
    """The web app, traced into memory."""
    assert telemetry.setup("trialwatch-test", span_exporter=SPANS, metric_reader=METRICS, log_exporter=LOGS)
    application = web.create_app()
    telemetry.instrument_app(application)
    return application


def everything_recorded() -> str:
    """Every span's name, attributes and events, and every log line, as one text."""
    recorded = []
    for span in SPANS.get_finished_spans():
        recorded.append(span.name)
        recorded.append(json.dumps(dict(span.attributes), default=str))
        recorded.append(str(span.status.description))
        for event in span.events:
            recorded.append(json.dumps({"event": event.name, **dict(event.attributes)}, default=str))
    for log in LOGS.get_finished_logs():
        recorded.append(str(log.log_record.body))
        recorded.append(json.dumps(dict(log.log_record.attributes or {}), default=str))
    return "\n".join(recorded)


def metric_points(name: str) -> list:
    points = []
    data = METRICS.get_metrics_data()
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    points.extend(metric.data.data_points)
    return points


def test_a_search_leaves_no_trace_of_what_was_searched_for(traced_app, app_conn, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    SPANS.clear()
    LOGS.clear()
    client = TestClient(traced_app, raise_server_exceptions=False)
    assert client.get("/find", params={"q": f"{SECRET} Paris"}).status_code == 200
    assert client.get("/find", params={"what": SECRET, "where": "Lyon", "km": "25"}).status_code == 200

    # an error while searching, inside search.find's span, whose message quotes the search
    def fails_quoting_the_search(*args, **kwargs):
        raise ValueError(f"could not read {SECRET}")

    monkeypatch.setattr(finding, "search", fails_quoting_the_search)
    assert client.get("/find", params={"q": SECRET}).status_code == 500

    names = [span.name for span in SPANS.get_finished_spans()]
    assert "GET /find" in names and "search.find" in names
    # the database queries are traced, without their parameters
    assert any(span.attributes.get("db.statement") for span in SPANS.get_finished_spans())
    recorded = everything_recorded()
    assert SECRET not in recorded and SECRET not in caplog.text
    # the error is recorded by its type only, on the request's span and on the search's inside it
    for name in ("GET /find", "search.find"):
        failed = [span for span in SPANS.get_finished_spans() if span.name == name and span.events]
        assert failed and failed[-1].events[0].attributes == {"exception.type": "ValueError"}
        assert failed[-1].status.description in (None, "ValueError")
    # no query string and no visitor's address anywhere; the route is kept
    for span in SPANS.get_finished_spans():
        assert not set(span.attributes) & set(telemetry.PRIVATE_ATTRIBUTES)
    assert any(span.attributes.get("http.route") == "/find" for span in SPANS.get_finished_spans())
    outcomes = {}
    for point in metric_points("trialwatch.search.requests"):
        outcomes[point.attributes["outcome"]] = point.value
    assert outcomes["empty"] >= 2 and outcomes["error"] >= 1


def test_health_checks_are_not_traced(traced_app, app_conn):
    SPANS.clear()
    client = TestClient(traced_app)
    assert client.get("/healthz").status_code == 200
    assert [span.name for span in SPANS.get_finished_spans() if span.name.startswith("GET")] == []


def test_a_job_attempt_is_a_trace_its_page_names(traced_app, app_conn, monkeypatch):
    SPANS.clear()

    def done(ctx, conn, params):
        ctx.add("did it")
        return Outcome({"done": True})

    monkeypatch.setitem(tasks.TASKS, "sync", tasks.Task("Sync", 1, False, done))
    job_id = queue(app_conn, "sync", {"source": "ctis"})
    claimed = jobs_db.claim_next(app_conn, "w")
    assert worker.run_one(app_conn, claimed, "w") == "succeeded"
    attempt = [span for span in SPANS.get_finished_spans() if span.name == "jobs.run"][0]
    assert (
        attempt.attributes["trialwatch.job.id"] == job_id and attempt.attributes["trialwatch.job.status"] == "succeeded"
    )
    trace_id = format(attempt.context.trace_id, "032x")
    assert jobs_db.job(app_conn, job_id).trace_id == trace_id
    # an admin opens it in Grafana from the job's page; a moderator sees its id only
    admin_page = signed_in_client(app_conn, "admin").get(f"/jobs/{job_id}").text
    assert "/grafana/explore?" in admin_page and trace_id in admin_page.replace("%22", '"')
    assert "/grafana/explore?" not in signed_in_client(app_conn, "moderator").get(f"/jobs/{job_id}").text
    finished = [point for point in metric_points("trialwatch.jobs.finished") if point.attributes["kind"] == "sync"]
    assert finished and finished[0].attributes["status"] == "succeeded"


def test_log_lines_are_json_with_their_trace():
    record = logging.LogRecord("trialwatch.web", logging.INFO, "", 0, "job %s: %s", (7, "succeeded"), None)
    with telemetry.tracer.start_as_current_span("a span"):
        line = json.loads(telemetry.JsonFormatter().format(record))
    assert (line["level"], line["logger"], line["message"]) == ("INFO", "trialwatch.web", "job 7: succeeded")
    assert len(line["trace_id"]) == 32 or "trace_id" not in line


def test_the_worker_says_it_is_alive(traced_app, tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "HEARTBEAT_FILE", tmp_path / "alive")
    telemetry.heartbeat()
    assert (tmp_path / "alive").exists()
    beats = metric_points("trialwatch.worker.heartbeat")
    assert beats and beats[0].value > 0


def test_uvicorn_logs_an_unhandled_error_by_its_type_only():
    try:
        raise ValueError(f"could not read {SECRET}")
    except ValueError:
        record = logging.LogRecord(
            "uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application\n", None, sys.exc_info()
        )
    assert privacy.ErrorTypeOnlyFilter().filter(record)
    line = telemetry.JsonFormatter().format(record)
    assert SECRET not in line
    assert "Exception in ASGI application ValueError" in line and "test_telemetry.py" in line
