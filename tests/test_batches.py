"""Model calls in Message Batches (ADR-009): a batched check judges like a direct one, in rounds, at batch
prices; refusals are asked again directly, unanswered requests sent again, unused answers recorded."""

from decimal import Decimal

from conftest import default_settings
from test_evidence import (
    CT,
    CT_2,
    ERRORED,
    EXPIRED,
    GOOD,
    PROMPT,
    REFUSED,
    FakeClient,
    answer,
    appendix_pages,
    good_but,
    pages_with,
    readable,
    seed,
)

from trialwatch import batching, llm
from trialwatch.checks import evidence as ev
from trialwatch.db import batches as batches_db
from trialwatch.db import checks as checks_db


def run_until_done(conn, client, trials, settings, state):
    """A batched run as a job goes: round after round, until it returns its summary."""
    outcomes = []
    while True:
        outcome = ev.run_batched(conn, client, trials, settings, state, PROMPT)
        outcomes.append(outcome)
        if isinstance(outcome, ev.RunSummary):
            return outcome, outcomes


def calls_of(conn, run_id):
    return conn.execute(
        "SELECT trial_id, call_number, tier, llm_batch_id IS NOT NULL, custom_id IS NOT NULL, cost_usd, NOT (request ? 'messages') "
        "FROM core.llm_call WHERE check_run_id = %s ORDER BY id",
        (run_id,),
    ).fetchall()


def test_batch_params_leave_out_the_fallback():
    request = {
        "model": "claude-sonnet-5-5",
        "max_tokens": 10,
        "betas": [llm.FALLBACK_BETA],
        "fallbacks": "default",
        "messages": [],
    }
    assert llm.batch_params(request) == {"model": "claude-sonnet-5-5", "max_tokens": 10, "messages": []}
    # the same request is the same custom id, whatever its fallback
    assert llm.request_key(request) == llm.request_key({"model": "claude-sonnet-5-5", "max_tokens": 10, "messages": []})
    assert len(llm.request_key(request)) == 64


def test_batch_prices_are_half_the_list():
    direct = llm.price_tokens("claude-sonnet-5-5", 1_000_000, 1_000_000)
    batched = llm.price_tokens("claude-sonnet-5-5", 1_000_000, 1_000_000, batch=True)
    assert direct.cost_usd == Decimal("12") and batched.cost_usd == Decimal("6")
    assert batched.prices["input"] == "1.000"
    haiku = llm.price_tokens("claude-haiku-5-5", 1000, 0, batch=True)
    assert haiku.prices["input"] == "0.050"


def test_batches_are_split_by_count_and_size(monkeypatch):
    monkeypatch.setattr(llm, "BATCH_MOST_REQUESTS", 2)
    items = []
    for number in range(5):
        items.append((f"id{number}", {"messages": [number]}))
    assert [len(chunk) for chunk in llm.batch_chunks(items)] == [2, 2, 1]


def test_a_batched_check_judges_as_a_direct_one_at_half_the_price(conn):
    trial = seed(conn)
    direct = ev.run(conn, FakeClient(GOOD), [trial], default_settings(), PROMPT)
    client = FakeClient(GOOD)
    state = {}
    summary, outcomes = run_until_done(conn, client, [trial], default_settings(), state)
    # one round: sent, then judged from its answer
    assert isinstance(outcomes[0], batching.Waiting) and len(outcomes) == 2
    assert summary.labels == direct.labels == {"not full": 1}
    assert (summary.evidence, summary.verified) == (direct.evidence, direct.verified)
    # half, to the millionth of a dollar core.llm_call keeps
    assert abs(summary.cost_usd - direct.cost_usd / 2) <= Decimal("0.000001")
    calls = calls_of(conn, summary.run_id)
    assert calls == [(trial, 1, "batch", True, True, summary.cost_usd, False)]
    # the same parts and trace as the direct run's judgement
    rows = conn.execute(
        "SELECT run_id, parts, label FROM checks.judgement WHERE trial_id = %s ORDER BY run_id", (trial,)
    ).fetchall()
    assert rows[0][1:] == rows[1][1:]
    run = conn.execute("SELECT status FROM checks.run WHERE id = %s", (summary.run_id,)).fetchone()
    assert run == ("succeeded",)
    batch = batches_db.batches_of_run(conn, check_run_id=summary.run_id)
    assert [(item.status, item.round) for item in batch] == [("collected", 1)]
    assert batches_db.items(conn, batch[0].id)[0].llm_call_id is not None
    # the batch was sent without the fallback (a Haiku setup has none anyway)
    assert "fallbacks" not in client.batches.sent[0]["requests"][0]["params"]


