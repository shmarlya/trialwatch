"""MeSH concepts (ADR-008): which descriptors a trial's conditions name, and which a query names.

A trial's conditions (and the MedDRA terms CTIS publishes for them) are matched to MeSH entry terms
without AI, each match kept with the field and words it was found in:
- exact-name: the whole text is an entry term ("Type 2 Diabetes Mellitus"),
- same-words: the whole text has the same words as one ("Mellitus, Type 2 Diabetes"),
- phrase: words inside the text are an entry term ("... (colorectal cancer, ovarian cancer)"), the
  longest first, never overlapping; a single word only if it has MIN_WORD_LETTERS letters or more.
A trial's search document then carries a lexeme for each of its descriptors and for every descriptor
above them in the MeSH tree, so a query naming "Diabetes Mellitus" finds a trial filed under type 2.
"""

import re
from dataclasses import dataclass

import psycopg

from trialwatch.db import vocab as vocab_db
from trialwatch.sources import profile
from trialwatch.vocab import mesh

TOKEN_PREFIX = "zmesh"
MIN_WORD_LETTERS = 5
MAX_PHRASE_WORDS = 6
# entry terms too general to stand for a trial's condition when found as a single word in its text
TOO_GENERAL = ("disease", "diseases", "syndrome", "syndromes", "disorder", "disorders", "condition",
               "conditions", "patients")
# a query's words may begin a longer entry term with at most this many more words ("diabetes" begins
# "Diabetes Mellitus"); the broadest descriptors found are kept, at most MAX_QUERY_CONCEPTS
QUERY_EXTRA_WORDS = 1
MAX_QUERY_CONCEPTS = 6
WORD = re.compile(r"[^\W_]+")


@dataclass(frozen=True)
class Concept:
    ui: str
    name: str
    # exact-name | same-words | phrase | ai-quote
    method: str
    field: str
    quote: str


def token(ui: str) -> str:
    return TOKEN_PREFIX + ui.lower()


def condition_fields(read: profile.TrialProfile) -> list[tuple[str, str]]:
    """(field id, text) of the profile's conditions and MedDRA terms, as concepts are looked for in."""
    fields = []
    for number, condition in enumerate(read.conditions, start=1):
        fields.append((f"condition-{number}", condition.name))
    number = 0
    for term in read.coded_terms:
        if term.system == "meddra":
            number += 1
            fields.append((f"meddra-{number}", term.name))
    return fields


def _phrases(text: str) -> list[tuple[int, int, str]]:
    """Every run of 1 to MAX_PHRASE_WORDS words in the text: (first word, last word, the words as written)."""
    words = []
    for match in WORD.finditer(text):
        words.append((match.start(), match.end()))
    found = []
    for first in range(len(words)):
        for last in range(first, min(first + MAX_PHRASE_WORDS, len(words))):
            phrase = text[words[first][0]:words[last][1]]
            if first == last:
                letters = text[words[first][0]:words[first][1]]
                if len(letters) < MIN_WORD_LETTERS or letters.lower() in TOO_GENERAL:
                    continue
            found.append((first, last, phrase))
    return found


def _overlaps(span: tuple[int, int], taken: list[tuple[int, int]]) -> bool:
    for first, last in taken:
        if span[0] <= last and first <= span[1]:
            return True
    return False


def _longest_first(item: tuple[int, int, str]) -> tuple[int, int]:
    first, last, _phrase = item
    return (first - last, first)


def find(conn: psycopg.Connection, fields: list[tuple[str, str]]) -> list[Concept]:
    """The MeSH descriptors these texts name (none before MeSH is loaded)."""
    if not fields or not vocab_db.loaded(conn):
        return []
    texts = []
    for _field, text in fields:
        texts.append(text)
    exact = {}
    for match in vocab_db.terms_named(conn, texts):
        if match.key not in exact:
            exact[match.key] = []
        exact[match.key].append(match)
    same = {}
    for match in vocab_db.terms_with_same_words(conn, texts):
        if match.key not in same:
            same[match.key] = []
        same[match.key].append(match)
    found = []
    for field, text in fields:
        if text in exact:
            for match in exact[text]:
                _add(found, Concept(match.ui, match.name, "exact-name", field, text))
            continue
        if text in same:
            for match in same[text]:
                _add(found, Concept(match.ui, match.name, "same-words", field, text))
            continue
        for concept in _phrase_concepts(conn, field, text):
            _add(found, concept)
    return found


def _phrase_concepts(conn: psycopg.Connection, field: str, text: str) -> list[Concept]:
    phrases = _phrases(text)
    if not phrases:
        return []
    candidates = []
    for _first, _last, phrase in phrases:
        candidates.append(phrase)
    named = {}
    for match in vocab_db.terms_named(conn, candidates):
        if match.key not in named:
            named[match.key] = []
        named[match.key].append(match)
    taken = []
    found = []
    for first, last, phrase in sorted(phrases, key=_longest_first):
        if phrase not in named or _overlaps((first, last), taken):
            continue
        taken.append((first, last))
        for match in named[phrase]:
            found.append(Concept(match.ui, match.name, "phrase", field, phrase))
    return found


def _add(found: list[Concept], concept: Concept) -> None:
    if concept not in found:
        found.append(concept)


def with_ancestors(conn: psycopg.Connection, uis: list[str]) -> list[str]:
    """The descriptors above these in the MeSH tree (not these themselves), each once."""
    if not uis:
        return []
    above = []
    for numbers in vocab_db.tree_numbers(conn, uis).values():
        for tree_number in numbers:
            for ancestor in mesh.ancestor_tree_numbers(tree_number):
                if ancestor not in above:
                    above.append(ancestor)
    found = []
    for ui in vocab_db.descriptors_at(conn, above).values():
        if ui not in uis and ui not in found:
            found.append(ui)
    return found


# --- what a query names ------------------------------------------------------------------------

def _broadest(conn: psycopg.Connection, uis: list[str]) -> list[str]:
    """The descriptors none of the others is above."""
    numbers = vocab_db.tree_numbers(conn, uis)
    owner = {}
    for ui, tree_numbers in numbers.items():
        for tree_number in tree_numbers:
            owner[tree_number] = ui
    found = []
    for ui in uis:
        if ui not in numbers:
            continue
        under_another = False
        for tree_number in numbers[ui]:
            for ancestor in mesh.ancestor_tree_numbers(tree_number):
                if ancestor in owner and owner[ancestor] != ui:
                    under_another = True
        if not under_another and ui not in found:
            found.append(ui)
    return found


def for_query(conn: psycopg.Connection, words: list[str]) -> list[tuple[str, str]]:
    """(ui, name) of the MeSH descriptors a query's condition words name: an entry term the words are
    ("heart attack": Myocardial Infarction), else one with the same words, else the broadest of those
    whose name begins with them and one more word ("diabetes": Diabetes Mellitus, Diabetes Insipidus)."""
    if not words or not vocab_db.loaded(conn):
        return []
    phrase = " ".join(words)
    matches = vocab_db.terms_named(conn, [phrase])
    if not matches:
        matches = vocab_db.terms_with_same_words(conn, [phrase])
    uis = []
    names = {}
    for match in matches:
        if match.ui not in uis:
            uis.append(match.ui)
            names[match.ui] = match.name
    if not uis:
        for match in vocab_db.terms_with_words(conn, phrase, QUERY_EXTRA_WORDS):
            if match.ui not in uis:
                uis.append(match.ui)
                names[match.ui] = match.name
        uis = _broadest(conn, uis)
    found = []
    for ui in uis[:MAX_QUERY_CONCEPTS]:
        found.append((ui, names[ui]))
    return found
