"""The action → role chart: the one place that says who may do what in the web interface (ADR-006).

Every route that changes something names one of these actions, and tests/test_auth.py fails if one
doesn't. A role is stored per user (auth.app_user.role); what it allows is only here. "public" is
anyone not signed in: they see only the home page and the sign-in page, so no action names them.
Staff (moderators and admins) "view" trials, sponsors, documents, judgements, runs, prompts and
requirement sets, and "view.internal": unpublished judgements and their traces, sources and transfers,
gold datasets and who did what. /admin/roles shows this chart as a table.
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
    Action("view", "Browse trials, sponsors, their documents, judgements, runs, prompts and requirements "
           "(anyone not signed in sees only the home page)", (MODERATOR, ADMIN)),
    Action("view.internal", "See unpublished judgements and their traces, sources and transfers, gold datasets, "
           "and staff names in history", (MODERATOR, ADMIN)),
    Action("judgement.publish", "Publish a judgement so the public sees it, or withdraw it", (MODERATOR, ADMIN)),
    Action("job.view", "See jobs (/admin/jobs), a job's page and its live progress", (MODERATOR, ADMIN)),
    Action("spend.view", "See what TrialWatch spent on paid APIs (/admin/spend)", (MODERATOR, ADMIN)),
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
    Action("job.retry", "Try a failed, cancelled or held job again now", (ADMIN,)),
    Action("schedule.edit", "Change when scheduled jobs run, switch them on or off, start one now", (ADMIN,)),
    Action("spend.cap.set", "Set the monthly spending cap for automatic checks", (ADMIN,)),
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