def test_a_run_waits_while_its_batch_runs(conn):
    trial = seed(conn)
    client = FakeClient(GOOD)
    client.batches.polls_before_end = 1
    state = {}
    first = ev.run_batched(conn, client, [trial], default_settings(), state, PROMPT)
    assert isinstance(first, batching.Waiting)
    assert conn.execute("SELECT status FROM checks.run WHERE id = %s", (state["run_id"],)).fetchone() == ("waiting",)
    # a waiting run is not abandoned: another run leaves it alone
    assert checks_db.close_abandoned_runs(conn) == []
    second = ev.run_batched(conn, client, [trial], default_settings(), state, PROMPT)
    assert isinstance(second, batching.Waiting) and second.batch_id == first.batch_id
    summary = ev.run_batched(conn, client, [trial], default_settings(), state, PROMPT)
    assert summary.labels == {"not full": 1}
    # nothing was sent twice
    assert len(client.batches.sent) == 1


def test_widening_is_a_second_round(conn):
    extra = appendix_pages()
    for page in readable(["Far appendix marker."]):
        extra.append(page)
    trial = seed(conn, pages_with(extra))
    first = good_but([answer("endpoint:12", found=False, kind="other")])
    client = FakeClient(first, [answer("cancelled", found=False, kind="other"), GOOD[3]])
    settings = default_settings({"whole_pages": 0, "top_k": 1})
    summary, outcomes = run_until_done(conn, client, [trial], settings, {})
    assert [type(outcome).__name__ for outcome in outcomes] == ["Waiting", "Waiting", "RunSummary"]
    assert len(client.batches.sent) == 2
    assert "Far appendix marker" in client.requests[1]["messages"][0]["content"]
    assert summary.widened == 2
    assert [(row[1], row[2]) for row in calls_of(conn, summary.run_id)] == [(1, "batch"), (2, "batch")]
    rounds = [batch.round for batch in batches_db.batches_of_run(conn, check_run_id=summary.run_id)]
    assert rounds == [1, 2]


def test_an_unanswered_request_is_sent_again_then_fails_its_trial(conn):
    first = seed(conn)
    second = seed(conn, ct_number=CT_2)
    client = FakeClient(EXPIRED, GOOD, ERRORED, ERRORED)
    summary, outcomes = run_until_done(conn, client, [first, second], default_settings(), {})
    # sent three times in all, then its trial fails with an error a later attempt may fix
    assert len(client.batches.sent) == 3
    assert summary.labels == {"not full": 1}
    assert summary.failed[CT].startswith("BatchErrored: overloaded_error")
    run = conn.execute("SELECT status, trials, errors FROM checks.run WHERE id = %s", (summary.run_id,)).fetchone()
    assert run == ("failed", 2, 1)


def test_a_refusal_in_a_batch_is_asked_again_directly_with_the_fallback(conn):
    trial = seed(conn)
    client = FakeClient(REFUSED, GOOD)
    settings = default_settings({"model": "claude-sonnet-5-5"})
    summary, outcomes = run_until_done(conn, client, [trial], settings, {})
    assert summary.labels == {"not full": 1}
    # the batch without the fallback, then the direct call with it
    assert "fallbacks" not in client.requests[0] and client.requests[1]["fallbacks"] == "default"
    row = conn.execute(
        "SELECT tier, jsonb_array_length(attempts), attempts->0->'prices'->>'input', "
        "attempts->1->'prices'->>'input' FROM core.llm_call WHERE check_run_id = %s",
        (summary.run_id,),
    ).fetchone()
    # one call, its two billed attempts: the refusal at batch prices, the direct answer at list prices
    assert row == ("batch", 2, "1.000", "2.00")


def test_an_answer_no_trial_used_is_recorded_all_the_same(conn):
    trial = seed(conn)
    client = FakeClient(GOOD, GOOD)
    state = {}
    ev.run_batched(conn, client, [trial], default_settings(), state, PROMPT)
    # the trial's evidence changed before the answer came: it asks something else
    changed = default_settings({"max_tokens": 1234})
    summary, outcomes = run_until_done(conn, client, [trial], changed, state)
    calls = calls_of(conn, summary.run_id)
    assert [(call[1], call[6]) for call in calls] == [(1, True), (1, False)]
    assert len(client.batches.sent) == 2


