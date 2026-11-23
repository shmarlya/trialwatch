"""Places for patient search (ADR-008): GeoNames loaded offline, where each trial site is, and what a
place name, country name or postal code a search gives means."""

from places import load_places, write_files
from test_cli import Calls
from typer.testing import CliRunner

from trialwatch import cli
from trialwatch.db import core
from trialwatch.db import geo as geo_db
from trialwatch.geo import load as geo_loading
from test_sources_ctis import RECORD, FakeCtis, transfer


def trial(conn):
    core.act_as(conn, "test-person")
    transfer(conn, FakeCtis(RECORD))
    ct_number = RECORD["ctNumber"]
    return core.trials_with_scheme(conn, "eu-ct", [ct_number])[ct_number]


def point_of(conn, country, postcode, city):
    return conn.execute(
        "SELECT a.precision, a.matched, round(a.latitude::numeric, 2), round(a.longitude::numeric, 2) "
        "FROM geo.address_point a WHERE a.country_key = geo.name_key(%s) AND a.postcode_key = geo.postcode_key(%s) "
        "AND a.city_key = geo.name_key(%s)",
        (country, postcode, city),
    ).fetchone()


def test_loading_reads_countries_places_with_their_names_and_postcodes(conn, tmp_path):
    core.act_as(conn, "test-person")
    summary = load_places(conn, tmp_path)
    # places of the countries asked for only: Lyme Regis is in the UK
    assert (summary.countries, summary.places, summary.postcodes) == (6, 11, 8)
    assert summary.missing == []
    run = conn.execute("SELECT params->'countries', (counts->>'places')::int, jsonb_array_length(files), license "
                       "FROM core.reference_load WHERE name = 'geonames'").fetchone()
    assert run[0] == ["FR", "DE", "AT", "BE", "ES"] and run[1:3] == (11, 7) and "CC BY 4.0" in run[3]
    # a place by any of its names, accents and case aside
    vienna = geo_db.places_named(conn, ["WIEN"])
    assert [(place.name, place.country_name, place.in_gazetteer) for place in vienna] == [("Vienna", "Austria", True)]
    assert geo_db.places_named(conn, ["Wiedeń"])[0].geonameid == 2761369
    assert geo_db.places_named(conn, ["munchen"])[0].name == "Munich"
    # two places called Frankfurt: largest first
    frankfurts = geo_db.places_named(conn, ["Frankfurt"])
    assert [place.name for place in frankfurts] == ["Frankfurt am Main"]
    # countries by the names people type, in their languages; the UK is known but not in the EU/EEA
    assert geo_db.countries_named(conn, ["Allemagne"])[0].iso2 == "DE"
    assert geo_db.countries_named(conn, ["españa"])[0].name == "Spain"
    assert conn.execute("SELECT in_gazetteer FROM geo.country WHERE iso2 = 'GB'").fetchone() == (False,)
    # a postal code anywhere in the EU/EEA
    paris = geo_db.postcode_places(conn, "75013")
    assert [(place.country_code, place.place_name) for place in paris] == [("FR", "Paris 13")]


def test_each_site_gets_a_point_by_postcode_then_city_name(conn, tmp_path):
    trial_id = trial(conn)
    load_places(conn, tmp_path)
    core.set_trial_locations(conn, trial_id, "ctis", [
        core.Location("France", "Paris Cedex 13", "75013", "Hôpital Pitié-Salpêtrière", "Ongoing, recruiting"),
        core.Location("France", "Villejuif", "94 800", "Gustave Roussy", "Ongoing, recruiting"),
        # a mistyped postal code (Paris's) for a site in Lyon: the city wins
        core.Location("France", "Lyon", "75013", "Hôpital Édouard Herriot", "Ongoing, recruiting"),
        # no postal code GeoNames has: the city by one of its names
        core.Location("Belgium", "Ghent", "9999", "UZ Gent", "Ongoing, recruiting"),
        core.Location("Austria", "Wien", None, "AKH", "Ongoing, recruiting"),
        # a city spelled differently
        core.Location("Germany", "Frankfurt a. Main", None, "Uniklinik", "Ongoing, recruiting"),
        core.Location("Spain", "Atlantis", None, "Nowhere", "Ongoing, recruiting"),
        core.Location("Spain"),
    ])
    summary = geo_loading.geocode_missing(conn)
    assert summary.addresses == 7
    assert point_of(conn, "France", "75013", "Paris Cedex 13")[:2] == ("postcode", "Paris 13")
    assert point_of(conn, "France", "94 800", "Villejuif")[:2] == ("postcode", "Villejuif")
    assert point_of(conn, "France", "75013", "Lyon")[:2] == ("city", "Lyon")
    assert point_of(conn, "Belgium", "9999", "Ghent")[:2] == ("city", "Gent")
    assert point_of(conn, "Austria", None, "Wien")[:2] == ("city", "Vienna")
    assert point_of(conn, "Germany", None, "Frankfurt a. Main")[:2] == ("city-fuzzy", "Frankfurt am Main")
    assert point_of(conn, "Spain", None, "Atlantis") == ("none", None, None, None)
    # nothing new: nothing to do
    assert geo_loading.geocode_missing(conn).addresses == 0
    assert geo_db.precision_counts(conn) == {"city": 3, "city-fuzzy": 1, "none": 1, "postcode": 2}
    assert geo_db.unmatched_cities(conn, 5) == [("Spain", "Atlantis", 1)]


def test_distances_are_great_circle_kilometres(conn):
    villejuif = conn.execute("SELECT geo.distance_km(48.85341, 2.3488, 48.7922, 2.3634)").fetchone()[0]
    lyon = conn.execute("SELECT geo.distance_km(48.85341, 2.3488, 45.74846, 4.84671)").fetchone()[0]
    assert 6 < villejuif < 8 and 390 < lyon < 395


def test_nothing_is_geocoded_before_geonames_is_loaded(conn):
    trial_id = trial(conn)
    core.set_trial_locations(conn, trial_id, "ctis", [core.Location("France", "Paris", "75013")])
    assert geo_loading.geocode_missing(conn).addresses == 0
    assert conn.execute("SELECT count(*) FROM geo.address_point").fetchone() == (0,)


def test_the_commands_load_geocode_and_explain_a_name(app_conn, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_env_file", Calls())
    trial_id = trial(app_conn)
    core.set_trial_locations(app_conn, trial_id, "ctis", [core.Location("France", "Villejuif", "94800")])
    folder = write_files(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["geo", "load", "--from", str(folder), "--country", "fr", "--country", "be"])
    assert result.exit_code == 0, result.stdout
    assert "2 postal codes" not in result.stdout and "geocoded 1 site address\n" in result.stdout
    assert "postcode: 1" in result.stdout
    result = runner.invoke(cli.app, ["geo", "find", "Parijs"])
    assert "place Paris, France" in result.stdout
    result = runner.invoke(cli.app, ["geo", "find", "94800"])
    assert "postcode 94800 Villejuif, France" in result.stdout
    result = runner.invoke(cli.app, ["geo", "geocode", "--again"])
    assert result.exit_code == 0 and "geocoded 1 site address\n" in result.stdout
    # any country may be loaded (TRIALWATCH_COUNTRIES says which by default); a code must be one
    result = runner.invoke(cli.app, ["geo", "load", "--country", "usa"])
    assert result.exit_code != 0 and "not a two-letter country code" in result.output
