"""Loading GeoNames into the geo schema, and finding where each trial site is (ADR-008).

Geocoding a site address, in this order (each step only when the one before found nothing):
1. its postal code in its country (GeoNames postal codes): the place of that code whose name is the
   site's city, else the middle of the code's places;
2. a shorter form of its postal code (the longest prefix of 3+ characters GeoNames has: some countries'
   files hold only the first part of a code);
3. its city by name, in any language, in its country (the largest place of that name);
4. its city by a name like it (trigram similarity), in its country.
A postal code point more than POSTCODE_CITY_KM from the city of the same name is taken as a mistyped
code: the city wins. An address none of these finds is kept as 'none', and never in a distance search.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from trialwatch.db import geo as geo_db
from trialwatch.db import reference as reference_db
from trialwatch.geo import geonames

POSTCODE_CITY_KM = 40
# words in a site's city that are not part of its name ("Paris Cedex 13", "Villejuif CEDEX")
CITY_NOISE = ("cedex",)

Progress = Callable[[str], None] | None


def _report(progress: Progress, line: str) -> None:
    if progress is not None:
        progress(line)


@dataclass
class LoadSummary:
    run_id: int
    countries: int = 0
    places: int = 0
    postcodes: int = 0
    # postal code files GeoNames does not have for a country (or the folder did not hold)
    missing: list[str] = field(default_factory=list)


def load(conn: psycopg.Connection, countries: list[str], from_dir: Path | None = None,
         progress: Progress = None) -> LoadSummary:
    """Replace the geo reference data with GeoNames' files (downloaded, or read from `from_dir`), for
    these EU/EEA countries; then forget every site's point, so the next geocoding finds them with the
    new data. One transaction: a load that fails leaves the earlier data."""
    with geonames.work_folder() as work, conn.transaction():
        work_dir = Path(work)
        summary = LoadSummary(run_id=reference_db.start_load(conn, "geonames", geonames.LICENSE,
                                                             {"countries": list(countries)}))
        files = []

        country_file = _required(geonames.COUNTRY_INFO, from_dir, work_dir)
        files.append(_file_record(country_file))
        # the names people type in the main EU languages, and CTIS's spellings (geo/countries.json)
        names_by_country = geonames.eea_names()
        country_rows = []
        name_rows = []
        for country in geonames.read_countries(country_file.path):
            in_gazetteer = country.iso2 in countries
            country_rows.append((country.iso2, country.iso3, country.name, list(country.languages), in_gazetteer))
            name_rows.append((country.iso2, country.name))
            if country.iso2 in names_by_country:
                for name in names_by_country[country.iso2]:
                    name_rows.append((country.iso2, name))
        geo_db.replace_countries(conn, country_rows, name_rows)
        summary.countries = len(country_rows)
        _report(progress, f"countries: {summary.countries}")

        cities_file = _required(geonames.CITIES, from_dir, work_dir)
        places = geonames.read_places(cities_file.path, countries)
        files.append(_file_record(cities_file))
        place_rows = []
        place_names = []
        for place in places:
            place_rows.append((place.geonameid, place.name, place.country_code, place.population, place.latitude,
                               place.longitude))
            for name in place.names:
                place_names.append((place.geonameid, name))
        geo_db.replace_places(conn, place_rows, place_names)
        summary.places = len(place_rows)
        _report(progress, f"places: {summary.places} with {len(place_names)} names")

        for country in countries:
            postcode_file = geonames.get_file(f"{country}.zip", from_dir, work_dir)
            if postcode_file is None:
                summary.missing.append(country)
                continue
            files.append(_file_record(postcode_file))
            rows = []
            for postcode in geonames.read_postcodes(postcode_file.path):
                rows.append((postcode.country_code, postcode.postcode, postcode.place_name, postcode.latitude,
                             postcode.longitude))
            held = geo_db.replace_postcodes(conn, country, rows)
            summary.postcodes += held
            _report(progress, f"postcodes {country}: {held}")

        reference_db.finish_load(conn, summary.run_id, files, {"places": summary.places,
                                                                "postcodes": summary.postcodes})
        geo_db.forget_points(conn)
    return summary


def _required(name: str, from_dir: Path | None, work_dir: Path) -> geonames.SourceFile:
    found = geonames.get_file(name, from_dir, work_dir)
    if found is None:
        raise FileNotFoundError(f"{name} is not in {from_dir}")
    return found


def _file_record(source_file: geonames.SourceFile) -> dict:
    return {"name": source_file.name, "from": source_file.origin, "sha256": source_file.sha256}


# --- geocoding site addresses ------------------------------------------------------------------

@dataclass
class GeocodeSummary:
    addresses: int = 0
    # precision -> how many addresses it found
    found: dict[str, int] = field(default_factory=dict)


def _city_name(city: str | None) -> str | None:
    """The site's city without words that are not part of the name ("Paris Cedex 13" -> "Paris")."""
    if city is None:
        return None
    words = []
    for word in city.replace("-", " - ").split():
        if word.lower() in CITY_NOISE or word.isdigit():
            continue
        words.append(word)
    name = " ".join(words).replace(" - ", "-").strip(" -")
    if not name:
        return None
    return name


