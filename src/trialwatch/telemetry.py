"""What TrialWatch measures about itself, for admins (OpenTelemetry, shown in Grafana): traces of what
took how long, counters such as model calls and their cost, and its logs.

The code records through OpenTelemetry's API. A process sends what it records only after setup(),
and only when OTEL_EXPORTER_OTLP_ENDPOINT names where to (compose's lgtm container); until then every
recording here does nothing and costs next to nothing (the tests, the command line). Logs are JSON
lines on stderr either way (log_config), each with the trace it was written in.

Never record what someone searched for, or who, or a prompt's or answer's text: names, counts and
durations only (ADR-008). Every span is redacted before it leaves the process (RedactingExporter): no
query string, no visitor's address, and on /find no error message, only its type. Queries are traced
without their parameters.
"""

import json
import logging
import os
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

tracer = trace.get_tracer("trialwatch")
meter = metrics.get_meter("trialwatch")

# Model calls (trialwatch.llm), by model, operation (check, annotate) and outcome (ok, error)
llm_calls = meter.create_counter("trialwatch.llm.calls", unit="{call}", description="model calls")
llm_tokens = meter.create_counter(
    "trialwatch.llm.tokens",
    unit="{token}",
    description="tokens billed, by kind (input, output, cache_write, cache_read)",
)
llm_cost = meter.create_counter("trialwatch.llm.cost", unit="USD", description="what priced model calls cost")
llm_duration = meter.create_histogram("trialwatch.llm.duration", unit="s", description="how long a model call took")
# Patient searches (/find), by outcome: hits, empty (nothing found), error
search_requests = meter.create_counter("trialwatch.search.requests", unit="{search}", description="patient searches")
search_duration = meter.create_histogram("trialwatch.search.duration", unit="s", description="how long a search took")
# Trial records fetched from a source, by source and outcome: ok, not_found, error
source_fetches = meter.create_counter("trialwatch.source.fetches", unit="{fetch}", description="records fetched")
# Job attempts that ended, by kind and status (succeeded, failed, queued again, held, cancelled)
jobs_finished = meter.create_counter("trialwatch.jobs.finished", unit="{attempt}", description="job attempts ended")
jobs_duration = meter.create_histogram("trialwatch.jobs.duration", unit="s", description="how long a job attempt took")

ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
# Span attributes never exported: those that can hold a request's query string (what someone searched
# for), and the visitor's address (behind the proxy, their own), which with a search's span would say
# who searched when
PRIVATE_ATTRIBUTES = (
    "url.query",
    "url.full",
    "http.target",
    "http.url",
    "net.peer.ip",
    "net.peer.port",
    "client.address",
    "client.port",
    "http.client_ip",
    "net.sock.peer.addr",
    "net.sock.peer.port",
)
# Pages whose errors may quote what someone searched for: a span keeps only the error's type
PRIVATE_PATHS = ("/find",)
# Not traced: the health checks Docker and the proxy make every few seconds
UNTRACED_PATHS = "healthz,readyz"
# Libraries that log every request with its whole address (a query string included), and the
# development server's file watcher: warnings only. httpx2 is the client of Anthropic's SDK and the tests
QUIET_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2", "watchfiles")


# --- the worker's heartbeat ------------------------------------------------------------------------

# The file the worker touches while it is alive, for compose's healthcheck to read its age
HEARTBEAT_FILE = Path(os.environ.get("TRIALWATCH_HEARTBEAT_FILE", "/tmp/trialwatch-worker.alive"))
_last_heartbeat: float | None = None


def heartbeat() -> None:
    """The worker is alive: its file's time and the trialwatch.worker.heartbeat gauge say when."""
    global _last_heartbeat
    _last_heartbeat = time.time()
    try:
        HEARTBEAT_FILE.touch()
    except OSError:  # a read-only place: the gauge still says it
        pass


def _heartbeat_observation(options: CallbackOptions) -> Iterable[Observation]:
    if _last_heartbeat is None:
        return []
    return [Observation(_last_heartbeat)]


meter.create_observable_gauge(
    "trialwatch.worker.heartbeat",
    callbacks=[_heartbeat_observation],
    unit="s",
    description="when the worker last said it is alive (Unix time)",
)


