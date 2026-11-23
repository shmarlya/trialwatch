"""Bruckner et al.'s files in data/bruckner/ (see its README.md), as TrialWatch reads them:

- build(): the converter that makes their gold dataset version under data/gold/bruckner/ (ADR-003);
- compare_due(): runs our due-date rules on their CTIS export and compares the result with their
  verdicts, trial by trial (ADR-001: reproduce their verdicts first). tests/test_due_date.py runs it.

Their files are written by R: empty values are 'NA', yes/no flags '1' and '0' or 'NA'.
- <date>_ctis_study_final_dataset.csv: one row per due trial (277): the verdict, what the scientific
  results document contained, lay summary, days to results, completion date, comments;
- ctis_merged_reviews_consolidated_with_comments.csv: one row per results document read (206), with
  each reviewer's answer per part. It is not valid UTF-8 (text copied from CTIS was mangled before
  publication), so broken bytes are read as U+FFFD; only free text is affected, never a label;
- CTIS_trials_Phases_II-IV_20251110.csv and CTIS_trials_with_PIP_20251110.csv: the CTIS search exports
  of 2025-11-10 their trials were picked from (the second lists trials with a paediatric plan).

In the dataset every trial of the final dataset is an item. Its expected parts are the reviewers'
consensus (the third reviewer where there is one, else the first two when they agree), and its
expected label is "full" when their results document was full, else "not full". The 71 trials with
no results document to read (47 not fully reported, 24 extended) have no expected label: a check that
reads documents has nothing to be compared on there, but the items stay searchable.
"""

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from trialwatch.gold.converters import due_date
from trialwatch.db.core import Identifier
from trialwatch.gold.datasets import Item, Review

NAME = "bruckner"
# Named after their final dataset's date: a later release of their files is a new version
VERSION = "2026-03-16"
VERDICT_FILE = "2026-03-16_ctis_study_final_dataset.csv"
REVIEWS_FILE = "ctis_merged_reviews_consolidated_with_comments.csv"
EXPORT_FILE = "CTIS_trials_Phases_II-IV_20251110.csv"
# Same day's export of the trials with a paediatric investigation plan
PIP_FILE = "CTIS_trials_with_PIP_20251110.csv"
# The day of the CTIS export their trials were picked from; documents posted later may differ from what they read
SNAPSHOT_DAY = date(2025, 11, 10)
REQUIREMENTS = "eu-ctr-results"
PARTS = ("participants", "outcomes", "cancelled")
LABELS = ("full", "not full")

VERDICTS = ("reported fully on time", "reported fully late", "not fully reported", "extended", "cancelled")
# Their three flags for what the scientific results document contained; exactly one is set per trial
RESULTS_DOCUMENT = {
    "full_scientific_results": "full",
    "incomplete_scientific_results": "incomplete",
    "no_scientific_results_document": "none",
}
# Their columns shown on a gold item's page as they are, by the name we show
VERDICT_DETAILS = {
    "verdict": "legal_compliance_assessment",
    "trial status": "Overall.trial.status",
    "phase": "Trial.phase",
    "sponsor": "Sponsor.Co.Sponsors",
    "sponsor type": "Sponsor.type",
    "end of trial": "Global.end.of.the.trial",
    "regulatory status": "regulatory_status",
    "completion date": "completion_date",
}
# Reviewer -> suffix of their columns in the reviews file
REVIEWERS = {"first": "till_christie", "second": "team", "third": "ghassan"}
# Their "asked for a check" columns don't follow the suffix pattern for the first reviewer
CHECK_COLUMN = {"first": "check", "second": "check_team", "third": "check_ghassan"}
FLAGS = {"1": True, "0": False, "NA": None}
EXPORT_KEY = "Trial number"


# --- reading their files ---------------------------------------------------------------------------


def _text(value: str | None) -> str | None:
    """A cell's text, or None for R's 'NA' and empty cells."""
    if value is None:
        return None
    value = value.strip()
    if value in ("", "NA"):
        return None
    return value


def _unless_na(value: str) -> str | None:
    """A cell as it is, or None for R's 'NA' (comments keep their spacing, as released)."""
    if value == "NA":
        return None
    return value


def _one_line(value: str | None) -> str | None:
    """Titles in their file carry the line breaks of the CTIS form."""
    text = _text(value)
    if text is None:
        return None
    return " ".join(text.split())


def _flag(row: dict[str, str], column: str) -> bool | None:
    if row[column] not in FLAGS:
        raise ValueError(f"{column} is {row[column]!r}, expected 1, 0 or NA")
    return FLAGS[row[column]]


