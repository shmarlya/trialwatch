"""Gold evaluations (gold/evaluate.py, the gold pipelines in .github/workflows/): the hardest items for
the short pipeline, the gates, and `trialwatch gold eval` running our whole workflow on a dataset's
trials (CTIS and the model faked here)."""

import json
from contextlib import contextmanager

import anthropic
from test_cli import Calls
from conftest import default_settings
from test_evidence import (CT, CT_2, GOOD, PAGES, PROMPT, FakeClient, PageTexts, answer, appendix_pages, good_but,
                           pages_with, payload, readable, seed)
from test_gold_compare import FULL, NOT_FULL, an_item, meta
from test_sources_ctis import FakeCtis
from typer.testing import CliRunner

from trialwatch import cli
from trialwatch.checks import requirements
from trialwatch.checks import evidence as ev
from trialwatch.db import core
from trialwatch.gold import compare, datasets, evaluate
from trialwatch.sources import ctis, registry
from trialwatch.trials import ingest

BRUCKNER = "bruckner/2026-03-16"


def test_the_short_pipelines_sample_is_the_hardest_50_and_can_be_made_again():
    dataset = datasets.load(BRUCKNER)
    chosen = evaluate.hardest(dataset, 50, 15)
    labels = [item.expected_label for item in chosen]
    assert len(chosen) == 50 and labels.count("full") >= 15 and labels.count("not full") >= 15
    # hardest first
    scores = [evaluate.hardness(item) for item in chosen]
    assert scores == sorted(scores, reverse=True)
    committed = []
    for line in (evaluate.EVALS_DIR / "hard-50.txt").read_text().splitlines():
        if line and not line.startswith("#"):
            committed.append(line)
    keys = [item.key for item in chosen]
    assert committed == keys, "evals/hard-50.txt is stale: trialwatch gold sample --dataset bruckner/2026-03-16"


def test_a_sample_keeps_only_its_items_and_names_unknown_keys():
    dataset = datasets.load(BRUCKNER)
    first, second = dataset.items[0].key, dataset.items[1].key
    assert [item.key for item in evaluate.only_items(dataset, [second, first]).items] == [second, first]
    try:
        evaluate.only_items(dataset, ["no-such-key"])
        raise AssertionError("an unknown key must be refused")
    except KeyError as exc:
        assert "no-such-key" in str(exc)


def scored_with(labels, not_in_run=0, errors=0, false_accusations=0):
    scored = compare.Scored(dataset="test/v1", run_id=1, model="m", effort="low", prompt="p", requirements="r",
                            label_rules=requirements.load("eu-ctr-results").labels)
    scored.labels = labels
    scored.compared = sum(labels.values())
    scored.labelled = scored.compared + not_in_run + errors
    scored.not_in_run = ["x"] * not_in_run
    scored.errors = {f"e{n}": "boom" for n in range(errors)}
    scored.false_accusations = ["f"] * false_accusations
    return scored


GATES = evaluate.Gates(min_label_agreement=0.9, max_false_accusations=0, min_decided=0.8, max_missing=0.1)


def test_the_gates_pass_or_say_what_was_missed():
    good = evaluate.measures(scored_with({("full", "full"): 9, ("not full", "not full"): 9, ("undecided", "full"): 2}))
    assert evaluate.failures(good, GATES) == []
    bad = evaluate.measures(scored_with({("full", "full"): 5, ("not full", "full"): 3, ("undecided", "full"): 4},
                                        not_in_run=3, false_accusations=3))
    failed = evaluate.failures(bad, GATES)
    assert len(failed) == 4
    assert failed[0].startswith("label agreement 62.5% (5/8)") and "3 false accusations" in failed[1]
    assert failed[2].startswith("decided 66.7% (8/12)") and failed[3].startswith("missing 20.0% (3/15)")
    assert evaluate.load_gates().max_false_accusations == 0


