"""Reading a search query (ADR-008): "diabetes Paris" -> condition words "diabetes", near Paris (France),
within 50 km. Rules and a gazetteer, no model call: the same query always reads the same way, nothing
leaves the server, and the page shows how it was read so a person can correct it.

In this order, each step taking the words it uses out of the query:
1. healthy-volunteer phrases ("healthy volunteers", "volontaires sains"), an age ("45 ans", "age 45"),
   a phase ("phase 2");
2. a country by any of its names ("Allemagne", "Polska");
3. a postal code GeoNames has (75013 is Paris 13e; in the country named, if one is);
4. a place: up to three words that name an EU/EEA place, looked for from the end of the query (people
   write the condition first). Small places count only after a word like "near", "à", "bei" and never
   a word of the lexicon ("como" is Spanish before it is the Italian town);
5. words for children, older people, women, men, rare diseases;
6. what is left, without stop words, are the condition words: each matches words that begin like it
   (diabet:*), or the category it names (krebs: cancer); together they may name MeSH concepts
   ("heart attack": Myocardial Infarction), which match too.
A word that is both a condition's name in MeSH and a place is a condition unless "near" or the like
comes before it; the other reading is offered as an alternative.
"""

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import psycopg

from trialwatch.db import geo as geo_db
from trialwatch.db import vocab as vocab_db
from trialwatch.search import categories, concepts
from trialwatch.search.constants import (AGE_GROUP_WORDS, CHILDREN, DEFAULT_RADIUS_KM,
                                         OLDER)

LEXICON_FILE = Path(__file__).with_name("lexicon.json")
MAX_CHARACTERS = 200
MAX_WORDS = 15
MAX_PLACE_WORDS = 3
# a place is taken for what it names when it has this many people, or a place hint comes before it
BIG_PLACE = 15000
SMALL_PLACE = 1000
# a word matches words beginning like it once it has this many letters (shorter ones: the word itself)
PREFIX_LETTERS = 4
# a word of this many letters ending in "s" also matches without it ("tumours" -> tumour:*)
PLURAL_LETTERS = 6
POSTCODE = re.compile(r"^(\d{4,5}|\d{2}-\d{3}|\d{4}[a-z]{2})$")
NUMBER = re.compile(r"^\d{1,3}$")
LEXEME = re.compile(r"[^\W_]+")


@cache
def lexicon() -> dict[str, list[str]]:
    with LEXICON_FILE.open(encoding="utf-8") as file:
        data = json.load(file)
    words = {}
    for name, items in data.items():
        if name != "about":
            words[name] = items
    return words


@dataclass(frozen=True)
class Place:
    # city | postcode
    kind: str
    name: str
    country_code: str
    country_name: str
    latitude: float
    longitude: float
    geonameid: int | None = None
    # the words of the query it was read from
    matched: str = ""


@dataclass(frozen=True)
class Alternative:
    """Another way to read the query, as the search's own parameters."""

    label: str
    params: dict


