"""Calls to the model and what they cost: the one place TrialWatch talks to Anthropic.

- client(): Anthropic's SDK client, aimed at Anthropic's API itself
- call(client, request, operation): send one request; the answer's text, who answered, the tokens of
  every billed attempt and their cost. The evidence check (checks/evidence.py) and patient search's
  AI tags (search/annotate.py) both call it.
- record(conn, call, ...): add a call's billed attempts to core.api_call, the source of every spend total

Prices are one current list, prices.json: Anthropic's list prices per million tokens. A call is priced
per billed attempt: when a model declined and a fallback model answered (server-side fallback), the
response's usage lists each attempt (usage.iterations) with the model that ran it, and each is priced
at that model's rate. A model missing from the list is left unpriced (cost None), never $0, so a total
never quietly leaves it out. A new model in a setup needs its price here first.
"""

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import anthropic
import psycopg

from trialwatch import telemetry
from trialwatch.db import costs as costs_db

# Calls go to Anthropic's API itself, whatever ANTHROPIC_BASE_URL a shell may set (e.g. a proxy)
ANTHROPIC_API = "https://api.anthropic.com"
# The SDK tries an overloaded or timed-out call again this many times before it is an error
MAX_RETRIES = 4
# On a safety refusal the API can re-run the request on the model Anthropic recommends
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Errors that are the same for every call (no or bad credentials): whatever runs the calls stops
BAD_CREDENTIALS = (anthropic.AuthenticationError, anthropic.PermissionDeniedError)

PRICES_FILE = Path(__file__).parent / "prices.json"
MILLION = Decimal(1_000_000)
# The token kinds a call is billed for, as prices.json names their prices
TOKEN_KINDS = ("input", "output", "cache_write", "cache_read")


class MessagesClient(Protocol):
    """The slice of anthropic.Anthropic we use, so tests can pass a fake."""

    @property
    def beta(self) -> Any: ...


def client() -> anthropic.Anthropic:
    """The SDK client; it reads ANTHROPIC_API_KEY from the environment."""
    return anthropic.Anthropic(base_url=ANTHROPIC_API, max_retries=MAX_RETRIES)


def uses_fallbacks(model: str, fallbacks: bool) -> bool:
    """Whether a request asks for the server-side fallback: Haiku models have none."""
    if not fallbacks:
        return False
    return not model.startswith("claude-haiku")


# --- prices ------------------------------------------------------------------------------------

_prices: dict | None = None


def price_list() -> dict:
    """prices.json's models: model id -> its prices (as decimal strings), read once."""
    global _prices
    if _prices is None:
        _prices = json.loads(PRICES_FILE.read_text())["models"]
    return _prices


