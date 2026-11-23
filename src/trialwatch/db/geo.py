"""Queries on the geo schema (ADR-008, patient search): GeoNames reference data, where each trial
site is (geo.address_point), and the places a search names. Names are compared by geo.name_key and
postcodes by geo.postcode_key, both in SQL, so what is stored and what is looked up are always
normalised the same way.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

# --- loading reference data ----------------------------------------------------------------------


def start_load(conn: psycopg.Connection, countries: Sequence[str]) -> int:
    row = conn.execute(
        "INSERT INTO geo.load_run (countries, created_by) VALUES (%s, current_setting('trialwatch.actor')::bigint) "
        "RETURNING id",
        (list(countries),),
    ).fetchone()
    return row[0]


def finish_load(conn: psycopg.Connection, run_id: int, files: list[dict], places: int, postcodes: int) -> None:
    conn.execute(
        "UPDATE geo.load_run SET files = %s, places = %s, postcodes = %s, finished_at = clock_timestamp() WHERE id = %s",
        (Jsonb(files), places, postcodes, run_id),
    )


def replace_countries(conn: psycopg.Connection, countries: Sequence[tuple[str, str, str, list[str], bool]],
                      names: Sequence[tuple[str, str]]) -> None:
    """Every country (iso2, iso3, English name, languages, in the EU/EEA) and the names people use for
    them ((iso2, name)), replacing what was there."""
    conn.execute("DELETE FROM geo.country_name")
    conn.execute("DELETE FROM geo.country")
    with conn.cursor() as cur:
        with cur.copy("COPY geo.country (iso2, iso3, name, languages, in_gazetteer) FROM STDIN") as copy:
            for country in countries:
                copy.write_row(country)
    conn.execute("CREATE TEMPORARY TABLE country_name_in (iso2 char(2), name text) ON COMMIT DROP")
    with conn.cursor() as cur:
        with cur.copy("COPY country_name_in (iso2, name) FROM STDIN") as copy:
            for name in names:
                copy.write_row(name)
    conn.execute(
        """
        INSERT INTO geo.country_name (name_key, iso2, name)
        SELECT DISTINCT ON (geo.name_key(i.name), i.iso2) geo.name_key(i.name), i.iso2, i.name
        FROM country_name_in i JOIN geo.country c ON c.iso2 = i.iso2
        WHERE geo.name_key(i.name) <> ''
        ORDER BY geo.name_key(i.name), i.iso2, i.name
        """
    )
    conn.execute("DROP TABLE country_name_in")


def replace_places(conn: psycopg.Connection, places: Sequence[tuple[int, str, str, int, float, float]],
                   names: Sequence[tuple[int, str]]) -> None:
    """The places ((geonameid, name, country, population, latitude, longitude)) and every name of each
    ((geonameid, name)), replacing what was there."""
    conn.execute("DELETE FROM geo.place")
    with conn.cursor() as cur:
        with cur.copy("COPY geo.place (geonameid, name, country_code, population, latitude, longitude) FROM STDIN") as copy:
            for place in places:
                copy.write_row(place)
    conn.execute("CREATE TEMPORARY TABLE place_name_in (geonameid bigint, name text) ON COMMIT DROP")
    with conn.cursor() as cur:
        with cur.copy("COPY place_name_in (geonameid, name) FROM STDIN") as copy:
            for name in names:
                copy.write_row(name)
    conn.execute(
        "INSERT INTO geo.place_name (geonameid, name_key) "
        "SELECT DISTINCT geonameid, geo.name_key(name) FROM place_name_in WHERE geo.name_key(name) <> ''"
    )
    conn.execute("DROP TABLE place_name_in")


def replace_postcodes(conn: psycopg.Connection, country: str, postcodes: Sequence[tuple[str, str, str, float, float]]) -> int:
    """A country's postal codes ((country, postcode, place name, latitude, longitude)), replacing its
    earlier ones. Returns how many it holds."""
    conn.execute("DELETE FROM geo.postcode WHERE country_code = %s", (country,))
    conn.execute("CREATE TEMPORARY TABLE postcode_in (country_code char(2), postcode text, place_name text, "
                 "latitude double precision, longitude double precision) ON COMMIT DROP")
    with conn.cursor() as cur:
        with cur.copy("COPY postcode_in (country_code, postcode, place_name, latitude, longitude) FROM STDIN") as copy:
            for postcode in postcodes:
                copy.write_row(postcode)
    cursor = conn.execute(
        "INSERT INTO geo.postcode (country_code, postcode_key, postcode, place_name, latitude, longitude) "
        "SELECT country_code, geo.postcode_key(postcode), postcode, place_name, latitude, longitude FROM postcode_in "
        "WHERE country_code = %s AND geo.postcode_key(postcode) <> ''",
        (country,),
    )
    conn.execute("DROP TABLE postcode_in")
    return cursor.rowcount


def forget_points(conn: psycopg.Connection) -> int:
    """Forget where every site address is, so geocoding finds them again (after a load, or --again)."""
    return conn.execute("DELETE FROM geo.address_point").rowcount


# --- where each trial site is ------------------------------------------------------------------

@dataclass(frozen=True)
class Address:
    """A distinct site address of core.trial_location, as written and as keys."""

    country: str
    postcode: str | None
    city: str | None
    country_key: str
    postcode_key: str
    city_key: str


def addresses_without_point(conn: psycopg.Connection) -> list[Address]:
    """Site addresses (a city or a postcode in a country) not geocoded yet."""
    with conn.cursor(row_factory=class_row(Address)) as cur:
        return cur.execute(
            """
            SELECT DISTINCT ON (l.country_key, l.postcode_key, l.city_key)
                   l.country, l.postcode, l.city, l.country_key, l.postcode_key, l.city_key
            FROM core.trial_location l
            WHERE (l.city_key <> '' OR l.postcode_key <> '')
              AND NOT EXISTS (SELECT FROM geo.address_point a WHERE a.country_key = l.country_key
                                 AND a.postcode_key = l.postcode_key AND a.city_key = l.city_key)
            ORDER BY l.country_key, l.postcode_key, l.city_key, l.id
            """
        ).fetchall()


def country_code(conn: psycopg.Connection, name: str) -> str | None:
    """The ISO code of a country by any name we know it by."""
    row = conn.execute(
        "SELECT iso2 FROM geo.country_name WHERE name_key = geo.name_key(%s) ORDER BY iso2 LIMIT 1", (name,)
    ).fetchone()
    if row is None:
        return None
    return row[0]


@dataclass(frozen=True)
class Point:
    name: str
    latitude: float
    longitude: float
    population: int = 0


def postcode_points(conn: psycopg.Connection, iso2: str, postcode_key: str) -> list[Point]:
    """The places GeoNames gives for this postal code in this country (a code can cover several)."""
    with conn.cursor(row_factory=class_row(Point)) as cur:
        return cur.execute(
            "SELECT place_name AS name, latitude, longitude FROM geo.postcode "
            "WHERE country_code = %s AND postcode_key = %s ORDER BY place_name",
            (iso2, postcode_key),
        ).fetchall()


def city_points(conn: psycopg.Connection, iso2: str, city: str) -> list[Point]:
    """Places of this country with exactly this name (in any language), largest first."""
    with conn.cursor(row_factory=class_row(Point)) as cur:
        return cur.execute(
            """
            SELECT p.name, p.latitude, p.longitude, p.population
            FROM geo.place_name n JOIN geo.place p ON p.geonameid = n.geonameid
            WHERE n.name_key = geo.name_key(%s) AND p.country_code = %s
            ORDER BY p.population DESC, p.geonameid
            """,
            (city, iso2),
        ).fetchall()


# names closer than this are taken as the same place spelled differently ("Wroclaw" for "Wrocław" is
# exact anyway; this is for "Bruxelles-Capitale", "S. Giovanni Rotondo")
FUZZY_SIMILARITY = 0.6


def fuzzy_city_point(conn: psycopg.Connection, iso2: str, city: str) -> Point | None:
    """The place of this country whose name is most like this one, if alike enough."""
    with conn.cursor(row_factory=class_row(Point)) as cur:
        return cur.execute(
            """
            SELECT p.name, p.latitude, p.longitude, p.population
            FROM geo.place_name n JOIN geo.place p ON p.geonameid = n.geonameid
            WHERE n.name_key %% geo.name_key(%(city)s) AND p.country_code = %(country)s
              AND similarity(n.name_key, geo.name_key(%(city)s)) >= %(least)s
            ORDER BY similarity(n.name_key, geo.name_key(%(city)s)) DESC, p.population DESC, p.geonameid
            LIMIT 1
            """,
            {"city": city, "country": iso2, "least": FUZZY_SIMILARITY},
        ).fetchone()


def save_point(conn: psycopg.Connection, address: Address, iso2: str | None, point: Point | None, precision: str) -> None:
    latitude = None
    longitude = None
    matched = None
    if point is not None:
        latitude = point.latitude
        longitude = point.longitude
        matched = point.name
    conn.execute(
        """
        INSERT INTO geo.address_point (country_key, postcode_key, city_key, iso2, latitude, longitude, precision, matched)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (country_key, postcode_key, city_key) DO UPDATE SET
            iso2 = EXCLUDED.iso2, latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
            precision = EXCLUDED.precision, matched = EXCLUDED.matched, geocoded_at = clock_timestamp()
        """,
        (address.country_key, address.postcode_key, address.city_key, iso2, latitude, longitude, precision, matched),
    )


def precision_counts(conn: psycopg.Connection) -> dict[str, int]:
    """How many site addresses were found by each precision."""
    counts = {}
    for precision, count in conn.execute(
        "SELECT precision, count(*) FROM geo.address_point GROUP BY precision ORDER BY precision"
    ).fetchall():
        counts[precision] = count
    return counts


def unmatched_cities(conn: psycopg.Connection, limit: int) -> list[tuple[str, str, int]]:
    """(country, city, sites) of the site addresses no point was found for, most sites first."""
    rows = conn.execute(
        """
        SELECT min(l.country), min(l.city), count(*)
        FROM geo.address_point a
        JOIN core.trial_location l ON l.country_key = a.country_key AND l.postcode_key = a.postcode_key
                                  AND l.city_key = a.city_key
        WHERE a.precision = 'none'
        GROUP BY a.country_key, a.city_key
        ORDER BY count(*) DESC, min(l.city)
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    found = []
    for country, city, sites in rows:
        found.append((country, city, sites))
    return found