def test_the_gates_files_name_only_known_gates_and_leave_out_the_rest(tmp_path):
    full = evaluate.load_gates(evaluate.GATES_DIR / "full.json")
    assert (full.min_label_agreement, full.max_clear_false_accusations, full.baseline) == (0.9, 0, None)
    short = evaluate.load_gates(evaluate.GATES_DIR / "short.json")
    # the hardest 50 are gated against their own baseline, not fixed shares
    assert short.min_label_agreement is None and short.min_decided is None
    assert short.baseline == evaluate.EVALS_DIR / "baselines" / "gold-short.json" and short.max_drop == 2
    assert short.max_contradicting == 0 and short.max_unstable is None
    unknown = tmp_path / "gates.json"
    unknown.write_text(json.dumps({"min_label_agrement": 0.9}))
    try:
        evaluate.load_gates(unknown)
        raise AssertionError("a misspelt gate must be refused, not ignored")
    except ValueError as exc:
        assert "min_label_agrement" in str(exc)


def test_a_clear_false_accusation_fails_and_a_contested_one_is_only_counted():
    scored = scored_with({("full", "full"): 9, ("not full", "full"): 2}, false_accusations=2)
    scored.contested_accusations = ["f"]
    found = evaluate.measures(scored)
    assert (found.false_accusations, found.clear_false_accusations) == (2, 1)
    assert evaluate.failures(found, evaluate.Gates(max_clear_false_accusations=1)) == []
    assert evaluate.failures(found, evaluate.Gates(max_clear_false_accusations=0)) == [
        "1 clear false accusations, which no reviewer's reading supports (at most 0)"]


def test_a_baseline_gate_allows_a_few_items_worse_and_no_more(tmp_path):
    gates = evaluate.Gates(baseline=tmp_path / "baseline.json", max_drop=2)
    found = evaluate.measures(scored_with({("full", "full"): 20, ("full", "not full"): 5, ("undecided", "full"): 5}))
    assert found.disagreed == 5 and found.decided == 25
    assert evaluate.failures(found, gates, baseline=None) == [
        f"no baseline at {tmp_path / 'baseline.json'}: record an accepted run with --write-baseline"]
    data = evaluate.baseline_data([found], {"sample": "s"})
    (tmp_path / "baseline.json").write_text(json.dumps(data))
    baseline = evaluate.read_baseline(tmp_path / "baseline.json")
    assert (baseline.decided, baseline.disagreed, baseline.runs) == (25, 5, 1)
    assert evaluate.failures(found, gates, baseline=baseline) == []
    worse = evaluate.measures(scored_with({("full", "full"): 17, ("full", "not full"): 8, ("undecided", "full"): 5}))
    assert evaluate.failures(worse, gates, baseline=baseline) == [
        "8 items disagree with the gold label, the baseline had 5 (at most 2 more)"]
    fewer = evaluate.measures(scored_with({("full", "full"): 17, ("full", "not full"): 5, ("undecided", "full"): 8}))
    assert evaluate.failures(fewer, gates, baseline=baseline) == ["22 items decided, the baseline had 25 (at most 2 fewer)"]


def test_repeated_runs_gate_the_median_share_and_the_worst_accusations():
    good = evaluate.measures(scored_with({("full", "full"): 10}))
    bad = evaluate.measures(scored_with({("full", "full"): 5, ("not full", "full"): 5}, false_accusations=5))
    gates = evaluate.Gates(min_label_agreement=0.9, max_false_accusations=0, max_unstable=1)
    failed = evaluate.failures([good, good, bad], gates, unstable=2)
    assert failed == ["5 false accusations (at most 0)", "2 items changed label between the runs (at most 1)"]


def test_items_whose_label_changes_between_runs_are_unstable():
    first, second = scored_with({}), scored_with({})
    first.item_labels = {"a": "full", "b": "not full", "c": "undecided"}
    second.item_labels = {"a": "full", "b": "undecided", "c": "undecided"}
    assert evaluate.unstable_items([first, second]) == {"b": ["not full", "undecided"]}
    # moving between a label and undecided is reported; between not full and full it is gated
    third = scored_with({})
    third.item_labels = {"a": "not full", "b": "not full", "c": "undecided"}
    unstable = evaluate.unstable_items([first, second, third])
    assert evaluate.contradicting_items(unstable, [first, second, third]) == ["a"]
    assert evaluate.failures(evaluate.measures(first), evaluate.Gates(max_contradicting=0), contradicting=1) == [
        '1 items were "not full" in one run and "full" in another (at most 0)']


