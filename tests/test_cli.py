"""The command line (trialwatch.cli): every command reads .env first, and `gold compare` scores a check
run against a gold dataset."""

from importlib.metadata import version

from test_gold_compare import ACCUSED, FAILED, ITEMS, a_judged_run, meta
from typer.testing import CliRunner

from trialwatch import cli
from trialwatch.gold import datasets


class Calls:
    """Stands in for a function, counting its calls (here: reading .env, which tests never do: it
    holds a real API key)."""

    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1


def test_every_command_reads_the_env_file_first(monkeypatch):
    loaded = Calls()
    monkeypatch.setattr(cli, "load_env_file", loaded)
    result = CliRunner().invoke(cli.app, ["version"])
    assert (result.exit_code, result.stdout.strip()) == (0, version("trialwatch"))
    assert loaded.count == 1


def test_gold_compare_scores_a_run_against_the_newest_dataset_of_its_requirement_set(app_conn, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_env_file", Calls())
    monkeypatch.setenv("TRIALWATCH_GOLD_DIR", str(tmp_path))
    run_id = a_judged_run(app_conn)
    dataset_meta = meta("v1")
    dataset_meta["evidence"]["as_of"] = "2025-11-10"
    datasets.write_version(tmp_path, dataset_meta, ITEMS)
    result = CliRunner().invoke(cli.app, ["gold", "compare", "--run", str(run_id)])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].startswith(f"run {run_id} against test/v1 · model claude-test")
    assert "false accusations (ours not full, theirs full): 1; missed (ours full, theirs not full): 1" in lines
    # each disagreement and error, for a person to look at
    assert f"  {ACCUSED} ours not full (" in result.stderr
    assert f"  {FAILED}: InternalServerError: overloaded" in result.stderr


def test_gold_compare_says_what_it_cannot_compare(app_conn, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_env_file", Calls())
    monkeypatch.setenv("TRIALWATCH_GOLD_DIR", str(tmp_path))
    run_id = a_judged_run(app_conn)
    no_dataset = CliRunner().invoke(cli.app, ["gold", "compare", "--run", str(run_id)])
    assert no_dataset.exit_code == 2 and f"no gold dataset answers run {run_id}'s requirement" in no_dataset.stderr
    datasets.write_version(tmp_path, meta("v1"), ITEMS)
    unknown_version = CliRunner().invoke(cli.app, ["gold", "compare", "--run", str(run_id), "--dataset", "test/v9"])
    assert unknown_version.exit_code == 2 and "no gold dataset 'test/v9'" in unknown_version.stderr
    unknown_run = CliRunner().invoke(cli.app, ["gold", "compare", "--run", "987654321", "--dataset", "test/v1"])
    assert unknown_run.exit_code == 2 and "no check run 987654321" in unknown_run.stderr
