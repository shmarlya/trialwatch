"""Queries on the search schema (ADR-008, patient search): saving search documents and the MeSH
concepts of a trial, and finding recruiting trials by words, place, distance and who can join.

A trial is found only while it recruits: its document says so (its profile) and no list of a source
that holds it says otherwise now (core.source_listing is re-read at every sync, so it is fresher than
a profile, which follows the record). Distances are from the nearest site in a country where the
trial recruits; a site whose address has no point (geo.address_point 'none') is never in a distance
search.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from math import cos, radians

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from trialwatch.db.overview import like_text

# ts_rank_cd's weights for D, C, B, A words (A: conditions; see search/index.py)
RANK_WEIGHTS = [0.1, 0.2, 0.6, 1.0]
# lexemes that are not words people type, kept out of "did you mean"
NOT_WORDS = "^(zmesh|zcat|zmeddra)"
# how alike a word must be to a known one to be suggested instead
SUGGEST_SIMILARITY = 0.45

# --- building documents --------------------------------------------------------------------------


def document_hash(conn: psycopg.Connection, trial_id: int) -> bytes | None:
    row = conn.execute("SELECT profile_hash FROM search.document WHERE trial_id = %s", (trial_id,)).fetchone()
    if row is None:
        return None
    return bytes(row[0])


def delete_document(conn: psycopg.Connection, trial_id: int) -> None:
    conn.execute("DELETE FROM search.trial_concept WHERE trial_id = %s", (trial_id,))
    conn.execute("DELETE FROM search.document WHERE trial_id = %s", (trial_id,))


def save_document(conn: psycopg.Connection, document) -> None:
    """Store a search document (search/index.py SearchDocument), its words weighted A to D."""
    conn.execute(
        """
        INSERT INTO search.document (trial_id, source_id, profile_hash, recruiting, recruiting_countries, title,
                                     conditions, categories, phase, age_groups, female, male, healthy_volunteers,
                                     patients, rare, annotation_id, age_min, age_max, interventions, purposes, tsv)
        VALUES (%(trial)s, (SELECT id FROM core.source WHERE code = %(source)s), %(hash)s, %(recruiting)s,
                %(countries)s, %(title)s, %(conditions)s, %(categories)s, %(phase)s, %(ages)s, %(female)s,
                %(male)s, %(healthy)s, %(patients)s, %(rare)s, %(annotation)s, %(age_min)s, %(age_max)s,
                %(interventions)s, %(purposes)s,
                setweight(to_tsvector('search.trialwatch', %(a)s), 'A')
                || setweight(array_to_tsvector(%(lexemes_a)s::text[]), 'A')
                || setweight(to_tsvector('search.trialwatch', %(b)s), 'B')
                || setweight(array_to_tsvector(%(lexemes_b)s::text[]), 'B')
                || setweight(to_tsvector('search.trialwatch', %(c)s), 'C')
                || setweight(to_tsvector('search.trialwatch', %(d)s), 'D'))
        ON CONFLICT (trial_id) DO UPDATE SET
            source_id = EXCLUDED.source_id, profile_hash = EXCLUDED.profile_hash, recruiting = EXCLUDED.recruiting,
            recruiting_countries = EXCLUDED.recruiting_countries, title = EXCLUDED.title,
            conditions = EXCLUDED.conditions, categories = EXCLUDED.categories, phase = EXCLUDED.phase,
            age_groups = EXCLUDED.age_groups, female = EXCLUDED.female, male = EXCLUDED.male,
            healthy_volunteers = EXCLUDED.healthy_volunteers, patients = EXCLUDED.patients, rare = EXCLUDED.rare,
            annotation_id = EXCLUDED.annotation_id, age_min = EXCLUDED.age_min, age_max = EXCLUDED.age_max,
            interventions = EXCLUDED.interventions, purposes = EXCLUDED.purposes,
            tsv = EXCLUDED.tsv, built_at = clock_timestamp()
        """,
        {"trial": document.trial_id, "source": document.source, "hash": document.profile_hash,
         "recruiting": document.recruiting, "countries": document.recruiting_countries, "title": document.title,
         "conditions": document.conditions, "categories": document.categories, "phase": document.phase,
         "ages": document.age_groups, "female": document.female, "male": document.male,
         "healthy": document.healthy_volunteers, "patients": document.patients, "rare": document.rare,
         "annotation": document.annotation_id, "age_min": document.age_min, "age_max": document.age_max,
         "interventions": document.interventions, "purposes": document.purposes,
         "a": document.text_a, "lexemes_a": document.lexemes_a, "b": document.text_b,
         "lexemes_b": document.lexemes_b, "c": document.text_c, "d": document.text_d},
    )


def save_concepts(conn: psycopg.Connection, trial_id: int, concepts: Sequence) -> None:
    """Replace the trial's MeSH concepts (search/concepts.py Concept)."""
    conn.execute("DELETE FROM search.trial_concept WHERE trial_id = %s", (trial_id,))
    for concept in concepts:
        conn.execute(
            "INSERT INTO search.trial_concept (trial_id, ui, method, field, quote) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (trial_id, concept.ui, concept.method, concept.field, concept.quote),
        )


