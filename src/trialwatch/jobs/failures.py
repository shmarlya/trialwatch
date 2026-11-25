"""What a job failed on, item by item, as its page shows it: which trial, what went wrong with it, and
how to look at it or do it again.

A job keeps its last attempt's failures (jobs.job.failures): item -> why. The item names what failed
the way its step does: a transfer "KEY/document X" (or "KEY/record", "KEY/file abc…"), a check the
trial's name (its EU CT number), a tagging the trial's id, a refresh "transfer ctis, KEY/…" (its step,
then the step's item). Here they are grouped by trial, each with its trial page when we hold the trial,
and the job that would redo it: a transfer of that key again (every document downloaded again), or a
check of that trial with the same setup.
"""

from dataclasses import dataclass, field

import psycopg

from trialwatch.checks import settings as check_settings
from trialwatch.db import core, overview
from trialwatch.db.jobs import Job


@dataclass
class FailedItem:
    """One trial (or one step) a job failed on."""

    # what it is: a source key, a trial's name, or a step that failed as a whole ("search words")
    label: str
    # what went wrong with it: (where, why); where is "" when the item failed as a whole
    problems: list[tuple[str, str]] = field(default_factory=list)
    trial_id: int | None = None
    # the form that redoes it (POST /jobs), when one can: its fields
    redo: dict[str, str] | None = None


def failed_items(conn: psycopg.Connection, job: Job) -> list[FailedItem]:
    """The job's failures, grouped by trial, in the order they were recorded."""
    items: dict[str, FailedItem] = {}
    for name, why in job.failures.items():
        kind, params, item = job.kind, job.params, name
        if job.kind == "refresh":
            kind, params, item = _refresh_step(name, job.params)
        label, where = _split(kind, item)
        key = f"{kind}:{params.get('source', '')}:{label}"
        if key not in items:
            items[key] = _item(conn, kind, params, label)
        items[key].problems.append((where, str(why)))
    return list(items.values())


def redo_all(job: Job, items: list[FailedItem]) -> dict[str, str] | None:
    """The form that redoes every failed trial of a transfer at once (keys, every document again)."""
    if job.kind != "transfer":
        return None
    keys = []
    for item in items:
        if item.redo is not None and item.redo.get("step") == "transfer":
            keys.append(item.label)
    if len(keys) < 2:
        return None
    return {"step": "transfer", "source": job.params.get("source", ""), "key_text": " ".join(keys), "recheck": "true"}


def _refresh_step(name: str, params: dict) -> tuple[str, dict, str]:
    """A refresh's failure, "transfer ctis, KEY/where": the step's kind, its params, and its own item."""
    step, _, item = name.partition(", ")
    words = step.split()
    if words and words[0] == "transfer" and len(words) == 2:
        return "transfer", {"source": words[1]}, item or step
    if words and words[0] == "check":
        return "check", {"config": params.get("config", check_settings.DEFAULT)}, item or step
    if words and words[0] == "annotate":
        return "annotate", {}, item or step
    return "refresh", params, name


def _split(kind: str, item: str) -> tuple[str, str]:
    """(the trial's label, where in it): a transfer's "KEY/document X" -> ("KEY", "document X")."""
    if kind == "transfer" and "/" in item:
        label, where = item.split("/", 1)
        return label, where
    return item, ""


def _item(conn: psycopg.Connection, kind: str, params: dict, label: str) -> FailedItem:
    item = FailedItem(label)
    if kind == "transfer" and params.get("source") and label not in ("run", "search words"):
        found = overview.trials_of_keys(conn, params["source"], [label])
        if found:
            item.trial_id = found[0]
        item.redo = {"step": "transfer", "source": params["source"], "key_text": label, "recheck": "true"}
    elif kind == "check":
        trial_id = _trial_of_name(conn, label)
        if trial_id is not None:
            item.trial_id = trial_id
            item.redo = {
                "step": "evidence",
                "trial_ids": str(trial_id),
                "config": params.get("config", check_settings.DEFAULT),
            }
    elif kind == "annotate" and label.isdigit():
        item.trial_id = int(label)
    return item


def _trial_of_name(conn: psycopg.Connection, name: str) -> int | None:
    """A check names a trial by its EU CT number, or "trial <id>" without one."""
    if name.startswith("trial ") and name[6:].isdigit():
        return int(name[6:])
    found = core.trials_with_scheme(conn, "eu-ct", [name])
    return found.get(name)
