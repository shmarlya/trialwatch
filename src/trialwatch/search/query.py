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
from typing import Protocol

from trialwatch.search import categories, concepts

LEXICON_FILE = Path(__file__).with_name("lexicon.json")
MAX_CHARACTERS = 200
MAX_WORDS = 15
MAX_PLACE_WORDS = 3
DEFAULT_RADIUS_KM = 50
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


class Lookups(Protocol):
    """What reading a query looks up (search/lookups.py: the database; tests: a fake)."""

    def places(self, names: list[str]) -> list: ...

    def countries(self, names: list[str]) -> list: ...

    def postcodes(self, code: str) -> list: ...

    def concepts(self, words: list[str]) -> list[tuple[str, str]]: ...

    def condition_names(self, names: list[str]) -> list[str]: ...


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

def _take_postcode(words: list[str | None], lookups: Lookups, interpretation: Interpretation) -> None:
    for position in range(len(words)):
        word = words[position]
        if word is None or not POSTCODE.fullmatch(word):
            continue
        found = lookups.postcodes(word)
        if not found:
            continue
        # the likeliest first (lookups order them), or the one in the country the query names; up to
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


def _take_country(words: list[str | None], lookups: Lookups, interpretation: Interpretation) -> None:
    candidates = _candidates(words)
    names = []
    for _start, _length, phrase in candidates:
        names.append(phrase)
    if not names:
        return
    by_name = {}
    for country in lookups.countries(names):
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
        if match.eea and match.population >= least:
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


def _take_place(words: list[str | None], original: list[str], lookups: Lookups,
                interpretation: Interpretation) -> None:
    candidates = _candidates(words)
    names = []
    for _start, _length, phrase in candidates:
        names.append(phrase)
    if not names:
        return
    by_name = {}
    for place in lookups.places(names):
        if place.name_key not in by_name:
            by_name[place.name_key] = []
        by_name[place.name_key].append(place)
    condition_names = lookups.condition_names(names)
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
            if other.eea and other.geonameid != place.geonameid and other.population >= least:
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
        interpretation.age_group = "0-17"
    if _take_phrases(words, lexicon()["older"]):
        interpretation.age_group = "65+"
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


def parse(text: str, lookups: Lookups) -> Interpretation:
    """Read a query typed in one box."""
    interpretation = Interpretation(text=text)
    original = words_of(text)
    words: list[str | None] = []
    for word in original:
        words.append(word)
    _take_age(words, interpretation)
    _take_phase(words, interpretation)
    _take_filters(words, interpretation)
    _take_country(words, lookups, interpretation)
    _take_postcode(words, lookups, interpretation)
    if interpretation.place is None:
        _take_place(words, original, lookups, interpretation)
    _read_conditions(_condition_words(words), lookups, interpretation)
    _settle_place(interpretation)
    return interpretation


def parse_parts(what: str, where: str, lookups: Lookups) -> Interpretation:
    """Read a query given in two boxes: the condition (no place read from it) and the place."""
    interpretation = Interpretation(text=f"{what} {where}".strip())
    words: list[str | None] = []
    for word in words_of(what):
        words.append(word)
    _take_age(words, interpretation)
    _take_phase(words, interpretation)
    _take_filters(words, interpretation)
    place_words: list[str | None] = []
    original = words_of(where)
    for word in original:
        place_words.append(word)
    if place_words:
        _take_country(place_words, lookups, interpretation)
        _take_postcode(place_words, lookups, interpretation)
        if interpretation.place is None:
            _take_place(place_words, original, lookups, interpretation)
        left = _condition_words(place_words)
        if left and interpretation.place is None and interpretation.country is None:
            interpretation.notes.append(f"no place called “{' '.join(left)}” in the EU/EEA")
    _read_conditions(_condition_words(words), lookups, interpretation)
    _settle_place(interpretation)
    return interpretation


def _read_conditions(words: list[str], lookups: Lookups, interpretation: Interpretation) -> None:
    interpretation.condition_words = words
    if not words:
        return
    interpretation.concepts = lookups.concepts(words)
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


def _carries_meaning(word: str) -> bool:
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
        if relaxed and not _carries_meaning(word):
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
    elif interpretation.age_group == "0-17":
        parts.append("children (0–17)")
    elif interpretation.age_group == "65+":
        parts.append("older people (65+)")
    if interpretation.sex is not None:
        parts.append(interpretation.sex)
    if interpretation.phase is not None:
        parts.append(f"phase {interpretation.phase}")
    if interpretation.rare is True:
        parts.append("rare diseases")
    return parts
