"""Scores one check run against a gold dataset (ADR-001, ADR-003): the run's judgements next to the
dataset's expected answers, part by part and per trial. Works for any dataset whose items name
trials by identifier and whose labels answer the run's requirement set.

An item counts when it has an expected label and the run judged its trial without an error. It is set
aside when the run read a document version posted on or after the day the dataset's reviewers read
theirs (dataset.json evidence.as_of): ours and theirs may then be different documents, and a
disagreement would say nothing about the check. The costliest error is ours "not full" where theirs
is "full": a false accusation.
"""

from dataclasses import dataclass, field
from datetime import date

import psycopg

from trialwatch.db import checks as checks_db
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
    false_accusations: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


def evidence_day(dataset: Dataset) -> date | None:
    """The day the dataset's reviewers read their documents, if the dataset says."""
    as_of = dataset.evidence.get("as_of")
    if not as_of:
        return None
    return date.fromisoformat(as_of)


def _trial_of_item(conn: psycopg.Connection, item: datasets.Item) -> int | None:
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
    scored = Scored(dataset=dataset.ref, run_id=run_id, model=run.model, effort=run.effort, prompt=run.prompt,
                    requirements=run.requirements)
    for part in dataset.parts:
        scored.parts[part] = [0, 0]
        scored.parts_open[part] = 0
    judgements = checks_db.judgements(conn, run_id)
    read_day = evidence_day(dataset)
    for item in dataset.items:
        if item.expected_label is None:
            continue
        scored.labelled += 1
        trial_id = _trial_of_item(conn, item)
        if trial_id is None or trial_id not in judgements:
            scored.not_in_run.append(item.key)
            continue
        judgement = judgements[trial_id]
        if judgement.input_tokens:
            scored.input_tokens += judgement.input_tokens
        if judgement.output_tokens:
            scored.output_tokens += judgement.output_tokens
        if judgement.error:
            scored.errors[item.key] = judgement.error
            continue
        posted = judgement.latest_published_at
        if read_day is not None and posted is not None and posted.date() >= read_day:
            scored.set_aside.append(item.key)
            continue
        scored.compared += 1
        comparison = datasets.compare(item, dataset.parts, judgement.parts, judgement.label)
        for part, outcome in comparison.parts.items():
            if outcome == datasets.AGREE:
                scored.parts[part][0] += 1
                scored.parts[part][1] += 1
            elif outcome == datasets.DISAGREE:
                scored.parts[part][0] += 1
            elif outcome == datasets.OURS_OPEN:
                scored.parts_open[part] += 1
        pair = (judgement.label, item.expected_label)
        if pair not in scored.labels:
            scored.labels[pair] = 0
        scored.labels[pair] += 1
        if comparison.false_accusation:
            scored.false_accusations.append(item.key)
        if judgement.label == "full" and item.expected_label != "full":
            scored.missed.append(item.key)
        if comparison.label == datasets.DISAGREE:
            scored.disagreements.append(Disagreement(item.key, judgement.label, item.expected_label, judgement.parts,
                                                     item.expected_parts, item.comment))
    return scored


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
    lines.append(f"false accusations (ours not full, theirs full): {len(scored.false_accusations)}; "
                 f"missed (ours full, theirs not full): {len(scored.missed)}")
    lines.append(f"tokens in {scored.input_tokens:,}, out {scored.output_tokens:,}")
    return lines


def disagreement_lines(scored: Scored) -> list[str]:
    """Each disagreement and error as a line of text, for the command line."""
    lines = []
    for disagreement in scored.disagreements:
        comment = disagreement.comment
        if not comment:
            comment = ""
        lines.append(f"  {disagreement.key} ours {disagreement.ours} ({_parts_text(disagreement.our_parts)}), "
                     f"theirs {disagreement.theirs} ({_parts_text(disagreement.expected_parts)}): {comment}")
    for key, error in scored.errors.items():
        lines.append(f"  {key}: {error}")
    return lines
