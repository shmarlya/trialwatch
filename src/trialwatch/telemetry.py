"""What TrialWatch measures about itself, for admins (OpenTelemetry): traces of what took how long,
and counters such as model calls and their cost.

The code records through OpenTelemetry's API only. Until a process sets up an exporter, every
recording here does nothing and costs next to nothing (the tests, the command line). Sending them to
Grafana comes with the production setup (refactor plan, phase 8).

Never record what someone searched for, or a prompt's or answer's text: names, counts and durations
only (ADR-008).
"""

from opentelemetry import metrics, trace

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
