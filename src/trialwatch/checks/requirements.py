"""Requirement sets (ADR-004): what a trial must report, and how "enough" is decided, as data.

A requirement set is a JSON file in src/trialwatch/checks/requirement_sets/. It names the document
kinds to read and lists requirements. A requirement is one question, or one per item of the trial's
registered information (e.g. one per primary endpoint: "for_each": "primary_endpoints", with the
item's fields in its key and question templates). Each requirement names a rule type from the small
vocabulary below and the part of the judgement it counts towards. Parts combine into a label.

The code knows no registry, regulation or document type: those are in the files. The rule
vocabulary is generic:

- count_stated: a verified quote stating a count of people above or at zero (dates, years, ages,
  doses and numbered labels such as "Table 2" are not counts);
- statement_present: a verified quote;
- numeric_result: a verified quote of kind numeric_result; a verified quote of a "not reported" kind
  (the set's not_reported_kinds) seen in the whole document means not reported.

For every rule, "not found" means not reported only when the model read the whole document (every
page, each with readable text); otherwise it is undecided, for a person, never a guess. The model
may also answer "I don't know" to any question; that is undecided too, whatever it read.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

RULE_TYPES = ("count_stated", "statement_present", "numeric_result")
INFERENCE_TYPES = ("nobody_enrolled_contradicted_by_count",)


@dataclass(frozen=True)
class Requirement:
    key: str
    part: str
    question: str
    rule: str
    guidance: str = ""
    search: tuple[str, ...] = ()
    # for one-per-item requirements: which list of the registered information, and the item field
    # whose text is searched for
    for_each: str | None = None
    search_from: str | None = None


@dataclass(frozen=True)
class RequirementSet:
    name: str
    title: str
    description: str
    document_kinds: tuple[str, ...]
    not_reported_kinds: frozenset[str]
    requirements: tuple[Requirement, ...]
    parts: dict
    inferences: tuple[dict, ...]
    label: dict
    # the file as read, and its sha256: stored with every run that uses it
    text: str
    sha256: bytes


@dataclass(frozen=True)
class Question:
    """One requirement as asked about one trial."""

    key: str
    part: str
    text: str
    rule: str
    guidance: str = ""
    search: tuple[str, ...] = ()
    # the registered item the question is about (e.g. the endpoint's text), if any
    item_text: str | None = None


def set_names() -> list[str]:
    """Every requirement set file, by name, sorted."""
    names = []
    for set_file in files("trialwatch.checks.requirement_sets").iterdir():
        if set_file.name.endswith(".json"):
            names.append(set_file.name.removesuffix(".json"))
    return sorted(names)


def load(name: str) -> RequirementSet:
    """Read and check a requirement set; raises FileNotFoundError or ValueError. Only the files of
    checks/requirement_sets/ are sets: a name is never a path (it may come from a form)."""
    if name not in set_names():
        raise FileNotFoundError(f"no requirement set {name!r} in checks/requirement_sets/")
    set_file = files("trialwatch.checks.requirement_sets").joinpath(f"{name}.json")
    text = set_file.read_text()
    data = json.loads(text)
    requirements = []
    for entry in data["requirements"]:
        if entry["rule"] not in RULE_TYPES:
            raise ValueError(f"{name}: unknown rule {entry['rule']!r} (known: {', '.join(RULE_TYPES)})")
        if entry["part"] not in data["parts"]:
            raise ValueError(f"{name}: requirement {entry['key']!r} counts towards an unknown part {entry['part']!r}")
        search = []
        for word in entry.get("search", []):
            search.append(word)
        requirements.append(Requirement(
            key=entry["key"], part=entry["part"], question=entry["question"], rule=entry["rule"],
            guidance=entry.get("guidance", ""), search=tuple(search), for_each=entry.get("for_each"),
            search_from=entry.get("search_from"),
        ))
    _check_set(name, data, requirements)
    inferences = []
    for inference in data.get("inferences", []):
        inferences.append(inference)
    kinds = []
    for kind in data["document_kinds"]:
        kinds.append(kind)
    return RequirementSet(
        name=data["name"], title=data["title"], description=data["description"], document_kinds=tuple(kinds),
        not_reported_kinds=frozenset(data["not_reported_kinds"]), requirements=tuple(requirements),
        parts=data["parts"], inferences=tuple(inferences), label=data["label"], text=text,
        sha256=hashlib.sha256(text.encode()).digest(),
    )


def _check_set(name: str, data: dict, requirements: list[Requirement]) -> None:
    """ValueError for what the rules would otherwise ignore silently: a set named unlike its file (runs
    and gold datasets find it by name), a way of combining a part's questions other than "all" (the
    only one the rules apply), and a part or requirement a label condition or inference names that
    doesn't exist."""
    if data["name"] != name:
        raise ValueError(f"{name}.json says its name is {data['name']!r}: a set's name is its file's name")
    for part, settings in data["parts"].items():
        combine = settings.get("combine", "all")
        if combine != "all":
            raise ValueError(f"{name}: part {part!r} combines its questions by {combine!r}; only 'all' is supported")
    for condition in ("not_full_when", "full_when"):
        for value in ("true", "false"):
            for part in data["label"][condition].get(value, []):
                if part not in data["parts"]:
                    raise ValueError(f"{name}: label.{condition} names an unknown part {part!r}")
    fixed_keys = []
    for requirement in requirements:
        if requirement.for_each is None:
            fixed_keys.append(requirement.key)
    for inference in data.get("inferences", []):
        if inference["type"] not in INFERENCE_TYPES:
            raise ValueError(f"{name}: unknown inference {inference['type']!r}")
        if inference["part"] not in data["parts"]:
            raise ValueError(f"{name}: inference names an unknown part {inference['part']!r}")
        if inference["count_requirement"] not in fixed_keys:
            raise ValueError(f"{name}: inference names an unknown requirement {inference['count_requirement']!r}")


