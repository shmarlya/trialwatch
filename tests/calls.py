"""A model call recorded as a check's (core.llm_call), for tests that build a run by hand."""

from trialwatch.db import costs as costs_db


def recorded_call(conn, run_id, trial_id, *, input_tokens, output_tokens, cost_usd=None, number=1, answer="{}"):
    """One billed call of the trial's check in the run (`cost_usd`: a decimal string; None: unpriced); its id."""
    attempt = {
        "model": "claude-test",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "cost_usd": cost_usd,
        "prices": None,
    }
    return costs_db.record_call(
        conn,
        operation="check",
        model="claude-test",
        served_by="claude-test",
        request={"model": "claude-test"},
        response=answer,
        stop_reason="end_turn",
        latency_ms=10,
        error=None,
        request_id=None,
        attempts=[attempt],
        check_run_id=run_id,
        trial_id=trial_id,
        call_number=number,
    )
