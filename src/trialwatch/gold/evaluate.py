"""A gold evaluation (ADR-001, ADR-003): our whole workflow on a gold dataset's trials (transfer their
evidence from CTIS, run the evidence check, score the run against the labels) and fixed gates that
decide whether it passes. The two pipelines in .github/workflows/ run it: eval-full.yml on every
labelled item, eval-short.yml on the hardest 50 (evals/hard-50.txt, from `trialwatch gold sample`).

The gates (evals/gates/full.json, evals/gates/short.json); a gate left out is not checked:
- min_label_agreement: of the items where ours is "full" or "not full", the share that agrees
- max_false_accusations: ours "not full" where the gold label is "full" (a count)
- max_clear_false_accusations: of them, those no reviewer's own reading supports
- min_decided: of the compared items, the share ours did not leave "undecided"
- max_missing: the share we could not compare: not transferred, not checked, or the check failed
- max_unstable: items whose label differs between repeated runs (--repeat)
- max_contradicting: of them, items "not full" in one run and "full" in another
- baseline, max_drop: no more than max_drop items worse than the accepted baseline, in items that
  disagree with the gold label and in items decided
Items set aside (we read a document posted after the reviewers' day) count for none of them. With
repeated runs, shares are the runs' median and accusations the worst run's.

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

REPO_DIR = Path(__file__).parent.parent.parent.parent
EVALS_DIR = REPO_DIR / "evals"
GATES_DIR = EVALS_DIR / "gates"
# the full pipeline's gates; the short one passes evals/gates/short.json
GATES_FILE = GATES_DIR / "full.json"


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
    """What an evaluation must reach. A gate left out of the file (None) is not checked."""

    min_label_agreement: float | None = None
    max_false_accusations: int | None = None
    # false accusations no reviewer's own reading supports (compare.py)
    max_clear_false_accusations: int | None = None
    min_decided: float | None = None
    max_missing: float | None = None
    # items whose label differs between repeated runs (--repeat)
    max_unstable: int | None = None
    # of them, items that were an accusation in one run and cleared in another: the flip that could
    # publish a wrong accusation
    max_contradicting: int | None = None
    # a baseline file (relative to the repository): the run may not be worse than it by more than
    # max_drop items, in disagreements or in decided items
    baseline: Path | None = None
    max_drop: int = 0


def load_gates(path: Path = GATES_FILE) -> Gates:
    data = json.loads(path.read_text())
    known = {"description", "min_label_agreement", "max_false_accusations", "max_clear_false_accusations",
             "min_decided", "max_missing", "max_unstable", "max_contradicting", "baseline", "max_drop"}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{path}: unknown gates {', '.join(sorted(unknown))}")

    def number(name, kind):
        if data.get(name) is None:
            return None
        return kind(data[name])

    baseline = None
    if data.get("baseline"):
        baseline = REPO_DIR / data["baseline"]
    return Gates(min_label_agreement=number("min_label_agreement", float),
                 max_false_accusations=number("max_false_accusations", int),
                 max_clear_false_accusations=number("max_clear_false_accusations", int),
                 min_decided=number("min_decided", float), max_missing=number("max_missing", float),
                 max_unstable=number("max_unstable", int), max_contradicting=number("max_contradicting", int),
                 baseline=baseline, max_drop=int(data.get("max_drop", 0)))


@dataclass(frozen=True)
class Measures:
    labelled: int
    compared: int
    decided: int
    agreed: int
    false_accusations: int
    missing: int
    set_aside: int
    clear_false_accusations: int = 0

    @property
    def disagreed(self) -> int:
        return self.decided - self.agreed

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
    clear = len(scored.false_accusations) - len(scored.contested_accusations)
    return Measures(scored.labelled, scored.compared, decided, agreed, len(scored.false_accusations), missing,
                    len(scored.set_aside), clear)


@dataclass(frozen=True)
class Baseline:
    """The measures of the last accepted evaluation of a sample (median over its repeats)."""

    decided: int
    disagreed: int
    compared: int
    runs: int
    recorded_at: str


def read_baseline(path: Path) -> Baseline | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    return Baseline(int(data["decided"]), int(data["disagreed"]), int(data["compared"]), int(data["runs"]),
                    str(data["recorded_at"]))


def baseline_data(runs: list[Measures], about: dict) -> dict:
    """What --write-baseline stores: the median decided and disagreed counts of the runs."""
    from datetime import UTC, datetime

    data = {"description": "The measures of the last accepted gold evaluation of this sample (median over its "
                           "repeats); gates with a baseline fail when a run is worse by more than max_drop items",
            **about,
            "runs": len(runs),
            "compared": _median([run.compared for run in runs]),
            "decided": _median([run.decided for run in runs]),
            "disagreed": _median([run.disagreed for run in runs]),
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds")}
    return data


def _median(values: list):
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def unstable_items(scores: list[compare.Scored]) -> dict[str, list[str]]:
    """Item key -> its label in each run, for the items whose label was not the same in every run that
    compared them (repeated runs on the same evidence)."""
    labels: dict[str, list[str]] = {}
    for scored in scores:
        for key, label in scored.item_labels.items():
            labels.setdefault(key, []).append(label)
    unstable = {}
    for key, found in labels.items():
        if len(set(found)) > 1:
            unstable[key] = found
    return unstable


def contradicting_items(unstable: dict[str, list[str]], scores: list[compare.Scored]) -> list[str]:
    """The unstable items that were an accusation in one run and cleared the trial in another."""
    if not scores:
        return []
    rules = scores[0].label_rules
    found = []
    for key, labels in unstable.items():
        meanings = {meaning_of(rules, label) for label in labels}
        if "accusation" in meanings and "clears" in meanings:
            found.append(key)
    return sorted(found)


def failures(found: Measures | list[Measures], gates: Gates, unstable: int = 0,
             baseline: Baseline | None = None, contradicting: int = 0) -> list[str]:
    """Each gate the evaluation missed, as a sentence; empty when it passes. With repeated runs, the
    shares are each run's median and the accusations the worst run's."""
    runs = found if isinstance(found, list) else [found]
    failed = []
    agreement = _median([run.label_agreement for run in runs])
    decided = _median([run.decided_share for run in runs])
    missing = _median([run.missing_share for run in runs])
    accusations = max(run.false_accusations for run in runs)
    clear = max(run.clear_false_accusations for run in runs)
    one = runs[0] if len(runs) == 1 else None
    if gates.min_label_agreement is not None and agreement < gates.min_label_agreement:
        counts = f" ({one.agreed}/{one.decided})" if one else " (median of the runs)"
        failed.append(f"label agreement {agreement:.1%}{counts} is below {gates.min_label_agreement:.0%}")
    if gates.max_false_accusations is not None and accusations > gates.max_false_accusations:
        failed.append(f"{accusations} false accusations (at most {gates.max_false_accusations})")
    if gates.max_clear_false_accusations is not None and clear > gates.max_clear_false_accusations:
        failed.append(f"{clear} clear false accusations, which no reviewer's reading supports "
                      f"(at most {gates.max_clear_false_accusations})")
    if gates.min_decided is not None and decided < gates.min_decided:
        counts = f" ({one.decided}/{one.compared})" if one else " (median of the runs)"
        failed.append(f"decided {decided:.1%}{counts} is below {gates.min_decided:.0%}")
    if gates.max_missing is not None and missing > gates.max_missing:
        counts = f" ({one.missing}/{one.labelled})" if one else " (median of the runs)"
        failed.append(f"missing {missing:.1%}{counts} is above {gates.max_missing:.0%}")
    if gates.max_unstable is not None and unstable > gates.max_unstable:
        failed.append(f"{unstable} items changed label between the runs (at most {gates.max_unstable})")
    if gates.max_contradicting is not None and contradicting > gates.max_contradicting:
        failed.append(f"{contradicting} items were \"not full\" in one run and \"full\" in another "
                      f"(at most {gates.max_contradicting})")
    if gates.baseline is not None:
        if baseline is None:
            failed.append(f"no baseline at {gates.baseline}: record an accepted run with --write-baseline")
        else:
            disagreed = _median([run.disagreed for run in runs])
            decided_items = _median([run.decided for run in runs])
            if disagreed > baseline.disagreed + gates.max_drop:
                failed.append(f"{disagreed} items disagree with the gold label, the baseline had {baseline.disagreed} "
                              f"(at most {gates.max_drop} more)")
            if decided_items < baseline.decided - gates.max_drop:
                failed.append(f"{decided_items} items decided, the baseline had {baseline.decided} "
                              f"(at most {gates.max_drop} fewer)")
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


@dataclass(frozen=True)
class RunResult:
    """One run of a gold evaluation: its score, its summary line and how retrieval did."""

    scored: compare.Scored
    summary: str
    retrieval: RetrievalMeasures | None = None


def _count_gate(value: int | None) -> str:
    return "—" if value is None else f"≤ {value}"


def _share_gate(sign: str, value: float | None) -> str:
    return "—" if value is None else f"{sign} {value:.0%}"


def _spread(values: list, shown) -> str:
    """One run: its value; several: the median and every run's value."""
    if len(values) == 1:
        return shown(values[0])
    each = ", ".join(shown(value) for value in values)
    return f"{shown(_median(values))} (runs: {each})"