def _missing_columns(fieldnames: list[str] | None, wanted: list[str]) -> list[str]:
    present = fieldnames
    if present is None:
        present = []
    missing = []
    for column in wanted:
        if column not in present:
            missing.append(column)
    return missing


def _read_csv(path: Path, wanted: list[str], what: str) -> list[dict[str, str]]:
    """The rows of a CSV file; broken UTF-8 becomes U+FFFD, a byte-order mark is dropped."""
    text = path.read_bytes().decode("utf-8", errors="replace").removeprefix("﻿")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    missing = _missing_columns(reader.fieldnames, wanted)
    if missing:
        raise ValueError(f"{path.name} has no {', '.join(missing)} column(s); is it {what}?")
    rows = []
    for row in reader:
        rows.append(row)
    return rows


def _verdict_columns() -> list[str]:
    columns = [
        "trial_number",
        "legal_compliance_assessment",
        "laypersons_results",
        "days_to_results",
        "comments",
        "regulatory_status",
        "completion_date",
        "extended_not_due_yet",
    ]
    for column in RESULTS_DOCUMENT:
        columns.append(column)
    return columns


def _review_columns() -> list[str]:
    columns = ["trial_number", "results_pub_date_DDMMYYYY", "agreement_flag", "comments_final", "cancelled_final_data"]
    for suffix in REVIEWERS.values():
        for part in PARTS:
            columns.append(f"{part}_{suffix}")
        columns.append(f"comments_{suffix}")
    for column in CHECK_COLUMN.values():
        columns.append(column)
    return columns


@dataclass(frozen=True)
class Verdict:
    """Their final assessment of one due trial (a row of the final dataset)."""

    ct_number: str
    verdict: str
    # full | incomplete | none
    results_document: str
    lay_summary: bool
    days_to_results: int | None
    comment: str | None
    regulatory_status: str | None
    completion_date: str | None
    extended: bool
    row: dict


def verdict_of(row: dict[str, str]) -> Verdict:
    """One row of the final dataset; ValueError when its labels contradict each other."""
    verdict = row["legal_compliance_assessment"]
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    documents = []
    for column, label in RESULTS_DOCUMENT.items():
        if row[column] == "1":
            documents.append(label)
    if len(documents) != 1:
        raise ValueError(f"expected one results-document flag, found {len(documents)}")
    days = _text(row["days_to_results"])
    # Only a trial reported fully has a number of days to its results, and it always has one
    reported_fully = verdict.startswith("reported fully")
    if (days is None) == reported_fully:
        raise ValueError(f"days_to_results {row['days_to_results']!r} doesn't fit verdict {verdict!r}")
    days_to_results = None
    if days is not None:
        days_to_results = int(days)
    return Verdict(
        ct_number=row["trial_number"].strip(),
        verdict=verdict,
        results_document=documents[0],
        lay_summary=row["laypersons_results"] == "1",
        days_to_results=days_to_results,
        comment=_unless_na(row["comments"]),
        regulatory_status=_text(row["regulatory_status"]),
        completion_date=_text(row["completion_date"]),
        extended=row["extended_not_due_yet"] == "1",
        row=row,
    )


def verdicts(data_dir: Path) -> list[Verdict]:
    found = []
    for row in _read_csv(data_dir / VERDICT_FILE, _verdict_columns(), "the final study dataset"):
        found.append(verdict_of(row))
    return found


@dataclass(frozen=True)
class ReviewedDocument:
    """Their reviewers' reading of one trial's results document (a row of the merged reviews)."""

    ct_number: str
    posted_on: date
    reviewers_agreed: bool
    final_comment: str | None
    cancelled: bool
    reviews: list[Review]
    row: dict


def _reviews_of(row: dict[str, str]) -> list[Review]:
    """Every reviewer who answered at least one part, with their answers."""
    found = []
    for reviewer, suffix in REVIEWERS.items():
        parts = {}
        answered = False
        for part in PARTS:
            parts[part] = _flag(row, f"{part}_{suffix}")
            if parts[part] is not None:
                answered = True
        # A reviewer who left every part empty did not review this document
        if answered:
            found.append(Review(reviewer, parts, _flag(row, CHECK_COLUMN[reviewer]), _text(row[f"comments_{suffix}"])))
    return found