@dataclass(frozen=True)
class CallCost:
    """One billed attempt of a call: the model that ran it, its tokens and what they cost."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    # None: the model is not in the price list
    cost_usd: Decimal | None = None
    # the prices it was computed with, US dollars per million tokens
    prices: dict | None = None

    @property
    def priced(self) -> bool:
        return self.cost_usd is not None


def prices_for(model: str, prompt_tokens: int) -> dict | None:
    """The four prices of `model` for a request with this many prompt tokens, or None when the model
    is not listed. Some models cost more above a prompt length (prices.json long_prompt)."""
    listed = price_list().get(model)
    if listed is None:
        return None
    chosen = listed
    long_prompt = listed.get("long_prompt")
    if long_prompt is not None and prompt_tokens > long_prompt["over_prompt_tokens"]:
        chosen = long_prompt
    prices = {}
    for kind in TOKEN_KINDS:
        prices[kind] = chosen[kind]
    return prices


def price_tokens(
    model: str, input_tokens: int, output_tokens: int, cache_write_tokens: int = 0, cache_read_tokens: int = 0
) -> CallCost:
    prompt_tokens = input_tokens + cache_write_tokens + cache_read_tokens
    prices = prices_for(model, prompt_tokens)
    if prices is None:
        return CallCost(model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens)
    counts = {
        "input": input_tokens,
        "output": output_tokens,
        "cache_write": cache_write_tokens,
        "cache_read": cache_read_tokens,
    }
    cost = Decimal(0)
    for kind in TOKEN_KINDS:
        cost += Decimal(counts[kind]) * Decimal(prices[kind]) / MILLION
    return CallCost(model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost, prices)


def _count(usage: Any, name: str) -> int:
    """A token count from a usage object; 0 when the API did not report it."""
    count = getattr(usage, name, 0)
    if not count:
        return 0
    return count


def price_usage(usage: Any, model: str) -> list[CallCost]:
    """The billed attempts of one response, priced. `model` is the model the response says answered;
    an attempt that names no model of its own ran on it."""
    if usage is None:
        return []
    attempts = getattr(usage, "iterations", None)
    if not attempts:
        attempts = [usage]
    costs = []
    for attempt in attempts:
        attempt_model = getattr(attempt, "model", None)
        if not attempt_model:
            attempt_model = model
        costs.append(
            price_tokens(
                attempt_model,
                _count(attempt, "input_tokens"),
                _count(attempt, "output_tokens"),
                _count(attempt, "cache_creation_input_tokens"),
                _count(attempt, "cache_read_input_tokens"),
            )
        )
    return costs


def total_cost(costs: list[CallCost]) -> Decimal:
    """What the priced attempts cost together (unpriced ones count as nothing; see unpriced_count)."""
    total = Decimal(0)
    for cost in costs:
        if cost.cost_usd is not None:
            total += cost.cost_usd
    return total


def unpriced_count(costs: list[CallCost]) -> int:
    count = 0
    for cost in costs:
        if cost.cost_usd is None:
            count += 1
    return count


def dollars(amount: Decimal | float | None) -> str:
    """An amount as pages show it: $1.23, or $0.0042 below a cent; "—" when unknown."""
    if amount is None:
        return "—"
    value = Decimal(amount)
    if value != 0 and abs(value) < Decimal("0.01"):
        return f"${value:.4f}"
    return f"${value:,.2f}"


# --- calls -------------------------------------------------------------------------------------


@dataclass
class CallResult:
    """One model call: its answer's text as sent, who answered, every billed attempt priced, and an
    error when there is no usable answer."""

    error: str | None = None
    # each billed attempt priced (more than one after a fallback)
    costs: list[CallCost] = field(default_factory=list)
    # the provider's request id, to match its own bills
    request_id: str | None = None
    # the model that answered (after a fallback, not the one asked), why it stopped, its answer as sent,
    # and how long the call took
    served_by: str | None = None
    stop_reason: str | None = None
    raw_text: str | None = None
    duration_ms: int = 0

    # Tokens of every billed attempt: a declined attempt before a fallback is billed too

    @property
    def input_tokens(self) -> int:
        return sum(cost.input_tokens for cost in self.costs)

    @property
    def output_tokens(self) -> int:
        return sum(cost.output_tokens for cost in self.costs)

    @property
    def cache_write_tokens(self) -> int:
        return sum(cost.cache_write_tokens for cost in self.costs)

    @property
    def cache_read_tokens(self) -> int:
        return sum(cost.cache_read_tokens for cost in self.costs)


def first_text(content: list[Any], default: str | None) -> str | None:
    """The text of the first text block in a response's content, or `default` if there is none.
    The response may also hold thinking blocks, which carry no answer."""
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            return block.text
    return default


def call(messages_client: MessagesClient, request: dict, operation: str) -> CallResult:
    """Send one request and price it. `operation` says what it is for (check, annotate). An error a
    retry may fix (overloaded, too large, a timeout), a refusal or running out of tokens is the
    result's error; bad credentials raise (the same for every call)."""
    model = request.get("model", "")
    started = time.monotonic()
    with telemetry.tracer.start_as_current_span("llm.call") as span:
        span.set_attribute("gen_ai.system", "anthropic")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("trialwatch.operation", operation)
        result = _send(messages_client, request, started)
        if result.served_by:
            span.set_attribute("gen_ai.response.model", result.served_by)
        span.set_attribute("gen_ai.usage.input_tokens", result.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", result.output_tokens)
        if result.error is not None:
            # the kind of error only: a refusal's or an API error's text may quote the request
            span.set_attribute("error.type", result.error.split(":", 1)[0].split(" (", 1)[0])
    _measure(result, model, operation)
    return result


def _send(messages_client: MessagesClient, request: dict, started: float) -> CallResult:
    try:
        # the prepared request dictionary is passed as keyword arguments to the SDK call
        response = messages_client.beta.messages.create(**request)
    except BAD_CREDENTIALS:
        raise
    except anthropic.APIError as exc:  # e.g. overloaded, too large, a timeout: the caller records it and goes on
        return CallResult(error=f"{type(exc).__name__}: {exc}", duration_ms=_milliseconds_since(started))
    # anything else (no credentials at all, a bug) is the same for every call too: it raises
    served_by = getattr(response, "model", None)
    priced_as = served_by
    if not priced_as:
        priced_as = request.get("model", "")
    result = CallResult(
        costs=price_usage(response.usage, priced_as),
        request_id=getattr(response, "_request_id", None),
        served_by=served_by,
        stop_reason=response.stop_reason,
        raw_text=first_text(response.content, ""),
        duration_ms=_milliseconds_since(started),
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        result.error = f"stop_reason {response.stop_reason}"
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        if category:
            result.error = f"{result.error} ({category})"
    return result


def _measure(result: CallResult, model: str, operation: str) -> None:
    outcome = "ok"
    if result.error is not None:
        outcome = "error"
    labels = {"model": model, "operation": operation, "outcome": outcome}
    telemetry.llm_calls.add(1, labels)
    telemetry.llm_duration.record(result.duration_ms / 1000, labels)
    for cost in result.costs:
        cost_labels = {"model": cost.model, "operation": operation}
        for kind in TOKEN_KINDS:
            count = getattr(cost, f"{kind}_tokens")
            if count:
                telemetry.llm_tokens.add(count, {"model": cost.model, "operation": operation, "kind": kind})
        if cost.cost_usd is not None:
            telemetry.llm_cost.add(float(cost.cost_usd), cost_labels)


def _milliseconds_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def record(
    conn: psycopg.Connection,
    result: CallResult,
    *,
    operation: str,
    check_run_id: int | None = None,
    trial_id: int | None = None,
    call_number: int | None = None,
    annotation_run_id: int | None = None,
) -> None:
    """Add the call's billed attempts to core.api_call, made for the session's actor."""
    for cost in result.costs:
        costs_db.record_call(
            conn,
            cost,
            operation=operation,
            check_run_id=check_run_id,
            trial_id=trial_id,
            call_number=call_number,
            request_id=result.request_id,
            annotation_run_id=annotation_run_id,
        )
