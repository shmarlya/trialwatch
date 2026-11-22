"""Measuring patient search (ADR-008, phase 3) without calling a model: how each query of a query set
(evals/search-queries.json) is read compared with how it should be, how many recruiting trials it
finds near the place and in the place's country, and how many plain text matching finds in CTIS's
list (each condition word in the listed conditions or title, the country in its listed countries):
what CTIS's own search would give. More found is not better by itself; which trials are relevant to
a query needs judged trials (recall, nDCG), the next step.
"""

import json
from dataclasses import dataclass, field
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import psycopg

from trialwatch.db import search as search_db
from trialwatch.search import query
from trialwatch.search.lookups import DatabaseLookups
from trialwatch.web import search as web_search

NEAR_KM = 30
FIELDS = ("condition", "near", "country", "age", "age_group", "healthy_volunteers", "sex", "phase")


@dataclass(frozen=True)
class QueryCase:
    text: str
    language: str
    kind: str
    expected: dict


def load_queries(path: Path) -> list[QueryCase]:
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    found = []
    for item in data["queries"]:
        found.append(QueryCase(item["text"], item["language"], item["kind"], item["expected"]))
    return found


def _km(first: tuple[float, float], second: tuple[float, float]) -> float:
    half_latitude = radians(second[0] - first[0]) / 2
    half_longitude = radians(second[1] - first[1]) / 2
    a = sin(half_latitude) ** 2 + cos(radians(first[0])) * cos(radians(second[0])) * sin(half_longitude) ** 2
    return 2 * 6371 * asin(min(1.0, sqrt(a)))


def wrong_fields(reading: query.Interpretation, expected: dict) -> list[str]:
    """The expected fields the reading got wrong, in words."""
    wrong = []
    for name in FIELDS:
        if name not in expected:
            continue
        want = expected[name]
        if name == "condition":
            got = " ".join(reading.condition_words)
            if got != want:
                wrong.append(f"condition “{got}”, not “{want}”")
        elif name == "near":
            if reading.place is None:
                wrong.append("no place")
            else:
                distance = _km((reading.place.latitude, reading.place.longitude), (want[0], want[1]))
                if distance > NEAR_KM:
                    wrong.append(f"place {reading.place.name}, {reading.place.country_name} ({distance:.0f} km off)")
        elif name == "country":
            got = None
            if reading.country is not None:
                got = reading.country[0]
            if got != want:
                wrong.append(f"country {got}, not {want}")
        else:
            got = getattr(reading, name)
            if got != want:
                wrong.append(f"{name} {got}, not {want}")
    return wrong


@dataclass
class CaseResult:
    case: QueryCase
    reading: list[str]
    wrong: list[str]
    near: int | None
    in_country: int | None
    anywhere: int
    baseline: int
    corrected: dict[str, str] = field(default_factory=dict)


def run_case(conn: psycopg.Connection, case: QueryCase) -> CaseResult:
    reading = query.parse(case.text, DatabaseLookups(conn))
    wrong = wrong_fields(reading, case.expected)
    described = query.describe(reading)
    page = web_search.run(conn, reading, 1)
    near = None
    in_country = None
    country_name = None
    if reading.place is not None:
        near = page.total
        country_name = reading.place.country_name
        everywhere = web_search.search_filter(reading, query.tsquery_text(reading))
        everywhere.latitude = None
        everywhere.longitude = None
        everywhere.radius_km = None
        everywhere.country = reading.place.country_code
        everywhere.country_name = reading.place.country_name
        in_country = search_db.count_matching(conn, everywhere)
    elif reading.country is not None:
        in_country = page.total
        country_name = reading.country[1]
    anywhere_filter = web_search.search_filter(reading, query.tsquery_text(reading))
    anywhere_filter.latitude = None
    anywhere_filter.longitude = None
    anywhere_filter.radius_km = None
    anywhere_filter.country = None
    anywhere_filter.country_name = None
    anywhere = search_db.count_matching(conn, anywhere_filter)
    baseline = search_db.naive_count(conn, reading.condition_words, country_name)
    return CaseResult(case, described, wrong, near, in_country, anywhere, baseline, page.corrected)


@dataclass
class Report:
    results: list[CaseResult]

    def read_right(self) -> int:
        count = 0
        for result in self.results:
            if not result.wrong:
                count += 1
        return count

    def zero_results(self) -> int:
        """Queries that found no recruiting trial where they asked (near the place, else anywhere)."""
        count = 0
        for result in self.results:
            found = result.anywhere
            if result.near is not None:
                found = result.near
            elif result.in_country is not None:
                found = result.in_country
            if found == 0:
                count += 1
        return count

    def baseline_zero(self) -> int:
        count = 0
        for result in self.results:
            if result.baseline == 0:
                count += 1
        return count


def evaluate(conn: psycopg.Connection, cases: list[QueryCase]) -> Report:
    results = []
    for case in cases:
        results.append(run_case(conn, case))
    return Report(results)