def reviewed_document_of(row: dict[str, str]) -> ReviewedDocument:
    """One row of the merged reviews; ValueError when malformed."""
    posted_on = datetime.strptime(row["results_pub_date_DDMMYYYY"].strip(), "%d/%m/%Y").date()
    if row["agreement_flag"] not in ("TRUE", "FALSE"):
        raise ValueError(f"agreement_flag is {row['agreement_flag']!r}, expected TRUE or FALSE")
    return ReviewedDocument(
        ct_number=row["trial_number"].strip(),
        posted_on=posted_on,
        reviewers_agreed=row["agreement_flag"] == "TRUE",
        final_comment=_text(row["comments_final"]),
        cancelled=row["cancelled_final_data"] == "1",
        reviews=_reviews_of(row),
        row=row,
    )


def reviewed_documents(data_dir: Path) -> dict[str, ReviewedDocument]:
    """CT number -> their reviewers' reading of its results document."""
    found = {}
    for row in _read_csv(data_dir / REVIEWS_FILE, _review_columns(), "the merged reviews file"):
        document = reviewed_document_of(row)
        found[document.ct_number] = document
    return found


def export_rows(path: Path) -> dict[str, dict[str, str]]:
    """CT number -> its row of a CTIS search export, as it is."""
    found = {}
    for row in _read_csv(path, [EXPORT_KEY], "a CTIS search export"):
        found[row[EXPORT_KEY].strip()] = row
    return found


def consensus(reviews: list[Review], part: str) -> bool | None:
    """The third reviewer settles a part; otherwise the first two must agree."""
    answers = {}
    for review in reviews:
        answers[review.reviewer] = review.parts.get(part)
    third = answers.get("third")
    if third is not None:
        return third
    first = answers.get("first")
    second = answers.get("second")
    if first is not None and first == second:
        return first
    return None


# --- the gold dataset (ADR-003) ------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def meta(data_dir: Path, items: int) -> dict:
    return {
        "name": NAME,
        "version": VERSION,
        "title": "Bruckner et al. 2026: results reporting of EU Phase II-IV trials in CTIS",
        "description": (
            "Two or three reviewers read each due trial's CTIS summary of results and answered three "
            "questions: does it report how many participants took part, does it give a result for every "
            "primary endpoint, and does it say nobody was enrolled. The expected label is 'full' when "
            "their final assessment of the scientific results document was full."
        ),
        "citation": (
            "Bruckner T, Dike CE, Caquelin L, Freeman A, Aspromonti DA, DeVito NJ, Song Z, Karam G, "
            "Nilsonne G. Assessing Compliance with Reporting Requirements in European Phase II-IV Clinical "
            "Trials: A Cross-Sectional Observational Study. medRxiv preprint, 2026. "
            "doi:10.64898/2026.04.03.26350111"
        ),
        "url": "https://osf.io/sn4j2/overview",
        "requirements": REQUIREMENTS,
        "parts": list(PARTS),
        "labels": list(LABELS),
        "evidence": {
            "document_kinds": ["results-summary"],
            "as_of": SNAPSHOT_DAY.isoformat(),
            "description": (
                "The summary of results posted in CTIS before the export of 2025-11-10. A trial whose "
                "results were submitted again later may now have documents the reviewers never read."
            ),
        },
        "built_from": [
            {"file": f"data/bruckner/{VERDICT_FILE}", "sha256": _sha256(data_dir / VERDICT_FILE)},
            {"file": f"data/bruckner/{REVIEWS_FILE}", "sha256": _sha256(data_dir / REVIEWS_FILE)},
        ],
        "built_with": "trialwatch gold build bruckner",
        "items": items,
    }


def build(data_dir: Path) -> tuple[dict, list[Item]]:
    """The dataset's meta and items, from their files in `data_dir`; ValueError on a malformed row."""
    reviewed = reviewed_documents(data_dir)
    items = []
    for verdict in verdicts(data_dir):
        details = {}
        for shown, column in VERDICT_DETAILS.items():
            details[shown] = _text(verdict.row.get(column))
        details["results document"] = verdict.results_document
        details["lay summary"] = verdict.lay_summary
        details["days to results"] = verdict.days_to_results
        expected_parts = {}
        for part in PARTS:
            expected_parts[part] = None
        expected_label = None
        reviews = []
        reviewers_agreed = None
        evidence = []
        document = reviewed.get(verdict.ct_number)
        if document is not None:
            for part in PARTS:
                expected_parts[part] = consensus(document.reviews, part)
            if verdict.results_document == "full":
                expected_label = "full"
            else:
                expected_label = "not full"
            reviews = document.reviews
            reviewers_agreed = document.reviewers_agreed
            evidence.append({"kind": "results-summary", "posted_on": document.posted_on.isoformat()})
            details["regulatory framework"] = _text(document.row["regulatory_framework"])
            details["reviewers' final comment"] = document.final_comment
            details["team discussion"] = _text(document.row["for_team_discussion"])
        items.append(
            Item(
                key=verdict.ct_number,
                identifiers=[Identifier("eu-ct", verdict.ct_number)],
                title=_one_line(verdict.row.get("Title.of.the.trial")),
                expected_parts=expected_parts,
                expected_label=expected_label,
                reviews=reviews,
                reviewers_agreed=reviewers_agreed,
                comment=verdict.comment,
                evidence=evidence,
                details=details,
            )
        )
    return meta(data_dir, len(items)), items


