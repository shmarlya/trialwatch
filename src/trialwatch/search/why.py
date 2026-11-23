"""Why a search found each trial, in words, on its card (ADR-008): which word of the condition, which
category or which MeSH concept matched, and in which part of the trial. Its distance is on the card
already. Asked of the database for the page's hits only, the same way the search matched them (the
words of the trial's search document, by weight: search/index.py)."""

import psycopg

from trialwatch.db import search as search_db
from trialwatch.db.search import SearchHit
from trialwatch.search import query

# A condition is cut to this many characters on the card (some are a paragraph)
LONGEST_CONDITION = 80
# What each weight of a search document holds, as the card says where a word was found
WHERE_A_WORD_IS = {
    "b": "in its title",
    "c": "in what it tests or its main objective",
    "d": "in who can join (its inclusion criteria)",
}


def explain(conn: psycopg.Connection, reading: query.Interpretation, hits: list[SearchHit]) -> dict[int, list[str]]:
    """Trial id -> why the search found it: each condition word found in it, and where (or the category
    it named); and, when the words alone did not find it, the MeSH concept that did."""
    asked = []
    for term in query.terms(reading):
        # a word like "2" or "typ" finds nothing worth saying by itself
        if term.kind == "word" and not query.carries_meaning(term.label):
            continue
        asked.append(term)
    if not asked or not hits:
        return {}
    trial_ids = [hit.trial_id for hit in hits]
    found = {}
    tsqueries = [term.tsquery for term in asked]
    for match in search_db.terms_found(conn, trial_ids, tsqueries):
        found[(match.trial_id, match.position - 1)] = match
    concept_uis = [term.ui for term in asked if term.kind == "concept"]
    own_concepts = {}
    for concept in search_db.concepts_found(conn, trial_ids, concept_uis):
        own_concepts[(concept.trial_id, concept.ui)] = concept
    why = {}
    for trial_id in trial_ids:
        reasons = _reasons(trial_id, asked, found, own_concepts)
        if reasons:
            why[trial_id] = reasons
    return why


def _reasons(trial_id: int, asked: list[query.Term], found: dict, own_concepts: dict) -> list[str]:
    # where -> the words found there ("in its title": ["heart", "attack"]), in the order asked
    words_by_place: dict[str, list[str]] = {}
    words_found = []
    words_asked = []
    for position, term in enumerate(asked):
        match = found.get((trial_id, position))
        if match is None or not match.found:
            continue
        place = None
        if term.kind == "word":
            place = _where_a_word_is(match)
            words_found.append(term.label)
        elif term.kind == "category" and term.word not in words_found:
            place = f"filed under {term.label}"
            words_found.append(term.word)
        if place is not None:
            words_by_place.setdefault(place, []).append(term.label if term.kind == "word" else term.word)
    reasons = []
    for place, words in words_by_place.items():
        quoted = [f"“{word}”" for word in words]
        reasons.append(f"{' and '.join(quoted)} {place}")
    for term in asked:
        if term.kind == "word":
            words_asked.append(term.label)
    every_word = all(word in words_found for word in words_asked)
    if every_word:
        return reasons
    # the words alone didn't find it: a MeSH concept they name did
    for position, term in enumerate(asked):
        match = found.get((trial_id, position))
        if term.kind != "concept" or match is None or not match.found:
            continue
        reasons.append(_concept_reason(term, own_concepts.get((trial_id, term.ui))))
    return reasons


def _where_a_word_is(match: search_db.TermFound) -> str:
    if match.weight == "a" and match.conditions:
        return f"in its condition “{_short(match.conditions[0])}”"
    if match.weight == "a":
        return "in another name of its condition (a translation or a synonym)"
    return WHERE_A_WORD_IS[match.weight]


def _short(text: str) -> str:
    if len(text) <= LONGEST_CONDITION:
        return text
    return text[:LONGEST_CONDITION].rsplit(" ", 1)[0] + " …"


def _concept_reason(term: query.Term, own: search_db.ConceptFound | None) -> str:
    if own is None:
        return f"filed under the medical term “{term.label}” (MeSH)"
    if own.own_ui == term.ui:
        return f"its condition “{_short(own.quote)}” is the medical term “{term.label}” (MeSH)"
    return f"its condition “{_short(own.quote)}” ({own.own_name}) is a kind of “{term.label}” (MeSH)"