@dataclass
class Interpretation:
    text: str
    condition_words: list[str] = field(default_factory=list)
    # (ui, name) of MeSH descriptors the condition words name
    concepts: list[tuple[str, str]] = field(default_factory=list)
    # slugs of categories a condition word names
    categories: list[str] = field(default_factory=list)
    place: Place | None = None
    # (iso2, English name) of a country to search in
    country: tuple[str, str] | None = None
    # the distance from the place; None: the whole country
    radius_km: int | None = None
    age: int | None = None
    # an age group a word gave ("children": 0-17, "elderly": 65+), when no age was given
    age_group: str | None = None
    sex: str | None = None
    healthy_volunteers: bool | None = None
    phase: str | None = None
    category: str | None = None
    rare: bool | None = None
    alternatives: list[Alternative] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --- words -------------------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Unicode in one form, French elisions split (d'alzheimer -> d alzheimer), at most MAX_CHARACTERS."""
    folded = unicodedata.normalize("NFKC", text)[:MAX_CHARACTERS]
    folded = re.sub(r"\b([dljmnstc]|qu)['’]", r"\1 ", folded, flags=re.IGNORECASE)
    return folded


def words_of(text: str) -> list[str]:
    """The query's words in lower case, punctuation aside (a hyphen or slash splits words), at most MAX_WORDS."""
    cleaned = _clean(text).lower()
    found = []
    for word in re.split(r"[\s,;:/()!?\"«»“”+&|*<>\[\]{}=~^]+", cleaned):
        word = word.strip(".'’-")
        if word:
            found.append(word)
    return found[:MAX_WORDS]


def _phrase_at(words: list[str | None], start: int, length: int) -> str | None:
    """The words from start as one phrase, or None if one of them is used already."""
    if start + length > len(words):
        return None
    taken = []
    for position in range(start, start + length):
        if words[position] is None:
            return None
        taken.append(words[position])
    return " ".join(taken)


def _take(words: list[str | None], start: int, length: int) -> None:
    for position in range(start, start + length):
        words[position] = None


def _take_phrases(words: list[str | None], phrases: list[str]) -> bool:
    """Take every occurrence of these phrases out of the words, longest first; True if any was there."""
    by_length = {}
    for phrase in phrases:
        length = len(phrase.split())
        if length not in by_length:
            by_length[length] = []
        by_length[length].append(phrase)
    found = False
    for length in sorted(by_length, reverse=True):
        for start in range(len(words)):
            phrase = _phrase_at(words, start, length)
            if phrase is not None and phrase in by_length[length]:
                _take(words, start, length)
                found = True
    return found


def _lexicon_word(word: str) -> bool:
    for name, items in lexicon().items():
        if word in items:
            return True
    return False


def _hint_before(original: list[str], start: int) -> bool:
    """A place hint ("near", "à", "bei") comes right before position start."""
    if start == 0:
        return False
    return original[start - 1] in lexicon()["place_hints"]


# --- age, phase ----------------------------------------------------------------------------------

def _take_age(words: list[str | None], interpretation: Interpretation) -> None:
    units = lexicon()["age_units"]
    age_words = lexicon()["age_words"]
    for position in range(len(words)):
        word = words[position]
        if word is None:
            continue
        number = None
        span = None
        joined = re.fullmatch(r"(\d{1,3})(yo|y|ans|j|yrs)", word)
        if joined is not None:
            number = int(joined.group(1))
            span = (position, 1)
        elif NUMBER.fullmatch(word):
            following = None
            if position + 1 < len(words):
                following = words[position + 1]
            previous = None
            if position > 0:
                previous = words[position - 1]
            if following is not None and following in units:
                number = int(word)
                span = (position, 2)
                if position + 2 < len(words) and words[position + 2] == "old":
                    span = (position, 3)
            elif previous is not None and previous in age_words:
                number = int(word)
                span = (position - 1, 2)
        if number is not None and number <= 120:
            interpretation.age = number
            _take(words, span[0], span[1])
            return


def _take_phase(words: list[str | None], interpretation: Interpretation) -> None:
    roman = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}
    for position in range(len(words) - 1):
        if words[position] not in lexicon()["phase_words"]:
            continue
        following = words[position + 1]
        if following is None:
            continue
        if following in roman:
            following = roman[following]
        if following in ("1", "2", "3", "4"):
            interpretation.phase = following
            _take(words, position, 2)
            return


# --- places --------------------------------------------------------------------------------------

def _take_postcode(words: list[str | None], conn: psycopg.Connection, interpretation: Interpretation) -> None:
    for position in range(len(words)):
        word = words[position]
        if word is None or not POSTCODE.fullmatch(word):
            continue
        found = geo_db.postcode_places(conn, word)
        if not found:
            continue
        # the likeliest first (the database orders them), or the one in the country the query names; up to
        # three others are offered
        chosen = found[0]
        if interpretation.country is not None:
            for candidate in found:
                if candidate.country_code == interpretation.country[0]:
                    chosen = candidate
                    break
        for other in found[:4]:
            if other is chosen:
                continue
            interpretation.alternatives.append(Alternative(
                f"{word} in {other.country_name}", {"where": f"{word} {other.country_name}"}))
        interpretation.place = Place("postcode", f"{chosen.postcode} {chosen.place_name}", chosen.country_code,
                                     chosen.country_name, chosen.latitude, chosen.longitude, None, word)
        _take(words, position, 1)
        return


