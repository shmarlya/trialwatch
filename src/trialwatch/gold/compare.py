"""Scores one check run against a gold dataset (ADR-001, ADR-003): the run's judgements next to the
dataset's expected answers, part by part and per trial. Works for any dataset whose items name
trials by identifier and whose labels answer the run's requirement set.

An item counts when it has an expected label and the run judged its trial without an error. It is set
aside when the run read a document version posted on or after the day the dataset's reviewers read
theirs (dataset.json evidence.as_of): ours and theirs may then be different documents, and a
disagreement would say nothing about the check. The costliest error is ours "not full" where theirs
is "full": a false accusation. It is "clear" when every reviewer's own reading of the item also
cleared the trial, and "contested" when at least one reviewer read it as we did.

For the items it disagrees on or leaves open, the score keeps every question's answer and rule, so a
report shows why without opening the database.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import psycopg

from trialwatch.checks import requirements
from trialwatch.llm import dollars
from trialwatch.db import checks as checks_db
from trialwatch.db import costs as costs_db
from trialwatch.db import core
from trialwatch.gold import datasets
from trialwatch.gold.datasets import Dataset


@dataclass(frozen=True)
class Disagreement:
    key: str
    ours: str
    theirs: str
    our_parts: dict
    expected_parts: dict
    comment: str | None
    # for a false accusation: "clear" (no reviewer read it as an accusation) or "contested"
    tier: str | None = None


@dataclass
class Scored:
    dataset: str
    run_id: int
    model: str
    effort: str
    prompt: str
    requirements: str
    # items with an expected label
    labelled: int = 0
    # of them: the run's trial for it was judged, and compared
    compared: int = 0
    # items whose trial the run did not judge (not in the database, or not chosen)
    not_in_run: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    set_aside: list[str] = field(default_factory=list)
    # part -> [compared, agreed]; parts ours left open are counted apart
    parts: dict[str, list[int]] = field(default_factory=dict)
    parts_open: dict[str, int] = field(default_factory=dict)
    # (ours, theirs) -> items, ours being full, not full or undecided (left to a person)
    labels: dict[tuple[str, str], int] = field(default_factory=dict)
    # the requirement set's labels, which say what each one means (checks/requirements.py)
    label_rules: tuple[requirements.LabelRule, ...] = ()
    false_accusations: list[str] = field(default_factory=list)
    # of them: at least one reviewer read the item as an accusation too
    contested_accusations: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    # items ours left open (undecided)
    undecided: list[str] = field(default_factory=list)
    # decided items whose label matches the final label or at least one reviewer's own reading
    agrees_with_a_reviewer: int = 0
    # item key -> our label, for every compared item (repeated runs are compared by it)
    item_labels: dict[str, str] = field(default_factory=dict)
    # item key -> the gold label, for every compared item
    expected_labels: dict[str, str] = field(default_factory=dict)
    # item key -> each question's answer and rule, for the items ours disagrees on or leaves open
    details: dict[str, list[checks_db.AnswerDetail]] = field(default_factory=dict)
    disagreements: list[Disagreement] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    # what the judged items' model calls cost (core.llm_call), and how many calls ran on a model not in
    # the price list
    cost_usd: Decimal = Decimal(0)
    unpriced: int = 0


def evidence_day(dataset: Dataset) -> date | None:
    """The day the dataset's reviewers read their documents, if the dataset says."""
    as_of = dataset.evidence.get("as_of")
    if not as_of:
        return None
    return date.fromisoformat(as_of)


def set_aside(dataset: Dataset, published_at: datetime | None) -> bool:
    """Our judgement read a document posted on or after the day the dataset's reviewers read theirs:
    ours and theirs may be different documents, so the two are not compared."""
    read_day = evidence_day(dataset)
    if read_day is None or published_at is None:
        return False
    return published_at.date() >= read_day


def trial_of_item(conn: psycopg.Connection, item: datasets.Item) -> int | None:
    """The trial (after merges) one of the item's identifiers belongs to, if any."""
    wanted = []
    for identifier in item.identifiers:
        wanted.append(core.normalised(identifier))
    found = core.trials_with_identifiers(conn, wanted)
    for identifier in wanted:
        if identifier in found:
            return found[identifier]
    return None


