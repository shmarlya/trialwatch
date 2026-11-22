"""TrialWatch's command line: the workflow steps (ADR-005: register a source, sync its list, transfer
evidence), the evidence check (ADR-004), the gold datasets (ADR-003) and the web interface's users
(ADR-006). Every command reads .env
first, so outside compose it uses the same database and API key as the web interface."""

import json
import signal
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import psycopg
import typer

from trialwatch import db
from trialwatch.auth import accounts, actions, passwords
from trialwatch.checks import evidence
from trialwatch.checks import requirements as requirement_sets
from trialwatch.checks import settings as check_settings
from trialwatch.checks.model import load_prompt
from trialwatch.db import auth as auth_db
from trialwatch.db import core as core_db
from trialwatch.env import load_env_file
from trialwatch.gold import bruckner, datasets
from trialwatch.gold import compare as gold_compare
from trialwatch.gold import evaluate as gold_evaluate
from trialwatch.sources import ctis, registry, snapshot, stages
from trialwatch.sources.ctis_client import CtisClient
from trialwatch.sources.ctis_client import CT_NUMBER

app = typer.Typer(no_args_is_help=True, help="TrialWatch: EU clinical-trial results watchdog.")
source_app = typer.Typer(no_args_is_help=True, help="Data sources (ADR-005): the registry, syncing lists, transferring evidence.")
app.add_typer(source_app, name="source")
check_app = typer.Typer(no_args_is_help=True, help="Our checks: rules and model calls that judge a trial.")
app.add_typer(check_app, name="check")
gold_app = typer.Typer(no_args_is_help=True, help="Gold datasets: hand-checked answers we compare our checks against.")
app.add_typer(gold_app, name="gold")
user_app = typer.Typer(no_args_is_help=True, help="People who can sign in to the web interface, and their role (ADR-006).")
app.add_typer(user_app, name="user")
jobs_app = typer.Typer(no_args_is_help=True, help="Queued jobs that `trialwatch worker` runs, and their retries.")
app.add_typer(jobs_app, name="jobs")

# Calls go to Anthropic's API itself, whatever ANTHROPIC_BASE_URL a shell may set (e.g. a proxy)
ANTHROPIC_API = "https://api.anthropic.com"
# Converters that build a gold dataset version from a source's own files (ADR-003)
GOLD_CONVERTERS = {"bruckner": bruckner.build}


@app.callback()
def settings() -> None:
    # Outside compose, DATABASE_URL and ANTHROPIC_API_KEY come from .env (the environment's own win)
    load_env_file()


@app.command("version")
def show_version() -> None:
    typer.echo(version("trialwatch"))


# How long a worker started by `trialwatch web` gets to stop after Ctrl-C before it is killed
WORKER_STOP_SECONDS = 10


def start_worker() -> subprocess.Popen:
    """`trialwatch worker` as a child process, writing to this terminal."""
    return subprocess.Popen([sys.executable, "-m", "trialwatch", "worker"])


def stop_worker(child: subprocess.Popen) -> None:
    """Ask the worker to stop as Ctrl-C does (a job it was running is queued again once its heartbeat
    lapses), and kill it if it doesn't."""
    if child.poll() is not None:
        return
    child.send_signal(signal.SIGINT)
    try:
        child.wait(timeout=WORKER_STOP_SECONDS)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


