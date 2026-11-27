"""Requirement sets (ADR-004): what a trial must report, and how "enough" is decided, as data.

A requirement set is a JSON file in src/trialwatch/checks/requirement_sets/. It names the document
kinds to read and lists requirements. A requirement is one question, or one per item of the trial's
registered information (e.g. one per primary endpoint: "for_each": "primary_endpoints", with the
item's fields in its key and question templates). Each requirement names a rule type from the small
vocabulary below and the part of the judgement it counts towards. Parts combine into a label.

The code knows no registry, regulation or document type: those are in the files. The rule
vocabulary is generic:

- count_stated: a verified quote stating a count of people above or at zero, also as a table gives it
  ("Subjects enrolled 168") or in words ("Three patients") (dates, years, ages,
  doses and numbered labels such as "Table 2" are not counts);
- statement_present: a verified quote;
- numeric_result: a verified quote of kind numeric_result, or of a kind the set counts as reported
  (reported_kinds, e.g. a partial result: data for some of an endpoint's components); a verified quote of a "not reported" kind
  (the set's not_reported_kinds) seen in the whole document means not reported; one of an "open" kind
  (open_kinds, e.g. a result of a stage of the trial that never started) leaves it to a person.

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
# What a quote is evidence of (the model's answer, checks/ask.py). Requirement sets say which of these
# besides numeric_result count as reported (reported_kinds), which mean "not reported"
# (not_reported_kinds) and which leave a question open for a person (open_kinds):
# not_conducted is a result that could not exist, e.g. of a stage of the trial that never started;
# partial_result gives data for some of an endpoint's components only.
KINDS = ("numeric_result", "narrative_only", "chart_only", "redacted", "not_analysed", "not_conducted",
         "partial_result", "other")
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


# What a label means next to a gold label (gold/datasets.py): the results are not reported in full (an
# accusation), they are (clears the trial), or nothing is decided (open: left to a person, not counted)
LABEL_MEANINGS = ("accusation", "clears", "open")


@dataclass(frozen=True)
class LabelRule:
    """A label a judgement can get. The set's labels are tried in order: the first whose condition
    holds is the judgement's; the last has no condition."""

    name: str
    # one of LABEL_MEANINGS
    means: str
    # "any": a part named under `false` is False or one under `true` is True; "all": every one is so;
    # None: always (the last label)
    when: str | None = None
    true: tuple[str, ...] = ()
    false: tuple[str, ...] = ()


def meaning_of(labels: tuple[LabelRule, ...], name: str | None) -> str | None:
    """What a label means (LABEL_MEANINGS), or None for no label or one the set doesn't have."""
    for rule in labels:
        if rule.name == name:
            return rule.means
    return None


@dataclass(frozen=True)
class RequirementSet:
    name: str
    title: str
    description: str
    document_kinds: tuple[str, ...]
    not_reported_kinds: frozenset[str]
    # kinds besides numeric_result that settle a numeric_result question as reported
    reported_kinds: frozenset[str]
    # kinds that leave a numeric_result question undecided, for a person: neither reported nor missing
    open_kinds: frozenset[str]
    requirements: tuple[Requirement, ...]
    parts: dict
    inferences: tuple[dict, ...]
    labels: tuple[LabelRule, ...]
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
    labels = []
    for entry in data["labels"]:
        when = None
        condition = {}
        if "when_any" in entry:
            when = "any"
            condition = entry["when_any"]
        if "when_all" in entry:
            when = "all"
            condition = entry["when_all"]
        labels.append(LabelRule(name=entry["name"], means=entry["means"], when=when,
                                true=tuple(condition.get("true", [])), false=tuple(condition.get("false", []))))
    return RequirementSet(
        name=data["name"], title=data["title"], description=data["description"], document_kinds=tuple(kinds),
        not_reported_kinds=frozenset(data["not_reported_kinds"]), open_kinds=frozenset(data.get("open_kinds", [])),
        reported_kinds=frozenset(data.get("reported_kinds", [])),
        requirements=tuple(requirements),
        parts=data["parts"], inferences=tuple(inferences), labels=tuple(labels), text=text,
        sha256=hashlib.sha256(text.encode()).digest(),
    )


