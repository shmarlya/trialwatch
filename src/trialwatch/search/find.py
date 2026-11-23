"""Patient search's one entry point (ADR-008): `find(conn, params)` reads a query and searches the
trials that recruit now; when nothing is found, it says what would find something (any of the words, a
word spelled as trials spell it, a wider distance). /find (search/routes.py) and `trialwatch search
eval` (evaluate.py) both search through it.

A query comes from the home page's one box (`q`), or from the refine form's boxes (`what`, `where`)
and filters (`km`, `age`, `sex`, `hv`, `phase`, `cat`), which override what the box was read as.
"""

from dataclasses import dataclass, field
from urllib.parse import urlencode

import psycopg

from trialwatch import telemetry
from trialwatch.db import search as search_db
from trialwatch.search import categories, concepts, query, why
from trialwatch.search.constants import DISTANCES, FILTER_PHASES, RADII
from trialwatch.sources import registry

RESULTS_PER_PAGE = 20


@dataclass
class SearchParams:
    """The page's parameters, checked: anything not understood is left out."""

    q: str = ""
    what: str = ""
    where: str = ""
    km: int | None = None
    age: int | None = None
    sex: str | None = None
    healthy_volunteers: bool | None = None
    phase: str | None = None
    category: str | None = None
    page: int = 1

    def given(self) -> bool:
        return bool(self.q.strip() or self.what.strip() or self.where.strip() or self.category)


# A page further than any search goes (an address someone typed): its offset must still fit the database
LAST_PAGE = 10_000


def _number(text: str, least: int, most: int) -> int | None:
    text = text.strip()
    # isdigit() alone also takes "²" or "①", which int() refuses
    if not (text.isascii() and text.isdigit()):
        return None
    number = int(text)
    if number < least or number > most:
        return None
    return number


def params_from(
    q: str, what: str, where: str, km: str, age: str, sex: str, hv: str, phase: str, cat: str, page: int
) -> SearchParams:
    params = SearchParams(q=_text(q), what=_text(what), where=_text(where))
    distance = _number(km, 0, 200)
    if distance is not None and distance in DISTANCES:
        params.km = distance
    params.age = _number(age, 0, 120)
    if sex in ("female", "male"):
        params.sex = sex
    if hv == "yes":
        params.healthy_volunteers = True
    if hv == "no":
        params.healthy_volunteers = False
    if phase in FILTER_PHASES:
        params.phase = phase
    if categories.by_slug(cat) is not None:
        params.category = cat
    if page > 1:
        params.page = min(page, LAST_PAGE)
    return params


def _text(text: str) -> str:
    """A search box's text as it is read: at most MAX_CHARACTERS, without NUL characters, which the
    database refuses."""
    return text.replace("\x00", "")[: query.MAX_CHARACTERS]


def find(conn: psycopg.Connection, params: SearchParams) -> "SearchPage":
    """Read the query, then search with it, and say why it found each trial on the page. Its span
    (search.find) holds counts and kinds only, never the query's words (ADR-008)."""
    with telemetry.tracer.start_as_current_span("search.find") as span:
        page = search(conn, read(conn, params), params.page)
        page.why = why.explain(conn, page.reading, page.hits)
        span.set_attribute("trialwatch.search.total", page.total)
        span.set_attribute("trialwatch.search.page", page.page)
        span.set_attribute("trialwatch.search.place", page.reading.place is not None)
        span.set_attribute("trialwatch.search.relaxed", page.relaxed)
        span.set_attribute("trialwatch.search.corrected", len(page.corrected))
    return page


def read(conn: psycopg.Connection, params: SearchParams) -> query.Interpretation:
    """How the query is read: the one box, or the two boxes; then the filters chosen on the page."""
    if params.what.strip() or params.where.strip():
        reading = query.parse(params.what, conn, params.where)
    else:
        reading = query.parse(params.q, conn)
    if params.km is not None and reading.place is not None:
        if params.km == 0:
            reading.radius_km = None
        else:
            reading.radius_km = params.km
    if params.age is not None:
        reading.age = params.age
        reading.age_group = None
    if params.sex is not None:
        reading.sex = params.sex
    if params.healthy_volunteers is not None:
        reading.healthy_volunteers = params.healthy_volunteers
    if params.phase is not None:
        reading.phase = params.phase
    if params.category is not None:
        reading.category = params.category
    return reading


def search_filter(reading: query.Interpretation, tsquery: str | None) -> search_db.SearchFilter:
    found = search_db.SearchFilter(
        tsquery=tsquery,
        age=reading.age,
        age_group=reading.age_group,
        sex=reading.sex,
        healthy_volunteers=reading.healthy_volunteers,
        phase=reading.phase,
        category=reading.category,
        rare=reading.rare,
    )
    if reading.place is not None:
        found.latitude = reading.place.latitude
        found.longitude = reading.place.longitude
        if reading.radius_km is None:
            found.radius_km = None
            found.country = reading.place.country_code
            found.country_name = reading.place.country_name
        else:
            found.radius_km = float(reading.radius_km)
    elif reading.country is not None:
        found.country = reading.country[0]
        found.country_name = reading.country[1]
    return found


