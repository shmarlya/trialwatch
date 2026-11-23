"""Search documents (ADR-008): one per trial, built from its profile so a search can find it.

rebuild_trial runs inside every transfer, right after the trial's profile is stored, so a trial is
findable the moment it arrives; a profile that did not change since its document was built is skipped.
`trialwatch search reindex` rebuilds every document from stored profiles (after the indexing rules,
the categories or the vocabulary changed), fetching nothing.

What a document's words weigh (search.document, weights A to D, A the most):
  A  conditions, their translations, MedDRA term names, the trial's own MeSH concepts (lexemes)
  B  public and full title and their translations, its lay categories and the MeSH concepts above its
     own (lexemes)
  C  products, active substances, ATC names, main objective
  D  inclusion criteria and their translations
Exclusion criteria are never indexed.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg

from trialwatch.db import search as search_db
from trialwatch.geo import countries
from trialwatch.search import categories, concepts
from trialwatch.search.constants import MEDDRA_TOKEN
from trialwatch.sources import profile

Progress = Callable[[str], None] | None


@dataclass
class SearchDocument:
    trial_id: int
    source: str
    profile_hash: bytes
    recruiting: bool
    recruiting_countries: list[str]
    title: str | None
    conditions: list[str]
    categories: list[str]
    phase: str | None
    age_groups: list[str]
    female: bool | None
    male: bool | None
    healthy_volunteers: bool | None
    patients: bool | None
    rare: bool
    # text per weight, parsed into words by the search.trialwatch configuration
    text_a: str
    text_b: str
    text_c: str
    text_d: str
    # lexemes added as they are (concepts, codes, categories), per weight
    lexemes_a: list[str] = field(default_factory=list)
    lexemes_b: list[str] = field(default_factory=list)
    # from the AI tags it was built with (search/annotate.py), each kept with a checked quote
    annotation_id: int | None = None
    age_min: float | None = None
    age_max: float | None = None
    interventions: list[str] = field(default_factory=list)
    purposes: list[str] = field(default_factory=list)


def _add_text(parts: list[str], text: str | None) -> None:
    if text is not None and text.strip():
        parts.append(text.strip())


def _add_lexeme(lexemes: list[str], lexeme: str) -> None:
    if lexeme not in lexemes:
        lexemes.append(lexeme)


def _is_rare(read: profile.TrialProfile) -> bool:
    for condition in read.conditions:
        if condition.rare is True:
            return True
    return False


def _facts_named(facts: dict | None, name: str) -> list[dict]:
    if facts is None or not isinstance(facts.get(name), list):
        return []
    return facts[name]


def _years(registered: float | None, facts: dict | None, name: str) -> float | None:
    """An age limit: the one the source registers, else the one the AI tags quoted from the criteria."""
    if registered is not None:
        return registered
    if facts is None or not isinstance(facts.get(name), dict):
        return None
    return facts[name]["years"]


def document_for(trial_id: int, source: str, read: profile.TrialProfile, profile_hash: bytes,
                 found: list[concepts.Concept], above: list[str], facts: dict | None = None,
                 annotation_id: int | None = None) -> SearchDocument:
    """The search document of a trial's profile, with the MeSH concepts its conditions name, the
    concepts above those, and the AI tags (`facts`) it was annotated with, if any."""
    a = []
    lexemes_a = []
    for condition in read.conditions:
        _add_text(a, condition.name)
        for translation in condition.translations:
            _add_text(a, translation.text)
    for synonym in _facts_named(facts, "synonyms"):
        _add_text(a, synonym["term"])
    for term in read.coded_terms:
        _add_text(a, term.name)
        if term.system == "meddra":
            _add_lexeme(lexemes_a, MEDDRA_TOKEN + term.code)
    for concept in found:
        _add_lexeme(lexemes_a, concepts.token(concept.ui))

    b = []
    lexemes_b = []
    _add_text(b, read.public_title)
    _add_text(b, read.full_title)
    for translation in read.title_translations:
        _add_text(b, translation.text)
    trial_categories = categories.for_areas(read.areas)
    if not trial_categories:
        # the registry files it under no area we have a category for ("Not possible to specify"): the
        # AI tags' categories stand in
        for tag in _facts_named(facts, "categories"):
            if tag["category"] not in trial_categories:
                trial_categories.append(tag["category"])
    for slug in trial_categories:
        _add_lexeme(lexemes_b, categories.token(slug))
    for ui in above:
        _add_lexeme(lexemes_b, concepts.token(ui))

    c = []
    for product in read.products:
        if product.role == "placebo":
            continue
        _add_text(c, product.name)
        for substance in product.substances:
            _add_text(c, substance)
        _add_text(c, product.atc_name)
    _add_text(c, read.main_objective)

    d = []
    for criterion in read.inclusion:
        _add_text(d, criterion.text)
        for translation in criterion.translations:
            _add_text(d, translation.text)

    condition_names = []
    for condition in read.conditions:
        condition_names.append(condition.name)
    # by code: search compares countries by code (geo/countries.py), however a source spells them
    recruiting_codes = []
    for name in profile.recruiting_countries(read):
        code = countries.iso2_of(name)
        if code is not None and code not in recruiting_codes:
            recruiting_codes.append(code)
    title = read.public_title
    if title is None:
        title = read.full_title
    return SearchDocument(
        trial_id=trial_id, source=source, profile_hash=profile_hash, recruiting=read.recruiting,
        recruiting_countries=recruiting_codes, title=title, conditions=condition_names,
        categories=trial_categories, phase=read.phase, age_groups=list(read.age_groups), female=read.female,
        male=read.male, healthy_volunteers=read.healthy_volunteers, patients=read.patients, rare=_is_rare(read),
        text_a="\n".join(a), text_b="\n".join(b), text_c="\n".join(c), text_d="\n".join(d),
        lexemes_a=lexemes_a, lexemes_b=lexemes_b, annotation_id=annotation_id, age_min=_years(read.age_min, facts, "age_min"),
        age_max=_years(read.age_max, facts, "age_max"), interventions=_tag_values(facts, "interventions", "kind"),
        purposes=_tag_values(facts, "purposes", "purpose"),
    )


def _tag_values(facts: dict | None, name: str, key: str) -> list[str]:
    found = []
    for tag in _facts_named(facts, name):
        if tag[key] not in found:
            found.append(tag[key])
    return found


def rebuild_trial(conn: psycopg.Connection, trial_id: int, force: bool = False) -> bool:
    """Build the trial's search document from its stored profile, unless the document was built from
    the same profile already (`force`: rebuild anyway). A trial without a profile has no document.
    Returns True when it built one."""
    stored = profile.current(conn, trial_id)
    if stored is None:
        search_db.delete_document(conn, trial_id)
        return False
    if not force and search_db.document_hash(conn, trial_id) == stored.profile_hash:
        return False
    read = stored.read
    found = concepts.find(conn, concepts.condition_fields(read))
    for coded in concepts.from_coded_terms(read):
        if coded not in found:
            found.append(coded)
    # the AI tags of this very profile, if it was annotated (an annotation of an older profile is not used)
    annotation = search_db.latest_annotation(conn, trial_id, bytes(stored.profile_hash))
    facts = None
    annotation_id = None
    if annotation is not None:
        facts = annotation.facts
        annotation_id = annotation.id
        for tag in _facts_named(facts, "concepts"):
            ai_concept = concepts.Concept(tag["ui"], tag["name"], "ai-quote", tag["field"], tag["quote"])
            if ai_concept not in found:
                found.append(ai_concept)
    uis = []
    for concept in found:
        if concept.ui not in uis:
            uis.append(concept.ui)
    above = concepts.with_ancestors(conn, uis)
    document = document_for(trial_id, stored.source, read, stored.profile_hash, found, above, facts, annotation_id)
    with conn.transaction():
        search_db.save_concepts(conn, trial_id, found)
        search_db.save_document(conn, document)
    return True


@dataclass
class ReindexSummary:
    trials: int = 0
    built: int = 0


def rebuild_all(conn: psycopg.Connection, force: bool = False, progress: Progress = None) -> ReindexSummary:
    """Rebuild every trial's search document from its stored profile, then the "did you mean" words."""
    summary = ReindexSummary()
    after = 0
    while True:
        trial_ids = search_db.trials_with_profiles(conn, after, 500)
        if not trial_ids:
            break
        for trial_id in trial_ids:
            summary.trials += 1
            if rebuild_trial(conn, trial_id, force):
                summary.built += 1
            after = trial_id
        if progress is not None:
            progress(f"{summary.trials} trials, {summary.built} rebuilt")
    search_db.refresh_terms(conn)
    return summary
