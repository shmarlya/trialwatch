"""GeoNames reference data (https://www.geonames.org, CC BY 4.0) for patient search (ADR-008): the
countries, the populated places of the EU/EEA with their names in many languages, and their postal
codes with a point each. Read from GeoNames' download files, fetched once per load (or read from a
folder that holds them), never called per site or per search.

Files (https://download.geonames.org/export/):
- dump/countryInfo.txt: one country a line (ISO codes, English name, languages)
- dump/cities1000.zip: every place with 1000+ people, with its alternate names (Wien, Vienna, Vienne)
- zip/<CC>.zip: a country's postal codes, each with a place name and a point
"""

import io
import json
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from trialwatch import reference


GEONAMES = "https://download.geonames.org/export"
COUNTRY_INFO = "countryInfo.txt"
CITIES = "cities1000.zip"
LICENSE = "GeoNames, CC BY 4.0 (https://www.geonames.org)"
COUNTRY_NAMES = Path(__file__).with_name("countries.json")


def url_of(file_name: str) -> str:
    """Where GeoNames serves a file: the dump files and, for <CC>.zip, the postal code files."""
    if file_name in (COUNTRY_INFO, CITIES):
        return f"{GEONAMES}/dump/{file_name}"
    return f"{GEONAMES}/zip/{file_name}"


def eea_names() -> dict[str, list[str]]:
    """ISO code -> the names people type for each EU/EEA country (src/trialwatch/geo/countries.json)."""
    with COUNTRY_NAMES.open(encoding="utf-8") as file:
        data = json.load(file)
    return data["eea"]


def eea_countries() -> list[str]:
    codes = []
    for code in eea_names():
        codes.append(code)
    return codes


@dataclass(frozen=True)
class Country:
    iso2: str
    iso3: str
    name: str
    languages: tuple[str, ...]


@dataclass(frozen=True)
class Place:
    geonameid: int
    name: str
    country_code: str
    population: int
    latitude: float
    longitude: float
    # its own name, its ASCII name and every alternate name GeoNames gives
    names: tuple[str, ...]


@dataclass(frozen=True)
class Postcode:
    country_code: str
    postcode: str
    place_name: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class SourceFile:
    """A GeoNames file as read: where from, and its sha256 (recorded with the load run)."""

    name: str
    origin: str
    path: Path
    sha256: str


# --- getting the files -------------------------------------------------------------------------

def get_file(name: str, from_dir: Path | None, work_dir: Path) -> SourceFile | None:
    """The file from `from_dir` when given (a .zip may also be there as its .txt), else downloaded into
    `work_dir`. None when there is no such file (a country with no postal code file)."""
    if from_dir is not None:
        candidates = [from_dir / name]
        if name.endswith(".zip"):
            candidates.append(from_dir / (name[:-4] + ".txt"))
        for path in candidates:
            if path.exists():
                return SourceFile(name, str(path), path, reference.sha256_of(path))
        return None
    url = url_of(name)
    path = work_dir / name
    # False: GeoNames has no such file (it has no postal codes for Greece)
    if not reference.download(url, path, timeout=60.0):
        return None
    return SourceFile(name, url, path, reference.sha256_of(path))


def work_folder() -> tempfile.TemporaryDirectory:
    """A temporary folder for downloads (the project's data folder may be read-only in a container)."""
    return tempfile.TemporaryDirectory(prefix="trialwatch-geonames-")


def _lines(path: Path) -> Iterator[str]:
    """The lines of a text file, or of the .txt file inside a zip of the same name."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            member = path.stem + ".txt"
            with archive.open(member) as raw:
                for line in io.TextIOWrapper(raw, encoding="utf-8"):
                    yield line.rstrip("\n")
        return
    with path.open(encoding="utf-8") as file:
        for line in file:
            yield line.rstrip("\n")


# --- reading them ------------------------------------------------------------------------------

def read_countries(path: Path) -> list[Country]:
    """countryInfo.txt: ISO, ISO3, ISO-numeric, fips, Country, Capital, Area, Population, Continent, tld,
    CurrencyCode, CurrencyName, Phone, Postal Code Format, Postal Code Regex, Languages, geonameid, ..."""
    found = []
    for line in _lines(path):
        if not line or line.startswith("#"):
            continue
        columns = line.split("\t")
        if len(columns) < 16:
            continue
        languages = []
        for language in columns[15].split(","):
            if language.strip():
                languages.append(language.strip())
        found.append(Country(columns[0], columns[1], columns[4], tuple(languages)))
    return found


def _number(text: str) -> int:
    if text.strip().isdigit():
        return int(text)
    return 0


def read_places(path: Path, countries: list[str]) -> list[Place]:
    """cities1000: geonameid, name, asciiname, alternatenames, latitude, longitude, feature class,
    feature code, country code, cc2, admin1-4, population, elevation, dem, timezone, modified; the
    places of these countries only."""
    wanted = set(countries)
    found = []
    for line in _lines(path):
        columns = line.split("\t")
        if len(columns) < 15 or columns[8] not in wanted:
            continue
        names = [columns[1]]
        if columns[2] and columns[2] not in names:
            names.append(columns[2])
        for name in columns[3].split(","):
            if name and name not in names:
                names.append(name)
        found.append(Place(
            geonameid=int(columns[0]), name=columns[1], country_code=columns[8], population=_number(columns[14]),
            latitude=float(columns[4]), longitude=float(columns[5]), names=tuple(names),
        ))
    return found


def read_postcodes(path: Path) -> list[Postcode]:
    """A postal code file: country code, postal code, place name, admin name1, admin code1, admin name2,
    admin code2, admin name3, admin code3, latitude, longitude, accuracy."""
    found = []
    for line in _lines(path):
        columns = line.split("\t")
        if len(columns) < 11 or not columns[9] or not columns[10]:
            continue
        found.append(Postcode(columns[0], columns[1], columns[2], float(columns[9]), float(columns[10])))
    return found