@app.command("web")
def web(
    host: Annotated[str, typer.Option(help="Address to listen on; the default is this machine only")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8010,
    reload: Annotated[bool, typer.Option("--reload", help="Restart when the code changes (development)")] = False,
    with_worker: Annotated[
        bool, typer.Option("--worker/--no-worker", help="Also run a worker for the jobs it queues (stopped with it)")
    ] = True,
) -> None:
    """Web interface: browse trials, documents, judgements and gold datasets, and start the workflow steps.
    It also runs a worker, so the jobs it queues run; --no-worker when one runs elsewhere (compose's
    `worker` service). Several workers are safe: each takes a different job."""
    import uvicorn

    child = None
    if with_worker:
        child = start_worker()
        typer.echo(f"worker started (pid {child.pid}); it stops with the web server")
    try:
        uvicorn.run("trialwatch.web.app:app", host=host, port=port, reload=reload)
    finally:
        if child is not None:
            stop_worker(child)


@app.command("worker")
def worker(
    once: Annotated[bool, typer.Option("--once", help="Run the queued jobs that are due, then stop")] = False,
) -> None:
    """Run queued jobs one at a time (those people start on the web interface, and the weekly refresh),
    trying failed ones again later. `trialwatch web` starts one by itself; run this where the web
    server runs with --no-worker (compose's `worker` service)."""
    import logging

    from trialwatch.jobs import worker as job_worker

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    try:
        ran = job_worker.run(once=once)
    except KeyboardInterrupt:
        typer.echo("stopped: a job it was running is taken over by the next worker after its heartbeat lapses")
        return
    typer.echo(f"{ran} jobs run")


@jobs_app.command("list")
def jobs_list(
    status: Annotated[str | None, typer.Option(help="Only jobs in this status, e.g. failed, held, queued")] = None,
    limit: Annotated[int, typer.Option(help="How many, newest first")] = 30,
) -> None:
    """The newest jobs, with their status, attempts and last error."""
    from trialwatch.db import jobs as jobs_db

    with db.connect_system() as conn:
        if status:
            found = jobs_db.jobs_with_status(conn, (status,), limit)
        else:
            found = jobs_db.recent_jobs(conn, limit)
    for job in found:
        error = ""
        if job.last_error:
            error = f" · {job.last_error[:120]}"
        typer.echo(f"{job.id:>6} {job.kind:<8} {job.status:<9} {job.attempts}/{job.max_attempts} {job.actor}{error}")


@jobs_app.command("retry")
def jobs_retry(job_id: Annotated[int, typer.Argument(help="The job, as `jobs list` shows it")]) -> None:
    """Queue a failed, cancelled or held job again now, with one more attempt."""
    from trialwatch.db import jobs as jobs_db

    with db.connect() as conn:
        if not jobs_db.retry_now(conn, job_id):
            raise typer.BadParameter(f"job {job_id} is not failed, cancelled or held")
    typer.echo(f"job {job_id} queued")


@jobs_app.command("refresh")
def jobs_refresh() -> None:
    """Queue the weekly refresh now (sync every source, transfer what changed, check what changed), as
    TrialWatch itself; `trialwatch worker` runs it."""
    from trialwatch.db import jobs as jobs_db
    from trialwatch.jobs import MAX_ATTEMPTS, PRIORITY_AUTOMATIC

    with db.connect_system() as conn:
        job_id = jobs_db.enqueue(conn, "refresh", {}, max_attempts=MAX_ATTEMPTS["refresh"], automatic=True,
                                 priority=PRIORITY_AUTOMATIC)
    typer.echo(f"refresh queued as job {job_id}")


def _echo_failures(failed: dict[str, str]) -> None:
    for where, message in failed.items():
        typer.echo(f"  {where}: {message}", err=True)


def _registered(conn, code: str) -> core_db.RegisteredSource:
    found = core_db.registered_source(conn, code)
    if found is None:
        raise typer.BadParameter(f"no source {code!r} is registered (see `trialwatch source list`)")
    return found


def _read_config(path: Path | None, adapter: registry.Adapter) -> dict:
    """A config from a JSON file, or the adapter's default; checked by the adapter."""
    if path is None:
        config = adapter.default_config()
    else:
        try:
            config = json.loads(path.read_text())
        except ValueError as exc:
            raise typer.BadParameter(f"{path} is not JSON: {exc}", param_hint="--config") from exc
    if not isinstance(config, dict):
        raise typer.BadParameter("a config is a JSON object", param_hint="--config")
    problems = adapter.config_problems(config)
    if problems:
        raise typer.BadParameter("; ".join(problems), param_hint="--config")
    return config


def _lines_of(path: Path) -> list[str]:
    """The non-empty lines of a file, without # comments."""
    lines = []
    for line in path.read_text().splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            lines.append(text)
    return lines


# --- sources (ADR-005) ---------------------------------------------------------------------------

@source_app.command("list")
def source_list() -> None:
    """The registered sources: adapter, on or off, and how many of their trials are listed and transferred."""
    from trialwatch.db import overview

    with db.connect() as conn:
        rows = overview.source_rows(conn)
    for row in rows:
        state = "on"
        if not row.enabled:
            state = "off"
        typer.echo(f"{row.code} ({row.adapter}, {state}): {row.name}")
        typer.echo(f"  listed {row.listed:,} · no longer listed {row.delisted:,} · not transferred {row.not_transferred:,} · "
                   f"complete {row.complete:,} · stale {row.stale:,} · failed {row.failed:,}")


@source_app.command("register")
def source_register(
    adapter: Annotated[str, typer.Argument(help="The adapter that reads it, e.g. ctis")],
    code: Annotated[str | None, typer.Option(help="Short name in URLs and commands (default: the adapter's)")] = None,
    name: Annotated[str | None, typer.Option(help="Readable name (default: the adapter's)")] = None,
    url: Annotated[str | None, typer.Option(help="Where the source is")] = None,
    config: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="Its config as a JSON file (default: the adapter's)")
    ] = None,
) -> None:
    """Register a data source: the adapter that reads it and its config."""
    try:
        adapter_type = registry.adapter_class(adapter)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    chosen_config = _read_config(config, adapter_type())
    chosen_code = code
    if not chosen_code:
        chosen_code = adapter_type.name
    chosen_name = name
    if not chosen_name:
        chosen_name = adapter_type.label
    chosen_url = url
    if not chosen_url:
        chosen_url = adapter_type.url
    with db.connect() as conn:
        source_id = core_db.register_source(conn, code=chosen_code, kind=adapter_type.kind, name=chosen_name,
                                            url=chosen_url, adapter=adapter, config=chosen_config)
    typer.echo(f"registered source {chosen_code} (id {source_id})")


