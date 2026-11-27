"""A run's model calls in Message Batches (ADR-009): what checks and AI tagging share.

A batched run goes in rounds. Each round goes through the run's trials not judged (or tagged) yet, as
the direct run would, with a llm.BatchReplay in place of the client: a trial whose calls all have
answers in the run's batches is judged and saved as usual; a trial whose next call has no answer yet
waits, and its request goes into the round's batch. The run then waits (its job too, jobs/worker.py)
until Anthropic has answered that batch, and the next round starts. A text check whose first answers
leave questions open asks again with every page in the next round, so a run takes one round, or two.

Every answer is recorded in core.llm_call when its batch is collected, once (it is billed, whatever
becomes of its trial), and later rounds read it from there: a trial that uses it brings that row up to
date with its request, so nothing billed goes unrecorded and nothing is recorded twice.

- answers(conn, client, batches, operation): the answers of a run's batches (collecting those that
  ended), or the batch still running
- settle(...): a run that ends early collects its ended batches and cancels the others
- submit(...): the requests the round left waiting, sent as a batch (or several, when they are many)
- estimate(...), over_cap(...): what a round's requests may cost, against the spending cap before sending
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import psycopg

from trialwatch import llm
from trialwatch.db import batches as batches_db
from trialwatch.db import costs as costs_db
from trialwatch.db.batches import Batch, BatchItem
from trialwatch.llm import CallResult, MessagesClient

log = logging.getLogger("trialwatch.batching")

# Characters per input token, to estimate what a batch will cost before it is sent: trial documents (tables,
# numbers, other languages) measured about 2.6 (2026-11-27, live run); lower is safer for the cap, and it
# matters most near a long-prompt price tier
CHARS_PER_TOKEN = 2.5
# A PDF's bytes per input token in that estimate: a page of text is billed at about 1,500-3,000 tokens and a
# text-heavy PDF page takes some 30-100 KB; erring high keeps the cap safe without holding a round for nothing
PDF_BYTES_PER_TOKEN = 30
# Output tokens counted per request in that estimate (answers with their thinking run to a few thousand)
OUTPUT_ALLOWANCE = 3000
# How often a run waiting on its batch in the foreground (the command line) asks Anthropic, in seconds
FOREGROUND_POLL = 30.0


@dataclass(frozen=True)
class Waiting:
    """A batched run is waiting on this batch (core.llm_batch id)."""

    batch_id: int
    provider_batch_id: str
    round: int
    requests: int


def answers(
    conn: psycopg.Connection, client: MessagesClient, batches: list[Batch], operation: str
) -> tuple[dict[str, CallResult], dict[str, int], Batch | None]:
    """The answers of a run's batches by custom id (a later batch's answer replaces an earlier one), how
    often each request was sent, and the first batch Anthropic is still answering (None when all have
    ended: the answers are complete). A batch that has ended is collected first: its answers recorded."""
    sends: dict[str, int] = {}
    for batch in batches:
        batch_items = batches_db.items(conn, batch.id)
        for item in batch_items:
            if item.custom_id not in sends:
                sends[item.custom_id] = 0
            sends[item.custom_id] += 1
        if batch.status == "submitted":
            state = llm.batch_state(client, batch.provider_batch_id)
            batches_db.set_counts(conn, batch.id, state.counts(), state.ended)
            if not state.ended:
                return {}, sends, batch
            collect(conn, client, batch, batch_items, operation)
    found: dict[str, CallResult] = {}
    batch_ids = [batch.id for batch in batches]
    for answer in batches_db.recorded_answers(conn, batch_ids):
        found[answer.custom_id] = llm.result_from_recorded(answer)
    return found, sends, None


def collect(
    conn: psycopg.Connection, client: MessagesClient, batch: Batch, items: list[BatchItem], operation: str
) -> int:
    """Record an ended batch's answers in core.llm_call (each once: a request recorded already is left
    as it is) and mark it collected; returns how many were recorded now."""
    models = {}
    for item in items:
        models[item.custom_id] = item.model
    results = llm.batch_results(client, batch.provider_batch_id, models, operation)
    recorded = 0
    with conn.transaction():
        for item in items:
            result = results.get(item.custom_id)
            if result is None or item.llm_call_id is not None:
                continue
            result.llm_batch_id = batch.id
            # the request as recorded until a trial uses the answer and records the request it sent
            request = {"model": item.model, "batch": batch.provider_batch_id}
            llm.record(
                conn,
                result,
                request,
                operation=operation,
                check_run_id=batch.check_run_id,
                trial_id=item.trial_id,
                call_number=item.call_number,
                annotation_run_id=batch.annotation_run_id,
            )
            recorded += 1
        batches_db.collected(conn, batch.id)
    return recorded


def settle(conn: psycopg.Connection, client: MessagesClient, batches: list[Batch], operation: str) -> None:
    """A run that ends before its rounds do: its ended batches are collected (their answers are billed)
    and the others cancelled at Anthropic, where what they answered is billed all the same. Best
    effort: a batch Anthropic can't be asked about is left as it is, and logged."""
    for batch in batches:
        if batch.status != "submitted":
            continue
        try:
            state = llm.batch_state(client, batch.provider_batch_id)
            batches_db.set_counts(conn, batch.id, state.counts(), state.ended)
            if state.ended:
                collect(conn, client, batch, batches_db.items(conn, batch.id), operation)
            elif state.status == "in_progress":
                llm.cancel_batch(client, batch.provider_batch_id)
        except Exception as exc:  # Anthropic or the network: the run ends all the same
            log.warning("batch %s could not be settled: %s", batch.provider_batch_id, exc)


