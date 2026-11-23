"""AI tags with checked quotes (ADR-008, patient search): one model call per recruiting trial tags it
with plain-language categories, the kind of medicine tested, its purpose, synonyms of its conditions
in English and its countries' languages, MeSH concepts from a shortlist, and the age limits its
inclusion criteria give. Every tag names a field of the trial's profile and quotes it; a tag is kept
only when its quote is in that field as whole words (checks/verify.py verify_in_text) and the tag is
one of the values asked for; the others are kept as dropped, with why. Exclusion criteria are never
sent: a condition they name is not the trial's.

A run records the model, its effort and the exact prompt it sent (checks/prompts/annotate.md, one
current text); each call is a core.llm_call row (operation 'annotate'). The trial's search
document is then rebuilt with the kept tags (search/index.py).
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import psycopg

from trialwatch import llm
from trialwatch.checks.prompts import Prompt, load_prompt
from trialwatch.checks.verify import verify_in_text
from trialwatch.db import geo as geo_db
from trialwatch.db import search as search_db
from trialwatch.db import vocab as vocab_db
from trialwatch.llm import MessagesClient, total_cost
from trialwatch.search import categories, index
from trialwatch.sources import profile

OPERATION = "annotate"
SETTINGS_FILE = Path(__file__).parent / "configs" / "annotate.json"
INTERVENTION_KINDS = ("small-molecule", "antibody-or-protein", "vaccine", "cell-or-gene-therapy",
                      "radiopharmaceutical", "diagnostic-agent", "blood-or-plasma-product", "other")
PURPOSES = ("treatment", "prevention", "diagnosis", "supportive-care", "screening", "basic-science", "other")
AGE_UNITS = {"years": Decimal(1), "months": Decimal(12), "weeks": Decimal("52.1775"), "days": Decimal("365.25")}
MAX_SYNONYM_CHARACTERS = 80
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")

Progress = Callable[[str], None] | None
ShouldStop = Callable[[], bool] | None


@dataclass(frozen=True)
class Settings:
    model: str
    effort: str
    prompt: str
    max_tokens: int
    max_languages: int
    max_candidates: int


def load_settings() -> Settings:
    with SETTINGS_FILE.open(encoding="utf-8") as file:
        data = json.load(file)
    return Settings(data["model"], data["effort"], data["prompt"], data["max_tokens"], data["max_languages"],
                    data["max_candidates"])


# --- what the model is sent ----------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    id: str
    text: str


def fields_for(read: profile.TrialProfile) -> list[Field]:
    """The profile's fields the model reads, each with an id its tags name: conditions and their
    translations, titles, the main objective, inclusion criteria (English), products. Never the
    exclusion criteria."""
    found = []
    for number, condition in enumerate(read.conditions, start=1):
        found.append(Field(f"condition-{number}", condition.name))
        for translation in condition.translations:
            found.append(Field(f"condition-{number}-{translation.language}", translation.text))
    term_number = 0
    for term in read.coded_terms:
        term_number += 1
        found.append(Field(f"meddra-{term_number}", term.name))
    if read.public_title:
        found.append(Field("title-public", read.public_title))
    if read.full_title:
        found.append(Field("title-full", read.full_title))
    if read.main_objective:
        found.append(Field("objective", read.main_objective))
    for number, criterion in enumerate(read.inclusion, start=1):
        found.append(Field(f"inclusion-{number}", criterion.text))
    for number, product in enumerate(read.products, start=1):
        parts = []
        if product.role:
            parts.append(f"{product.role}:")
        if product.name:
            parts.append(product.name)
        if product.substances:
            parts.append(f"({', '.join(product.substances)})")
        if product.form:
            parts.append(f"· {product.form}")
        if product.origins:
            parts.append(f"· {', '.join(product.origins)}")
        if parts:
            found.append(Field(f"product-{number}", " ".join(parts)))
    return found


def languages_for(conn: psycopg.Connection, read: profile.TrialProfile, most: int) -> list[str]:
    """English, then the first language of each country where the trial recruits (GeoNames), at most `most`."""
    found = ["en"]
    for country in profile.recruiting_countries(read):
        iso2 = geo_db.country_code(conn, country)
        if iso2 is None:
            continue
        spoken = geo_db.country_languages(conn, iso2)
        if spoken:
            # GeoNames lists a country's languages most spoken first, e.g. fr-BE, nl-BE, de-BE
            code = spoken[0].split("-")[0].lower()
            if len(code) == 2 and code not in found:
                found.append(code)
        if len(found) >= most:
            break
    return found[:most]


def candidates_for(conn: psycopg.Connection, read: profile.TrialProfile, most: int) -> list[tuple[str, str]]:
    """(ui, name) of MeSH descriptors most like the trial's conditions: the shortlist its concepts are picked from."""
    texts = []
    for condition in read.conditions:
        texts.append(condition.name)
    for term in read.coded_terms:
        texts.append(term.name)
    if not texts or not vocab_db.loaded(conn):
        return []
    found = []
    for match in vocab_db.similar_terms(conn, texts, most):
        found.append((match.ui, match.name))
    return found


