"""The public search page and recruiting trials' pages (ADR-008): "diabetes Paris" finds the trial
recruiting in Villejuif, not the one in Lyon; ended trials are never found; a recruiting trial's page
is public with no people on it; searches are private."""

import html
import json
import re

import pytest
from fastapi.testclient import TestClient
from places import load_places
from recruiting_trials import EMAIL, LYON, MADRID, PARIS, VILLEJUIF, ManyCtis, listing, record
from test_sources_ctis import transfer
from vocabulary import load_mesh

from trialwatch.db import core
from trialwatch.db import search as search_db
from trialwatch.geo import countries
from trialwatch.sources import ctis
from trialwatch.trials import ingest
from trialwatch.web import app as web
from trialwatch.web import privacy

NEAR_PARIS = "2026-500011-11-00"
IN_LYON = "2026-500012-11-00"
ENDED = "2026-500013-11-00"
ASTHMA_MADRID = "2026-500014-11-00"


@pytest.fixture
def trials(app_conn, tmp_path):
    core.act_as(app_conn, "test-person")
    load_places(app_conn, tmp_path)
    load_mesh(app_conn, tmp_path)
    records = {
        NEAR_PARIS: record(NEAR_PARIS, sites=(VILLEJUIF,), title="Semaglutide in type 2 diabetes"),
        IN_LYON: record(IN_LYON, sites=(LYON,), title="Another diabetes medicine"),
        ENDED: record(ENDED, sites=(PARIS,), status="Ended", status_code=8, title="An ended diabetes trial"),
        ASTHMA_MADRID: record(ASTHMA_MADRID, sites=(MADRID,), title="An inhaler",
                              conditions=({"name": "Asthma", "translations": {}, "rare": False},), terms=(),
                              inclusion=("Adults with asthma",), age_codes=("2",)),
    }
    rows = [listing(NEAR_PARIS), listing(IN_LYON), listing(ENDED, status_code=8, countries={"France": 8}),
            listing(ASTHMA_MADRID, countries={"Spain": 4})]
    fake = ManyCtis(records, rows)
    ingest.sync_list(app_conn, core.registered_source(app_conn, "ctis"), ctis.CtisAdapter(fake))
    result = transfer(app_conn, fake, keys=list(records))
    assert result.failed == {}, result.failed
    return core.trials_with_scheme(app_conn, "eu-ct", list(records))


def shown(page, keys=(NEAR_PARIS, IN_LYON, ENDED, ASTHMA_MADRID)):
    found = set()
    for key in keys:
        if key in page:
            found.add(key)
    return found


def test_diabetes_paris_finds_the_site_in_villejuif_and_not_lyon(trials):
    public = TestClient(web.app)
    response = public.get("/find", params={"q": "diabetes Paris"})
    assert response.status_code == 200
    page = response.text
    assert shown(page) == {NEAR_PARIS}
    assert "near Paris, France" in page and "within 50 km" in page
    assert "Gustave Roussy" in page and "Villejuif" in page and "km</b>" in page
    # the ended trial has a site in Paris itself, and is never found
    assert ENDED not in page
    # farther: Lyon is about 390 km away
    assert shown(public.get("/find", params={"what": "diabetes", "where": "Paris", "km": "0"}).text) == {NEAR_PARIS, IN_LYON}
    assert shown(public.get("/find", params={"q": "diabète Lyon"}).text) == {IN_LYON}
    # a condition anywhere, and a postcode
    assert shown(public.get("/find", params={"q": "diabetes"}).text) == {NEAR_PARIS, IN_LYON}
    assert shown(public.get("/find", params={"q": "asthma 28046"}).text) == {ASTHMA_MADRID}


def test_who_can_join_narrows_the_search(trials):
    public = TestClient(web.app)
    # the asthma trial takes children only; the diabetes ones adults
    assert shown(public.get("/find", params={"q": "asthma child"}).text) == {ASTHMA_MADRID}
    assert shown(public.get("/find", params={"q": "diabetes enfant"}).text) == set()
    assert shown(public.get("/find", params={"q": "diabetes", "age": "45"}).text) == {NEAR_PARIS, IN_LYON}
    assert shown(public.get("/find", params={"q": "diabetes", "hv": "yes"}).text) == set()


def test_a_misspelled_word_is_read_as_the_known_one(trials):
    page = TestClient(web.app).get("/find", params={"q": "diabtes Paris"}).text
    assert shown(page) == {NEAR_PARIS} and "searched for <b>diabetes</b> instead" in page


def why_lines(page):
    """The "Why it is here" line of each result card."""
    found = []
    for line in re.findall(r'<p class="small why"><span class="muted">Why it is here:</span> (.*?)</p>', page):
        found.append(html.unescape(line))
    return found


def test_each_result_says_why_it_is_here(trials):
    public = TestClient(web.app)
    # a word of its condition
    assert why_lines(public.get("/find", params={"q": "diabetes Paris"}).text) == [
        "“diabetes” in its condition “Type 2 Diabetes Mellitus”"]
    # the same word in one trial's title, and in what the others test
    assert sorted(why_lines(public.get("/find", params={"q": "semaglutide"}).text)) == [
        "“semaglutide” in its title"] + ["“semaglutide” in what it tests or its main objective"] * 2
    # no trial has the word: a MeSH entry term found the concept its condition was filed under
    assert why_lines(public.get("/find", params={"q": "NIDDM"}).text) == [
        "its condition “Type 2 Diabetes Mellitus” is the medical term “Diabetes Mellitus, Type 2” (MeSH)"] * 2