@source_app.command("config")
def source_config(
    code: Annotated[str, typer.Argument(help="The source, e.g. ctis")],
    config: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="Replace its config with this JSON file")
    ] = None,
    enable: Annotated[bool | None, typer.Option("--enable/--disable", help="Switch the source on or off")] = None,
) -> None:
    """Show a source's config, or replace it (one current config; each run records the one it used)."""
    with db.connect() as conn:
        source = _registered(conn, code)
        if config is not None or enable is not None:
            chosen = source.config
            if config is not None:
                chosen = _read_config(config, registry.adapter_class(source.adapter)())
            enabled = source.enabled
            if enable is not None:
                enabled = enable
            core_db.set_source_config(conn, code, chosen, enabled)
            source = _registered(conn, code)
    typer.echo(json.dumps({"enabled": source.enabled, "config": source.config}, indent=2))


@source_app.command("sync")
def source_sync(code: Annotated[str, typer.Argument(help="The source, e.g. ctis")]) -> None:
    """Sync a source's list: every trial it lists (as its config filters them) becomes a trial here,
    with its key, title and identifiers. Records and documents come with `source transfer`."""
    with db.connect_system() as conn:
        source = _registered(conn, code)
        with registry.open_adapter(source.adapter) as adapter:
            try:
                result = stages.sync_list(conn, source, adapter, progress=typer.echo)
            except ValueError as exc:
                raise typer.BadParameter(str(exc)) from exc
    typer.echo(result.summary())
    _echo_failures(result.failed)
    if result.failed:
        raise typer.Exit(1)


@source_app.command("transfer")
def source_transfer(
    code: Annotated[str, typer.Argument(help="The source, e.g. ctis")],
    keys: Annotated[list[str] | None, typer.Argument(help="The source's keys of the trials, e.g. EU CT numbers")] = None,
    from_file: Annotated[
        Path | None, typer.Option("--file", exists=True, dir_okay=False, help="A file with one key per line")
    ] = None,
    which: Annotated[str | None, typer.Option(help="Instead of keys: pending | failed | stale | listed")] = None,
    limit: Annotated[int | None, typer.Option(min=1, help="Transfer at most this many trials")] = None,
    files_dir: Annotated[
        Path | None, typer.Option(help="Also write each file here, to read by hand (never overwritten or removed)")
    ] = None,
) -> None:
    """Transfer trials' evidence from a source: the full record and the documents its config names,
    with their page text read. Only trials whose evidence is complete and current can be checked."""
    given = []
    if keys:
        for key in keys:
            given.append(key)
    if from_file is not None:
        for key in _lines_of(from_file):
            given.append(key)
    if given and which:
        raise typer.BadParameter("give keys or --which, not both")
    if not given and not which:
        raise typer.BadParameter("give keys, --file or --which")
    with db.connect_system() as conn:
        source = _registered(conn, code)
        why = "chosen"
        if which:
            try:
                given = core_db.keys_to_transfer(conn, code, which)
            except ValueError as exc:
                raise typer.BadParameter(str(exc), param_hint="--which") from exc
            why = which
        if limit is not None:
            given = given[:limit]
        typer.echo(f"{len(given)} trials to transfer from {code} ({why})")
        with registry.open_adapter(source.adapter) as adapter:
            try:
                result = stages.transfer(conn, source, adapter, given, why=why, files_dir=files_dir,
                                         progress=typer.echo)
            except ValueError as exc:
                raise typer.BadParameter(str(exc)) from exc
    typer.echo(result.summary())
    _echo_failures(result.failed)
    if result.failed:
        raise typer.Exit(1)


