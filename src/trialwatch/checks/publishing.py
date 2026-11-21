"""Which judgements a run publishes by itself, and which wait for a person (ADR-007).

A judgement's confidence is measured, not asked of the model: judgements of the same kind are compared
with the gold datasets' labels, and the share that agreed is the confidence of every judgement of that
kind. A kind is the exact check that made it (model, prompt text, requirement set) with its label and
how its evidence came in: every quote verified or not, a question asked again with every page or not,
a question left unanswered or not. A different prompt text or model starts a new kind with no
measurement, so a changed check waits for people until gold shows it again.

At AUTO_PUBLISH_AT or above, with at least MIN_GOLD_ITEMS compared, a run publishes the judgement as it
finishes, any label; the publication keeps the confidence and its basis. Below that, or for an
"undecided" judgement, a moderator or admin decides on the trial page (/review lists them).

Gold items are counted once per trial and check (the newest judgement of that trial by that model and
prompt), and set aside when our judgement read a document posted on or after the day the dataset's
reviewers read theirs (as gold/compare.py does).
"""

from dataclasses import dataclass

import psycopg

from trialwatch.db import checks as checks_db
from trialwatch.db.checks import JudgementKind
from trialwatch.gold import compare, datasets

# A run publishes a judgement by itself from this share of agreeing gold items up
AUTO_PUBLISH_AT = 0.90
# ... and only once this many gold items of its kind were compared: fewer say too little
MIN_GOLD_ITEMS = 20


@dataclass(frozen=True)
class Kind:
    model: str
    prompt_sha256: str
    requirements: str
    label: str | None
    quotes_verified: bool
    widened: bool
    unanswered: bool

    def describe(self) -> str:
        quotes = "every quote verified" if self.quotes_verified else "a quote not found on its page"
        widened = "asked again with every page" if self.widened else "not asked again"
        answered = "a question unanswered" if self.unanswered else "every question answered"
        return (f"{self.label}; {quotes}; {widened}; {answered}; {self.model}, prompt {self.prompt_sha256[:12]}, "
                f"{self.requirements}")


def kind_of(judgement: JudgementKind) -> Kind:
    return Kind(judgement.model, judgement.prompt_sha256.hex(), judgement.requirements, judgement.label,
                judgement.quotes_verified, judgement.widened, judgement.unanswered)


@dataclass
class Tally:
    agreed: int = 0
    compared: int = 0

    @property
    def share(self) -> float | None:
        if self.compared == 0:
            return None
        return self.agreed / self.compared


@dataclass(frozen=True)
class Calibration:
    """Kind -> how its judgements compared with gold labels, and the dataset versions it came from."""

    tallies: dict[Kind, Tally]
    datasets: list[str]


def _newest_versions(requirements: str) -> list[datasets.Dataset]:
    """The newest version of each gold dataset whose labels answer this requirement set."""
    newest = {}
    for ref in datasets.dataset_refs():
        dataset = datasets.load(ref)
        if dataset.requirements == requirements:
            # refs come in name order, so a later version of a name replaces an earlier one
            newest[dataset.name] = dataset
    return list(newest.values())


def calibrate(conn: psycopg.Connection, requirements: str) -> Calibration:
    """How often each kind of judgement under the requirement set agreed with gold labels."""
    tallies: dict[Kind, Tally] = {}
    used = []
    for dataset in _newest_versions(requirements):
        used.append(dataset.ref)
        read_day = compare.evidence_day(dataset)
        trial_by_item = {}
        for item in dataset.items:
            if item.expected_label is None:
                continue
            trial_id = compare.trial_of_item(conn, item)
            if trial_id is not None:
                trial_by_item[item.key] = (item, trial_id)
        trial_ids = []
        for _item, trial_id in trial_by_item.values():
            trial_ids.append(trial_id)
        by_trial: dict[int, list[JudgementKind]] = {}
        for judgement in checks_db.judgement_kinds_of_trials(conn, trial_ids, requirements):
            by_trial.setdefault(judgement.trial_id, []).append(judgement)
        for item, trial_id in trial_by_item.values():
            seen_checks = set()
            # newest first: one judgement per trial and check counts
            for judgement in by_trial.get(trial_id, []):
                check = (judgement.model, judgement.prompt_sha256)
                if check in seen_checks:
                    continue
                seen_checks.add(check)
                posted = judgement.latest_published_at
                if read_day is not None and posted is not None and posted.date() >= read_day:
                    continue
                outcome = datasets.compare(item, dataset.parts, {}, judgement.label).label
                if outcome not in (datasets.AGREE, datasets.DISAGREE):
                    continue
                tally = tallies.setdefault(kind_of(judgement), Tally())
                tally.compared += 1
                if outcome == datasets.AGREE:
                    tally.agreed += 1
    return Calibration(tallies, used)


@dataclass(frozen=True)
class Decision:
    kind: Kind
    agreed: int
    compared: int
    # the measured share, once at least MIN_GOLD_ITEMS were compared
    confidence: float | None
    automatic: bool
    reason: str


def decide(judgement: JudgementKind, calibration: Calibration) -> Decision:
    """Whether a run may publish this judgement by itself, and why (or why a person must)."""
    kind = kind_of(judgement)
    tally = calibration.tallies.get(kind, Tally())
    confidence = None
    if tally.compared >= MIN_GOLD_ITEMS:
        confidence = tally.share
    measured = f"{tally.agreed} of {tally.compared} gold items agreed for judgements of this kind"
    if kind.label not in ("full", "not full"):
        return Decision(kind, tally.agreed, tally.compared, confidence, False,
                        "undecided: the check left it to a person")
    if confidence is None:
        return Decision(kind, tally.agreed, tally.compared, None, False,
                        f"{measured}; at least {MIN_GOLD_ITEMS} are needed to measure it")
    if confidence < AUTO_PUBLISH_AT:
        return Decision(kind, tally.agreed, tally.compared, confidence, False,
                        f"{measured} ({confidence:.0%}), below {AUTO_PUBLISH_AT:.0%}")
    return Decision(kind, tally.agreed, tally.compared, confidence, True,
                    f"{measured} ({confidence:.0%}), at least {AUTO_PUBLISH_AT:.0%}")


def basis_text(decision: Decision, calibration: Calibration) -> str:
    """What a run's publication records as its reason."""
    return f"{decision.reason}: {decision.kind.describe()}; gold {', '.join(calibration.datasets)}"


def auto_publish(conn: psycopg.Connection, run_id: int) -> list[int]:
    """Publish, as the session's actor, the run's judgements whose kind agreed with gold labels at
    least AUTO_PUBLISH_AT of the time; the others wait for a person. Returns the trials published."""
    judgements = checks_db.judgement_kinds_of_run(conn, run_id)
    if not judgements:
        return []
    calibration = calibrate(conn, judgements[0].requirements)
    published = []
    for judgement in judgements:
        decision = decide(judgement, calibration)
        if not decision.automatic:
            continue
        if checks_db.publish_judgement(conn, run_id, judgement.trial_id, confidence=decision.confidence,
                                       basis=basis_text(decision, calibration)):
            published.append(judgement.trial_id)
    return published