def test_a_recruiting_trial_has_a_public_page_without_people(trials):
    public = TestClient(web.app)
    page = public.get(f"/trials/{trials[NEAR_PARIS]}").text
    assert "Who can join" in page and "Type 2 Diabetes Mellitus" in page and "Gustave Roussy" in page
    assert "Adults aged 18 to 75 years with type 2 diabetes" in page and "Pregnancy" in page
    assert "Diabetes Mellitus, Type 2" in page  # its MeSH concept, with the words it was found in
    assert f"EUCT={NEAR_PARIS}" in page and 'rel="noreferrer noopener"' in page
    assert EMAIL not in page and "Investigator" not in page
    # an ended trial with no published judgement stays private
    assert public.get(f"/trials/{trials[ENDED]}").status_code == 404


def test_searches_are_private(trials, app_conn):
    public = TestClient(web.app)
    before = app_conn.execute("SELECT count(*) FROM core.audit_log").fetchone()[0]
    response = public.get("/find", params={"q": "diabetes Paris"})
    # a Referer carries only the site's origin, never the search's address
    assert response.headers["referrer-policy"] == "strict-origin"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex"
    # nothing about a search is written
    assert app_conn.execute("SELECT count(*) FROM core.audit_log").fetchone()[0] == before
    # every page has the same policy
    assert public.get("/").headers["referrer-policy"] == "strict-origin"
    record_args = ("127.0.0.1:5000", "GET", "/find?q=diabetes+Paris", "1.1", 200)
    import logging

    line = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', record_args, None)
    privacy.StripQueryFilter().filter(line)
    assert line.getMessage() == '127.0.0.1:5000 - "GET /find HTTP/1.1" 200'
    config = privacy.log_config()
    assert "strip_query" in config["handlers"]["access"]["filters"]
    assert json.dumps(config)


def test_the_home_page_offers_the_search_and_the_page_explains_itself(trials):
    public = TestClient(web.app)
    home = public.get("/").text
    assert 'action="/find"' in home and "Find a trial that is recruiting" in home
    empty = public.get("/find").text
    assert "diabetes Paris" in empty and "not medical advice" in empty and "GeoNames" in empty
    nothing = public.get("/find", params={"q": "zzzz Paris"}).text
    assert "No recruiting trial matches" in nothing


# Paris, as GeoNames places it (tests/places.py)
PARIS_POINT = (48.85341, 2.3488)
NICE = ("Nice", "06000", "Hôpital Pasteur", "France")
GENT = ("Gent", "9000", "UZ Gent", "Belgium")
FRANCE_AND_BELGIUM = "2026-500015-11-00"


def test_a_page_past_the_last_still_counts_only_trials_near_the_place(trials, app_conn):
    near_paris = search_db.SearchFilter(latitude=PARIS_POINT[0], longitude=PARIS_POINT[1], radius_km=50.0)
    hits, total = search_db.search_hits(app_conn, near_paris, 0, 10)
    assert (len(hits), total) == (1, 1)
    # the trials in Lyon and Madrid recruit too, but not within 50 km of Paris
    past_the_end, total_past_the_end = search_db.search_hits(app_conn, near_paris, 10, 10)
    assert (past_the_end, total_past_the_end) == ([], 1)


def test_a_whole_country_search_shows_the_nearest_site_in_that_country(trials, app_conn):
    # Gent, in Belgium, is about 230 km from Paris; Nice, in France, about 690 km
    records = {FRANCE_AND_BELGIUM: record(FRANCE_AND_BELGIUM, sites=(NICE, GENT), title="A two-country trial")}
    fake = ManyCtis(records, [listing(FRANCE_AND_BELGIUM, countries={"France": 4, "Belgium": 4})])
    ingest.sync_list(app_conn, core.registered_source(app_conn, "ctis"), ctis.CtisAdapter(fake))
    result = transfer(app_conn, fake, keys=list(records))
    assert result.failed == {}, result.failed
    in_france = search_db.SearchFilter(latitude=PARIS_POINT[0], longitude=PARIS_POINT[1], radius_km=None,
                                       country="FR", country_name="France")
    hits, total = search_db.search_hits(app_conn, in_france, 0, 10)
    by_key = {}
    for hit in hits:
        by_key[hit.source_key] = hit
    two_countries = by_key[FRANCE_AND_BELGIUM]
    assert (two_countries.site_city, two_countries.site_country) == ("Nice", "France")
    assert two_countries.sites_near == 1


def test_countries_are_compared_by_code_however_a_source_spells_them():
    assert (countries.iso2_of("France"), countries.iso2_of("Deutschland"), countries.iso2_of("  belgique ")) == (
        "FR", "DE", "BE")
    # a country we have no name for has no code: shown, but no part in finding trials by country
    assert countries.iso2_of("United States") is None and countries.iso2_of(None) is None
    assert (countries.name_of("FR"), countries.name_of("US")) == ("France", "US")


def test_a_site_whose_source_gives_its_point_is_found_without_geonames(trials, app_conn):
    # a second source places a site of the Lyon trial in Paris itself, with its own point and an address
    # GeoNames doesn't know
    site = core.Location(country="France", city="Nowhere-in-GeoNames", site="Hôpital du test", latitude=PARIS_POINT[0],
                         longitude=PARIS_POINT[1])
    core.set_trial_locations(app_conn, trials[IN_LYON], "manual", [site])
    near_paris = search_db.SearchFilter(latitude=PARIS_POINT[0], longitude=PARIS_POINT[1], radius_km=10.0)
    hits, _total = search_db.search_hits(app_conn, near_paris, 0, 10)
    by_key = {hit.source_key: hit for hit in hits}
    found = by_key[IN_LYON]
    assert (found.site, found.precision, round(found.distance_km)) == ("Hôpital du test", "source", 0)