# --- questions ---------------------------------------------------------------------------------

def _filled(template: str, item: dict) -> str:
    """A template with {field} replaced by the item's values (missing fields stay as they are)."""
    filled = template
    for name, value in item.items():
        filled = filled.replace("{" + name + "}", str(value))
    return filled


def _item_order(item: dict) -> tuple[int, str]:
    """Items in their registered order (by number; missing counts as 0), then by id."""
    number = item.get("number")
    if not isinstance(number, int):
        number = 0
    return (number, str(item.get("id")))


def questions_for(requirement_set: RequirementSet, registered: dict) -> list[Question]:
    """The questions for one trial: each fixed requirement once, each one-per-item requirement once
    for every item of the trial's registered information (e.g. every primary endpoint)."""
    questions = []
    for requirement in requirement_set.requirements:
        if requirement.for_each is None:
            questions.append(Question(requirement.key, requirement.part, requirement.question, requirement.rule,
                                      requirement.guidance, requirement.search))
            continue
        items = registered.get(requirement.for_each)
        if not isinstance(items, list):
            continue
        usable = []
        for item in items:
            if isinstance(item, dict):
                usable.append(item)
        for item in sorted(usable, key=_item_order):
            # collapse the registry's line breaks and repeated spaces into single spaces
            cleaned = {}
            for name, value in item.items():
                if isinstance(value, str):
                    cleaned[name] = " ".join(value.split())
                else:
                    cleaned[name] = value
            item_text = None
            if requirement.search_from:
                item_text = cleaned.get(requirement.search_from)
            questions.append(Question(_filled(requirement.key, cleaned), requirement.part,
                                      _filled(requirement.question, cleaned), requirement.rule,
                                      _filled(requirement.guidance, cleaned), requirement.search, item_text))
    return questions


# --- rules -------------------------------------------------------------------------------------

MONTHS = ("jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|janvier|février|mars|avril|mai|juin|juillet|août|"
          "januar|februar|märz|juni|juli|oktober|dezember|enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
          "gennaio|febbraio|aprile|maggio|giugno|luglio")