@source_app.command("locations")
def source_locations(code: Annotated[str, typer.Argument(help="The source, e.g. ctis")]) -> None:
    """Read again where each trial runs (sites, cities, countries) from the records already stored from a
    source, without fetching anything: for records transferred before locations were read, or after
    the adapter reads them differently. A transfer reads them anyway."""
    with db.connect_system() as conn:
        source = _registered(conn, code)
        adapter = registry.adapter_class(source.adapter)()
        trials = 0
        places = 0
        for trial_id, payload in core_db.current_records(conn, code):
            places += core_db.set_trial_locations(conn, trial_id, code, adapter.locations(payload))
            trials += 1
    typer.echo(f"{code}: read the locations of {trials:,} trial{"s" if trials != 1 else ""} ({places:,} sites and countries)")


# --- the evidence check (ADR-004) ----------------------------------------------------------------

def _ct_numbers_given(ct_numbers: list[str] | None, from_file: Path | None) -> list[str]:
    """The CT numbers from the arguments and the file, each once, checked for their format."""
    given = []
    if ct_numbers:
        for ct_number in ct_numbers:
            given.append(ct_number)
    if from_file is not None:
        for ct_number in _lines_of(from_file):
            given.append(ct_number)
    wanted = []
    for ct_number in given:
        if not CT_NUMBER.fullmatch(ct_number):
            raise typer.BadParameter(f"not an EU CT number: {ct_number!r}")
        if ct_number not in wanted:
            wanted.append(ct_number)
    if not wanted:
        raise typer.BadParameter("give CT numbers, --file or --all")
    return wanted


def _trials_to_judge(conn, ct_numbers: list[str] | None, from_file: Path | None, all_trials: bool,
                     requirement_set: requirement_sets.RequirementSet) -> list[int]:
    """core.trial ids: every trial a check of this requirement set may read now (--all), or the trials
    with these EU CT numbers."""
    if all_trials:
        return stages.ready_trials(conn, requirement_set.document_kinds)
    wanted = _ct_numbers_given(ct_numbers, from_file)
    found = core_db.trials_with_scheme(conn, "eu-ct", wanted)
    missing = []
    for ct_number in wanted:
        if ct_number not in found:
            missing.append(ct_number)
    if missing:
        raise typer.BadParameter(f"not in the database (run `trialwatch source transfer ctis` first): {', '.join(missing)}")
    trial_ids = []
    for ct_number in wanted:
        trial_ids.append(found[ct_number])
    return trial_ids


