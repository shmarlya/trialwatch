"""Calls to the model and what they cost: the one place TrialWatch talks to Anthropic.

- client(): Anthropic's SDK client, aimed at Anthropic's API itself
- call(client, request, operation): send one request; the answer's text, who answered, the tokens of
  every billed attempt and their cost. The evidence check (checks/evidence.py) and patient search's
  AI tags (search/annotate.py) both call it.
- record(conn, call, request, ...): add the call to core.llm_call (the request as sent, the answer, each
  billed attempt priced), the source of every spend total
- submit_batch / batch_state / batch_results / cancel_batch: the same requests sent as a Message Batch
  (ADR-009): every token at prices.json's batch_discount, the answers within 24 hours. A batch request
  can't ask for the server-side fallback (batch_params leaves it out); its results become CallResults
  exactly as a direct call's do.

Prices are one current list, prices.json: Anthropic's list prices per million tokens. A call is priced
per billed attempt: when a model declined and a fallback model answered (server-side fallback), the
response's usage lists each attempt (usage.iterations) with the model that ran it, and each is priced
at that model's rate. A model missing from the list is left unpriced (cost None), never $0, so a total
never quietly leaves it out. A new model in a setup needs its price here first.
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import anthropic
import psycopg

from trialwatch import telemetry
from trialwatch.db import batches as batches_db
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


def _price_file() -> dict:
    """prices.json as read, once."""
    global _prices
    if _prices is None:
        _prices = json.loads(PRICES_FILE.read_text())
    return _prices


def price_list() -> dict:
    """prices.json's models: model id -> its prices (as decimal strings)."""
    return _price_file()["models"]


def batch_discount() -> Decimal:
    """What a call sent in a Message Batch pays of each list price (0.5: half)."""
    return Decimal(_price_file()["batch_discount"])


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


def prices_for(model: str, prompt_tokens: int, batch: bool = False) -> dict | None:
    """The four prices of `model` for a request with this many prompt tokens, or None when the model
    is not listed. Some models cost more above a prompt length (prices.json long_prompt); a call sent
    in a batch pays the batch discount of each."""
    listed = price_list().get(model)
    if listed is None:
        return None
    chosen = listed
    long_prompt = listed.get("long_prompt")
    if long_prompt is not None and prompt_tokens > long_prompt["over_prompt_tokens"]:
        chosen = long_prompt
    prices = {}
    for kind in TOKEN_KINDS:
        price = chosen[kind]
        if batch:
            # kept as a decimal string, like the list's own prices
            price = str(Decimal(price) * batch_discount())
        prices[kind] = price
    return prices


def price_tokens(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    batch: bool = False,
) -> CallCost:
    prompt_tokens = input_tokens + cache_write_tokens + cache_read_tokens
    prices = prices_for(model, prompt_tokens, batch)
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


def price_usage(usage: Any, model: str, batch: bool = False) -> list[CallCost]:
    """The billed attempts of one response, priced (at batch prices when it came from a batch). `model`
    is the model the response says answered; an attempt that names no model of its own ran on it."""
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
                batch,
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
    # a call answered in a Message Batch: the provider's batch id and the request's id in it (a batch
    # result has no request id of its own)
    batch_id: str | None = None
    custom_id: str | None = None
    # its batch's row (core.llm_batch), once the batch is recorded
    llm_batch_id: int | None = None

    @property
    def tier(self) -> str:
        if self.batch_id is not None:
            return "batch"
        return "direct"

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
    result's error; bad credentials raise (the same for every call). Given a BatchReplay instead of a
    client, the answer comes from the run's batches (or Pending says it is not there yet)."""
    if isinstance(messages_client, BatchReplay):
        # a run that sends its calls in batches: the answer comes from its batch, or the trial waits
        return messages_client.answer(request, operation)
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
    result = result_from_message(response, request.get("model", ""), batch=False)
    result.request_id = getattr(response, "_request_id", None)
    result.duration_ms = _milliseconds_since(started)
    return result