@dataclass
class SearchPage:
    reading: query.Interpretation
    hits: list[search_db.SearchHit] = field(default_factory=list)
    total: int = 0
    page: int = 1
    pages: int = 1
    # within km -> trials, and "anywhere" (with a place)
    counts: dict[str, int] = field(default_factory=dict)
    # nothing had every word: these trials have any of them
    relaxed: bool = False
    # word -> the known word it was read as ("diabtes" -> "diabetes")
    corrected: dict[str, str] = field(default_factory=dict)
    # trial id -> its page at its source, if the source has public pages
    urls: dict[int, str | None] = field(default_factory=dict)
    # trial id -> why the search found it, in words (search/why.py)
    why: dict[int, list[str]] = field(default_factory=dict)


def _corrections(conn: psycopg.Connection, words: list[str]) -> dict[str, str]:
    """For each condition word no recruiting trial has (as the beginning of a word), the known word
    most like it."""
    found = {}
    for word in words:
        if len(word) < query.PREFIX_LETTERS or search_db.known_word(conn, word):
            continue
        suggestion = search_db.suggestion(conn, word)
        if suggestion is not None:
            found[word] = suggestion
    return found


def search(conn: psycopg.Connection, reading: query.Interpretation, page: int) -> SearchPage:
    """Search with the reading; if nothing has every condition word, read misspelled words as the
    known word most like them, then try any of the words."""
    result = SearchPage(reading=reading, page=page)
    offset = (page - 1) * RESULTS_PER_PAGE
    found = search_filter(reading, query.tsquery_text(reading))
    hits, total = search_db.search_hits(conn, found, offset, RESULTS_PER_PAGE)
    if total == 0 and reading.condition_words:
        corrected = _corrections(conn, reading.condition_words)
        if corrected:
            words = []
            for word in reading.condition_words:
                if word in corrected:
                    words.append(corrected[word])
                else:
                    words.append(word)
            reading.condition_words = words
            reading.concepts = concepts.for_query(conn, words)
            result.corrected = corrected
            found = search_filter(reading, query.tsquery_text(reading))
            hits, total = search_db.search_hits(conn, found, offset, RESULTS_PER_PAGE)
    if total == 0 and len(reading.condition_words) > 1:
        found = search_filter(reading, query.tsquery_text(reading, relaxed=True))
        hits, total = search_db.search_hits(conn, found, offset, RESULTS_PER_PAGE)
        result.relaxed = total > 0
    result.hits = hits
    for hit in hits:
        result.urls[hit.trial_id] = source_url(hit)
    result.total = total
    result.pages = max(1, -(-total // RESULTS_PER_PAGE))
    result.counts = search_db.radius_counts(conn, found)
    return result


def source_url(hit: search_db.SearchHit) -> str | None:
    """The trial's page at its source, from the adapter the hit names (no query per hit)."""
    if hit.source_key is None or not hit.source_adapter:
        return None
    return registry.adapter_class(hit.source_adapter)().public_url(hit.source_key)


# --- links the page offers ---------------------------------------------------------------------


def _where_of(reading: query.Interpretation) -> str:
    if reading.place is not None:
        if reading.place.kind == "postcode":
            return f"{reading.place.matched} {reading.place.country_name}"
        return f"{reading.place.name}, {reading.place.country_name}"
    if reading.country is not None:
        return reading.country[1]
    return ""


def _href(values: dict) -> str:
    kept = {}
    for name, value in values.items():
        if value is not None and value != "":
            kept[name] = value
    return "/find?" + urlencode(kept)


def kept_params(params: SearchParams) -> dict:
    """The page's own parameters, to keep in its links."""
    healthy = ""
    if params.healthy_volunteers is True:
        healthy = "yes"
    if params.healthy_volunteers is False:
        healthy = "no"
    return {
        "q": params.q,
        "what": params.what,
        "where": params.where,
        "km": params.km,
        "age": params.age,
        "sex": params.sex,
        "hv": healthy,
        "phase": params.phase,
        "cat": params.category,
    }


@dataclass
class Links:
    # (label, href) of other readings
    alternatives: list[tuple[str, str]] = field(default_factory=list)
    # (km, trials, href, current) per distance
    distances: list[tuple[int, int, str, bool]] = field(default_factory=list)
    previous: str | None = None
    next: str | None = None


def links(params: SearchParams, result: SearchPage) -> Links:
    reading = result.reading
    found = Links()
    what = " ".join(reading.condition_words)
    for alternative in reading.alternatives:
        values = {"what": what}
        for name, value in alternative.params.items():
            values[name] = value
        found.alternatives.append((alternative.label, _href(values)))
    if reading.place is not None and result.counts:
        for km in RADII:
            values = kept_params(params)
            values["q"] = ""
            values["what"] = what
            values["where"] = _where_of(reading)
            values["km"] = km
            found.distances.append((km, result.counts[str(km)], _href(values), reading.radius_km == km))
    if result.page > 1:
        values = kept_params(params)
        values["page"] = result.page - 1
        found.previous = _href(values)
    if result.page < result.pages:
        values = kept_params(params)
        values["page"] = result.page + 1
        found.next = _href(values)
    return found


def example_href(example: str) -> str:
    return _href({"q": example})