def _check_set(name: str, data: dict, requirements: list[Requirement]) -> None:
    """ValueError for what the rules would otherwise ignore silently: a set named unlike its file (runs
    and gold datasets find it by name), a way of combining a part's questions other than "all" (the
    only one the rules apply), a part or requirement a label condition or inference names that
    doesn't exist, and an answer kind that doesn't exist or would mean both not reported and open."""
    if data["name"] != name:
        raise ValueError(f"{name}.json says its name is {data['name']!r}: a set's name is its file's name")
    for part, settings in data["parts"].items():
        combine = settings.get("combine", "all")
        if combine != "all":
            raise ValueError(f"{name}: part {part!r} combines its questions by {combine!r}; only 'all' is supported")
    _check_labels(name, data)
    meanings = {"reported_kinds": "reported", "not_reported_kinds": "not reported", "open_kinds": "open"}
    for field_name in meanings:
        for kind in data.get(field_name, []):
            if kind not in KINDS:
                raise ValueError(f"{name}: {field_name} names an unknown kind {kind!r} (known: {', '.join(KINDS)})")
    fields = list(meanings)
    for index, first in enumerate(fields):
        for second in fields[index + 1:]:
            both = set(data.get(first, [])) & set(data.get(second, []))
            if both:
                raise ValueError(f"{name}: {', '.join(sorted(both))} can't mean both {meanings[first]} and "
                                 f"{meanings[second]}")
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
    # a range of ages or other measures: "18-64 years", "2 to 11 years"
    re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:[-–]|to)\s*\d+(?:[.,]\d+)?\s*(?:years?|yrs?|months?|weeks?|days?|hours?|mg|kg|ml)\b",
               re.IGNORECASE),
    re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:years?|yrs?|months?|weeks?|days?|hours?|mg|kg|ml|%)", re.IGNORECASE),
)


# Identifiers and codes are never counts: trial register numbers (2015-001234-56, NCT01234567,
# ISRCTN12345678), protocol and product codes such as ABC-123 or D7000C00001, labels such as Q1
IDENTIFIERS = (
    re.compile(r"\b\d{4}-\d{6}-\d{2}(?:-\d{2})?\b"),
    re.compile(r"\b(?:NCT|ISRCTN)\s*\d+\b", re.IGNORECASE),
    re.compile(r"\b[^\W\d_]+[-_/]?\d+[\w-]*"),
)
# The people a count counts, or the verb or noun that says they were counted. Results documents come in
# the languages of the EU: English, and the words for participants, patients and subjects in the most
# common others (French, Spanish, Portuguese, Italian, German, Dutch, Danish, Swedish, Polish)
PEOPLE = (r"(?:participants?|subjects?|patients?|people|persons?|volunteers?|children|adults?|women|men|infants?|"
          r"individuals|enrolled|enrol(?:l)?ment|randomi[sz]ed|personnes|volontaires|vrijwilligers|"
          r"participantes?|pacientes?|sujetos?|sujets?|patientes?|partecipanti|pazienti|soggetti|"
          r"teilnehmer(?:innen)?|patienten|probanden|deelnemers|pati[eë]nten|proefpersonen|deltagere|deltagare|"
          r"patienter|fors[oø]gspersoner|uczestnik[oó]w|pacjent[oó]w)")
NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+)"
# "168 patients", "12 healthy adult volunteers", "20 healthy female and male participants", "0 subjects"
COUNT_BEFORE_PEOPLE = re.compile(rf"(?<![\w.,]){NUMBER}\)?\s+(?:[^\W\d_]+[\s-]+){{0,4}}?{PEOPLE}\b",
                                 re.IGNORECASE)
# "n = 168", "(N=12)", and a table's "n (%) 92 (100%)"
COUNT_AFTER_N = re.compile(rf"\b[nN]\s*(?:=|\(\s*%\s*\))\s*{NUMBER}")
# What a results table writes before a count of people, without naming them: "Analysed: 40", "Exposed in
# phase 1a: 21", "Actual number included worldwide 517", "Full Analysis Set 14", "Total: worldwide 31", and
# participant flow tables ("Started 149 148", "Entered: 15")
COUNT_LABEL = (r"(?:analy[sz]ed|treated|exposed|included|recruited|dosed|started|entered|analysis\s+set|"
               r"safety\s+set|worldwide)")
# A count after the people it counts, as tables in results summaries give it, with or without a colon:
# "Number of subjects enrolled: 168", "Subjects enrolled 168", "Actual enrolment: 166", "Patients
# treated, n 23". Not a number that ends in a unit, a decimal or a percentage.
COUNT_AFTER_LABEL = re.compile(rf"\b(?:{PEOPLE}|{COUNT_LABEL})\b(?P<gap>[^.\d:;]{{0,60}}?):?\s*{NUMBER}(?![.,]\d|\s*(?:%|mg|kg))",
                               re.IGNORECASE)
# Words between the people and a number that make the number something else than how many they are:
# an age, a threshold, a group or place, a measurement ("patients aged 18", "participants with 2 prior
# lines", "subjects at site 3")
NOT_A_LABEL = re.compile(r"\b(?:aged?|older|younger|over|under|above|below|than|between|from|least|with|who|"
                         r"weigh\w*|per|each|every|at|had|having|received|receiving)\b", re.IGNORECASE)
NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
                "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
                "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
TENS = NUMBER_WORDS[20:]
UNITS = NUMBER_WORDS[1:10]
# A number written as a word, right before the people it counts ("Three patients", "twenty healthy
# volunteers", "Seventy-seven subjects"), not "no one" and not "one of the patients"
NUMBER_WORD = re.compile(rf"(?<!\bno\s)\b(?:({'|'.join(TENS)})[-\s]({'|'.join(UNITS)})|({'|'.join(NUMBER_WORDS)}))\b"
                         rf"(?=\s+(?:(?!of\b)[^\W\d_]+[\s-]+){{0,3}}?"
                         rf"{PEOPLE}\b)", re.IGNORECASE)


