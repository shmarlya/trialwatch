"""A run's model calls in Message Batches (ADR-009): what checks and AI tagging share.

A batched run goes in rounds. Each round goes through the run's trials not judged (or tagged) yet, as
the direct run would, with a llm.BatchReplay in place of the client: a trial whose calls all have
answers in the run's batches is judged and saved as usual (its calls recorded at batch prices); a trial
whose next call has no answer yet waits, and its request goes into the round's batch. The run then waits
(its job too, jobs/worker.py) until Anthropic has answered that batch, and the next round starts. A
text check whose first answers leave questions open asks again with every page in the next round, so a
run takes one round, or two.

- answers(conn, client, batches, operation): the answers of a run's ended batches, or the batch still
  running
- record_unused(...): answers nothing used (the trial changed, the run stopped), recorded as calls all the
  same: they were billed
- submit(...): the requests the round left waiting, sent as a batch (or several, when they are many)
- estimate(...): what a round's requests may cost, checked against the spending cap before sending
"""

import json
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

# Characters per input token, to estimate what a batch will cost before it is sent
CHARS_PER_TOKEN = 4
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
) -> tuple[dict[str, CallResult], dict[str, int], dict[tuple[int, str], BatchItem], Batch | None]:
    """The answers of a run's batches by custom id (a later batch's answer replaces an earlier one), how
    often each request was sent, each batch's requests by (batch, custom id), and the first batch
    Anthropic is still answering (None when all have ended: the answers are complete)."""
    found: dict[str, CallResult] = {}
    sends: dict[str, int] = {}
    items: dict[tuple[int, str], BatchItem] = {}
    for batch in batches:
        batch_items = batches_db.items(conn, batch.id)
        models = {}
        for item in batch_items:
            items[(batch.id, item.custom_id)] = item
            models[item.custom_id] = item.model
            if item.custom_id not in sends:
                sends[item.custom_id] = 0
            sends[item.custom_id] += 1
        if batch.status == "submitted":
            state = llm.batch_state(client, batch.provider_batch_id)
            batches_db.set_counts(conn, batch.id, state.counts(), state.ended)
            if not state.ended:
                return found, sends, items, batch
        results = llm.batch_results(client, batch.provider_batch_id, models, operation)
        for custom_id, result in results.items():
            result.llm_batch_id = batch.id
            found[custom_id] = result
        if batch.status == "submitted":
            batches_db.collected(conn, batch.id)
    return found, sends, items, None


def record_unused(
    conn: psycopg.Connection,
    replay: llm.BatchReplay,
    items: dict[tuple[int, str], BatchItem],
    operation: str,
    check_run_id: int | None = None,
    annotation_run_id: int | None = None,
) -> int:
    """Record the answers this round's trials did not use and nobody recorded yet (their trial's request
    changed, or the run stopped before it): billed, so kept like every call. Returns how many."""
    recorded = 0
    for custom_id, result in replay.answers.items():
        if custom_id in replay.used or custom_id in replay.pending:
            continue
        item = items.get((result.llm_batch_id, custom_id))
        if item is None or item.llm_call_id is not None:
            continue
        if result.error is not None and not result.costs:
            # not answered, so not billed: nothing to keep (an expired request)
            continue
        request = {"model": item.model, "unused": "its trial asked something else, or the run stopped first"}
        llm.record(
            conn,
            result,
            request,
            operation=operation,
            check_run_id=check_run_id,
            trial_id=item.trial_id,
            call_number=item.call_number,
            annotation_run_id=annotation_run_id,
        )
        recorded += 1
    return recorded


def estimate(pending: list[llm.PendingRequest]) -> Decimal:
    """What these requests may cost at batch prices: their characters as input tokens, and a fixed share
    of output each. An estimate only: the calls are priced from their usage when answered."""
    total = Decimal(0)
    for request in pending:
        characters = len(json.dumps(request.request.get("messages", []))) + len(str(request.request.get("system", "")))
        input_tokens = characters // CHARS_PER_TOKEN
        cost = llm.price_tokens(request.request.get("model", ""), input_tokens, OUTPUT_ALLOWANCE, batch=True)
        if cost.cost_usd is not None:
            total += cost.cost_usd
    return total


def over_cap(conn: psycopg.Connection, estimated: Decimal) -> bool:
    """Whether sending a batch that may cost this would take this month's spend over the cap."""
    cap = costs_db.monthly_cap(conn)
    if cap is None:
        return False
    return costs_db.spent_this_month(conn) + estimated > cap


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
