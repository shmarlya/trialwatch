"""Countries by their ISO 3166 two-letter code, from the names sources give them (countries.json).

Search compares countries by code (a trial's recruiting countries, its sites), so a second registry
spelling a country another way still meets the first. Sources give names; ingest and the search index
turn them into codes here, without the database. A name we don't know has no code: it is still shown,
but takes no part in finding trials by country (in CTIS: countries outside the EU/EEA, which have no
sites and no status).
"""

import json
import re
import unicodedata
from functools import cache
from pathlib import Path

COUNTRIES_FILE = Path(__file__).parent / "countries.json"
# anything but letters and digits, as geo.name_key (db/migrations) treats it
NOT_A_LETTER = re.compile(r"[\W_]+")


def name_key(name: str) -> str:
    """A name for comparing: lower case, no accents, anything but letters and digits as single spaces
    ("Česká republika" -> "ceska republika"), as the database's geo.name_key."""
    decomposed = unicodedata.normalize("NFKD", name.lower())
    letters = "".join(character for character in decomposed if not unicodedata.combining(character))
    return NOT_A_LETTER.sub(" ", letters).strip()


@cache
def _names() -> dict[str, str]:
    """Name key -> code, for every name countries.json gives a country."""
    by_key = {}
    data = json.loads(COUNTRIES_FILE.read_text())
    for code, names in data["eea"].items():
        for name in names:
            by_key[name_key(name)] = code
    return by_key


@cache
def _english_names() -> dict[str, str]:
    """Code -> the name pages show (the first, English, name countries.json gives)."""
    shown = {}
    data = json.loads(COUNTRIES_FILE.read_text())
    for code, names in data["eea"].items():
        shown[code] = names[0]
    return shown


def iso2_of(name: str | None) -> str | None:
    """The country's code, or None when the name is not one we know."""
    if not name:
        return None
    return _names().get(name_key(name))


def name_of(code: str) -> str:
    """The country's name as pages show it; the code itself for a country we have no name for."""
    return _english_names().get(code, code)
