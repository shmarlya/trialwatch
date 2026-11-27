"""The check's named setups (src/trialwatch/checks/settings.py, checks/configs/*.json)."""

import pytest
from conftest import default_settings
from test_evidence import GOOD, PROMPT, FakeClient, seed

from trialwatch.checks import evidence as ev
from trialwatch.checks import settings


def test_every_setup_in_the_package_can_be_used():
    found = settings.names()
    assert settings.DEFAULT in found
    for name in found:
        loaded = settings.load(name)
        assert loaded.config == name
        assert loaded.method in settings.METHODS


def test_the_default_setup_reads_page_text_against_the_eu_ctr_set():
    loaded = settings.load(settings.DEFAULT)
    assert (loaded.method, loaded.prompt, loaded.requirements) == ("text", "evidence", "eu-ctr-results")
    assert loaded.summary().startswith("default: ")


def test_a_name_is_never_a_path():
    for name in ("../default", "/tmp/default", "nothing-here"):
        with pytest.raises(FileNotFoundError):
            settings.load(name)


def good_setup() -> dict:
    return {"description": "x", "model": "claude-test", "effort": "high", "method": "text", "prompt": "evidence",
            "requirements": "eu-ctr-results", "max_tokens": 100, "fallbacks": True, "whole_pages": 15, "top_k": 4,
            "max_text_chars": 1000, "unreadable_pages_allowed": 1, "widen_top_k": 20, "widen_max_chars": 1000,
            "mode": "batch"}


def test_a_good_setup_has_no_problems():
    assert settings._problems("x", good_setup()) == []


def test_a_setup_with_a_missing_wrong_or_unknown_field_is_refused():
    missing = good_setup()
    del missing["model"]
    assert settings._problems("x", missing) == ["model is missing"]
    wrong_type = good_setup()
    wrong_type["top_k"] = "4"
    assert settings._problems("x", wrong_type) == ["top_k must be a int"]
    # a flag is not a number, even though Python counts True as 1
    flag = good_setup()
    flag["whole_pages"] = True
    assert settings._problems("x", flag) == ["whole_pages must be a int"]
    unknown = good_setup()
    unknown["temperature"] = 0
    assert settings._problems("x", unknown) == ["unknown field 'temperature'"]


def test_a_setup_naming_what_does_not_exist_is_refused():
    setup = good_setup()
    setup["method"] = "fax"
    setup["effort"] = "huge"
    setup["prompt"] = "nope"
    setup["requirements"] = "nope"
    setup["top_k"] = 0
    setup["mode"] = "someday"
    problems = settings._problems("x", setup)
    assert "mode must be one of batch, direct" in problems
    assert "method must be one of text, files" in problems
    assert "effort must be one of low, medium, high, xhigh, max" in problems
    assert "no prompt 'nope' in checks/prompts/" in problems
    assert "no requirement set 'nope' in checks/requirement_sets/" in problems
    assert "top_k must be 1 or more" in problems


def test_a_run_records_the_setup_it_used(conn):
    trial = seed(conn)
    summary = ev.run(conn, FakeClient(GOOD), [trial], default_settings(), PROMPT)
    params = conn.execute("SELECT params FROM checks.run WHERE id = %s", (summary.run_id,)).fetchone()[0]
    assert params["config"] == "default"
    assert params["method"] == "text"