def _fact_item(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _age_limit() -> dict:
    limit = _fact_item({"value": {"type": "number"}, "unit": {"type": "string", "enum": list(AGE_UNITS)},
                        "field": {"type": "string"}, "quote": {"type": "string"}},
                       ["value", "unit", "field", "quote"])
    return {"anyOf": [{"type": "null"}, limit]}


def answer_schema() -> dict:
    """The answer's JSON schema: the same for every trial (field ids and languages are checked in code)."""
    slugs = []
    for category in categories.all_categories():
        slugs.append(category.slug)
    quoted = {"field": {"type": "string"}, "quote": {"type": "string"}}
    category_properties = {"category": {"type": "string", "enum": slugs}}
    intervention_properties = {"kind": {"type": "string", "enum": list(INTERVENTION_KINDS)}}
    purpose_properties = {"purpose": {"type": "string", "enum": list(PURPOSES)}}
    synonym_properties = {"term": {"type": "string"}, "language": {"type": "string"}}
    concept_properties = {"ui": {"type": "string"}}
    for properties in (category_properties, intervention_properties, purpose_properties, synonym_properties,
                       concept_properties):
        for name, value in quoted.items():
            properties[name] = value
    return _fact_item({
        "categories": {"type": "array", "items": _fact_item(category_properties, ["category", "field", "quote"])},
        "interventions": {"type": "array", "items": _fact_item(intervention_properties, ["kind", "field", "quote"])},
        "purposes": {"type": "array", "items": _fact_item(purpose_properties, ["purpose", "field", "quote"])},
        "synonyms": {"type": "array", "items": _fact_item(synonym_properties, ["term", "language", "field", "quote"])},
        "concepts": {"type": "array", "items": _fact_item(concept_properties, ["ui", "field", "quote"])},
        "age_limits": _fact_item({"minimum": _age_limit(), "maximum": _age_limit()}, ["minimum", "maximum"]),
    }, ["categories", "interventions", "purposes", "synonyms", "concepts", "age_limits"])


def build_request(settings: Settings, prompt: Prompt, fields: list[Field], languages: list[str],
                  candidates: list[tuple[str, str]]) -> dict:
    field_lines = []
    for item in fields:
        text = " ".join(item.text.split())
        field_lines.append(f"[{item.id}] {text}")
    category_lines = []
    for category in categories.all_categories():
        category_lines.append(f"- {category.slug}: {category.name}")
    candidate_lines = []
    for ui, name in candidates:
        candidate_lines.append(f"- {ui}: {name}")
    if not candidate_lines:
        candidate_lines.append("(none)")
    content = (f"<trial>\n{chr(10).join(field_lines)}\n</trial>\n\n"
               f"<languages>{', '.join(languages)}</languages>\n\n"
               f"<categories>\n{chr(10).join(category_lines)}\n</categories>\n\n"
               f"<mesh_candidates>\n{chr(10).join(candidate_lines)}\n</mesh_candidates>")
    return {
        "model": settings.model,
        "max_tokens": settings.max_tokens,
        "system": prompt.text,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": settings.effort, "format": {"type": "json_schema", "schema": answer_schema()}},
        "messages": [{"role": "user", "content": content}],
    }


def request_as_sent(request: dict) -> dict:
    """The request for the record, without the system prompt (stored once with the run) and the schema."""
    kept = {}
    for name, value in request.items():
        if name not in ("system", "output_config"):
            kept[name] = value
    return kept


# --- checking the answer -------------------------------------------------------------------------

@dataclass
class Checked:
    facts: dict = field(default_factory=dict)
    dropped: list[dict] = field(default_factory=list)


def _drop(checked: Checked, tag: str, item: dict, why: str) -> None:
    checked.dropped.append({"tag": tag, "item": item, "why": why})


def field_id_of(item: dict) -> str:
    """The field id a tag names, as given or in its square brackets ("[condition-1]")."""
    named = item.get("field")
    if not isinstance(named, str):
        return ""
    return named.strip().strip("[]").strip()


def _quoted(checked: Checked, tag: str, item: dict, fields: dict[str, str]) -> bool:
    """The item names a field it was asked about and quotes it as whole words; else dropped. The
    field id is kept as asked (without brackets)."""
    field_id = field_id_of(item)
    if field_id:
        item["field"] = field_id
    quote = item.get("quote")
    if not isinstance(field_id, str) or field_id not in fields:
        _drop(checked, tag, item, "names no field it was given")
        return False
    if not isinstance(quote, str) or not verify_in_text(quote, fields[field_id]):
        _drop(checked, tag, item, "its quote is not in that field")
        return False
    return True


def _add(found: list[dict], fact: dict) -> None:
    if fact not in found:
        found.append(fact)


def _age(checked: Checked, name: str, limit: object, fields: dict[str, str]) -> dict | None:
    if limit is None:
        return None
    if not isinstance(limit, dict):
        _drop(checked, f"age {name}", {"value": limit}, "not an age limit")
        return None
    if not _quoted(checked, f"age {name}", limit, fields):
        return None
    if not field_id_of(limit).startswith("inclusion-"):
        _drop(checked, f"age {name}", limit, "not from an inclusion criterion")
        return None
    unit = limit.get("unit")
    value = limit.get("value")
    if unit not in AGE_UNITS or not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        _drop(checked, f"age {name}", limit, "not a number of years, months, weeks or days")
        return None
    numbers = []
    for number in NUMBER.findall(limit["quote"]):
        numbers.append(Decimal(number.replace(",", ".")))
    if Decimal(str(value)) not in numbers:
        _drop(checked, f"age {name}", limit, "its number is not in its quote")
        return None
    years = (Decimal(str(value)) / AGE_UNITS[unit]).quantize(Decimal("0.01"))
    return {"years": float(years), "value": value, "unit": unit, "field": limit["field"], "quote": limit["quote"]}


def _of_a_condition(item: dict) -> bool:
    """The tag quotes a condition or a MedDRA term (not a title or a criterion)."""
    field_id = field_id_of(item)
    return field_id.startswith("condition-") or field_id.startswith("meddra-")


def check_answer(answer: dict, fields: list[Field], languages: list[str], candidates: list[tuple[str, str]]) -> Checked:
    """Keep the tags whose quotes are in their fields and whose values were asked for; drop the rest, with why."""
    by_id = {}
    for item in fields:
        by_id[item.id] = item.text
    candidate_names = {}
    for ui, name in candidates:
        candidate_names[ui] = name
    slugs = []
    for category in categories.all_categories():
        slugs.append(category.slug)
    checked = Checked(facts={"categories": [], "interventions": [], "purposes": [], "synonyms": [], "concepts": [],
                             "age_min": None, "age_max": None})
    for item in answer.get("categories", []):
        if item.get("category") not in slugs:
            _drop(checked, "category", item, "not a category it was given")
        elif _quoted(checked, "category", item, by_id):
            _add(checked.facts["categories"], {"category": item["category"], "field": item["field"], "quote": item["quote"]})
    for item in answer.get("interventions", []):
        if item.get("kind") not in INTERVENTION_KINDS:
            _drop(checked, "intervention", item, "not a kind it was given")
        elif _quoted(checked, "intervention", item, by_id):
            _add(checked.facts["interventions"], {"kind": item["kind"], "field": item["field"], "quote": item["quote"]})
    for item in answer.get("purposes", []):
        if item.get("purpose") not in PURPOSES:
            _drop(checked, "purpose", item, "not a purpose it was given")
        elif _quoted(checked, "purpose", item, by_id):
            _add(checked.facts["purposes"], {"purpose": item["purpose"], "field": item["field"], "quote": item["quote"]})
    for item in answer.get("synonyms", []):
        term = item.get("term")
        if not isinstance(term, str) or not term.strip() or len(term) > MAX_SYNONYM_CHARACTERS:
            _drop(checked, "synonym", item, "not a name")
        elif item.get("language") not in languages:
            _drop(checked, "synonym", item, "not in a language it was asked for")
        elif not field_id_of(item).startswith("condition-"):
            _drop(checked, "synonym", item, "not of a condition")
        elif _quoted(checked, "synonym", item, by_id):
            _add(checked.facts["synonyms"], {"term": " ".join(term.split()), "language": item["language"],
                                             "field": item["field"], "quote": item["quote"]})
    for item in answer.get("concepts", []):
        if item.get("ui") not in candidate_names:
            _drop(checked, "concept", item, "not one of the MeSH concepts it was given")
        elif not _of_a_condition(item):
            _drop(checked, "concept", item, "not of a condition")
        elif _quoted(checked, "concept", item, by_id):
            _add(checked.facts["concepts"], {"ui": item["ui"], "name": candidate_names[item["ui"]],
                                             "field": item["field"], "quote": item["quote"]})
    limits = answer.get("age_limits")
    if isinstance(limits, dict):
        checked.facts["age_min"] = _age(checked, "minimum", limits.get("minimum"), by_id)
        checked.facts["age_max"] = _age(checked, "maximum", limits.get("maximum"), by_id)
    return checked


# --- a run ---------------------------------------------------------------------------------------

@dataclass
class RunSummary:
    run_id: int
    trials: int = 0
    tagged: int = 0
    kept: int = 0
    dropped: int = 0
    stopped: bool = False
    # trial id -> what went wrong (worth another try)
    failed: dict[int, str] = field(default_factory=dict)
    cost_usd: Decimal = Decimal(0)


def annotate_trial(conn: psycopg.Connection, client: MessagesClient, settings: Settings, prompt: Prompt,
                   run_id: int, trial_id: int) -> tuple[int, int, str | None, Decimal]:
    """Tag one trial; returns (tags kept, tags dropped, error, cost)."""
    stored = profile.current(conn, trial_id)
    if stored is None:
        return 0, 0, "no profile", Decimal(0)
    read = stored.read
    fields = fields_for(read)
    languages = languages_for(conn, read, settings.max_languages)
    candidates = candidates_for(conn, read, settings.max_candidates)
    request = build_request(settings, prompt, fields, languages, candidates)
    call = llm.call(client, request, OPERATION)
    checked = Checked()
    error = call.error
    if error is None:
        try:
            answer = json.loads(call.raw_text)
            if not isinstance(answer, dict):
                raise ValueError("not an object")
            checked = check_answer(answer, fields, languages, candidates)
        except ValueError as exc:
            error = f"invalid answer: {exc}"
    cost = total_cost(call.costs)
    with conn.transaction():
        llm.record(conn, call, request_as_sent(request), operation=OPERATION, trial_id=trial_id, call_number=1,
                   annotation_run_id=run_id)
        search_db.save_annotation(conn, run_id=run_id, trial_id=trial_id, profile_hash=bytes(stored.profile_hash),
                                  facts=checked.facts, dropped=checked.dropped, error=error)
        if error is None:
            index.rebuild_trial(conn, trial_id, force=True)
    kept = 0
    for name in ("categories", "interventions", "purposes", "synonyms", "concepts"):
        kept += len(checked.facts.get(name, []))
    for name in ("age_min", "age_max"):
        if checked.facts.get(name) is not None:
            kept += 1
    return kept, len(checked.dropped), error, cost


def run(conn: psycopg.Connection, client: MessagesClient, trial_ids: list[int], settings: Settings | None = None,
        progress: Progress = None, should_stop: ShouldStop = None) -> RunSummary:
    """Tag these trials, one model call each, in one run."""
    if settings is None:
        settings = load_settings()
    prompt = load_prompt(settings.prompt)
    run_id = search_db.start_annotation_run(
        conn, model=settings.model, effort=settings.effort, prompt=prompt.name, prompt_sha256=prompt.sha256,
        prompt_text=prompt.text, params={"trials": len(trial_ids), "max_tokens": settings.max_tokens,
                                         "max_languages": settings.max_languages,
                                         "max_candidates": settings.max_candidates},
    )
    summary = RunSummary(run_id=run_id)
    status = "succeeded"
    error = None
    try:
        for number, trial_id in enumerate(trial_ids, start=1):
            if should_stop is not None and should_stop():
                summary.stopped = True
                status = "stopped"
                break
            kept, dropped, problem, cost = annotate_trial(conn, client, settings, prompt, run_id, trial_id)
            summary.trials += 1
            summary.kept += kept
            summary.dropped += dropped
            summary.cost_usd += cost
            if problem is None:
                summary.tagged += 1
            else:
                summary.failed[trial_id] = problem
            if progress is not None:
                line = f"{number}/{len(trial_ids)} trial {trial_id}: {kept} tags kept, {dropped} dropped"
                if problem is not None:
                    line = f"{line}, {problem}"
                progress(line)
    except BaseException as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        search_db.finish_annotation_run(conn, run_id, status, error)
    return summary