def result_from_message(message: Any, requested_model: str, batch: bool) -> CallResult:
    """A model's answer as a CallResult, whether it came back directly or in a batch: priced, who
    answered, the answer's text; a refusal or running out of tokens is its error."""
    served_by = getattr(message, "model", None)
    priced_as = served_by
    if not priced_as:
        priced_as = requested_model
    result = CallResult(
        costs=price_usage(message.usage, priced_as, batch),
        served_by=served_by,
        stop_reason=message.stop_reason,
        raw_text=first_text(message.content, ""),
    )
    if message.stop_reason in ("refusal", "max_tokens"):
        result.error = f"stop_reason {message.stop_reason}"
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None)
        if category:
            result.error = f"{result.error} ({category})"
    return result


def is_refusal(result: CallResult) -> bool:
    return result.stop_reason == "refusal"


def _measure(result: CallResult, model: str, operation: str) -> None:
    outcome = "ok"
    if result.error is not None:
        outcome = "error"
    labels = {"model": model, "operation": operation, "outcome": outcome, "tier": result.tier}
    telemetry.llm_calls.add(1, labels)
    if result.batch_id is None:
        # a batch result has no duration of its own: it waited with the whole batch
        telemetry.llm_duration.record(result.duration_ms / 1000, labels)
    for cost in result.costs:
        cost_labels = {"model": cost.model, "operation": operation, "tier": result.tier}
        for kind in TOKEN_KINDS:
            count = getattr(cost, f"{kind}_tokens")
            if count:
                token_labels = {"model": cost.model, "operation": operation, "kind": kind, "tier": result.tier}
                telemetry.llm_tokens.add(count, token_labels)
        if cost.cost_usd is not None:
            telemetry.llm_cost.add(float(cost.cost_usd), cost_labels)


def _milliseconds_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def record(
    conn: psycopg.Connection,
    result: CallResult,
    request: dict,
    *,
    operation: str,
    check_run_id: int | None = None,
    trial_id: int | None = None,
    call_number: int | None = None,
    annotation_run_id: int | None = None,
) -> int:
    """Add the call to core.llm_call, made for the session's actor: `request` as sent (without the system
    prompt, which its run keeps once; attached files by their sha256), the answer as received, how it
    ended, and each billed attempt priced. A call that failed is recorded too; a call answered in a batch
    names its batch (core.llm_batch) and its id in it, and that request is marked recorded. Returns its id."""
    attempts = []
    for cost in result.costs:
        cost_text = None
        if cost.cost_usd is not None:
            # JSON has no exact decimals
            cost_text = str(cost.cost_usd)
        attempts.append(
            {
                "model": cost.model,
                "input_tokens": cost.input_tokens,
                "output_tokens": cost.output_tokens,
                "cache_write_tokens": cost.cache_write_tokens,
                "cache_read_tokens": cost.cache_read_tokens,
                "cost_usd": cost_text,
                "prices": cost.prices,
            }
        )
    call_id = costs_db.record_call(
        conn,
        operation=operation,
        model=request.get("model", ""),
        served_by=result.served_by,
        request=request,
        response=result.raw_text,
        stop_reason=result.stop_reason,
        latency_ms=result.duration_ms,
        error=result.error,
        request_id=result.request_id,
        attempts=attempts,
        check_run_id=check_run_id,
        trial_id=trial_id,
        call_number=call_number,
        annotation_run_id=annotation_run_id,
        tier=result.tier,
        llm_batch_id=result.llm_batch_id,
        custom_id=result.custom_id,
    )
    if result.llm_batch_id is not None and result.custom_id is not None:
        batches_db.link_call(conn, result.llm_batch_id, result.custom_id, call_id)
    return call_id


# --- batches (ADR-009) -------------------------------------------------------------------------

# Anthropic's limits are 100,000 requests and 256 MB a batch; we stay well under both, so one batch is
# never refused for its size (a whole-files check sends PDFs of up to 23 MB)
BATCH_MOST_REQUESTS = 10_000
BATCH_MOST_BYTES = 200 * 1024 * 1024
# A batch ends within 24 hours; one not ended an hour after that is read as it is (its requests expired)
BATCH_LONGEST = 25 * 3600