def test_a_batch_over_the_cap_is_not_sent(conn):
    from trialwatch.db import costs as costs_db

    trial = seed(conn)
    costs_db.set_monthly_cap(conn, Decimal("0.000001"))
    client = FakeClient(GOOD)
    outcome = ev.run_batched(conn, client, [trial], default_settings(), {}, PROMPT, capped=True)
    assert isinstance(outcome, ev.Held) and outcome.estimated_usd > 0
    assert client.batches.sent == []


def test_the_command_line_waits_for_its_batches(conn, monkeypatch):
    monkeypatch.setattr(batching, "FOREGROUND_POLL", 0)
    trial = seed(conn)
    client = FakeClient(GOOD)
    client.batches.polls_before_end = 2
    lines = []
    summary = ev.run_batched(
        conn, client, [trial], default_settings(), {}, PROMPT, progress=lines.append, foreground=True
    )
    assert summary.labels == {"not full": 1}
    assert any("answered" in line for line in lines)


# --- review of PR 4 (ADR-009) ----------------------------------------------------------------------


def widening_trials(conn):
    """Two trials reading the same long results summary (one CTIS file): retrieval sends part of it."""
    extra = appendix_pages()
    for page in readable(["Far appendix marker."]):
        extra.append(page)
    first = seed(conn, pages_with(extra))
    second = seed(conn, ct_number=CT_2)
    return first, second


# Every question found: nothing to ask again with every page
SETTLED = good_but(
    [
        answer("cancelled", quote="Safety: no serious adverse events.", page=5, kind="other"),
        answer("endpoint:12", quote="Colorectal transit time was not analysed", page=4),
    ]
)


def widening_replies():
    """The first trial's answers (all settled); the second's (a question left open), then its widening."""
    open_first = good_but([answer("endpoint:12", found=False, kind="other")])
    return [SETTLED, open_first, [answer("cancelled", found=False, kind="other"), GOOD[3]]]


def test_a_run_waiting_on_a_batch_publishes_nothing_yet(conn):
    first, second = widening_trials(conn)
    client = FakeClient(*widening_replies())
    settings = default_settings({"whole_pages": 0, "top_k": 1})
    state = {}
    ev.run_batched(conn, client, [first, second], settings, state, PROMPT)
    # round 2: the first trial is judged, the second waits on its widening
    outcome = ev.run_batched(conn, client, [first, second], settings, state, PROMPT)
    assert isinstance(outcome, batching.Waiting)
    assert checks_db.judged_trials(conn, state["run_id"]) == {first}
    public = conn.execute(
        "SELECT count(*) FROM checks.judgement_current WHERE run_id = %s", (state["run_id"],)
    ).fetchone()
    assert public == (0,)
    assert state["run_id"] not in checks_db.runs_without_agreement(conn)
    # every answer of the ended batch is recorded already, also the one the waiting trial used
    calls = conn.execute(
        "SELECT trial_id, call_number FROM core.llm_call WHERE check_run_id = %s ORDER BY trial_id", (state["run_id"],)
    ).fetchall()
    assert calls == [(first, 1), (second, 1)]
    summary = ev.run_batched(conn, client, [first, second], settings, state, PROMPT)
    assert len(checks_db.judged_trials(conn, summary.run_id)) == 2 and not summary.failed
    public = conn.execute(
        "SELECT count(*) FROM checks.judgement_current WHERE run_id = %s", (summary.run_id,)
    ).fetchone()
    assert public == (2,)


def test_each_batch_answer_is_one_call_whatever_becomes_of_its_trial(conn, monkeypatch):
    trial = seed(conn)
    client = FakeClient(GOOD)
    state = {}
    ev.run_batched(conn, client, [trial], default_settings(), state, PROMPT)
    original = ev._save
    calls = []

    def fails_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the database went away")
        return original(*args, **kwargs)

    monkeypatch.setattr(ev, "_save", fails_once)
    try:
        ev.run_batched(conn, client, [trial], default_settings(), state, PROMPT)
    except RuntimeError:
        pass
    summary = ev.run_batched(conn, client, [trial], default_settings(), state, PROMPT)
    assert summary.labels == {"not full": 1}
    rows = conn.execute(
        "SELECT count(*), bool_and(request ? 'messages') FROM core.llm_call WHERE check_run_id = %s", (summary.run_id,)
    ).fetchone()
    # one row, brought up to date with the request the trial sent
    assert rows == (1, True)
    # its batch was downloaded once
    assert len(client.batches.results_of) == 1