# --- places a search names ---------------------------------------------------------------------

@dataclass(frozen=True)
class NamedPlace:
    """A place one of the given names matched, with its country; `name_key` is the name given."""

    name_key: str
    geonameid: int
    name: str
    country_code: str
    country_name: str
    # in a country whose places are loaded (TRIALWATCH_COUNTRIES)
    in_gazetteer: bool
    population: int
    latitude: float
    longitude: float


def places_named(conn: psycopg.Connection, names: Sequence[str]) -> list[NamedPlace]:
    """Every place whose name (in any language) is one of these, keyed by the name matched."""
    with conn.cursor(row_factory=class_row(NamedPlace)) as cur:
        return cur.execute(
            """
            SELECT given AS name_key, p.geonameid, p.name, p.country_code, c.name AS country_name, c.in_gazetteer,
                   p.population, p.latitude, p.longitude
            FROM unnest(%s::text[]) AS given
            JOIN geo.place_name n ON n.name_key = geo.name_key(given)
            JOIN geo.place p ON p.geonameid = n.geonameid
            JOIN geo.country c ON c.iso2 = p.country_code
            ORDER BY given, p.population DESC, p.geonameid
            """,
            (list(names),),
        ).fetchall()


@dataclass(frozen=True)
class NamedCountry:
    name_key: str
    iso2: str
    name: str