@check_app.command("evidence")
def check_evidence(
    ct_numbers: Annotated[list[str] | None, typer.Argument(help="EU CT numbers, e.g. 2025-522750-39-00")] = None,
    from_file: Annotated[
        Path | None, typer.Option("--file", exists=True, dir_okay=False, help="A file with one EU CT number per line")
    ] = None,
    all_trials: Annotated[
        bool, typer.Option("--all", help="Every trial whose evidence is complete and current, with page text to read")
    ] = False,
    limit: Annotated[int | None, typer.Option(min=1, help="Judge at most this many trials")] = None,
    config: Annotated[
        str, typer.Option(help="The setup to run with: a file in checks/configs/, without .json (see `check configs`)")
    ] = check_settings.DEFAULT,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show questions and pages per trial; call nothing")] = False,
) -> None:
    """Judge trials with a named setup (checks/configs/): retrieve the pages of every document of its
    requirement set's kinds linked to the trial, have the model quote evidence, verify every quote
    against the page text, and decide with the set's rules. Each run adds rows, with a step-by-step
    trace per trial, and records the setup it used."""
    try:
        settings = check_settings.load(config)
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    requirement_set = requirement_sets.load(settings.requirements)
    system_prompt = load_prompt(settings.prompt)
    with db.connect_system() as conn:
        trial_ids = _trials_to_judge(conn, ct_numbers, from_file, all_trials, requirement_set)
        if limit is not None:
            trial_ids = trial_ids[:limit]
        try:
            evidence.refuse_unready(conn, trial_ids, requirement_set)
        except evidence.NotReady as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(f"{settings.summary()} (prompt sha256 {system_prompt.sha256.hex()[:12]}) · {len(trial_ids)} trials")
        if dry_run:
            names = core_db.trial_names(conn, trial_ids)
            for trial_id in trial_ids:
                questions, pages = evidence.preview_trial(conn, trial_id, requirement_set)
                typer.echo(f"{names[trial_id]} (trial {trial_id}): {len(pages)} pages, {len(questions)} questions")
                for question in questions:
                    typer.echo(f"  {question.key}")
            return
        import anthropic

        client = anthropic.Anthropic(base_url=ANTHROPIC_API, max_retries=4)
        summary = evidence.run(conn, client, trial_ids, settings, system_prompt, progress=typer.echo,
                               requirement_set=requirement_set)
    typer.echo(summary.summary())
    _echo_failures(summary.failed)
    if summary.failed:
        raise typer.Exit(1)


@check_app.command("configs")
def check_configs() -> None:
    """The setups a check can run with (checks/configs/*.json), each with its settings."""
    for name in check_settings.names():
        try:
            settings = check_settings.load(name)
        except ValueError as exc:
            typer.echo(f"{name}: can't be used: {exc}", err=True)
            continue
        typer.echo(settings.summary())
        typer.echo(f"  {settings.description}")
        typer.echo(f"  whole up to {settings.whole_pages} pages, {settings.top_k} pages per question, "
                   f"max_tokens {settings.max_tokens}, fallbacks {settings.fallbacks}")


# --- gold datasets (ADR-003) ---------------------------------------------------------------------

@gold_app.command("build")
def gold_build(
    converter: Annotated[str, typer.Argument(help="Which source's files to convert: bruckner")],
    data_dir: Annotated[
        Path, typer.Option(exists=True, file_okay=False, help="Folder with the source's files")
    ] = Path("data/bruckner"),
) -> None:
    """Build a gold dataset version under data/gold/ and lock it in data/gold/versions.lock."""
    if converter not in GOLD_CONVERTERS:
        raise typer.BadParameter(f"choose one of: {', '.join(sorted(GOLD_CONVERTERS))}", param_hint="CONVERTER")
    build_version = GOLD_CONVERTERS[converter]
    meta, items = build_version(data_dir)
    try:
        ref, written = datasets.write_version(datasets.gold_dir(), meta, items)
    except datasets.VersionIsFixed as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if written:
        typer.echo(f"{ref}: {len(items)} items written and locked")
    else:
        typer.echo(f"{ref}: unchanged ({len(items)} items)")


@gold_app.command("datasets")
def gold_datasets() -> None:
    """List the gold dataset versions and check each against data/gold/versions.lock."""
    for ref in datasets.dataset_refs():
        dataset = datasets.load(ref)
        labelled = 0
        for item in dataset.items:
            if item.expected_label is not None:
                labelled += 1
        typer.echo(f"{ref}: {len(dataset.items)} items, {labelled} with a label · requirements {dataset.requirements}")
    problems = datasets.lock_problems()
    for problem in problems:
        typer.echo(f"  {problem}", err=True)
    if problems:
        raise typer.Exit(1)