# Numbers in a quote that are not counts of people: dates, years, ages, doses and numbered labels
NOT_A_COUNT = (
    re.compile(r"\b\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\b"),
    re.compile(rf"\b\d{{1,2}}\.?\s+(?:{MONTHS})\w*\.?\s+\d{{2,4}}\b", re.IGNORECASE),
    re.compile(rf"\b(?:{MONTHS})\w*\.?\s+\d{{1,2}},?\s+\d{{4}}\b", re.IGNORECASE),
    # a year, unless it is followed by the people it counts ("2014 patients")
    re.compile(r"\b(?:19|20)\d{2}\b(?!\s*(?:participants|patients|subjects|persons|people|volunteers|children|"
               r"adults|women|men|infants))", re.IGNORECASE),
    re.compile(r"\b(?:table|figure|fig|section|part|phase|week|day|month|visit|cycle|cohort|arm|page|version|"
               r"study|protocol|amendment|appendix|listing|step|stage|grade|level)\.?\s*[a-z]?\d+(?:[.-]\d+)*",
               re.IGNORECASE),
    re.compile(r"[<>≤≥]=?\s*\d+(?:[.,]\d+)?\s*(?:years?|yrs?|y)\b", re.IGNORECASE),
    re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:years?|yrs?|months?|weeks?|days?|hours?|mg|kg|ml|%)", re.IGNORECASE),
)


# Identifiers and codes are never counts: trial register numbers (2015-001234-56, NCT01234567,
# ISRCTN12345678), protocol and product codes such as ABC-123 or D7000C00001, labels such as Q1
IDENTIFIERS = (
    re.compile(r"\b\d{4}-\d{6}-\d{2}(?:-\d{2})?\b"),
    re.compile(r"\b(?:NCT|ISRCTN)\s*\d+\b", re.IGNORECASE),
    re.compile(r"\b[^\W\d_]+[-_/]?\d+[\w-]*"),
)
# The people a count counts, or the verb that says they were counted
PEOPLE = (r"(?:participants?|subjects?|patients?|people|persons?|volunteers?|children|adults?|women|men|infants?|"
          r"individuals|enrolled|randomi[sz]ed)")
NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+)"
# "168 patients", "12 healthy adult volunteers", "0 subjects"
COUNT_BEFORE_PEOPLE = re.compile(rf"(?<![\w.,]){NUMBER}\s+(?:[^\W\d_]+[\s-]+){{0,3}}?{PEOPLE}\b", re.IGNORECASE)
# "n = 168", "(N=12)"
COUNT_AFTER_N = re.compile(rf"\b[nN]\s*=\s*{NUMBER}")
# "Number of subjects enrolled: 168"
COUNT_AFTER_LABEL = re.compile(rf"\b{PEOPLE}\b[^.\d:]{{0,40}}:\s*{NUMBER}", re.IGNORECASE)


def participant_numbers(text: str) -> list[int]:
    """The counts of people a quote states: a number tied to people ("168 patients", "n = 168",
    "Number of subjects enrolled: 168"). Dates, years, ages, doses, numbered labels ("Table 2",
    "Week 12") and identifiers are taken out first, and a number on its own never counts: "aged 18 to
    65" states no count."""
    remaining = text
    for pattern in NOT_A_COUNT:
        remaining = pattern.sub(" ", remaining)
    for pattern in IDENTIFIERS:
        remaining = pattern.sub(" ", remaining)
    numbers = []
    for pattern in (COUNT_BEFORE_PEOPLE, COUNT_AFTER_N, COUNT_AFTER_LABEL):
        for digits in pattern.findall(remaining):
            numbers.append(int(digits.replace(",", "")))
    return numbers


def some_number_above_zero(text: str) -> bool:
    """The quote gives a count above zero (e.g. "12 participants", not "0 participants")."""
    for number in participant_numbers(text):
        if number != 0:
            return True
    return False


def settle(trace: dict[str, dict], key: str, value: bool | None, rule: str) -> bool | None:
    """Record in the trace which rule gave `key` its value, and return the value."""
    trace[key] = {"value": value, "rule": rule}
    return value


def settle_question(requirement_set: RequirementSet, question: Question, item: Any,
                    trace: dict[str, dict]) -> bool | None:
    """The value of one question under its rule. `item` is its evidence (found: True, False or None
    for "I don't know"; verified, kind, quote, whole) or None when the model gave no answer."""
    key = question.key
    if item is None:
        return settle(trace, key, None, "no answer")
    if item.found and item.verified:
        if question.rule == "count_stated":
            if participant_numbers(item.quote):
                return settle(trace, key, True, "verified quote with a count")
            return settle(trace, key, None, "verified quote without a count")
        if question.rule == "statement_present":
            return settle(trace, key, True, "verified quote")
        if item.kind == "numeric_result":
            return settle(trace, key, True, "verified numeric result")
        if item.kind in requirement_set.not_reported_kinds:
            # the numeric result may be on a page the model was not shown (a retrieval miss)
            if item.whole:
                return settle(trace, key, False, f"verified {item.kind}")
            return settle(trace, key, None, f"verified {item.kind} in the retrieved pages only")
        return settle(trace, key, None, f"verified {item.kind}")
    if item.found is None:
        # "I don't know" is an answer of its own: for a person, never read as "not reported"
        return settle(trace, key, None, "the model doesn't know")
    if item.found:
        return settle(trace, key, None, "quote not on the cited page")
    # "not found" only means "not reported" if the model read every page (else a retrieval miss)
    if item.whole:
        return settle(trace, key, False, "not found in the whole document")
    return settle(trace, key, None, "not found in the retrieved pages")


