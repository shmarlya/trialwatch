"""Publishing every judgement, with how far judgements of its kind agree with gold labels (ADR-007).

Everything is public: as a run finishes it publishes every judgement it made without an error, any
label, however often its kind agreed with the gold datasets. Nothing waits for a person. What staff
can still do is withdraw one with a reason, and a withdrawn judgement stays public, marked.

What a publication records is a measurement, not a permission: a judgement's confidence is how often
judgements of the same kind agreed with gold labels. A kind is the exact check that made it (model,
prompt text, requirement set) with its label and how its evidence came in: every quote verified or
not, a question asked again with every page or not, a question left unanswered or not. A different
prompt text or model starts a new kind with no measurement. The confidence is recorded once at least
MIN_GOLD_ITEMS were compared; the basis (how many agreed, of how many, from which datasets) always.

Gold items are counted once per trial and check (the newest judgement of that trial by that model and
prompt), and set aside when our judgement read a document posted on or after the day the dataset's
reviewers read theirs (as gold/compare.py does). A run is measured only by runs started before it, so
a run that checks gold trials never counts its own judgements towards its own agreement.
"""

from dataclasses import dataclass

import psycopg

from trialwatch.checks import requirements as requirement_sets
from trialwatch.db import checks as checks_db
from trialwatch.db.checks import JudgementKind
from trialwatch.gold import compare, datasets

# A confidence is recorded once this many gold items of a kind were compared: fewer say too little
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


def calibrate(conn: psycopg.Connection, requirements: str, before_run: int | None = None) -> Calibration:
    """How often each kind of judgement under the requirement set agreed with gold labels; with
    `before_run`, only by judgements of runs started before that one."""
    tallies: dict[Kind, Tally] = {}
    used = []
    # what each label means next to a gold label (an accusation, clearing a trial, left open)
    label_rules = requirement_sets.load(requirements).labels
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
        for judgement in checks_db.judgement_kinds_of_trials(conn, trial_ids, requirements, before_run):
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
                outcome = datasets.compare(item, dataset.parts, {}, judgement.label, label_rules).label
                if outcome not in (datasets.AGREE, datasets.DISAGREE):
                    continue
                tally = tallies.setdefault(kind_of(judgement), Tally())
                tally.compared += 1
                if outcome == datasets.AGREE:
                    tally.agreed += 1
    return Calibration(tallies, used)


@dataclass(frozen=True)
class Agreement:
    """How a judgement's kind compared with gold labels."""

    kind: Kind
    agreed: int
    compared: int
    # the measured share, once at least MIN_GOLD_ITEMS were compared
    confidence: float | None
    reason: str


def measure(judgement: JudgementKind, calibration: Calibration) -> Agreement:
    """How often judgements of this one's kind agreed with gold labels, in words."""
    kind = kind_of(judgement)
    tally = calibration.tallies.get(kind, Tally())
    measured = f"{tally.agreed} of {tally.compared} gold items agreed for judgements of this kind"
    if tally.compared < MIN_GOLD_ITEMS:
        return Agreement(kind, tally.agreed, tally.compared, None,
                         f"{measured}; at least {MIN_GOLD_ITEMS} are needed to measure it")
    confidence = tally.share
    return Agreement(kind, tally.agreed, tally.compared, confidence, f"{measured} ({confidence:.0%})")


def basis_text(agreement: Agreement, calibration: Calibration) -> str:
    """What a run's publication records as how its kind compared with gold."""
    datasets_used = ", ".join(calibration.datasets) or "none"
    return f"{agreement.reason}: {agreement.kind.describe()}; gold {datasets_used}"


def publish_run(conn: psycopg.Connection, run_id: int) -> list[int]:
    """Record on every judgement of the finished run without an error (each public now) how its kind
    compared with gold labels. Returns the trials published."""
    judgements = checks_db.judgement_kinds_of_run(conn, run_id)
    if not judgements:
        return []
    calibration = calibrate(conn, judgements[0].requirements, before_run=run_id)
    published = []
    for judgement in judgements:
        agreement = measure(judgement, calibration)
        done = checks_db.set_agreement(conn, run_id, judgement.trial_id, agreement.confidence,
                                       basis_text(agreement, calibration))
        if done:
            published.append(judgement.trial_id)
    return published