@gold_app.command("compare")
def gold_compare_run(
    run: Annotated[int, typer.Option(help="The check run whose judgements are scored")],
    dataset: Annotated[
        str | None, typer.Option(help="A dataset version, e.g. bruckner/2026-03-16 (default: the newest whose labels "
                                      "answer the run's requirement set)")
    ] = None,
) -> None:
    """Score one check run against a gold dataset, part by part and per trial."""
    with db.connect() as conn:
        ref = dataset
        if ref is None:
            ref = gold_compare.dataset_for_run(conn, run)
        if ref is None:
            raise typer.BadParameter(f"no gold dataset answers run {run}'s requirement set", param_hint="--dataset")
        try:
            scored = gold_compare.compare_run(conn, run, datasets.load(ref))
        except (FileNotFoundError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from exc
    for line in gold_compare.report_lines(scored):
        typer.echo(line)
    for line in gold_compare.disagreement_lines(scored):
        typer.echo(line, err=True)


@gold_app.command("sample")
def gold_sample(
    dataset: Annotated[str, typer.Option(help="A dataset version, e.g. bruckner/2026-03-16")],
    count: Annotated[int, typer.Option(min=1, help="How many items")] = 50,
    min_per_label: Annotated[int, typer.Option(min=0, help="At least this many items of each label")] = 15,
) -> None:
    """Print the hardest labelled items' keys, one per line, for a short gold evaluation (gold/evaluate.py
    hardness: reviewers disagreed, discussed, terminated, re-checked, unsettled or cancelled, commented)."""
    try:
        chosen = gold_evaluate.hardest(datasets.load(dataset), count, min_per_label)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc), param_hint="--dataset") from exc
    typer.echo(f"# The {len(chosen)} hardest labelled items of {dataset} (trialwatch gold sample --dataset {dataset} "
               f"--count {count} --min-per-label {min_per_label}): hardness, then key")
    for item in chosen:
        typer.echo(item.key)


def _labelled_ct_numbers(chosen: datasets.Dataset) -> list[str]:
    ct_numbers = []
    for item in chosen.items:
        ct_number = gold_evaluate.ct_number_of(item)
        if item.expected_label is not None and ct_number is not None and ct_number not in ct_numbers:
            ct_numbers.append(ct_number)
    return ct_numbers


@gold_app.command("snapshot")
def gold_snapshot(
    dataset: Annotated[str, typer.Option(help="A dataset version, e.g. bruckner/2026-03-16")],
    out: Annotated[Path, typer.Option(help="Write the snapshot here (.tar.gz)")],
    release: Annotated[str, typer.Option(help="The GitHub release the snapshot will be uploaded to")],
    lock: Annotated[Path, typer.Option(help="Write the lock that names it here")] = snapshot.LOCK_FILE,
    files_from: Annotated[
        Path | None, typer.Option(exists=True, file_okay=False,
                                  help="Take each document's file from this earlier --files-dir copy when it holds "
                                       "that document and version (records still come from CTIS)")
    ] = None,
    copied_on: Annotated[str, typer.Option(help="When the --files-from copy was made, for the index")] = "an earlier day",
) -> None:
    """Freeze what CTIS answers now for every labelled trial of a gold dataset: transfer them from CTIS
    (into this database) while recording each record and document file, then pack the recording
    (sources/snapshot.py) and write the lock the gold pipelines read it by."""
    try:
        chosen = datasets.load(dataset)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc), param_hint="--dataset") from exc
    ct_numbers = _labelled_ct_numbers(chosen)
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        with CtisClient() as live:
            client = live
            description = f"CTIS's answers for the labelled trials of {chosen.ref}"
            if files_from is not None:
                client = snapshot.LocalCopyFirst(live, files_from, copied_on)
                description += f"; document files from a copy of {copied_on} where it holds the same version"
            recording = snapshot.RecordingClient(client, Path(folder), description, dataset=chosen.ref)
            with db.connect_system() as conn:
                result = stages.transfer(conn, _registered(conn, "ctis"), ctis.CtisAdapter(recording), ct_numbers,
                                         why="gold snapshot", progress=typer.echo)
            index = recording.finish()
        packed = snapshot.pack(Path(folder), out)
    typer.echo(result.summary())
    _echo_failures(result.failed)
    origins: dict[str, int] = {}
    for trial in index["trials"].values():
        for document in trial["documents"].values():
            origins[document["from"]] = origins.get(document["from"], 0) + 1
    for origin, count in sorted(origins.items()):
        typer.echo(f"document files from {origin}: {count}")
    lock.write_text(json.dumps(snapshot.lock_data(chosen.ref, release, packed, index["made_at"]), indent=1) + "\n")
    typer.echo(f"{out}: {packed.trials} trials, {packed.files} files, sha256 {packed.sha256}; lock written to {lock}")