def trials_with_profiles(conn: psycopg.Connection, after_trial_id: int, limit: int) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT trial_id FROM core.trial_profile WHERE trial_id > %s ORDER BY trial_id LIMIT %s",
        (after_trial_id, limit),
    ).fetchall()
    found = []
    for row in rows:
        found.append(row[0])
    return found


def refresh_terms(conn: psycopg.Connection) -> int:
    """The words of recruiting trials' conditions and titles (weights A and B), for "did you mean"."""
    conn.execute("DELETE FROM search.term")
    cursor = conn.execute(
        """
        INSERT INTO search.term (term, documents)
        SELECT word, ndoc FROM ts_stat('SELECT tsv FROM search.document WHERE recruiting', 'AB')
        WHERE length(word) >= 4 AND word !~ %s AND word ~ '^[[:alpha:]]+$'
        """,
        (NOT_WORDS,),
    )
    return cursor.rowcount


@dataclass(frozen=True)
class TrialConcept:
    ui: str
    name: str
    method: str
    field: str
    quote: str


def trial_concepts(conn: psycopg.Connection, trial_id: int) -> list[TrialConcept]:
    with conn.cursor(row_factory=class_row(TrialConcept)) as cur:
        return cur.execute(
            """
            SELECT c.ui, coalesce(d.name, c.ui) AS name, c.method, c.field, c.quote
            FROM search.trial_concept c LEFT JOIN vocab.mesh_descriptor d ON d.ui = c.ui
            WHERE c.trial_id = %s ORDER BY c.field, c.ui
            """,
            (trial_id,),
        ).fetchall()


# --- finding trials ----------------------------------------------------------------------------

# A trial no list now calls recruiting (or no longer lists at all) is not shown, whatever its document says
_STILL_RECRUITING = """NOT EXISTS (
    SELECT FROM core.trial_source ts JOIN core.source_listing l USING (source_id, source_key)
    WHERE ts.trial_id = d.trial_id AND (l.recruiting IS FALSE OR l.delisted_at IS NOT NULL))"""


def public_recruiting(conn: psycopg.Connection, trial_id: int) -> bool:
    """The trial recruits now (as search finds it): its page is public without a judgement (ADR-008)."""
    row = conn.execute(
        f"SELECT EXISTS (SELECT FROM search.document d WHERE d.trial_id = %s AND d.recruiting AND {_STILL_RECRUITING})",
        (trial_id,),
    ).fetchone()
    return row[0]


@dataclass
class SearchFilter:
    """What a search asks for; None: anything."""

    # a tsquery in the search.trialwatch configuration's syntax (search/query.py builds it)
    tsquery: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    # the distance from the place; None with a place: the whole of `country`
    radius_km: float | None = None
    # ISO code and English name of a country the trial must recruit in
    country: str | None = None
    country_name: str | None = None
    age: int | None = None
    # an age group ("0-17", "18-64", "65+") when no age is given
    age_group: str | None = None
    # female | male
    sex: str | None = None
    healthy_volunteers: bool | None = None
    # a plain phase ("2"): trials in that phase, also integrated ones ("1-2", "2-3")
    phase: str | None = None
    category: str | None = None
    rare: bool | None = None


