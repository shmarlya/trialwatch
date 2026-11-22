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


class FakeChild:
    """A worker process that stops when asked."""

    pid = 4242

    def __init__(self):
        self.signals = []
        self.done = False

    def poll(self):
        return 0 if self.done else None

    def send_signal(self, number):
        self.signals.append(number)
        self.done = True

    def wait(self, timeout=None):
        return 0


def test_web_runs_a_worker_and_stops_it_with_the_server(monkeypatch):
    import signal

    import uvicorn

    monkeypatch.setattr(cli, "load_env_file", Calls())
    child = FakeChild()
    started = Calls()

    def start():
        started()
        return child

    served = []
    monkeypatch.setattr(cli, "start_worker", start)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: served.append(kwargs))
    result = CliRunner().invoke(cli.app, ["web"])
    assert result.exit_code == 0 and "worker started (pid 4242)" in result.stdout
    assert started.count == 1 and served and child.signals == [signal.SIGINT]

    alone = CliRunner().invoke(cli.app, ["web", "--no-worker"])
    assert alone.exit_code == 0 and started.count == 1 and "worker" not in alone.stdout


def test_a_worker_that_does_not_stop_is_killed(monkeypatch):
    import subprocess

    class Stubborn(FakeChild):
        killed = False

        def send_signal(self, number):
            self.signals.append(number)

        def wait(self, timeout=None):
            if timeout is not None and not self.killed:
                raise subprocess.TimeoutExpired("worker", timeout)
            return -9

        def kill(self):
            self.killed = True

    child = Stubborn()
    cli.stop_worker(child)
    assert child.killed