def estimate(pending: list[llm.PendingRequest]) -> Decimal:
    """What these requests may cost at batch prices: their text as input tokens (characters as written,
    not JSON-escaped), each attached PDF by its size, and a fixed share of output each. An estimate
    only: the calls are priced from their usage when answered."""
    total = Decimal(0)
    for request in pending:
        input_tokens = request_tokens(request.request)
        cost = llm.price_tokens(request.request.get("model", ""), input_tokens, OUTPUT_ALLOWANCE, batch=True)
        if cost.cost_usd is not None:
            total += cost.cost_usd
    return total


def request_tokens(request: dict) -> int:
    """About how many input tokens a request is: its text, and its PDFs by their size."""
    characters = len(str(request.get("system", "")))
    document_bytes = 0
    for message in request.get("messages", []):
        content = message.get("content", "")
        if isinstance(content, str):
            characters += len(content)
            continue
        for block in content:
            if block.get("type") == "text":
                characters += len(block.get("text", ""))
            elif block.get("type") == "document":
                data = block.get("source", {}).get("data", "")
                # base64: four characters for three bytes
                document_bytes += len(data) * 3 // 4
    return int(characters / CHARS_PER_TOKEN) + document_bytes // PDF_BYTES_PER_TOKEN


def over_cap(conn: psycopg.Connection, estimated: Decimal) -> bool:
    """Whether sending a batch that may cost this would take this month's spend over the cap: what was
    recorded, what batches sent and not answered yet may cost, and this one."""
    cap = costs_db.monthly_cap(conn)
    if cap is None:
        return False
    return costs_db.spent_this_month(conn) + batches_db.in_flight_usd(conn) + estimated > cap


def submit(
    conn: psycopg.Connection,
    client: MessagesClient,
    pending: list[llm.PendingRequest],
    operation: str,
    round_number: int,
    estimated: Decimal,
    check_run_id: int | None = None,
    annotation_run_id: int | None = None,
) -> Waiting:
    """Send the round's waiting requests as one batch (several, when there are more than one takes);
    each is recorded before it is sent. Returns the last one, which the run waits on (it asks about the
    others when it wakes). An error sending raises (the job tries again later); the batches sent stay."""
    pairs = []
    for request in pending:
        pairs.append((request.custom_id, request.request))
    by_custom_id = {}
    for request in pending:
        by_custom_id[request.custom_id] = request
    chunks = llm.batch_chunks(pairs)
    waiting = None
    for chunk in chunks:
        items = []
        for custom_id, request in chunk:
            waited = by_custom_id[custom_id]
            items.append(BatchItem(0, custom_id, waited.trial_id, waited.call_number, request.get("model", ""), None))
        share = estimated * len(chunk) / len(pairs)
        batch_id = batches_db.create(
            conn,
            operation=operation,
            round_number=round_number,
            items=items,
            estimated_usd=share,
            check_run_id=check_run_id,
            annotation_run_id=annotation_run_id,
        )
        try:
            provider_batch_id = llm.submit_batch(client, chunk, operation)
        except Exception as exc:
            batches_db.not_sent(conn, batch_id, f"{type(exc).__name__}: {exc}")
            raise
        batches_db.submitted(conn, batch_id, provider_batch_id)
        waiting = Waiting(batch_id, provider_batch_id, round_number, len(chunk))
    return waiting


def next_round(batches: list[Batch]) -> int:
    if not batches:
        return 1
    return batches[-1].round + 1


def wait_here(
    conn: psycopg.Connection, client: MessagesClient, waiting: Waiting, progress: Callable[[str], None] | None
) -> None:
    """Wait in this process until Anthropic has answered the batch (the command line has no worker to
    come back later)."""
    while True:
        state = llm.batch_state(client, waiting.provider_batch_id)
        batches_db.set_counts(conn, waiting.batch_id, state.counts(), state.ended)
        if progress is not None:
            progress(f"batch {waiting.provider_batch_id}: {state.done} of {waiting.requests} answered")
        if state.ended:
            return
        time.sleep(FOREGROUND_POLL)
