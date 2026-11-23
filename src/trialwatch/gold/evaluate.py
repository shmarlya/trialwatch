"""A gold evaluation (ADR-001, ADR-003): our whole workflow on a gold dataset's trials (transfer their
evidence from CTIS, run the evidence check, score the run against the labels) and fixed gates that
decide whether it passes. The two pipelines in .github/workflows/ run it: eval-full.yml on every
labelled item, eval-short.yml on the hardest 50 (evals/hard-50.txt, from `trialwatch gold sample`).

The gates (evals/gates.json), all shares of the labelled items in the sample:
- min_label_agreement: of the items where ours is "full" or "not full", the share that agrees
- max_false_accusations: ours "not full" where the gold label is "full" (a count)
- min_decided: of the compared items, the share ours did not leave "undecided"
- max_missing: the share we could not compare: not transferred, not checked, or the check failed
Items set aside (we read a document posted after the reviewers' day) count for none of them.

The report also measures retrieval (no gate): the gold labels don't say on which page an answer is, so
a search miss is seen where it shows: a question asked again with every page whose verified quote is
on a page the first call did not show.
"""

import json
from dataclasses import dataclass, replace
from pathlib import Path

import psycopg

from trialwatch.checks.requirements import meaning_of
from trialwatch.db import checks as checks_db
from trialwatch.gold import compare
from trialwatch.gold.datasets import Dataset, Item

EVALS_DIR = Path(__file__).parent.parent.parent.parent / "evals"
GATES_FILE = EVALS_DIR / "gates.json"


# --- the hardest items ----------------------------------------------------------------------------

def _mentions_termination(item: Item) -> bool:
    texts = [item.comment or "", str(item.details.get("trial status") or "")]
    for review in item.reviews:
        texts.append(review.comment or "")
    return "terminat" in " ".join(texts).lower()


def hardness(item: Item) -> int:
    """How hard the item is likely to be for our check: reviewers disagreed (3), the team discussed it
    (2), the trial was terminated (2), a reviewer asked for it to be checked again (1), a part the
    reviewers left unsettled or a trial that enrolled nobody (1), a comment on it (1)."""
    score = 0
    if item.reviewers_agreed is False:
        score += 3
    if item.details.get("team discussion"):
        score += 2
    if _mentions_termination(item):
        score += 2
    for review in item.reviews:
        if review.asked_to_check:
            score += 1
            break
    for value in item.expected_parts.values():
        if value is None:
            score += 1
            break
    if item.expected_parts.get("cancelled") is True:
        score += 1
    if item.comment:
        score += 1
    return score


def hardest(dataset: Dataset, count: int, min_per_label: int) -> list[Item]:
    """The `count` hardest labelled items, at least `min_per_label` of each label, hardest first (ties
    by key, so the same dataset always gives the same sample)."""
    labelled = []
    for item in dataset.items:
        if item.expected_label is not None:
            labelled.append(item)
    labelled.sort(key=lambda item: (-hardness(item), item.key))
    chosen = []
    for label in dataset.labels:
        taken = 0
        for item in labelled:
            if taken >= min_per_label:
                break
            if item.expected_label == label and item not in chosen:
                chosen.append(item)
                taken += 1
    for item in labelled:
        if len(chosen) >= count:
            break
        if item not in chosen:
            chosen.append(item)
    chosen.sort(key=lambda item: (-hardness(item), item.key))
    return chosen[:count]


def ct_number_of(item: Item) -> str | None:
    for identifier in item.identifiers:
        if identifier.scheme == "eu-ct":
            return identifier.value
    return None


def only_items(dataset: Dataset, keys: list[str]) -> Dataset:
    """The dataset with only these items (by key); KeyError names keys it does not have."""
    known = {}
    for item in dataset.items:
        known[item.key] = item
    missing = []
    for key in keys:
        if key not in known:
            missing.append(key)
    if missing:
        raise KeyError(f"not in {dataset.ref}: {', '.join(missing)}")
    kept = []
    for key in keys:
        kept.append(known[key])
    return replace(dataset, items=kept)


# --- the gates -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Gates:
    min_label_agreement: float
    max_false_accusations: int
    min_decided: float
    max_missing: float


def load_gates(path: Path = GATES_FILE) -> Gates:
    data = json.loads(path.read_text())
    return Gates(float(data["min_label_agreement"]), int(data["max_false_accusations"]),
                 float(data["min_decided"]), float(data["max_missing"]))


@dataclass(frozen=True)
class Measures:
    labelled: int
    compared: int
    decided: int
    agreed: int
    false_accusations: int
    missing: int
    set_aside: int

    @property
    def label_agreement(self) -> float:
        return self.agreed / self.decided if self.decided else 0.0

    @property
    def decided_share(self) -> float:
        return self.decided / self.compared if self.compared else 0.0

    @property
    def missing_share(self) -> float:
        return self.missing / self.labelled if self.labelled else 0.0


def measures(scored: compare.Scored) -> Measures:
    decided = 0
    agreed = 0
    for (ours, theirs), items in scored.labels.items():
        # decided: the label is an accusation or clears the trial, not left open
        if meaning_of(scored.label_rules, ours) in ("accusation", "clears"):
            decided += items
            if ours == theirs:
                agreed += items
    missing = len(scored.not_in_run) + len(scored.errors)
    return Measures(scored.labelled, scored.compared, decided, agreed, len(scored.false_accusations), missing,
                    len(scored.set_aside))