# --- logs ----------------------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, the trace and span it was written in,
    and an exception's traceback when one was logged."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            entry["trace_id"] = format(span.trace_id, "032x")
            entry["span_id"] = format(span.span_id, "016x")
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def log_config() -> dict:
    """Logging for the web server and the worker: JSON lines on stderr from INFO up, uvicorn's too, its
    access log without query strings (web/privacy.py)."""
    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"strip_query": {"()": "trialwatch.web.privacy.StripQueryFilter"}},
        "formatters": {"json": {"()": "trialwatch.telemetry.JsonFormatter"}},
        "handlers": {"stderr": {"class": "logging.StreamHandler", "formatter": "json", "stream": "ext://sys.stderr"}},
        "loggers": {
            "uvicorn": {"handlers": ["stderr"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            # on the logger, so every handler gets the line without its query string, OpenTelemetry's too
            "uvicorn.access": {"handlers": ["stderr"], "level": "INFO", "propagate": False, "filters": ["strip_query"]},
        },
        "root": {"handlers": ["stderr"], "level": "INFO"},
    }
    for name in QUIET_LOGGERS:
        config["loggers"][name] = {"level": "WARNING"}
    return config


# --- redaction -----------------------------------------------------------------------------------


def _error_type_only(event: Event) -> Event:
    """An exception event with its type only: its message and traceback may quote a search."""
    kept = {}
    for name, value in event.attributes.items():
        if name == "exception.type":
            kept[name] = value
    return Event(event.name, kept, event.timestamp)


def redacted(span: ReadableSpan) -> ReadableSpan:
    """A copy of a finished span without what may say what someone searched for or who they are: query
    strings and visitors' addresses everywhere, and on /find an error's message and traceback."""
    attributes = {}
    for name, value in span.attributes.items():
        if name not in PRIVATE_ATTRIBUTES:
            attributes[name] = value
    path = span.attributes.get("url.path") or span.attributes.get("http.route")
    events = list(span.events)
    if path in PRIVATE_PATHS:
        events = []
        for event in span.events:
            if event.name == "exception":
                events.append(_error_type_only(event))
            else:
                events.append(event)
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        events=events,
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class RedactingExporter(SpanExporter):
    """Sends spans on to another exporter, each redacted first."""

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        cleaned = []
        for span in spans:
            cleaned.append(redacted(span))
        return self.inner.export(cleaned)

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


# --- sending it to Grafana -----------------------------------------------------------------------

_set_up = False


def exporting() -> bool:
    return bool(os.environ.get(ENDPOINT))


def setup(service: str, span_exporter=None, metric_reader=None, log_exporter=None) -> bool:
    """Once per process: send traces, metrics and logs to OTEL_EXPORTER_OTLP_ENDPOINT (OTLP over HTTP),
    each span redacted first, and trace database queries (without their parameters) and outgoing HTTP
    calls. Without an endpoint, and without exporters given (the tests give in-memory ones), nothing is
    set up. Returns whether it is set up."""
    global _set_up
    if _set_up:
        return True
    if span_exporter is None and not exporting():
        return False
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.instrumentation.logging.handler import LoggingHandler
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, SimpleLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    resource = Resource.create({"service.name": service, "service.namespace": "trialwatch"})
    tracer_provider = TracerProvider(resource=resource)
    if span_exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(BatchSpanProcessor(RedactingExporter(OTLPSpanExporter())))
    else:
        tracer_provider.add_span_processor(SimpleSpanProcessor(RedactingExporter(span_exporter)))
    trace.set_tracer_provider(tracer_provider)

    if metric_reader is None:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

    logger_provider = LoggerProvider(resource=resource)
    if log_exporter is None:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    else:
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger().addHandler(handler)
    # uvicorn's access log doesn't reach the root logger; its lines are already without query strings
    logging.getLogger("uvicorn.access").addHandler(handler)

    PsycopgInstrumentor().instrument(enable_commenter=False, capture_parameters=False)
    HTTPXClientInstrumentor().instrument()
    _set_up = True
    return True


def instrument_app(app) -> None:
    """Trace the web app's requests (not the health checks), when set up."""
    if not _set_up:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls=UNTRACED_PATHS)


def trace_id() -> str | None:
    """The trace the code runs in now, as Grafana names it; None when nothing is traced."""
    span = trace.get_current_span().get_span_context()
    if not span.is_valid:
        return None
    return format(span.trace_id, "032x")