@dataclass(frozen=True)
class BatchState:
    """Where a submitted batch is: in_progress, canceling or ended; and its requests' counts so far."""

    status: str
    processing: int = 0
    succeeded: int = 0
    errored: int = 0
    canceled: int = 0
    expired: int = 0

    @property
    def ended(self) -> bool:
        return self.status == "ended"

    @property
    def done(self) -> int:
        return self.succeeded + self.errored + self.canceled + self.expired

    def counts(self) -> dict:
        return {
            "processing": self.processing,
            "succeeded": self.succeeded,
            "errored": self.errored,
            "canceled": self.canceled,
            "expired": self.expired,
        }


def batch_params(request: dict) -> dict:
    """A request as a batch can take it: without the server-side fallback, which batches refuse (a
    refusal in a batch is asked again directly, with it). Everything else is sent as it is."""
    params = {}
    for name, value in request.items():
        if name in ("fallbacks", "stream"):
            continue
        if name == "betas":
            kept = [beta for beta in value if beta != FALLBACK_BETA]
            if kept:
                params[name] = kept
            continue
        params[name] = value
    return params


def batch_chunks(items: list[tuple[str, dict]]) -> list[list[tuple[str, dict]]]:
    """(custom id, request) pairs split into batches Anthropic accepts, in their order."""
    chunks = []
    current: list[tuple[str, dict]] = []
    size = 0
    for item in items:
        item_size = len(json.dumps(item[1]))
        too_many = len(current) >= BATCH_MOST_REQUESTS
        too_big = size + item_size > BATCH_MOST_BYTES
        if current and (too_many or too_big):
            chunks.append(current)
            current = []
            size = 0
        current.append(item)
        size += item_size
    if current:
        chunks.append(current)
    return chunks


def submit_batch(messages_client: MessagesClient, items: list[tuple[str, dict]], operation: str) -> str:
    """Send these (custom id, request) pairs as one Message Batch; returns the provider's batch id. Bad
    credentials raise, as for a call; any other API error raises too (the job tries again later)."""
    requests = []
    for custom_id, request in items:
        requests.append({"custom_id": custom_id, "params": batch_params(request)})
    betas = []
    for _custom_id, request in items:
        for beta in batch_params(request).get("betas", []):
            if beta not in betas:
                betas.append(beta)
    with telemetry.tracer.start_as_current_span("llm.batch.submit") as span:
        span.set_attribute("gen_ai.system", "anthropic")
        span.set_attribute("trialwatch.operation", operation)
        span.set_attribute("trialwatch.batch.requests", len(requests))
        if betas:
            batch = messages_client.beta.messages.batches.create(requests=requests, betas=betas)
        else:
            batch = messages_client.beta.messages.batches.create(requests=requests)
        span.set_attribute("trialwatch.batch.id", batch.id)
    return batch.id


def batch_state(messages_client: MessagesClient, batch_id: str) -> BatchState:
    batch = messages_client.beta.messages.batches.retrieve(batch_id)
    counts = batch.request_counts
    return BatchState(
        status=batch.processing_status,
        processing=counts.processing,
        succeeded=counts.succeeded,
        errored=counts.errored,
        canceled=counts.canceled,
        expired=counts.expired,
    )


def cancel_batch(messages_client: MessagesClient, batch_id: str) -> None:
    """Ask Anthropic to stop a batch; requests already answered stay answered (and billed)."""
    messages_client.beta.messages.batches.cancel(batch_id)


def batch_results(
    messages_client: MessagesClient, batch_id: str, requested_models: dict[str, str], operation: str
) -> dict[str, CallResult]:
    """An ended batch's results by custom id, each a CallResult like a direct call's, priced at batch
    prices. `requested_models` maps each custom id to the model it asked for. A request the batch did
    not answer has an error that says why: BatchErrored (worth asking again), BatchInvalidRequest (not),
    BatchExpired or BatchCanceled."""
    results = {}
    for line in messages_client.beta.messages.batches.results(batch_id):
        custom_id = line.custom_id
        outcome = line.result
        model = requested_models.get(custom_id, "")
        if outcome.type == "succeeded":
            result = result_from_message(outcome.message, model, batch=True)
        else:
            result = CallResult(error=batch_error(outcome))
        result.batch_id = batch_id
        result.custom_id = custom_id
        _measure(result, model, operation)
        results[custom_id] = result
    return results