@gold_app.command("eval")
def gold_eval(
    dataset: Annotated[str, typer.Option(help="A dataset version, e.g. bruckner/2026-03-16")],
    sample: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="Only these items: a file with one key per line")
    ] = None,
    config: Annotated[str, typer.Option(help="The check's setup: a file in checks/configs/, without .json")] = check_settings.DEFAULT,
    gates: Annotated[Path, typer.Option(exists=True, dir_okay=False, help="The gates to pass")] = gold_evaluate.GATES_FILE,
    report: Annotated[Path | None, typer.Option(help="Also write the report here, as Markdown")] = None,
    title: Annotated[str, typer.Option(help="The report's heading")] = "Gold evaluation",
    frozen: Annotated[
        Path | None, typer.Option("--snapshot", exists=True, dir_okay=False,
                                  help="Read CTIS from this frozen snapshot (.tar.gz) instead of the network")
    ] = None,
    lock: Annotated[Path, typer.Option(help="The lock the snapshot must match")] = snapshot.LOCK_FILE,
) -> None:
    """Our whole workflow on a gold dataset's labelled items: transfer their evidence from CTIS, run the
    evidence check (it calls the model), score the run against the labels, and fail (exit 1) when a
    gate in evals/gates.json is missed. The two gold pipelines in .github/workflows/ run this."""
    try:
        chosen = datasets.load(dataset)
        settings = check_settings.load(config)
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if sample is not None:
        try:
            chosen = gold_evaluate.only_items(chosen, _lines_of(sample))
        except KeyError as exc:
            raise typer.BadParameter(str(exc), param_hint="--sample") from exc
    requirement_set = requirement_sets.load(settings.requirements)
    if requirement_set.name != chosen.requirements:
        raise typer.BadParameter(f"{config} checks {requirement_set.name}, but {chosen.ref} answers {chosen.requirements}")
    ct_numbers = _labelled_ct_numbers(chosen)
    typer.echo(f"{chosen.ref}: {len(ct_numbers)} labelled trials · {settings.summary()}")
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        evidence_from = "live from CTIS"
        if frozen is not None:
            locked = snapshot.read_lock(lock)
            if locked.dataset != chosen.ref:
                raise typer.BadParameter(f"the lock is for {locked.dataset}, not {chosen.ref}", param_hint="--lock")
            try:
                snapshot.unpack(frozen, Path(folder), locked.sha256)
            except ValueError as exc:
                raise typer.BadParameter(str(exc), param_hint="--snapshot") from exc
            evidence_from = f"frozen snapshot {frozen.name} (sha256 {locked.sha256[:12]}…, made {locked.made_at})"
        typer.echo(f"evidence: {evidence_from}")
        with db.connect_system() as conn:
            source = _registered(conn, "ctis")
            if frozen is not None:
                # the CTIS adapter as always, answered by the snapshot: everything after the network runs as usual
                transferred = stages.transfer(conn, source, ctis.CtisAdapter(snapshot.ReplayClient(Path(folder))),
                                              ct_numbers, why="gold eval", progress=typer.echo)
            else:
                with registry.open_adapter(source.adapter) as adapter:
                    transferred = stages.transfer(conn, source, adapter, ct_numbers, why="gold eval",
                                                  progress=typer.echo)
    typer.echo(transferred.summary())
    _echo_failures(transferred.failed)
    system_prompt = load_prompt(settings.prompt)
    with db.connect_system() as conn:
        found = core_db.trials_with_scheme(conn, "eu-ct", ct_numbers)
        trial_ids = []
        for ct_number in ct_numbers:
            if ct_number in found:
                trial_ids.append(found[ct_number])
        # a trial whose evidence did not come in completely is not checked: it counts as missing
        blocked = stages.not_ready(conn, trial_ids, requirement_set.document_kinds)
        ready = []
        for trial_id in trial_ids:
            if trial_id not in blocked:
                ready.append(trial_id)
        typer.echo(f"{len(ready)} of {len(ct_numbers)} trials ready for the check")
        import anthropic

        client = anthropic.Anthropic(base_url=ANTHROPIC_API, max_retries=4)
        summary = evidence.run(conn, client, ready, settings, system_prompt, progress=typer.echo,
                               requirement_set=requirement_set)
        scored = gold_compare.compare_run(conn, summary.run_id, chosen)
        retrieval = gold_evaluate.retrieval_measures(conn, summary.run_id)
    loaded_gates = gold_evaluate.load_gates(gates)
    text = gold_evaluate.report_markdown(title, scored, loaded_gates, f"evidence: {evidence_from}\n{summary.summary()}",
                                         retrieval)
    typer.echo(text)
    if report is not None:
        report.write_text(text)
    if gold_evaluate.failures(gold_evaluate.measures(scored), loaded_gates):
        raise typer.Exit(1)