def compare_run(conn: psycopg.Connection, run_id: int, dataset: Dataset) -> Scored:
    """Score run `run_id` against `dataset`; ValueError when the run is unknown or answered another
    requirement set than the dataset's labels."""
    run = checks_db.run_config(conn, run_id)
    if run is None:
        raise ValueError(f"no check run {run_id}")
    if run.requirements != dataset.requirements:
        raise ValueError(f"run {run_id} judged against {run.requirements}, but {dataset.ref} answers "
                         f"{dataset.requirements}")
    requirement_set = requirements.load(dataset.requirements)
    scored = Scored(dataset=dataset.ref, run_id=run_id, model=run.model, effort=run.effort, prompt=run.prompt,
                    requirements=run.requirements, label_rules=requirement_set.labels)
    for part in dataset.parts:
        scored.parts[part] = [0, 0]
        scored.parts_open[part] = 0
    judgements = checks_db.judgements(conn, run_id)
    billed = costs_db.trial_spend(conn, run_id)
    # item key -> trial, for the items whose answers the report shows
    to_explain: dict[str, int] = {}
    for item in dataset.items:
        if item.expected_label is None:
            continue
        scored.labelled += 1
        trial_id = trial_of_item(conn, item)
        if trial_id is None or trial_id not in judgements:
            scored.not_in_run.append(item.key)
            continue
        judgement = judgements[trial_id]
        if judgement.input_tokens:
            scored.input_tokens += judgement.input_tokens
        if judgement.output_tokens:
            scored.output_tokens += judgement.output_tokens
        if trial_id in billed:
            scored.cost_usd += billed[trial_id].cost_usd
            scored.unpriced += billed[trial_id].unpriced_calls
        if judgement.error:
            scored.errors[item.key] = judgement.error
            continue
        if set_aside(dataset, judgement.latest_published_at):
            scored.set_aside.append(item.key)
            continue
        scored.compared += 1
        comparison = datasets.compare(item, dataset.parts, judgement.parts, judgement.label, scored.label_rules)
        for part, outcome in comparison.parts.items():
            if outcome == datasets.AGREE:
                scored.parts[part][0] += 1
                scored.parts[part][1] += 1
            elif outcome == datasets.DISAGREE:
                scored.parts[part][0] += 1
            elif outcome == datasets.OURS_OPEN:
                scored.parts_open[part] += 1
        scored.item_labels[item.key] = judgement.label
        scored.expected_labels[item.key] = item.expected_label
        reviewers = reviewer_labels(requirement_set, item)
        ours_means = requirements.meaning_of(scored.label_rules, judgement.label)
        if ours_means == "open":
            scored.undecided.append(item.key)
            to_explain[item.key] = trial_id
        elif judgement.label == item.expected_label or judgement.label in reviewers:
            scored.agrees_with_a_reviewer += 1
        pair = (judgement.label, item.expected_label)
        if pair not in scored.labels:
            scored.labels[pair] = 0
        scored.labels[pair] += 1
        tier = None
        if comparison.false_accusation:
            scored.false_accusations.append(item.key)
            tier = "clear"
            for label in reviewers:
                if requirements.meaning_of(scored.label_rules, label) == "accusation":
                    tier = "contested"
            if tier == "contested":
                scored.contested_accusations.append(item.key)
        ours_clears = requirements.meaning_of(scored.label_rules, judgement.label) == "clears"
        if ours_clears and requirements.meaning_of(scored.label_rules, item.expected_label) != "clears":
            scored.missed.append(item.key)
        if comparison.label == datasets.DISAGREE:
            scored.disagreements.append(Disagreement(item.key, judgement.label, item.expected_label, judgement.parts,
                                                     item.expected_parts, item.comment, tier))
            to_explain[item.key] = trial_id
    if to_explain:
        by_trial: dict[int, list[checks_db.AnswerDetail]] = {}
        for detail in checks_db.answer_details(conn, run_id):
            by_trial.setdefault(detail.trial_id, []).append(detail)
        for key, trial_id in to_explain.items():
            scored.details[key] = by_trial.get(trial_id, [])
    return scored


def reviewer_labels(requirement_set: requirements.RequirementSet, item: datasets.Item) -> list[str]:
    """The label each of the item's reviewers' own reading of the parts gives, under the set's rules."""
    labels = []
    for review in item.reviews:
        labels.append(requirements.label_for(requirement_set, review.parts))
    return labels


def dataset_for_run(conn: psycopg.Connection, run_id: int) -> str | None:
    """The newest dataset version whose labels answer the run's requirement set, if any."""
    run = checks_db.run_config(conn, run_id)
    if run is None:
        return None
    chosen = None
    for ref in datasets.dataset_refs():
        if datasets.load(ref).requirements == run.requirements:
            chosen = ref
    return chosen