def countries_named(conn: psycopg.Connection, names: Sequence[str]) -> list[NamedCountry]:
    """The countries known by one of these names, keyed by the name matched; English name."""
    with conn.cursor(row_factory=class_row(NamedCountry)) as cur:
        return cur.execute(
            """
            SELECT given AS name_key, c.iso2, c.name
            FROM unnest(%s::text[]) AS given
            JOIN geo.country_name n ON n.name_key = geo.name_key(given)
            JOIN geo.country c ON c.iso2 = n.iso2
            ORDER BY given, c.in_gazetteer DESC, c.iso2
            """,
            (list(names),),
        ).fetchall()


@dataclass(frozen=True)
class PostcodePlace:
    country_code: str
    country_name: str
    postcode: str
    place_name: str
    latitude: float
    longitude: float


def postcode_places(conn: psycopg.Connection, postcode: str) -> list[PostcodePlace]:
    """Where this postal code is, in every EU/EEA country that has it, one point per country (the
    middle of its places). A code is often in several countries (75013 is in France, Germany, Italy,
    Estonia...): the one whose trial sites use it most comes first, then the one whose place is the
    largest (Paris), so the reading people most likely mean is first and the others are alternatives."""
    with conn.cursor(row_factory=class_row(PostcodePlace)) as cur:
        return cur.execute(
            """
            WITH found AS (
                SELECT p.country_code, c.name AS country_name, min(p.postcode) AS postcode,
                       min(p.place_name) AS place_name, avg(p.latitude) AS latitude, avg(p.longitude) AS longitude
                FROM geo.postcode p JOIN geo.country c ON c.iso2 = p.country_code
                WHERE p.postcode_key = geo.postcode_key(%s) AND c.in_gazetteer
                GROUP BY p.country_code, c.name
            )
            SELECT f.country_code, f.country_name, f.postcode, f.place_name, f.latitude, f.longitude
            FROM found f
            ORDER BY
                (SELECT count(*) FROM core.trial_location l JOIN geo.country_name n ON n.name_key = l.country_key
                 WHERE n.iso2 = f.country_code AND l.postcode_key = geo.postcode_key(f.postcode)) DESC,
                (SELECT max(pl.population) FROM geo.place_name pn JOIN geo.place pl ON pl.geonameid = pn.geonameid
                 WHERE pl.country_code = f.country_code AND pn.name_key = geo.name_key(f.place_name)) DESC NULLS LAST,
                f.country_code
            """,
            (postcode,),
        ).fetchall()


def loaded(conn: psycopg.Connection) -> bool:
    """Whether GeoNames places were loaded at all (searching by place needs them)."""
    return conn.execute("SELECT EXISTS (SELECT FROM geo.place)").fetchone()[0]


def country_languages(conn: psycopg.Connection, iso2: str) -> list[str]:
    """A country's languages, most spoken first (GeoNames, e.g. ["fr-BE", "nl-BE", "de-BE"])."""
    row = conn.execute("SELECT languages FROM geo.country WHERE iso2 = %s", (iso2,)).fetchone()
    if row is None:
        return []
    return row[0]