def _number_of(word: str) -> int:
    index = NUMBER_WORDS.index(word.lower())
    if index <= 20:
        return index
    return (index - 18) * 10


def _number_for_words(found: re.Match) -> str:
    """The digits of "three", "twenty" or "seventy-seven"."""
    if found.group(1):
        return str(_number_of(found.group(1)) + _number_of(found.group(2)))
    return str(_number_of(found.group(3)))


def _digits_for_words(text: str) -> str:
    return NUMBER_WORD.sub(_number_for_words, text)


def participant_numbers(text: str) -> list[int]:
    """The counts of people a quote states: a number tied to people ("168 patients", "Three patients",
    "n = 168", "Number of subjects enrolled: 168", "Subjects enrolled 168"). Dates, years, ages, doses,
    numbered labels ("Table 2", "Week 12") and identifiers are taken out first, and a number on its own
    never counts: "aged 18 to 65" states no count. Each number is counted once, however many patterns
    find it."""
    remaining = _digits_for_words(text)
    for pattern in NOT_A_COUNT:
        remaining = pattern.sub(lambda found: " " * len(found.group(0)), remaining)
    for pattern in IDENTIFIERS:
        remaining = pattern.sub(lambda found: " " * len(found.group(0)), remaining)
    by_position: dict[int, int] = {}
    # each pattern with the group that holds its number
    for pattern, group in ((COUNT_BEFORE_PEOPLE, 1), (COUNT_AFTER_N, 1), (COUNT_AFTER_LABEL, 2)):
        for found in pattern.finditer(remaining):
            if pattern is COUNT_AFTER_LABEL and NOT_A_LABEL.search(found.group("gap")):
                continue
            by_position[found.start(group)] = int(found.group(group).replace(",", ""))
    numbers = []
    for position in sorted(by_position):
        numbers.append(by_position[position])
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
        if item.kind in requirement_set.reported_kinds:
            # e.g. a partial result: some of the endpoint's components have data, as reviewers count it
            return settle(trace, key, True, f"verified {item.kind}: counted as reported")
        if item.kind in requirement_set.open_kinds:
            # e.g. a result that could not exist: neither reported nor missing, for a person
            return settle(trace, key, None, f"verified {item.kind}: left for a person")
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


def _check_labels(name: str, data: dict) -> None:
    """ValueError unless the labels are ordered conditions on known parts, each with a meaning, names
    once, and a last label with no condition (so every judgement gets one)."""
    labels = data.get("labels", [])
    if not labels:
        raise ValueError(f"{name}: no labels")
    names = []
    for position, entry in enumerate(labels, start=1):
        if entry.get("means") not in LABEL_MEANINGS:
            raise ValueError(f"{name}: label {entry.get('name')!r} means {entry.get('means')!r}; "
                             f"a label means one of {', '.join(LABEL_MEANINGS)}")
        if entry["name"] in names:
            raise ValueError(f"{name}: label {entry['name']!r} is named twice")
        names.append(entry["name"])
        conditions = [key for key in ("when_any", "when_all") if key in entry]
        last = position == len(labels)
        if last and conditions:
            raise ValueError(f"{name}: the last label, {entry['name']!r}, must have no condition (it is the default)")
        if not last and len(conditions) != 1:
            raise ValueError(f"{name}: label {entry['name']!r} needs one condition, when_any or when_all")
        for key in conditions:
            for value in ("true", "false"):
                for part in entry[key].get(value, []):
                    if part not in data["parts"]:
                        raise ValueError(f"{name}: label {entry['name']!r} names an unknown part {part!r}")


def _any_holds(parts: dict[str, bool | None], rule: LabelRule) -> bool:
    """Any part named under `false` is False, or any named under `true` is True."""
    for part in rule.false:
        if parts.get(part) is False:
            return True
    for part in rule.true:
        if parts.get(part) is True:
            return True
    return False


def _all_hold(parts: dict[str, bool | None], rule: LabelRule) -> bool:
    """Every part named under `true` is True and every one named under `false` is False."""
    for part in rule.true:
        if parts.get(part) is not True:
            return False
    for part in rule.false:
        if parts.get(part) is not False:
            return False
    return True


def label_for(requirement_set: RequirementSet, parts: dict[str, bool | None]) -> str:
    """The first of the set's labels whose condition the parts meet (the last one has none)."""
    for rule in requirement_set.labels:
        if rule.when is None:
            return rule.name
        if rule.when == "any" and _any_holds(parts, rule):
            return rule.name
        if rule.when == "all" and _all_hold(parts, rule):
            return rule.name
    raise ValueError(f"{requirement_set.name}: no label applies (its last label must have no condition)")


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
    label = label_for(requirement_set, parts)
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