def report_markdown(title: str, results: RunResult | list[RunResult], gates: Gates,
                    baseline: Baseline | None = None) -> str:
    """The evaluation as Markdown, for the pipeline's summary page and its saved report: the gates, the
    measures (median and each run's, when repeated), the items that changed label between runs, and
    for each run its disagreements and undecided items with every question's answer and rule."""
    if isinstance(results, RunResult):
        results = [results]
    runs = [measures(result.scored) for result in results]
    scores = [result.scored for result in results]
    unstable = unstable_items(scores)
    contradicting = contradicting_items(unstable, scores)
    failed = failures(runs, gates, len(unstable), baseline, len(contradicting))
    lines = [f"# {title}", ""]
    if failed:
        lines.append("**FAILED**")
        for reason in failed:
            lines.append(f"- {reason}")
    else:
        lines.append("**PASSED** every gate")
    one = runs[0] if len(runs) == 1 else None

    def counted(share: str, top: int, bottom: int) -> str:
        return f"{share} ({top}/{bottom})" if one else share

    lines += ["", "| Measure | Value | Gate |", "|---|---|---|"]
    agreement = _spread([run.label_agreement for run in runs], lambda v: f"{v:.1%}")
    if one:
        agreement = counted(agreement, one.agreed, one.decided)
    decided = _spread([run.decided_share for run in runs], lambda v: f"{v:.1%}")
    if one:
        decided = counted(decided, one.decided, one.compared)
    missing = _spread([run.missing_share for run in runs], lambda v: f"{v:.1%}")
    if one:
        missing = counted(missing, one.missing, one.labelled)
    lines += [f"| label agreement | {agreement} | {_share_gate('≥', gates.min_label_agreement)} |",
              f"| false accusations | {_spread([run.false_accusations for run in runs], str)} | "
              f"{_count_gate(gates.max_false_accusations)} |",
              f"| clear false accusations | {_spread([run.clear_false_accusations for run in runs], str)} | "
              f"{_count_gate(gates.max_clear_false_accusations)} |",
              f"| decided | {decided} | {_share_gate('≥', gates.min_decided)} |",
              f"| missing | {missing} | {_share_gate('≤', gates.max_missing)} |"]
    if gates.baseline is not None:
        base = "none" if baseline is None else str(baseline.disagreed)
        lines.append(f"| items disagreeing with the gold label | {_spread([run.disagreed for run in runs], str)} | "
                     f"≤ baseline {base} + {gates.max_drop} |")
        base = "none" if baseline is None else str(baseline.decided)
        lines.append(f"| items decided | {_spread([run.decided for run in runs], str)} | "
                     f"≥ baseline {base} − {gates.max_drop} |")
    if len(runs) > 1:
        lines.append(f"| items that changed label between runs | {len(unstable)} | "
                     f"{_count_gate(gates.max_unstable)} |")
        lines.append(f"| … of them between not full and full | {len(contradicting)} | "
                     f"{_count_gate(gates.max_contradicting)} |")
    lines.append(f"| set aside | {_spread([run.set_aside for run in runs], str)} | — |")
    retrievals = [result.retrieval for result in results if result.retrieval is not None]
    if retrievals:
        rates = [retrieval.search_hit_rate for retrieval in retrievals]
        missed = [retrieval.missed_by_search for retrieval in retrievals]
        shown = _spread(rates, lambda v: "—" if v is None else f"{v:.1%}") if None not in rates or len(rates) == 1 \
            else ", ".join("—" if v is None else f"{v:.1%}" for v in rates)
        lines.append(f"| search hit rate (retrieval) | {shown}; missed by the search {_spread(missed, str)} | — |")
    if unstable:
        lines += ["", "## Items that changed label between runs", "", "```"]
        for key, labels in sorted(unstable.items()):
            flag = " ← not full and full" if key in contradicting else ""
            lines.append(f"  {key}: {' · '.join(labels)} (theirs {results[0].scored.expected_labels.get(key)}){flag}")
        lines += ["```"]
    for number, result in enumerate(results, start=1):
        heading = f" · run {number} of {len(results)}" if len(results) > 1 else ""
        lines += ["", f"## Summary{heading}", "", "```", result.summary]
        if result.retrieval is not None:
            lines.append(result.retrieval.line())
        lines += compare.report_lines(result.scored)
        lines += ["```", "", f"## Disagreements{heading}", "", "```"]
        lines += compare.disagreement_lines(result.scored)
        lines += ["```", "", f"## Undecided{heading}", "", "```"]
        lines += compare.undecided_lines(result.scored)
        lines += ["```"]
    lines.append("")
    return "\n".join(lines)