@dataclass(frozen=True)
class SearchHit:
    trial_id: int
    source: str
    source_key: str | None
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
    recruiting_countries: list[str]
    source_updated_on: date | None
    rank: float
    # the nearest recruiting site (with a place)
    distance_km: float | None = None
    site: str | None = None
    site_city: str | None = None
    site_country: str | None = None
    precision: str | None = None
    # recruiting sites within the radius
    sites_near: int | None = None
    # how many trials the search finds in all
    total: int = 0


def age_group_of(age: int) -> str:
    if age < 18:
        return "0-17"
    if age < 65:
        return "18-64"
    return "65+"


def _in_country(column: str) -> str:
    """The country named in `column` (as the source spells it) is the filter's country: by its name, or
    by any name GeoNames gives the country's code."""
    return (f"(geo.name_key({column}) = geo.name_key(%(country_name)s)"
            f" OR EXISTS (SELECT FROM geo.country_name n WHERE n.name_key = geo.name_key({column})"
            " AND n.iso2 = %(country)s))")


def _conditions(f: SearchFilter, params: dict) -> list[str]:
    """The WHERE conditions of the filter on search.document d (q.query is the tsquery)."""
    conditions = ["d.recruiting", _STILL_RECRUITING]
    if f.tsquery is not None:
        conditions.append("d.tsv @@ q.query")
    if f.country is not None:
        conditions.append(f"EXISTS (SELECT FROM unnest(d.recruiting_countries) AS c WHERE {_in_country('c')})")
        params["country"] = f.country
        params["country_name"] = f.country_name
    age_group = f.age_group
    if f.age is not None:
        age_group = age_group_of(f.age)
        # the exact limits the inclusion criteria give, where the AI tags found them (with a quote)
        conditions.append("(d.age_min IS NULL OR %(age)s >= d.age_min) AND (d.age_max IS NULL OR %(age)s <= d.age_max)")
        params["age"] = f.age
    if age_group is not None:
        conditions.append("(cardinality(d.age_groups) = 0 OR %(age_group)s = ANY(d.age_groups))")
        params["age_group"] = age_group
    if f.sex == "female":
        conditions.append("d.female IS NOT FALSE")
    if f.sex == "male":
        conditions.append("d.male IS NOT FALSE")
    if f.healthy_volunteers is True:
        conditions.append("d.healthy_volunteers IS TRUE")
    if f.healthy_volunteers is False:
        conditions.append("d.patients IS NOT FALSE")
    if f.phase is not None:
        conditions.append("%(phase)s = ANY(string_to_array(d.phase, '-'))")
        params["phase"] = f.phase
    if f.category is not None:
        conditions.append("%(category)s = ANY(d.categories)")
        params["category"] = f.category
    if f.rare is True:
        conditions.append("d.rare")
    return conditions


def _box(latitude: float, radius_km: float) -> tuple[float, float]:
    """Degrees of latitude and longitude a radius spans around a latitude (a box that holds the circle)."""
    degrees_latitude = radius_km / 111.0
    shrink = cos(radians(latitude))
    if shrink < 0.05:
        shrink = 0.05
    return degrees_latitude, radius_km / (111.0 * shrink)


def _query_parts(f: SearchFilter, box_km: float | None) -> tuple[str, dict]:
    """The CTEs `q` (the query), `matched` (documents) and, with a place, `sites` (each matched
    trial's recruiting sites within box_km, with their distance)."""
    params = {"tsquery": f.tsquery, "weights": RANK_WEIGHTS}
    conditions = _conditions(f, params)
    sql = f"""
        WITH q AS (SELECT to_tsquery('search.trialwatch', coalesce(%(tsquery)s, '')) AS query),
        matched AS (
            SELECT d.*, CASE WHEN %(tsquery)s::text IS NULL THEN 0
                             ELSE ts_rank_cd(%(weights)s::float4[], d.tsv, q.query, 1) END AS rank
            FROM search.document d, q
            WHERE {' AND '.join(conditions)}
        )"""
    if f.latitude is not None and box_km is not None:
        degrees_latitude, degrees_longitude = _box(f.latitude, box_km)
        params["latitude"] = f.latitude
        params["longitude"] = f.longitude
        params["dlat"] = degrees_latitude
        params["dlon"] = degrees_longitude
        sql += """,
        sites AS (
            SELECT m.trial_id, l.site, l.city, l.country, a.precision,
                   geo.distance_km(%(latitude)s, %(longitude)s, a.latitude, a.longitude) AS km
            FROM matched m
            JOIN core.trial_location l ON l.trial_id = m.trial_id AND l.country = ANY(m.recruiting_countries)
            JOIN geo.address_point a ON a.country_key = l.country_key AND a.postcode_key = l.postcode_key
                                    AND a.city_key = l.city_key
            WHERE a.latitude BETWEEN %(latitude)s - %(dlat)s AND %(latitude)s + %(dlat)s
              AND a.longitude BETWEEN %(longitude)s - %(dlon)s AND %(longitude)s + %(dlon)s
        )"""
    return sql, params