def _candidates(words: list[str | None]) -> list[tuple[int, int, str]]:
    """Every run of up to MAX_PLACE_WORDS unused words, from the end of the query, longest first."""
    found = []
    for start in range(len(words) - 1, -1, -1):
        for length in range(MAX_PLACE_WORDS, 0, -1):
            phrase = _phrase_at(words, start, length)
            if phrase is not None:
                found.append((start, length, phrase))
    return found


def _take_country(words: list[str | None], conn: psycopg.Connection, interpretation: Interpretation) -> None:
    candidates = _candidates(words)
    names = []
    for _start, _length, phrase in candidates:
        names.append(phrase)
    if not names:
        return
    by_name = {}
    for country in geo_db.countries_named(conn, names):
        if country.name_key not in by_name:
            by_name[country.name_key] = country
    for start, length, phrase in candidates:
        if phrase in by_name and _phrase_at(words, start, length) is not None:
            country = by_name[phrase]
            interpretation.country = (country.iso2, country.name)
            _take(words, start, length)
            return


def _own_name(match, phrase: str) -> bool:
    """The place is called this (or its name begins with it), not only known by it in another
    language: "Marburg" is Marburg an der Lahn, not Maribor (Marburg an der Drau, an old German name)."""
    name = " ".join(words_of(match.name))
    return name == phrase or name.startswith(phrase + " ")


def _best_place(matches: list, country_hint: str | None, phrase: str, least: int):
    """The place a name most likely means, among EU/EEA places with at least `least` people (matches
    come largest first): in the country the query names, else one whose own name it is, else the
    largest. ("Roma" is Rome, not the village of Roma in Sweden: too small to count.)"""
    candidates = []
    for match in matches:
        if match.in_gazetteer and match.population >= least:
            candidates.append(match)
    if country_hint is not None:
        for match in candidates:
            if match.country_code == country_hint:
                return match
    for match in candidates:
        if _own_name(match, phrase):
            return match
    if candidates:
        return candidates[0]
    return None


def _take_place(words: list[str | None], original: list[str], conn: psycopg.Connection,
                interpretation: Interpretation) -> None:
    candidates = _candidates(words)
    names = []
    for _start, _length, phrase in candidates:
        names.append(phrase)
    if not names:
        return
    by_name = {}
    for place in geo_db.places_named(conn, names):
        if place.name_key not in by_name:
            by_name[place.name_key] = []
        by_name[place.name_key].append(place)
    condition_names = _condition_names(conn, names)
    country_hint = None
    if interpretation.country is not None:
        country_hint = interpretation.country[0]
    for start, length, phrase in candidates:
        if phrase not in by_name:
            continue
        hinted = _hint_before(original, start)
        if length == 1 and _lexicon_word(phrase) and not hinted:
            continue
        least = BIG_PLACE
        if hinted:
            least = SMALL_PLACE
        place = _best_place(by_name[phrase], country_hint, phrase, least)
        if place is None:
            continue
        if phrase in condition_names and not hinted:
            # a condition's name first ("marburg"); being near the place is the other reading
            interpretation.alternatives.append(Alternative(
                f"near {place.name}, {place.country_name}", {"where": f"{place.name}, {place.country_name}"}))
            continue
        interpretation.place = Place("city", place.name, place.country_code, place.country_name, place.latitude,
                                     place.longitude, place.geonameid, phrase)
        _take(words, start, length)
        if hinted:
            words[start - 1] = None
        others = []
        for other in by_name[phrase]:
            if other.in_gazetteer and other.geonameid != place.geonameid and other.population >= least:
                others.append(other)
        for other in others[:3]:
            interpretation.alternatives.append(Alternative(
                f"near {other.name}, {other.country_name}", {"where": f"{other.name}, {other.country_name}"}))
        return


