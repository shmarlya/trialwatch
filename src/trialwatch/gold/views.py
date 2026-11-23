"""Gold datasets next to our judgements, as the gold pages and the trial page show them (ADR-003): the
files under data/gold/ are searched here, never stored in the database."""

from dataclasses import dataclass
from datetime import datetime
from functools import cache

from trialwatch.checks import requirements as requirement_sets
from trialwatch.db import overview
from trialwatch.db.core import Identifier, normalised
from trialwatch.gold import compare
from trialwatch.gold import datasets as gold_datasets


@dataclass(frozen=True)
class GoldRow:
    item: gold_datasets.Item
    trial_id: int | None
    judgement: overview.LatestJudgement | None
    comparison: gold_datasets.Comparison | None
    reviewers_disagreed: bool
    # our judgement read a document posted on or after the day the reviewers read theirs: not compared
    set_aside: bool


@dataclass
class GoldSummary:
    items: int = 0
    labelled: int = 0
    synced: int = 0
    checked: int = 0
    agree: int = 0
    disagree: int = 0
    ours_open: int = 0
    false_accusations: int = 0
    set_aside: int = 0


def stored_identifiers(item: gold_datasets.Item) -> list[tuple[str, str]]:
    found = []
    for identifier in item.identifiers:
        stored = normalised(identifier)
        found.append((stored.scheme, stored.value))
    return found


@cache
def label_rules(requirements_name: str) -> tuple[requirement_sets.LabelRule, ...]:
    """A requirement set's labels, which say what each label means next to a gold label; read once (gold
    pages compare every item of a dataset)."""
    return requirement_sets.load(requirements_name).labels


def set_aside(dataset: gold_datasets.Dataset, published_at: datetime | None) -> bool:
    """Our judgement read a document posted on or after the day the dataset's reviewers read theirs, so
    ours and theirs may be different documents (gold/compare.py)."""
    return compare.set_aside(dataset, published_at)


def gold_rows(conn, dataset: gold_datasets.Dataset) -> list[GoldRow]:
    """Every item of the dataset with the trial we hold for it (if any) and our newest judgement of
    that trial under the dataset's requirement set, compared with the item's expected answers."""
    wanted = []
    for item in dataset.items:
        wanted.extend(stored_identifiers(item))
    trial_by_identifier = overview.trials_for_identifiers(conn, wanted)
    trial_ids = []
    for trial_id in trial_by_identifier.values():
        trial_ids.append(trial_id)
    judgement_by_trial = overview.latest_judgements(conn, trial_ids, dataset.requirements)
    rows = []
    for item in dataset.items:
        trial_id = None
        for key in stored_identifiers(item):
            if key in trial_by_identifier:
                trial_id = trial_by_identifier[key]
                break
        judgement = None
        comparison = None
        aside = False
        if trial_id is not None and trial_id in judgement_by_trial:
            judgement = judgement_by_trial[trial_id]
            aside = set_aside(dataset, judgement.latest_published_at)
            if not aside:
                comparison = gold_datasets.compare(
                    item, dataset.parts, judgement.parts, judgement.label, label_rules(dataset.requirements)
                )
        disagreed = gold_datasets.reviewers_disagreed(item)
        rows.append(GoldRow(item, trial_id, judgement, comparison, disagreed, aside))
    return rows


def gold_summary(rows: list[GoldRow]) -> GoldSummary:
    summary = GoldSummary()
    for row in rows:
        summary.items += 1
        if row.item.expected_label is not None:
            summary.labelled += 1
        if row.trial_id is not None:
            summary.synced += 1
        if row.set_aside:
            summary.set_aside += 1
        if row.comparison is None:
            continue
        summary.checked += 1
        if row.comparison.label == gold_datasets.AGREE:
            summary.agree += 1
        elif row.comparison.label == gold_datasets.DISAGREE:
            summary.disagree += 1
        elif row.comparison.label == gold_datasets.OURS_OPEN:
            summary.ours_open += 1
        if row.comparison.false_accusation:
            summary.false_accusations += 1
    return summary


# What the "ours" filter keeps
GOLD_OURS_FILTERS = {
    "": "any",
    "not-synced": "not in our database",
    "synced": "in our database",
    "checked": "checked by us",
    "agree": "our label agrees",
    "disagree": "our label disagrees",
    "false-accusation": "false accusation (ours not full, theirs full)",
}


def keeps(row: GoldRow, text: str, label_filter: str, reviewers: str, ours: str) -> bool:
    if text and not gold_datasets.matches_text(row.item, text):
        return False
    if label_filter == "none" and row.item.expected_label is not None:
        return False
    if label_filter and label_filter != "none" and row.item.expected_label != label_filter:
        return False
    if reviewers == "disagreed" and not row.reviewers_disagreed:
        return False
    if ours == "not-synced" and row.trial_id is not None:
        return False
    if ours == "synced" and row.trial_id is None:
        return False
    if ours == "checked" and row.comparison is None:
        return False
    if ours in ("agree", "disagree"):
        if row.comparison is None or row.comparison.label != ours:
            return False
    if ours == "false-accusation":
        if row.comparison is None or not row.comparison.false_accusation:
            return False
    return True


def gold_items_for(identifiers: list, judgements: list) -> list[tuple]:
    """(dataset, item, comparison with our newest judgement under its requirement set) for every
    gold item naming one of the trial's identifiers."""
    wanted = []
    for identifier in identifiers:
        wanted.append(Identifier(identifier.scheme, identifier.value))
    found = []
    for dataset, item in gold_datasets.items_for_identifiers(wanted):
        comparison = None
        for judgement in judgements:
            if comparable(judgement, dataset):
                if not set_aside(dataset, judgement.latest_published_at):
                    comparison = gold_datasets.compare(
                        item, dataset.parts, judgement.parts, judgement.label, label_rules(dataset.requirements)
                    )
                break
        found.append((dataset, item, comparison))
    return found


def comparable(judgement: overview.JudgementRow, dataset: gold_datasets.Dataset) -> bool:
    """A judgement whose run has ended, without an error, under the requirement set the dataset's
    labels answer."""
    if judgement.error or judgement.run_status == "running":
        return False
    return judgement.requirements == dataset.requirements
