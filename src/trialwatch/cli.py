from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer

from trialwatch import db
from trialwatch.ctis.client import CtisClient
from trialwatch.ctis.export import captured_on_from_name, seed_export
from trialwatch.ctis.store import fetch_trials
from trialwatch.gold.bruckner import seed_verdicts

app = typer.Typer(no_args_is_help=True, help="TrialWatch: EU clinical-trial results watchdog.")
ctis_app = typer.Typer(no_args_is_help=True, help="CTIS (EU Clinical Trials Information System).")
app.add_typer(ctis_app, name="ctis")
gold_app = typer.Typer(no_args_is_help=True, help="Gold labels: hand-checked answers we compare our checks against.")
app.add_typer(gold_app, name="gold")


@app.command("version")
def show_version() -> None:
    """Print the TrialWatch version."""
    typer.echo(f"trialwatch {version('trialwatch')}")


@ctis_app.command("fetch")
def ctis_fetch(
    ct_numbers: Annotated[list[str], typer.Argument(help="EU CT numbers, e.g. 2025-522750-39-00")],
) -> None:
    """Fetch trials from CTIS and store a snapshot for each one that changed."""
    with db.connect() as conn, CtisClient() as client:
        result = fetch_trials(
            conn, client.retrieve, ct_numbers, kind="fetch", args={"ct_numbers": ct_numbers}
        )
    typer.echo(
        f"run {result.run_id}: fetched {result.fetched}, changed {result.changed}, "
        f"failed {len(result.failed)}"
    )
    for ct_number, message in result.failed.items():
        typer.echo(f"  {ct_number}: {message}", err=True)
    if result.failed:
        raise typer.Exit(1)


@ctis_app.command("seed")
def ctis_seed(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="CSV export from the CTIS public search")],
    captured_on: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Day CTIS produced the export. Defaults to the _YYYYMMDD in the file name."),
    ] = None,
) -> None:
    """Load a CTIS portal CSV export (e.g. Bruckner et al.'s 2025-11-10 file) as snapshots."""
    try:
        day = captured_on.date() if captured_on else captured_on_from_name(path)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--captured-on") from exc
    with db.connect() as conn:
        result = seed_export(conn, path, day)
    typer.echo(
        f"run {result.run_id}: read {result.fetched}, new {result.changed}, skipped {len(result.failed)}"
    )
    for where, message in result.failed.items():
        typer.echo(f"  {where}: {message}", err=True)
    if result.failed:
        raise typer.Exit(1)


@gold_app.command("bruckner")
def gold_bruckner(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Their <date>_ctis_study_final_dataset.csv")],
) -> None:
    """Load Bruckner et al.'s final verdict per trial (the answer key for ADR-001)."""
    with db.connect() as conn:
        result = seed_verdicts(conn, path)
    typer.echo(
        f"run {result.run_id}: read {result.fetched}, new {result.changed}, skipped {len(result.failed)}"
    )
    for where, message in result.failed.items():
        typer.echo(f"  {where}: {message}", err=True)
    if result.failed:
        raise typer.Exit(1)