# --- users of the web interface (ADR-006) ---------------------------------------------------------
# The command line itself is not limited by roles: whoever runs it has the database's password.

def _role(role: str) -> str:
    if role not in actions.ROLES:
        raise typer.BadParameter(f"the role is one of {', '.join(actions.ROLES)}")
    return role


@user_app.command("add")
def user_add(
    email: Annotated[str, typer.Argument(help="Their email address: what they sign in with")],
    role: Annotated[str, typer.Option(help="admin or moderator")],
    password: Annotated[str, typer.Option(prompt=True, hide_input=True, confirmation_prompt=True,
                                          help="Asked for when not given")],
) -> None:
    """Add a person who can sign in to the web interface (the first admin is added this way)."""
    chosen_role = _role(role)
    chosen = accounts.normalised_email(email)
    problem = accounts.email_problem(chosen)
    if problem is None:
        problem = passwords.password_problem(password)
    if problem is not None:
        raise typer.BadParameter(problem)
    try:
        with db.connect() as conn:
            auth_db.add_user(conn, chosen, passwords.hashed(password), chosen_role)
    except psycopg.Error as exc:
        typer.echo(f"not added: {str(exc).splitlines()[0]}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"added {chosen} as {chosen_role}")


@user_app.command("list")
def user_list() -> None:
    """Every user with their role, and whether they are disabled."""
    with db.connect() as conn:
        rows = auth_db.users(conn)
    for row in rows:
        state = ""
        if row.disabled_at is not None:
            state = f"  disabled {row.disabled_at:%Y-%m-%d}"
        typer.echo(f"{row.email:<40} {row.role:<10}{state}")


@user_app.command("role")
def user_role(email: str, role: Annotated[str, typer.Argument(help="admin or moderator")]) -> None:
    """Change a user's role (they are signed out everywhere)."""
    chosen_role = _role(role)
    email = accounts.normalised_email(email)
    with db.connect() as conn:
        if not auth_db.set_role(conn, email, chosen_role):
            raise typer.BadParameter(f"no user {email!r}")
        auth_db.drop_sessions_of(conn, email)
    typer.echo(f"{email} is now {chosen_role}")


@user_app.command("password")
def user_password(
    email: str,
    password: Annotated[str, typer.Option(prompt=True, hide_input=True, confirmation_prompt=True,
                                          help="Asked for when not given")],
) -> None:
    """Set a user's password (they are signed out everywhere)."""
    email = accounts.normalised_email(email)
    problem = passwords.password_problem(password)
    if problem is not None:
        raise typer.BadParameter(problem)
    with db.connect() as conn:
        if not auth_db.set_password_hash(conn, email, passwords.hashed(password)):
            raise typer.BadParameter(f"no user {email!r}")
        auth_db.drop_sessions_of(conn, email)
    typer.echo(f"password of {email} set")


@user_app.command("disable")
def user_disable(email: str, enable: Annotated[bool, typer.Option("--enable", help="Enable them again")] = False) -> None:
    """Stop a user signing in (and sign them out), or with --enable let them in again."""
    email = accounts.normalised_email(email)
    with db.connect() as conn:
        if not auth_db.set_disabled(conn, email, not enable):
            raise typer.BadParameter(f"no user {email!r}")
        if not enable:
            auth_db.drop_sessions_of(conn, email)
    if enable:
        typer.echo(f"{email} enabled")
    else:
        typer.echo(f"{email} disabled")
