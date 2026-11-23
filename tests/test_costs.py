"""What model calls cost: pricing per billed attempt, the ledger (core.llm_call), and the totals a
judgement, a run and the spend page show."""

from decimal import Decimal
from types import SimpleNamespace

import anthropic
from conftest import default_settings
from test_evidence import GOOD, PROMPT, FakeClient, api_error, seed

from trialwatch import llm
from trialwatch.checks import evidence as ev
from trialwatch.db import costs as costs_db
from trialwatch.db import core, overview


def test_a_call_is_priced_by_its_tokens_at_the_list_price():
    cost = llm.price_tokens("claude-opus-5-5", input_tokens=1_000_000, output_tokens=100_000,
                              cache_write_tokens=0, cache_read_tokens=1_000_000)
    # $4 a million in, $20 a million out, $0.20 a million read from the cache
    assert cost.cost_usd == Decimal("4") + Decimal("2") + Decimal("0.2")
    assert cost.prices == {"input": "4.00", "output": "20.00", "cache_write": "5.00", "cache_read": "0.20"}


def test_a_long_prompt_costs_more_on_haiku():
    short = llm.price_tokens("claude-haiku-5-5", input_tokens=100_000, output_tokens=0)
    long = llm.price_tokens("claude-haiku-5-5", input_tokens=100_001, output_tokens=0)
    assert short.cost_usd == Decimal("0.01")
    assert long.prices["input"] == "0.50"
    # cached tokens count towards the prompt's length too
    cached = llm.price_tokens("claude-haiku-5-5", input_tokens=50_000, output_tokens=0, cache_read_tokens=60_000)
    assert cached.prices["input"] == "0.50"


def test_an_unknown_model_is_unpriced_not_free():
    cost = llm.price_tokens("claude-test", input_tokens=500, output_tokens=50)
    assert cost.cost_usd is None and not cost.priced
    assert llm.total_cost([cost]) == 0 and llm.unpriced_count([cost]) == 1


def test_a_fallback_is_priced_per_attempt_at_the_model_that_ran_it():
    declined = SimpleNamespace(type="message", model="claude-opus-5-5", input_tokens=1000, output_tokens=10,
                               cache_creation_input_tokens=0, cache_read_input_tokens=0)
    answered = SimpleNamespace(type="fallback_message", model="claude-opus-4-8", input_tokens=1000, output_tokens=500,
                               cache_creation_input_tokens=0, cache_read_input_tokens=0)
    usage = SimpleNamespace(input_tokens=1000, output_tokens=500, iterations=[declined, answered])
    priced = llm.price_usage(usage, "claude-opus-4-8")
    assert [cost.model for cost in priced] == ["claude-opus-5-5", "claude-opus-4-8"]
    assert priced[0].cost_usd == Decimal("0.0042")
    assert priced[1].cost_usd == Decimal("0.0175")
    # without iterations, the response's own model and usage
    plain = llm.price_usage(SimpleNamespace(input_tokens=10, output_tokens=0), "claude-sonnet-5-5")
    assert [(cost.model, cost.cost_usd) for cost in plain] == [("claude-sonnet-5-5", Decimal("0.00002"))]


def test_money_reads_plainly():
    assert llm.dollars(Decimal("1234.5")) == "$1,234.50"
    assert llm.dollars(Decimal("0.000075")) == "$0.0001"
    assert llm.dollars(Decimal(0)) == "$0.00"
    assert llm.dollars(None) == "—"


def test_a_run_records_every_billed_call_and_its_totals_agree(conn):
    trial = seed(conn)
    # the fake answers as the setup's model, claude-haiku-5-5: 500 tokens in, 50 out
    summary = ev.run(conn, FakeClient(GOOD), [trial], default_settings(), PROMPT)
    # one row per call, with each billed attempt priced and their sums
    calls = conn.execute("SELECT model, operation, check_run_id, trial_id, call_number, input_tokens, output_tokens, "
                         "cost_usd, unpriced_attempts, jsonb_array_length(attempts), attempts->0->>'cost_usd', actor_id "
                         "FROM core.llm_call WHERE check_run_id = %s", (summary.run_id,)).fetchall()
    expected = Decimal("0.000075")
    assert calls == [("claude-haiku-5-5", "check", summary.run_id, trial, 1, 500, 50, expected, 0, 1, "0.000075",
                      core.current_actor(conn))]
    assert summary.cost_usd == expected
    # the run's, the judgement's and the trace step's cost are the call's, as their pages show them
    run = overview.run_row(conn, summary.run_id)
    judgement = overview.run_judgements(conn, summary.run_id)[0]
    assert run.cost_usd == judgement.cost_usd == costs_db.run_cost(conn, summary.run_id) == expected
    assert (run.input_tokens, judgement.input_tokens, run.unpriced_calls) == (500, 500, 0)
    step = overview.run_steps(conn, summary.run_id, trial)
    call_step = [found for found in step if found.name == "call"][0]
    assert Decimal(call_step.data["cost_usd"]) == expected and call_step.data["input_tokens"] == 500
    assert costs_db.spent_this_month(conn) >= expected
    # Grafana's view of it: each billed attempt by model, without who the person was
    spend = conn.execute("SELECT model, made_for, cost_usd FROM report.spend").fetchall()
    assert spend == [("claude-haiku-5-5", "a person", expected)]


def test_the_cap_is_reached_when_this_months_spend_meets_it(conn):
    core.act_as(conn, "test-admin")
    assert costs_db.monthly_cap(conn) is None and not costs_db.cap_reached(conn)
    costs_db.set_monthly_cap(conn, Decimal("0"))
    assert costs_db.monthly_cap(conn) == 0 and costs_db.cap_reached(conn)
    costs_db.set_monthly_cap(conn, Decimal("12.50"))
    assert costs_db.monthly_cap(conn) == Decimal("12.50") and not costs_db.cap_reached(conn)
    costs_db.set_monthly_cap(conn, None)
    assert costs_db.monthly_cap(conn) is None


def test_a_failed_call_is_recorded_with_its_error_and_costs_nothing(conn):
    trial = seed(conn)
    summary = ev.run(conn, FakeClient(api_error(anthropic.InternalServerError, "overloaded")), [trial], default_settings(), PROMPT)
    assert list(summary.failed.values())[0].startswith("InternalServerError")
    call = conn.execute("SELECT error, attempts, cost_usd, response, request ? 'messages' FROM core.llm_call "
                        "WHERE check_run_id = %s", (summary.run_id,)).fetchone()
    assert call[0].startswith("InternalServerError") and call[1:4] == ([], None, None) and call[4]
    assert overview.run_row(conn, summary.run_id).cost_usd == 0
    assert costs_db.spend_totals(conn).calls == 0