def _combined(values: list[bool | None], empty_rule: str) -> tuple[bool | None, str]:
    """All of a part's questions: False if any is False, True if all are True, else None."""
    if not values:
        return None, empty_rule
    for value in values:
        if value is False:
            return False, "a requirement is not reported"
    for value in values:
        if value is not True:
            return None, "a requirement is undecided"
    return True, "every requirement reported"


def _matches(parts: dict[str, bool | None], condition: dict) -> bool:
    """Any part named under "false" is False, or any named under "true" is True."""
    for part in condition.get("false", []):
        if parts.get(part) is False:
            return True
    for part in condition.get("true", []):
        if parts.get(part) is True:
            return True
    return False


def _all_hold(parts: dict[str, bool | None], condition: dict) -> bool:
    """Every part named under "true" is True and every one named under "false" is False."""
    for part in condition.get("true", []):
        if parts.get(part) is not True:
            return False
    for part in condition.get("false", []):
        if parts.get(part) is not False:
            return False
    return True


def judge(requirement_set: RequirementSet, questions: list[Question], evidence: dict[str, Any]
          ) -> tuple[dict, dict]:
    """(parts and label, trace) for one trial: each question under its rule, parts combined, the
    set's inferences applied, then the label."""
    trace: dict[str, dict] = {}
    values_by_part: dict[str, list] = {}
    for part in requirement_set.parts:
        values_by_part[part] = []
    for question in questions:
        value = settle_question(requirement_set, question, evidence.get(question.key), trace)
        values_by_part[question.part].append(value)
    parts: dict[str, bool | None] = {}
    for part, settings in requirement_set.parts.items():
        values = values_by_part[part]
        # a part with one question takes that question's value and rule
        if len(values) == 1 and part in trace:
            parts[part] = values[0]
            continue
        value, rule = _combined(values, settings.get("empty", "no answer"))
        parts[part] = settle(trace, part, value, rule)
    for inference in requirement_set.inferences:
        if inference["type"] == "nobody_enrolled_contradicted_by_count":
            _nobody_enrolled_contradicted(inference, parts, evidence, trace, questions)
    if _matches(parts, requirement_set.label["not_full_when"]):
        label = "not full"
    elif _all_hold(parts, requirement_set.label["full_when"]):
        label = "full"
    else:
        label = "undecided"
    return {"parts": parts, "label": label}, trace


def _part_key(part: str, questions: list[Question]) -> str:
    """The trace key of a part: its name, or "part:<name>" when a question has the same key, so a part
    settled by an inference never hides what its question's own rule said."""
    for question in questions:
        if question.key == part:
            return f"part:{part}"
    return part


def _nobody_enrolled_contradicted(inference: dict, parts: dict[str, bool | None], evidence: dict[str, Any],
                                  trace: dict[str, dict], questions: list[Question]) -> None:
    """A trial that reports a count of people above zero did enrol someone: "nobody enrolled" is then
    False when nothing settled it, and undecided (for a person) when a quote said it was True."""
    count_key = inference["count_requirement"]
    part = inference["part"]
    trace_key = _part_key(part, questions)
    count_item = evidence.get(count_key)
    if trace.get(count_key, {}).get("value") is not True or count_item is None:
        return
    if not some_number_above_zero(count_item.quote):
        return
    if parts.get(part) is None:
        parts[part] = settle(trace, trace_key, False, f"{count_key} reported with a count above zero")
    elif parts.get(part) is True:
        # e.g. "No participants were enrolled in Phase 3" in a trial that enrolled 168 in Phase 2
        parts[part] = settle(trace, trace_key, None, f"verified quote conflicts with {count_key} above zero")