def test_a_refusal_is_asked_again_directly_once(conn):
    first, second = widening_trials(conn)
    open_first = good_but([answer("endpoint:12", found=False, kind="other")])
    client = FakeClient(SETTLED, REFUSED, open_first, [answer("cancelled", found=False, kind="other"), GOOD[3]])
    settings = default_settings({"model": "claude-sonnet-5-5", "whole_pages": 0, "top_k": 1})
    summary, outcomes = run_until_done(conn, client, [first, second], settings, {})
    assert len(checks_db.judged_trials(conn, summary.run_id)) == 2 and not summary.failed
    direct = [request for request in client.requests if "fallbacks" in request]
    # round 2 asked it directly; round 3 used that answer instead of asking again
    assert len(direct) == 1
    attempts = conn.execute(
        "SELECT jsonb_array_length(attempts) FROM core.llm_call WHERE trial_id = %s "
        "AND call_number = 1 AND check_run_id = %s",
        (second, summary.run_id),
    ).fetchone()
    assert attempts == (2,)


def test_a_run_whose_prompt_changed_while_it_waited_ends_and_a_new_one_starts(conn):
    import dataclasses

    trial = seed(conn)
    client = FakeClient(GOOD, GOOD)
    state = {}
    ev.run_batched(conn, client, [trial], default_settings(), state, PROMPT)
    first_run = state["run_id"]
    changed = dataclasses.replace(PROMPT, text=PROMPT.text + "\nOne more rule.", sha256=b"\x01" * 32)
    lines = []
    outcome = ev.run_batched(conn, client, [trial], default_settings(), state, changed, progress=lines.append)
    assert isinstance(outcome, batching.Waiting) and state["run_id"] != first_run
    assert any("another prompt or requirement set" in line for line in lines)
    closed = conn.execute("SELECT status, error FROM checks.run WHERE id = %s", (first_run,)).fetchone()
    assert closed[0] == "stopped" and "while it waited" in closed[1]
    # the first batch's answer is recorded all the same (it was billed)
    assert conn.execute("SELECT count(*) FROM core.llm_call WHERE check_run_id = %s", (first_run,)).fetchone() == (1,)


def test_an_interrupted_command_line_run_cancels_its_batch_and_closes_the_run(conn, monkeypatch):
    first = seed(conn)
    second = seed(conn, ct_number=CT_2)
    client = FakeClient(GOOD, GOOD)
    client.batches.polls_before_end = 100

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(batching, "wait_here", interrupted)
    state = {}
    try:
        ev.run_batched(conn, client, [first, second], default_settings(), state, PROMPT, foreground=True)
    except KeyboardInterrupt:
        pass
    assert client.batches.cancelled == ["msgbatch_1"]
    run = conn.execute("SELECT status, error FROM checks.run WHERE id = %s", (state["run_id"],)).fetchone()
    assert run == ("stopped", "KeyboardInterrupt: ")


def test_batch_errors_are_tried_again_later():
    from trialwatch.jobs.retry import is_transient

    assert is_transient("BatchErrored: overloaded_error: Overloaded")
    assert is_transient("widening: BatchExpired: the batch ended before this request was answered")
    assert is_transient("BatchCanceled: the batch was cancelled before this request was answered")
    assert not is_transient("BatchInvalidRequest: max_tokens: too large")


def test_the_estimate_reads_text_as_written_and_pdfs_by_size():
    bulgarian = "Резултати от клиничното изпитване" * 100
    request = {"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": bulgarian}]}
    assert batching.request_tokens(request) == int(len(bulgarian) / batching.CHARS_PER_TOKEN)
    pdf = "A" * 4_000_000  # base64 of 3 MB
    files = {
        "model": "claude-haiku-5-5",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf}},
                    {"type": "text", "text": "Questions"},
                ],
            }
        ],
    }
    assert batching.request_tokens(files) == int(len("Questions") / batching.CHARS_PER_TOKEN) + 3_000_000 // 30


def test_the_cap_counts_batches_sent_and_not_answered_yet(conn):
    from trialwatch.db import costs as costs_db

    trial = seed(conn)
    client = FakeClient(GOOD)
    client.batches.polls_before_end = 100
    ev.run_batched(conn, client, [trial], default_settings(), {}, PROMPT)
    in_flight = batches_db.in_flight_usd(conn)
    assert in_flight > 0
    costs_db.set_monthly_cap(conn, in_flight + Decimal("0.0001"))
    assert batching.over_cap(conn, Decimal("0.001"))
    assert not batching.over_cap(conn, Decimal("0.00001"))