# --- the whole query -----------------------------------------------------------------------------

def _take_filters(words: list[str | None], interpretation: Interpretation) -> None:
    if _take_phrases(words, lexicon()["healthy_volunteers"]):
        interpretation.healthy_volunteers = True
    if _take_phrases(words, lexicon()["children"]):
        interpretation.age_group = CHILDREN
    if _take_phrases(words, lexicon()["older"]):
        interpretation.age_group = OLDER
    if _take_phrases(words, lexicon()["female"]):
        interpretation.sex = "female"
    if _take_phrases(words, lexicon()["male"]):
        interpretation.sex = "male"
    if _take_phrases(words, lexicon()["rare"]):
        interpretation.rare = True


def _condition_words(words: list[str | None]) -> list[str]:
    found = []
    for word in words:
        if word is None:
            continue
        if word in lexicon()["stop_words"] or word in lexicon()["place_hints"]:
            continue
        if not LEXEME.search(word):
            continue
        found.append(word)
    return found


def parse(what: str, conn: psycopg.Connection, where: str | None = None) -> Interpretation:
    """Read a query: typed in one box (`what` alone: its place is read from it too), or in two (the
    condition in `what`, no place read from it, and the place in `where`)."""
    text = what
    if where is not None:
        text = f"{what} {where}".strip()
    interpretation = Interpretation(text=text)
    original = words_of(what)
    words: list[str | None] = list(original)
    _take_age(words, interpretation)
    _take_phase(words, interpretation)
    _take_filters(words, interpretation)
    if where is None:
        _read_place(words, original, conn, interpretation)
    else:
        place_original = words_of(where)
        place_words: list[str | None] = list(place_original)
        if place_words:
            _read_place(place_words, place_original, conn, interpretation)
            left = _condition_words(place_words)
            if left and interpretation.place is None and interpretation.country is None:
                interpretation.notes.append(f"no place called “{' '.join(left)}” in the EU/EEA")
    _read_conditions(_condition_words(words), conn, interpretation)
    _settle_place(interpretation)
    return interpretation


def _read_place(words: list[str | None], original: list[str], conn: psycopg.Connection,
                interpretation: Interpretation) -> None:
    """A country, a postal code, else a place, taken from the words."""
    _take_country(words, conn, interpretation)
    _take_postcode(words, conn, interpretation)
    if interpretation.place is None:
        _take_place(words, original, conn, interpretation)


def _condition_names(conn: psycopg.Connection, names: list[str]) -> list[str]:
    """The names that are a MeSH entry term of a condition (so a word like "marburg" is read as one)."""
    found = []
    for match in vocab_db.terms_named(conn, names):
        if match.key not in found:
            found.append(match.key)
    return found


def _read_conditions(words: list[str], conn: psycopg.Connection, interpretation: Interpretation) -> None:
    interpretation.condition_words = words
    if not words:
        return
    interpretation.concepts = concepts.for_query(conn, words)
    for word in words:
        for slug in categories.for_word(word):
            if slug not in interpretation.categories:
                interpretation.categories.append(slug)


def _settle_place(interpretation: Interpretation) -> None:
    """A place gets the default distance, and wins over a country also named (the place says where in
    it); a country named with no place means the whole country."""
    if interpretation.place is None:
        return
    interpretation.radius_km = DEFAULT_RADIUS_KM
    if interpretation.country is not None and interpretation.country[0] != interpretation.place.country_code:
        interpretation.notes.append(f"{interpretation.place.name} is in {interpretation.place.country_name}")
    interpretation.country = None


# --- the search's tsquery ------------------------------------------------------------------------