# --- our due-date rules against their verdicts (ADR-001) ------------------------------------------


@dataclass
class Disagreement:
    ct_number: str
    field: str
    ours: object
    theirs: object


@dataclass
class DueComparison:
    trials: int = 0
    ended: int = 0
    due_ours: set[str] = field(default_factory=set)
    due_theirs: set[str] = field(default_factory=set)
    # field -> [trials compared, trials that agree]
    fields: dict[str, list[int]] = field(default_factory=dict)
    disagreements: list[Disagreement] = field(default_factory=list)

    def check(self, ct_number: str, name: str, ours: object, theirs: object) -> None:
        if name not in self.fields:
            self.fields[name] = [0, 0]
        counts = self.fields[name]
        counts[0] += 1
        if ours == theirs:
            counts[1] += 1
        else:
            self.disagreements.append(Disagreement(ct_number, name, ours, theirs))


def compare_due(data_dir: Path) -> DueComparison:
    """Which trials our rules call due on the export day, and their verdicts, next to theirs.

    Inputs our rules can't compute yet are taken from them: whether the results document was full and
    had a lay summary, cancellations (their reviewers; our evidence check will replace these), and
    extensions (non-public data, always taken as given)."""
    export = export_rows(data_dir / EXPORT_FILE)
    with_pip = export_rows(data_dir / PIP_FILE)
    reviewed = reviewed_documents(data_dir)
    theirs = {}
    for verdict in verdicts(data_dir):
        theirs[verdict.ct_number] = verdict

    result = DueComparison(trials=len(export))
    for ct_number in theirs:
        result.due_theirs.add(ct_number)
    # CT number -> (paediatric, completion date) for the trials our rules call due
    ours = {}
    for ct_number, row in export.items():
        if row["Overall trial status"] != "Ended":
            continue
        result.ended += 1
        paediatric = due_date.is_paediatric(row["Age group"], ct_number in with_pip)
        end_date = due_date.parse_date(row["End date"])
        global_end_date = due_date.parse_date(row["Global end of the trial"])
        completion = due_date.completion_date(end_date, global_end_date)
        if due_date.is_due(completion, paediatric, SNAPSHOT_DAY):
            result.due_ours.add(ct_number)
            ours[ct_number] = (paediatric, completion)

    every_trial = set()
    for ct_number in result.due_ours:
        every_trial.add(ct_number)
    for ct_number in result.due_theirs:
        every_trial.add(ct_number)
    for ct_number in sorted(every_trial):
        result.check(ct_number, "due", ct_number in result.due_ours, ct_number in result.due_theirs)
        # The finer fields can only be compared where both sides have a verdict for the trial
        if ct_number not in ours or ct_number not in theirs:
            continue
        paediatric = ours[ct_number][0]
        completion = ours[ct_number][1]
        verdict = theirs[ct_number]
        document = reviewed.get(ct_number)
        result.check(ct_number, "paediatric", paediatric, verdict.regulatory_status == "paediatric")
        result.check(ct_number, "completion date", completion.isoformat(), verdict.completion_date)
        days = None
        cancelled = False
        if document is not None:
            days = (document.posted_on - completion).days
            cancelled = document.cancelled
        if verdict.days_to_results is not None:
            result.check(ct_number, "days to results", days, verdict.days_to_results)
        reported_fully = verdict.results_document == "full" and verdict.lay_summary
        our_verdict = due_date.verdict(
            cancelled=cancelled,
            extended=verdict.extended,
            reported_fully=reported_fully,
            days_to_results=days,
            paediatric=paediatric,
        )
        result.check(ct_number, "verdict", our_verdict, verdict.verdict)
    return result