def failures(found: Measures, gates: Gates) -> list[str]:
    """Each gate the evaluation missed, as a sentence; empty when it passes."""
    failed = []
    if found.label_agreement < gates.min_label_agreement:
        failed.append(f"label agreement {found.label_agreement:.1%} ({found.agreed}/{found.decided}) is below "
                      f"{gates.min_label_agreement:.0%}")
    if found.false_accusations > gates.max_false_accusations:
        failed.append(f"{found.false_accusations} false accusations (at most {gates.max_false_accusations})")
    if found.decided_share < gates.min_decided:
        failed.append(f"decided {found.decided_share:.1%} ({found.decided}/{found.compared}) is below "
                      f"{gates.min_decided:.0%}")
    if found.missing_share > gates.max_missing:
        failed.append(f"missing {found.missing_share:.1%} ({found.missing}/{found.labelled}) is above "
                      f"{gates.max_missing:.0%}")
    return failed


@dataclass
class RetrievalMeasures:
    # trials whose documents went to the model whole (few pages, or the files method): nothing searched
    trials_whole: int = 0
    # trials whose pages were searched per question
    trials_searched: int = 0
    # questions of the searched trials
    questions: int = 0
    # ... answered with a verified quote from the pages the search showed first
    found_first: int = 0
    # ... asked again with every page
    widened: int = 0
    # ... of them, then found with a verified quote on a page the first call did not show: search misses
    missed_by_search: int = 0
    # ... of them, found nowhere in the documents
    not_found: int = 0

    @property
    def search_hit_rate(self) -> float | None:
        """Of the verified quotes in searched trials, the share on a page the search showed first."""
        found = self.found_first + self.missed_by_search
        if found == 0:
            return None
        return self.found_first / found

    def line(self) -> str:
        rate = "—" if self.search_hit_rate is None else f"{self.search_hit_rate:.1%}"
        return (f"retrieval: {self.trials_searched} trials searched, {self.trials_whole} sent whole · "
                f"{self.questions} questions searched: {self.found_first} answered from the pages the search "
                f"showed, {self.widened} asked again with every page → {self.missed_by_search} found on a page "
                f"the search missed, {self.not_found} not found anywhere · search hit rate {rate}")


def retrieval_measures(conn: psycopg.Connection, run_id: int) -> RetrievalMeasures:
    """How retrieval did in the run, from its traces."""
    found = RetrievalMeasures()
    trials = {}
    for row in checks_db.retrieval_rows(conn, run_id):
        trials[row.trial_id] = row.whole
        if row.whole is not False:
            continue
        found.questions += 1
        verified = bool(row.found) and bool(row.verified) and row.file_sha256 is not None
        if not row.widened:
            if verified:
                found.found_first += 1
            continue
        found.widened += 1
        if not verified:
            if row.found is False:
                found.not_found += 1
            continue
        shown = set()
        for ref in row.first_pages or []:
            shown.add((ref["file"], ref["page"]))
        if (row.file_sha256.hex(), row.page_no) in shown:
            found.found_first += 1
        else:
            found.missed_by_search += 1
    for whole in trials.values():
        if whole is False:
            found.trials_searched += 1
        else:
            found.trials_whole += 1
    return found


def report_markdown(title: str, scored: compare.Scored, gates: Gates, run_summary: str,
                    retrieval: RetrievalMeasures | None = None) -> str:
    """The evaluation as Markdown, for the pipeline's summary page and its saved report."""
    found = measures(scored)
    failed = failures(found, gates)
    lines = [f"# {title}", ""]
    if failed:
        lines.append("**FAILED**")
        for reason in failed:
            lines.append(f"- {reason}")
    else:
        lines.append("**PASSED** every gate")
    lines += ["", "| Measure | Value | Gate |", "|---|---|---|",
              f"| label agreement | {found.label_agreement:.1%} ({found.agreed}/{found.decided}) | ≥ {gates.min_label_agreement:.0%} |",
              f"| false accusations | {found.false_accusations} | ≤ {gates.max_false_accusations} |",
              f"| decided | {found.decided_share:.1%} ({found.decided}/{found.compared}) | ≥ {gates.min_decided:.0%} |",
              f"| missing | {found.missing_share:.1%} ({found.missing}/{found.labelled}) | ≤ {gates.max_missing:.0%} |",
              f"| set aside | {found.set_aside} | — |"]
    if retrieval is not None:
        rate = "—" if retrieval.search_hit_rate is None else f"{retrieval.search_hit_rate:.1%}"
        lines += [f"| search hit rate (retrieval) | {rate} ({retrieval.missed_by_search} missed by the search) | — |"]
    lines += ["", "```", run_summary]
    if retrieval is not None:
        lines.append(retrieval.line())
    lines += compare.report_lines(scored)
    lines += ["```", "", "## Disagreements", "", "```"]
    lines += compare.disagreement_lines(scored)
    lines += ["```", ""]
    return "\n".join(lines)