def _parts_text(parts: dict) -> str:
    """Parts as part=1, part=0, or part=? when not settled."""
    shown = []
    for part, value in parts.items():
        if value is None:
            shown.append(f"{part}=?")
        elif value:
            shown.append(f"{part}=1")
        else:
            shown.append(f"{part}=0")
    return " ".join(shown)


def report_lines(scored: Scored) -> list[str]:
    """The score as lines of text, for the command line."""
    lines = [
        f"run {scored.run_id} against {scored.dataset} · model {scored.model}, effort {scored.effort}, "
        f"prompt {scored.prompt}, requirements {scored.requirements}",
        f"items with a label {scored.labelled}: compared {scored.compared}, not judged in the run "
        f"{len(scored.not_in_run)}, errors {len(scored.errors)}, set aside (we read documents posted after "
        f"the reviewers' day) {len(scored.set_aside)}",
    ]
    for part, counts in scored.parts.items():
        lines.append(f"  {part:<13} {counts[1]}/{counts[0]} agree with the reviewers, {scored.parts_open[part]} left open")
    for pair, count in sorted(scored.labels.items()):
        lines.append(f"  ours {pair[0]:<9} · theirs {pair[1]:<8} {count}")
    clear = len(scored.false_accusations) - len(scored.contested_accusations)
    lines.append(f"false accusations (ours not full, theirs full): {len(scored.false_accusations)} ({clear} clear, "
                 f"{len(scored.contested_accusations)} contested: a reviewer read it as we did); "
                 f"missed (ours full, theirs not full): {len(scored.missed)}")
    lines.append(f"decided items that agree with the final label or with at least one reviewer: "
                 f"{scored.agrees_with_a_reviewer}")
    lines.append(f"tokens in {scored.input_tokens:,}, out {scored.output_tokens:,}; cost {dollars(scored.cost_usd)}"
                 f"{_unpriced_text(scored.unpriced)}")
    return lines


def _unpriced_text(count: int) -> str:
    if not count:
        return ""
    return f" (+{count} calls on models not in the price list)"


def disagreement_lines(scored: Scored) -> list[str]:
    """Each disagreement (with each question's answer and rule) and error as lines of text."""
    lines = []
    for disagreement in scored.disagreements:
        comment = disagreement.comment
        if not comment:
            comment = ""
        tier = ""
        if disagreement.tier is not None:
            tier = f" [{disagreement.tier} false accusation]"
        lines.append(f"  {disagreement.key} ours {disagreement.ours} ({_parts_text(disagreement.our_parts)}), "
                     f"theirs {disagreement.theirs} ({_parts_text(disagreement.expected_parts)}){tier}: {comment}")
        lines += detail_lines(scored.details.get(disagreement.key, []))
    for key, error in scored.errors.items():
        lines.append(f"  {key}: {error}")
    return lines


def undecided_lines(scored: Scored) -> list[str]:
    """Each item ours left open, with the gold label and each question's answer and rule."""
    lines = []
    for key in scored.undecided:
        lines.append(f"  {key} theirs {scored.expected_labels.get(key)}")
        lines += detail_lines(scored.details.get(key, []))
    return lines


QUOTE_SHOWN = 160


def detail_lines(details: list[checks_db.AnswerDetail]) -> list[str]:
    """One line per question: its value and rule, then the model's answer (found, kind, page, quote)."""
    lines = []
    for detail in details:
        if detail.value is None:
            value = "undecided"
        elif detail.value:
            value = "yes"
        else:
            value = "no"
        rule = detail.rule or "no rule"
        if not detail.answered:
            answer = "no answer"
        elif detail.found is None:
            answer = "don't know"
        elif detail.found:
            answer = f"found, {detail.kind}, p.{detail.page_no}"
            if not detail.verified:
                answer += ", quote NOT on the page"
        else:
            answer = "not found"
        if detail.widened:
            answer += ", asked again with every page"
        lines.append(f"      {detail.key}: {value} · {rule} · {answer}")
        if detail.quote:
            quote = " ".join(detail.quote.split())
            if len(quote) > QUOTE_SHOWN:
                quote = quote[:QUOTE_SHOWN] + "…"
            lines.append(f"        “{quote}”")
        if detail.note:
            lines.append(f"        note: {' '.join(detail.note.split())}")
    return lines