def _lexeme(word: str) -> str | None:
    """A query word as a tsquery operand: letters and digits only, a prefix once long enough."""
    parts = LEXEME.findall(word.lower())
    if not parts:
        return None
    operands = []
    for part in parts:
        if len(part) >= PLURAL_LETTERS and part.endswith("s") and not part.endswith("ss"):
            part = part[:-1]
        if len(part) >= PREFIX_LETTERS:
            operands.append(f"{part}:*")
        else:
            operands.append(part)
    return " & ".join(operands)


def _word_operand(word: str) -> str | None:
    lexeme = _lexeme(word)
    if lexeme is None:
        return None
    alternatives = [f"({lexeme})"]
    for slug in categories.for_word(word):
        alternatives.append(categories.token(slug))
    return "(" + " | ".join(alternatives) + ")"


def carries_meaning(word: str) -> bool:
    """A word that can find trials by itself: not a number or a short word ("type 2", "typ", "de")."""
    letters = 0
    for character in word:
        if character.isalpha():
            letters += 1
    return letters >= PREFIX_LETTERS


def tsquery_text(interpretation: Interpretation, relaxed: bool = False) -> str | None:
    """The tsquery the search runs: every condition word (or its category), or a MeSH concept the
    words name; `relaxed`: any of the words that carry meaning on their own. None: no condition words
    (every recruiting trial)."""
    operands = []
    for word in interpretation.condition_words:
        if relaxed and not carries_meaning(word):
            continue
        operand = _word_operand(word)
        if operand is not None:
            operands.append(operand)
    if not operands:
        return None
    joiner = " & "
    if relaxed:
        joiner = " | "
    text = joiner.join(operands)
    concept_tokens = []
    for ui, _name in interpretation.concepts:
        concept_tokens.append(concepts.token(ui))
    if concept_tokens:
        text = f"({text}) | {' | '.join(concept_tokens)}"
    return text


@dataclass(frozen=True)
class Term:
    """One thing a search looks for, as tsquery_text puts it in the tsquery: a condition word, a category
    a word names, or a MeSH concept the words name."""

    # word | category | concept
    kind: str
    # as the page names it: the word, the category's name, the concept's name
    label: str
    tsquery: str
    # the word a category term comes from; a concept's id
    word: str | None = None
    ui: str | None = None


def terms(interpretation: Interpretation) -> list[Term]:
    """What the search's tsquery is made of, one term at a time (to say why a trial was found)."""
    found = []
    for word in interpretation.condition_words:
        lexeme = _lexeme(word)
        if lexeme is None:
            continue
        found.append(Term("word", word, lexeme))
        for slug in categories.for_word(word):
            category = categories.by_slug(slug)
            found.append(Term("category", category.name, categories.token(slug), word=word))
    for ui, name in interpretation.concepts:
        found.append(Term("concept", name, concepts.token(ui), ui=ui))
    return found


def describe(interpretation: Interpretation) -> list[str]:
    """How the query was read, part by part, for the page ("diabetes", "near Paris, France", "within 50 km")."""
    parts = []
    if interpretation.condition_words:
        parts.append(" ".join(interpretation.condition_words))
    else:
        parts.append("any condition")
    if interpretation.place is not None:
        parts.append(f"near {interpretation.place.name}, {interpretation.place.country_name}")
        if interpretation.radius_km is None:
            parts.append(f"anywhere in {interpretation.place.country_name}")
        else:
            parts.append(f"within {interpretation.radius_km} km")
    elif interpretation.country is not None:
        parts.append(f"in {interpretation.country[1]}")
    else:
        parts.append("anywhere in the EU/EEA")
    if interpretation.healthy_volunteers is True:
        parts.append("healthy volunteers")
    if interpretation.age is not None:
        parts.append(f"age {interpretation.age}")
    elif interpretation.age_group in AGE_GROUP_WORDS:
        parts.append(AGE_GROUP_WORDS[interpretation.age_group])
    if interpretation.sex is not None:
        parts.append(interpretation.sex)
    if interpretation.phase is not None:
        parts.append(f"phase {interpretation.phase}")
    if interpretation.rare is True:
        parts.append("rare diseases")
    return parts
