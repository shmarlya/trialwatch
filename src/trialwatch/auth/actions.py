"""The action → role chart: the one place that says who may do what in the web interface (ADR-006).

Every route that changes something names one of these actions, and tests/test_auth.py fails if one
doesn't. A role is stored per user (auth.app_user.role); what it allows is only here. "public" is
anyone not signed in: they may only browse. /admin/roles shows this chart as a table.
"""

from dataclasses import dataclass

ADMIN = "admin"
MODERATOR = "moderator"
ROLES = (ADMIN, MODERATOR)
# The column the chart page shows for anyone not signed in
PUBLIC = "public"


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    # who may do it; PUBLIC means everyone, signed in or not
    allowed: tuple[str, ...]


ACTIONS = (
    Action("view", "Browse trials, sponsors, sources, files, runs, prompts, requirements and gold",
           (PUBLIC, MODERATOR, ADMIN)),
    Action("job.view", "See a job's page and its live progress", (MODERATOR, ADMIN)),
    Action("trial.create", "Add a trial by hand with uploaded documents", (MODERATOR, ADMIN)),
    Action("trial.document.upload", "Upload a document to a trial", (MODERATOR, ADMIN)),
    Action("trial.sponsor.edit", "Add or remove a trial's sponsors", (MODERATOR, ADMIN)),
    Action("trial.transfer", "Transfer trials' evidence from a source", (MODERATOR, ADMIN)),
    Action("check.dry_run", "Start an evidence check as a dry run (no model calls)", (MODERATOR, ADMIN)),
    Action("check.run", "Start an evidence check that calls the model (spends API money)", (ADMIN,)),
    Action("source.sync", "Sync a source's list of trials", (ADMIN,)),
    Action("source.register", "Register a new source", (ADMIN,)),
    Action("source.configure", "Change a source's config or switch it on or off", (ADMIN,)),
    Action("job.stop.own", "Stop a job you started", (MODERATOR, ADMIN)),
    Action("job.stop.any", "Stop a job someone else started", (ADMIN,)),
    Action("user.manage", "Add users, change their role or password, disable them; see this chart", (ADMIN,)),
)


def action_named(name: str) -> Action:
    """The action called `name`. An unknown name is a mistake in the code, so it raises rather than
    leaving a route unguarded."""
    for action in ACTIONS:
        if action.name == name:
            return action
    raise KeyError(f"no action {name!r} in the chart (trialwatch/auth/actions.py)")


def allowed(role: str | None, name: str) -> bool:
    """Whether someone with `role` may do the action `name`; role None is someone not signed in."""
    action = action_named(name)
    if PUBLIC in action.allowed:
        return True
    if role is None:
        return False
    return role in action.allowed