def _same_name(first: str, second: str) -> bool:
    return " ".join(first.lower().split()) == " ".join(second.lower().split())


def _middle(points: list[geo_db.Point], name: str) -> geo_db.Point:
    """One point for a postal code's places: the place named like the site's city, else their middle."""
    for point in points:
        if _same_name(point.name, name):
            return point
    latitude = 0.0
    longitude = 0.0
    for point in points:
        latitude += point.latitude
        longitude += point.longitude
    return geo_db.Point(points[0].name, latitude / len(points), longitude / len(points))


def _postcode_point(conn: psycopg.Connection, iso2: str, postcode_key: str, city: str) -> geo_db.Point | None:
    if not postcode_key:
        return None
    points = geo_db.postcode_points(conn, iso2, postcode_key)
    if points:
        return _middle(points, city)
    # the longest prefix GeoNames has (e.g. its file holds "1012" for "1012AB")
    for length in range(len(postcode_key) - 1, 2, -1):
        points = geo_db.postcode_points(conn, iso2, postcode_key[:length])
        if points:
            return _middle(points, city)
    return None


def geocode_address(conn: psycopg.Connection, address: geo_db.Address) -> str:
    """Find and store where one site address is; returns the precision it was found by."""
    iso2 = geo_db.country_code(conn, address.country)
    if iso2 is None:
        geo_db.save_point(conn, address, None, None, "none")
        return "none"
    city = _city_name(address.city)
    city_point = None
    if city is not None:
        by_name = geo_db.city_points(conn, iso2, city)
        if by_name:
            city_point = by_name[0]
    city_text = ""
    if city is not None:
        city_text = city
    postcode_point = _postcode_point(conn, iso2, address.postcode_key, city_text)
    if postcode_point is not None:
        apart = None
        if city_point is not None:
            apart = geo_db.distance_km(conn, postcode_point.latitude, postcode_point.longitude, city_point.latitude,
                                       city_point.longitude)
        if apart is not None and apart > POSTCODE_CITY_KM:
            geo_db.save_point(conn, address, iso2, city_point, "city")
            return "city"
        geo_db.save_point(conn, address, iso2, postcode_point, "postcode")
        return "postcode"
    if city_point is not None:
        geo_db.save_point(conn, address, iso2, city_point, "city")
        return "city"
    if city is not None:
        fuzzy = geo_db.fuzzy_city_point(conn, iso2, city)
        if fuzzy is not None:
            geo_db.save_point(conn, address, iso2, fuzzy, "city-fuzzy")
            return "city-fuzzy"
    geo_db.save_point(conn, address, iso2, None, "none")
    return "none"


def geocode_missing(conn: psycopg.Connection, progress: Progress = None) -> GeocodeSummary:
    """Find where every site address not geocoded yet is (cheap when nothing is new: a transfer calls
    it for the sites it stored). Nothing is found before GeoNames is loaded: addresses wait."""
    summary = GeocodeSummary()
    if not geo_db.loaded(conn):
        return summary
    addresses = geo_db.addresses_without_point(conn)
    for number, address in enumerate(addresses, start=1):
        precision = geocode_address(conn, address)
        summary.addresses += 1
        if precision not in summary.found:
            summary.found[precision] = 0
        summary.found[precision] += 1
        if number % 500 == 0:
            _report(progress, f"{number}/{len(addresses)} addresses")
    return summary
