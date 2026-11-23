"""Measuring patient search without a model (ADR-008, phase 3): readings compared with the expected
ones, what each query finds, and the baseline of plain text matching on CTIS's list."""

import json

from test_cli import Calls
from test_search_web import trials  # noqa: F401  (the fixture)
from typer.testing import CliRunner

from trialwatch import cli
from trialwatch.search import evaluate, query

CASES = {"queries": [
    {"text": "diabetes Paris", "language": "en", "kind": "condition-place",
     "expected": {"condition": "diabetes", "near": [48.86, 2.35]}},
    {"text": "diabète Lyon", "language": "fr", "kind": "condition-place",
     "expected": {"condition": "diabète", "near": [45.76, 4.84]}},
    # read wrongly on purpose: the place is not Madrid
    {"text": "asthma 28046", "language": "en", "kind": "postcode",
     "expected": {"condition": "asthma", "near": [48.86, 2.35]}},
]}


def test_wrong_fields_says_what_was_misread(conn):
    reading = query.Interpretation(text="x", condition_words=["diabetes"], age_group="0-17")
    assert evaluate.wrong_fields(conn, reading, {"condition": "diabetes", "age_group": "0-17"}) == []
    assert evaluate.wrong_fields(conn, reading, {"condition": "asthma", "near": [48.86, 2.35], "country": "FR"}) == [
        "condition “diabetes”, not “asthma”", "no place", "country None, not FR"]


def test_the_report_counts_readings_results_and_the_baseline(trials, app_conn, tmp_path, monkeypatch):  # noqa: F811
    path = tmp_path / "queries.json"
    path.write_text(json.dumps(CASES), encoding="utf-8")
    report = evaluate.evaluate(app_conn, evaluate.load_queries(path))
    assert report.read_right() == 2
    first, second, third = report.results
    # near Paris: the Villejuif trial; in France and anywhere: both diabetes trials
    assert (first.near, first.in_country, first.anywhere) == (1, 2, 2)
    # plain text: the listing's conditions say "Type 2 Diabetes Mellitus" for every listed trial here
    assert first.baseline >= 2
    # in French: the translation finds it, the plain text on the English list does not
    assert second.near == 1 and second.baseline == 0
    assert third.wrong == ["place 28046 Madrid, Spain (1047 km off)"]
    monkeypatch.setattr(cli, "load_env_file", Calls())
    result = CliRunner().invoke(cli.app, ["search", "eval", "--queries", str(path)])
    assert result.exit_code == 0, result.stdout
    assert "read right: 2 of 3" in result.stdout and "wrong: place 28046 Madrid, Spain" in result.stdout