def fake_workflow(conn, monkeypatch, tmp_path, label):
    """CTIS, page text and the model faked; a gold dataset of one item labelled `label`. (The test's
    connection stands in for every one the command opens, so it writes as one actor.)"""
    core.act_as(conn, "test-person")
    monkeypatch.setattr(cli, "load_env_file", Calls())
    monkeypatch.setenv("TRIALWATCH_GOLD_DIR", str(tmp_path / "gold"))
    parts = NOT_FULL if label == "not full" else FULL
    datasets.write_version(tmp_path / "gold", meta("v1"), [an_item(CT, label, parts=parts)])

    @contextmanager
    def fake_adapter(name):
        yield ctis.CtisAdapter(FakeCtis(payload(CT)))

    real_transfer = ingest.transfer

    def transfer_with_page_text(*args, **kwargs):
        return real_transfer(*args, extract=PageTexts(PAGES), **kwargs)

    monkeypatch.setattr(registry, "open_adapter", fake_adapter)
    monkeypatch.setattr(ingest, "transfer", transfer_with_page_text)
    monkeypatch.setattr(anthropic, "Anthropic", lambda **settings: FakeClient(GOOD))
    gates = tmp_path / "gates.json"
    gates.write_text(json.dumps({"min_label_agreement": 0.9, "max_false_accusations": 0, "min_decided": 0.8,
                                 "max_missing": 0.1}))
    return gates


def test_gold_eval_runs_the_whole_workflow_and_passes_the_gates(app_conn, tmp_path, monkeypatch):
    gates = fake_workflow(app_conn, monkeypatch, tmp_path, "not full")
    report = tmp_path / "report.md"
    result = CliRunner().invoke(cli.app, ["gold", "eval", "--dataset", "test/v1", "--gates", str(gates),
                                          "--report", str(report), "--title", "Test eval"])
    assert result.exit_code == 0, result.output
    text = report.read_text()
    assert text.startswith("# Test eval") and "**PASSED** every gate" in text
    assert "| label agreement | 100.0% (1/1) | ≥ 90% |" in text
    # five pages go to the model whole: no search to measure
    assert "retrieval: 0 trials searched, 1 sent whole" in text and "| search hit rate (retrieval) | —; missed by the search 0 |" in text


def test_gold_eval_repeats_the_check_and_records_a_baseline(app_conn, tmp_path, monkeypatch):
    fake_workflow(app_conn, monkeypatch, tmp_path, "not full")
    baseline = tmp_path / "baseline.json"
    gates = tmp_path / "short.json"
    gates.write_text(json.dumps({"max_clear_false_accusations": 0, "max_unstable": 0, "max_contradicting": 0,
                                 "baseline": str(baseline), "max_drop": 0}))
    report = tmp_path / "report.md"
    first = CliRunner().invoke(cli.app, ["gold", "eval", "--dataset", "test/v1", "--gates", str(gates), "--repeat", "2",
                                         "--report", str(report), "--write-baseline", str(baseline)])
    # no baseline yet when it ran: it fails, and records the one to commit
    assert first.exit_code == 1 and "no baseline at" in first.output
    recorded = json.loads(baseline.read_text())
    assert (recorded["runs"], recorded["decided"], recorded["disagreed"], len(recorded["run_ids"])) == (2, 1, 0, 2)
    text = report.read_text()
    assert "## Summary · run 2 of 2" in text and "| items that changed label between runs | 0 | ≤ 0 |" in text
    second = CliRunner().invoke(cli.app, ["gold", "eval", "--dataset", "test/v1", "--gates", str(gates)])
    assert second.exit_code == 0, second.output
    assert "| items disagreeing with the gold label | 0 | ≤ baseline 0 + 0 |" in second.output