def batch_error(outcome: Any) -> str:
    """Why a batch did not answer a request, starting with a name retry rules can match."""
    if outcome.type == "expired":
        return "BatchExpired: the batch ended before this request was answered"
    if outcome.type == "canceled":
        return "BatchCanceled: the batch was cancelled before this request was answered"
    detail = getattr(outcome.error, "error", None)
    error_type = getattr(detail, "type", "error")
    message = getattr(detail, "message", "")
    if error_type == "invalid_request_error":
        return f"BatchInvalidRequest: {message}"
    return f"BatchErrored: {error_type}: {message}"


def request_key(request: dict) -> str:
    """A request's id in a batch: the sha256 of what the batch is sent (64 hex digits, the longest custom
    id Anthropic takes), so the same request asked again finds its answer."""
    sent = json.dumps(batch_params(request), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(sent.encode()).hexdigest()


@dataclass
class PendingRequest:
    """A request a run needs answered in its next batch: for which trial, and which of its calls."""

    custom_id: str
    request: dict
    trial_id: int
    call_number: int


class Pending(Exception):
    """The trial's next call has no answer yet: it goes into the next batch, and the trial waits for it."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(f"waiting for the answer to {custom_id} in a batch")
        self.custom_id = custom_id


# A request a batch did not answer (errored, expired) is sent again in the next batch, this many times in all
BATCH_MOST_SENDS = 3
# Batch results that may be worth sending again; any other error is the call's error
RESENDABLE = ("BatchErrored", "BatchExpired", "BatchCanceled")


class BatchReplay:
    """Stands in for the client while a run's trials are checked (or tagged) from batch answers: the run
    goes through each trial as it would with direct calls, and llm.call asks this instead of Anthropic.
    A request answered in one of the run's batches gets its answer; a refusal there is asked again
    directly with the server-side fallback, when the request has one, its two attempts billed as one call;
    any other request raises Pending and is kept for the next batch, so the trial is judged once all its
    calls are answered. `answers` are by custom id, `sends` count how often each was sent already."""

    def __init__(self, answers: dict[str, CallResult], sends: dict[str, int], direct: MessagesClient) -> None:
        self.answers = answers
        self.sends = sends
        self.direct = direct
        self.pending: dict[str, PendingRequest] = {}
        # the answers a trial used: recorded with that trial's calls
        self.used: set[str] = set()
        self.trial_id: int | None = None
        self.calls = 0

    @property
    def beta(self) -> Any:
        return self.direct.beta

    def begin(self, trial_id: int) -> None:
        """The calls that follow are this trial's, numbered from 1."""
        self.trial_id = trial_id
        self.calls = 0

    def answer(self, request: dict, operation: str) -> CallResult:
        self.calls += 1
        custom_id = request_key(request)
        found = self.answers.get(custom_id)
        if found is None or self._send_again(custom_id, found):
            if custom_id not in self.pending:
                self.pending[custom_id] = PendingRequest(custom_id, request, self.trial_id, self.calls)
            raise Pending(custom_id)
        self.used.add(custom_id)
        if is_refusal(found) and "fallbacks" in request:
            return self._ask_directly(found, request, operation)
        return found

    def _send_again(self, custom_id: str, found: CallResult) -> bool:
        if found.error is None:
            return False
        if not found.error.startswith(RESENDABLE):
            return False
        return self.sends.get(custom_id, 0) < BATCH_MOST_SENDS

    def _ask_directly(self, refused: CallResult, request: dict, operation: str) -> CallResult:
        """The refused request sent directly, where the fallback model may answer it: one call whose
        billed attempts are the batch's refusal (at batch prices) and the direct call's."""
        again = call(self.direct, request, operation)
        costs = []
        for cost in refused.costs:
            costs.append(cost)
        for cost in again.costs:
            costs.append(cost)
        again.costs = costs
        again.batch_id = refused.batch_id
        again.custom_id = refused.custom_id
        again.llm_batch_id = refused.llm_batch_id
        return again