_HIT_COLUMNS = """
    m.trial_id, s.code AS source,
    (SELECT ts.source_key FROM core.trial_source ts WHERE ts.trial_id = m.trial_id AND ts.source_id = m.source_id
     ORDER BY ts.source_key LIMIT 1) AS source_key,
    m.title, m.conditions, m.categories, m.phase, m.age_groups, m.female, m.male, m.healthy_volunteers, m.patients,
    m.rare, m.recruiting_countries,
    (SELECT max(l.source_updated_on) FROM core.trial_source ts JOIN core.source_listing l USING (source_id, source_key)
     WHERE ts.trial_id = m.trial_id) AS source_updated_on,
    m.rank::float AS rank"""


def search_hits(conn: psycopg.Connection, f: SearchFilter, offset: int, limit: int) -> tuple[list[SearchHit], int]:
    """A page of the trials the filter finds, most relevant first (then nearest), and how many in all."""
    with_place = f.latitude is not None and f.longitude is not None
    if not with_place:
        sql, params = _query_parts(f, None)
        sql += f"""
        SELECT {_HIT_COLUMNS}, count(*) OVER () AS total
        FROM matched m JOIN core.source s ON s.id = m.source_id
        ORDER BY m.rank DESC, m.trial_id
        OFFSET %(offset)s LIMIT %(limit)s"""
    else:
        radius = f.radius_km
        if radius is None:
            # the whole country: any distance, nearest first
            radius = 5000.0
        sql, params = _query_parts(f, radius)
        params["radius"] = radius
        site_conditions = ["km <= %(radius)s"]
        if f.country is not None:
            # the nearest site in that country, not one across a border that happens to be closer
            site_conditions.append(_in_country("country"))
        sql += f""",
        nearest AS (
            SELECT DISTINCT ON (trial_id) trial_id, site, city, country, precision, km,
                   count(*) OVER (PARTITION BY trial_id) AS sites_near
            FROM sites WHERE {' AND '.join(site_conditions)}
            ORDER BY trial_id, km
        )
        SELECT {_HIT_COLUMNS}, n.km AS distance_km, n.site, n.city AS site_city, n.country AS site_country,
               n.precision, n.sites_near, count(*) OVER () AS total
        FROM matched m JOIN nearest n ON n.trial_id = m.trial_id JOIN core.source s ON s.id = m.source_id
        ORDER BY m.rank DESC, n.km, m.trial_id
        OFFSET %(offset)s LIMIT %(limit)s"""
    hits = _page_of_hits(conn, sql, params, offset, limit)
    total = 0
    if hits:
        total = hits[0].total
    elif offset > 0:
        # past the last page: the same search's first row still says how many it finds in all
        first = _page_of_hits(conn, sql, params, 0, 1)
        if first:
            total = first[0].total
    return hits, total


def _page_of_hits(conn: psycopg.Connection, sql: str, params: dict, offset: int, limit: int) -> list[SearchHit]:
    page_params = dict(params)
    page_params["offset"] = offset
    page_params["limit"] = limit
    with conn.cursor(row_factory=class_row(SearchHit)) as cur:
        return cur.execute(sql, page_params).fetchall()


# the distances a search offers, in km; their counts tell a person how far to go
RADII = (10, 25, 50, 100, 200)