def test_gold_eval_fails_on_a_false_accusation(app_conn, tmp_path, monkeypatch):
    gates = fake_workflow(app_conn, monkeypatch, tmp_path, "full")
    result = CliRunner().invoke(cli.app, ["gold", "eval", "--dataset", "test/v1", "--gates", str(gates)])
    assert result.exit_code == 1
    assert "**FAILED**" in result.output and "1 false accusations (at most 0)" in result.output


def test_the_report_counts_a_quote_the_search_missed(conn):
    """A long trial is searched (top 1 page per question); a far page holds the answer to one question,
    which the model finds only when asked again with every page: a search miss."""
    extra = appendix_pages()
    for page in readable(["Far appendix marker for the colorectal endpoint."]):
        extra.append(page)
    trial = seed(conn, pages_with(extra))
    far_page = len(PAGES) + len(extra)
    first = good_but([answer("endpoint:12", found=False, kind="other")])
    second = [answer("cancelled", found=False, kind="other"),
              answer("endpoint:12", quote="Far appendix marker for the colorectal endpoint", page=far_page)]
    summary = ev.run(conn, FakeClient(first, second), [trial], default_settings({"whole_pages": 0, "top_k": 1}),
                     PROMPT)
    found = evaluate.retrieval_measures(conn, summary.run_id)
    assert (found.trials_searched, found.trials_whole, found.questions) == (1, 0, 4)
    assert (found.found_first, found.widened, found.missed_by_search, found.not_found) == (2, 2, 1, 1)
    assert found.search_hit_rate == 2 / 3
    assert "1 found on a page the search missed" in found.line()
    # a small trial goes to the model whole: nothing searched, nothing counted as a miss
    small = seed(conn, ct_number=CT_2)
    whole = ev.run(conn, FakeClient(GOOD), [small], default_settings(), PROMPT)
    measured = evaluate.retrieval_measures(conn, whole.run_id)
    assert (measured.trials_whole, measured.trials_searched, measured.questions) == (1, 0, 0)
    assert measured.search_hit_rate is None


def test_gold_eval_reads_a_frozen_snapshot_and_nothing_from_the_network(app_conn, tmp_path, monkeypatch):
    gates = fake_workflow(app_conn, monkeypatch, tmp_path, "not full")
    # recorded once from "CTIS" (the faked adapter fake_workflow opens)
    lock = tmp_path / "lock.json"
    archive = tmp_path / "snap.tar.gz"
    made = CliRunner().invoke(cli.app, ["gold", "snapshot", "--dataset", "test/v1", "--out", str(archive),
                                        "--release", "snapshot-test", "--lock", str(lock)])
    assert made.exit_code == 0, made.output
    locked = json.loads(lock.read_text())
    assert (locked["dataset"], locked["release"], locked["file"], locked["trials"]) == ("test/v1", "snapshot-test",
                                                                                        "snap.tar.gz", 1)

    # then replayed, with CTIS gone
    def no_network(name):
        raise AssertionError("an evaluation from a snapshot must not reach CTIS")

    monkeypatch.setattr(registry, "open_adapter", no_network)
    report = tmp_path / "report.md"
    result = CliRunner().invoke(cli.app, ["gold", "eval", "--dataset", "test/v1", "--gates", str(gates),
                                          "--snapshot", str(archive), "--lock", str(lock), "--report", str(report)])
    assert result.exit_code == 0, result.output
    assert f"evidence: frozen snapshot snap.tar.gz (sha256 {locked['sha256'][:12]}" in report.read_text()
    assert "**PASSED**" in report.read_text()

    # a snapshot the lock doesn't name is refused
    locked["sha256"] = "0" * 64
    lock.write_text(json.dumps(locked))
    refused = CliRunner().invoke(cli.app, ["gold", "eval", "--dataset", "test/v1", "--snapshot", str(archive),
                                           "--lock", str(lock)])
    assert refused.exit_code == 2 and "lock" in " ".join(refused.output.split())