def radius_counts(conn: psycopg.Connection, f: SearchFilter) -> dict[str, int]:
    """With a place: how many trials the search finds within each of RADII, and in the place's country
    (`country`) and anywhere (`anywhere`), whatever the filter's own radius."""
    if f.latitude is None:
        return {}
    largest = RADII[-1]
    sql, params = _query_parts(f, float(largest))
    counts = []
    for radius in RADII:
        counts.append(f"count(DISTINCT trial_id) FILTER (WHERE km <= {radius}) AS within_{radius}")
    sql += f"""
        SELECT {', '.join(counts)}, (SELECT count(*) FROM matched) AS anywhere FROM sites"""
    row = conn.execute(sql, params).fetchone()
    found = {}
    for position, radius in enumerate(RADII):
        found[str(radius)] = row[position]
    found["anywhere"] = row[len(RADII)]
    return found


def count_matching(conn: psycopg.Connection, f: SearchFilter) -> int:
    """How many trials the filter finds, place aside."""
    sql, params = _query_parts(f, None)
    sql += " SELECT count(*) FROM matched"
    return conn.execute(sql, params).fetchone()[0]


def known_word(conn: psycopg.Connection, word: str) -> bool:
    """Some recruiting trial's conditions or title hold a word beginning like this one."""
    row = conn.execute(
        "SELECT EXISTS (SELECT FROM search.term WHERE term LIKE replace(geo.name_key(%s), ' ', '') || '%%')", (word,)
    ).fetchone()
    return row[0]


def suggestion(conn: psycopg.Connection, word: str) -> str | None:
    """The known word most like this one ("diabtes": "diabetes"), if alike enough."""
    row = conn.execute(
        """
        SELECT term FROM search.term
        WHERE term %% geo.name_key(%(word)s) AND similarity(term, geo.name_key(%(word)s)) >= %(least)s
        ORDER BY similarity(term, geo.name_key(%(word)s)) DESC, documents DESC, term
        LIMIT 1
        """,
        {"word": word, "least": SUGGEST_SIMILARITY},
    ).fetchone()
    if row is None:
        return None
    return row[0]


@dataclass
class IndexCounts:
    documents: int = 0
    recruiting: int = 0
    concepts: int = 0
    terms: int = 0
    by_method: dict[str, int] = field(default_factory=dict)


def index_counts(conn: psycopg.Connection) -> IndexCounts:
    counts = IndexCounts()
    row = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE recruiting), (SELECT count(*) FROM search.term) FROM search.document"
    ).fetchone()
    counts.documents = row[0]
    counts.recruiting = row[1]
    counts.terms = row[2]
    for method, count in conn.execute(
        "SELECT method, count(DISTINCT trial_id) FROM search.trial_concept GROUP BY method ORDER BY method"
    ).fetchall():
        counts.by_method[method] = count
    counts.concepts = conn.execute("SELECT count(DISTINCT trial_id) FROM search.trial_concept").fetchone()[0]
    return counts


# --- AI tags (search.annotation_run, search.annotation) ------------------------------------------

def start_annotation_run(conn: psycopg.Connection, *, model: str, effort: str, prompt: str, prompt_sha256: bytes,
                         prompt_text: str, params: dict) -> int:
    row = conn.execute(
        "INSERT INTO search.annotation_run (model, effort, prompt, prompt_sha256, prompt_text, params, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, current_setting('trialwatch.actor')::bigint) RETURNING id",
        (model, effort, prompt, prompt_sha256, prompt_text, Jsonb(params)),
    ).fetchone()
    return row[0]


def finish_annotation_run(conn: psycopg.Connection, run_id: int, status: str, error: str | None) -> None:
    """Close the run with its totals, summed from its annotations."""
    conn.execute(
        """
        UPDATE search.annotation_run r SET status = %(status)s, error = %(error)s, finished_at = clock_timestamp(),
            trials = (SELECT count(*) FROM search.annotation a WHERE a.run_id = r.id),
            input_tokens = (SELECT coalesce(sum(a.input_tokens), 0) FROM search.annotation a WHERE a.run_id = r.id),
            output_tokens = (SELECT coalesce(sum(a.output_tokens), 0) FROM search.annotation a WHERE a.run_id = r.id),
            cost_usd = (SELECT coalesce(sum(c.cost_usd), 0) FROM core.api_call c WHERE c.annotation_run_id = r.id)
        WHERE r.id = %(run)s
        """,
        {"status": status, "error": error, "run": run_id},
    )


def save_annotation(conn: psycopg.Connection, *, run_id: int, trial_id: int, profile_hash: bytes, served_by: str | None,
                    facts: dict, dropped: list, request: dict | None, raw_answer: str | None, error: str | None,
                    input_tokens: int, output_tokens: int, cost_usd, duration_ms: int) -> int:
    row = conn.execute(
        """
        INSERT INTO search.annotation (run_id, trial_id, profile_hash, served_by, facts, dropped, request, raw_answer,
                                       error, input_tokens, output_tokens, cost_usd, duration_ms)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (run_id, trial_id, profile_hash, served_by, Jsonb(facts), Jsonb(dropped), Jsonb(request), raw_answer, error,
         input_tokens, output_tokens, cost_usd, duration_ms),
    ).fetchone()
    return row[0]


@dataclass(frozen=True)
class Annotation:
    id: int
    run_id: int
    model: str
    served_by: str | None
    facts: dict
    dropped: list
    created_at: datetime


def latest_annotation(conn: psycopg.Connection, trial_id: int, profile_hash: bytes) -> Annotation | None:
    """The trial's newest annotation without an error, of this profile (one of an older profile is not used)."""
    with conn.cursor(row_factory=class_row(Annotation)) as cur:
        return cur.execute(
            """
            SELECT a.id, a.run_id, r.model, a.served_by, a.facts, a.dropped, a.created_at
            FROM search.annotation a JOIN search.annotation_run r ON r.id = a.run_id
            WHERE a.trial_id = %s AND a.profile_hash = %s AND a.error IS NULL
            ORDER BY a.id DESC LIMIT 1
            """,
            (trial_id, profile_hash),
        ).fetchone()


def document_annotation(conn: psycopg.Connection, trial_id: int) -> Annotation | None:
    """The annotation the trial's search document was built with, if any."""
    with conn.cursor(row_factory=class_row(Annotation)) as cur:
        return cur.execute(
            """
            SELECT a.id, a.run_id, r.model, a.served_by, a.facts, a.dropped, a.created_at
            FROM search.document d JOIN search.annotation a ON a.id = d.annotation_id
            JOIN search.annotation_run r ON r.id = a.run_id
            WHERE d.trial_id = %s
            """,
            (trial_id,),
        ).fetchone()


def trials_to_annotate(conn: psycopg.Connection, prompt_sha256: bytes | None = None) -> list[int]:
    """Recruiting trials with no annotation of their current profile (`prompt_sha256`: also those
    annotated with another prompt), in trial order."""
    rows = conn.execute(
        f"""
        SELECT d.trial_id FROM search.document d
        WHERE d.recruiting AND {_STILL_RECRUITING}
          AND NOT EXISTS (SELECT FROM search.annotation a JOIN search.annotation_run r ON r.id = a.run_id
                          WHERE a.trial_id = d.trial_id AND a.profile_hash = d.profile_hash AND a.error IS NULL
                            AND (%(prompt)s::bytea IS NULL OR r.prompt_sha256 = %(prompt)s))
        ORDER BY d.trial_id
        """,
        {"prompt": prompt_sha256},
    ).fetchall()
    found = []
    for row in rows:
        found.append(row[0])
    return found


def naive_count(conn: psycopg.Connection, words: Sequence[str], country: str | None) -> int:
    """What plain text matching finds among the trials CTIS's list calls recruiting: each word in the
    listed conditions or title (anywhere in them), the country among its listed countries. The
    baseline patient search is measured against (search/evaluate.py)."""
    conditions = ["l.recruiting", "l.delisted_at IS NULL"]
    params = {}
    for number, word in enumerate(words):
        name = f"word{number}"
        conditions.append(f"(l.summary ->> 'conditions' ILIKE '%%' || %({name})s || '%%' "
                          f"OR l.title ILIKE '%%' || %({name})s || '%%')")
        params[name] = like_text(word)
    if country is not None:
        conditions.append("EXISTS (SELECT FROM jsonb_array_elements_text(l.summary -> 'trialCountries') AS c "
                          "WHERE split_part(c, ':', 1) = %(country)s)")
        params["country"] = country
    row = conn.execute(f"SELECT count(*) FROM core.source_listing l WHERE {' AND '.join(conditions)}", params).fetchone()
    return row[0]
