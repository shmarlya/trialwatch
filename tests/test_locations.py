"""Where a trial runs (core.trial_location): read from each source's record at a transfer (CTIS: its
sites, member states and other countries), never with the people at a site; shown on the trial page
and searched by country and city on the public home page, for people looking for a trial to join."""

import copy
import json

from conftest import signed_in_client
from fastapi.testclient import TestClient
from test_cli import Calls
from test_evidence import CT, seed
from test_publication import judged
from test_sources_ctis import CT as RECORD_CT
from test_sources_ctis import RECORD, FakeCtis, transfer
from typer.testing import CliRunner

from trialwatch import cli
from trialwatch.db import core, overview
from trialwatch.sources import ctis
from trialwatch.web import app as web

EMAIL = "investigator@hospital.example"


def site(city, postcode, name, country):
    return {"id": 1, "personInfo": {"firstName": "Ada", "lastName": "Investigator", "email": EMAIL,
                                    "telephone": "+3200000000"},
            "organisationAddressInfo": {"email": EMAIL, "phone": "+3200000001",
                                        "address": {"city": city, "postcode": postcode, "countryName": country},
                                        "organisation": {"name": name}}}


def with_sites(record):
    """The record with two member states (Spain with two sites, Belgium with none listed), a third
    member state with no part II, and two countries outside the EU/EEA."""
    changed = copy.deepcopy(record)
    application = changed["authorizedApplication"]
    application["authorizedPartsII"] = [
        {"mscInfo": {"mscName": "Spain", "countryName": "Spain", "trialStatus": "Ongoing, recruiting"},
         "trialSites": [site("Valencia", "46026", "Hospital La Fe", "Spain"),
                        site("Madrid", "28046", "Hospital La Paz", "Spain")]},
        {"mscInfo": {"mscName": "Belgium", "countryName": "Belgium", "trialStatus": "Ended"}, "trialSites": []},
    ]
    application["memberStatesConcerned"] = [{"mscName": "Spain"}, {"mscName": "Belgium"}, {"mscName": "Poland"}]
    application["authorizedPartI"]["rowCountriesInfo"] = [{"name": "Canada"}, {"name": "Spain"}]
    return changed


def test_ctis_gives_sites_with_the_status_in_their_country_then_countries_without_sites():
    found = ctis.locations(with_sites(RECORD))
    assert found == [
        core.Location("Spain", "Valencia", "46026", "Hospital La Fe", "Ongoing, recruiting"),
        core.Location("Spain", "Madrid", "28046", "Hospital La Paz", "Ongoing, recruiting"),
        core.Location("Belgium", status="Ended"),
        core.Location("Poland"),
        core.Location("Canada"),
    ]
    # nobody at a site is read
    assert EMAIL not in repr(found) and "Investigator" not in repr(found)
    assert ctis.locations({"ctNumber": CT}) == []


def test_a_transfer_stores_them_and_reading_them_differently_is_no_new_record_version(conn):
    core.act_as(conn, "test-person")
    record = with_sites(RECORD)
    transfer(conn, FakeCtis(record))
    trial_id = core.trials_with_scheme(conn, "eu-ct", [RECORD_CT])[RECORD_CT]
    rows = overview.trial_locations(conn, trial_id)
    assert [(row.country, row.city) for row in rows] == [
        ("Belgium", None), ("Canada", None), ("Poland", None), ("Spain", "Madrid"), ("Spain", "Valencia")]
    assert {row.source for row in rows} == {"ctis"}
    versions = conn.execute("SELECT count(*) FROM core.trial_record").fetchone()
    # locations are not part of `registered`: checks don't read them, so judgements don't go outdated
    assert "locations" not in ctis.registered(record)
    record["authorizedApplication"]["authorizedPartsII"][0]["trialSites"].pop()
    # the same content as stored, read again: no new version, the rows follow the record
    core.set_trial_locations(conn, trial_id, "ctis", ctis.locations(record))
    assert conn.execute("SELECT count(*) FROM core.trial_record").fetchone() == versions
    assert [row.city for row in overview.trial_locations(conn, trial_id) if row.city] == ["Valencia"]


def test_rows_from_another_source_stay_and_a_repeat_counts_once(conn):
    core.act_as(conn, "test-person")
    transfer(conn, FakeCtis(with_sites(RECORD)))
    trial_id = core.trials_with_scheme(conn, "eu-ct", [RECORD_CT])[RECORD_CT]
    held = core.set_trial_locations(conn, trial_id, "manual", [core.Location("Spain", "Sevilla"),
                                                               core.Location("Spain", "Sevilla"), core.Location(" ")])
    assert held == 1
    core.set_trial_locations(conn, trial_id, "ctis", [])
    assert [(row.country, row.city, row.source) for row in overview.trial_locations(conn, trial_id)] == [
        ("Spain", "Sevilla", "manual")]


def test_the_command_reads_them_again_from_stored_records(app_conn, monkeypatch):
    monkeypatch.setattr(cli, "load_env_file", Calls())
    trial_id = seed(app_conn)
    app_conn.execute("UPDATE core.trial_record SET payload = %s::jsonb WHERE id IN "
                     "(SELECT id FROM core.trial_record_current WHERE trial_id = %s)",
                     (json.dumps(with_sites(RECORD)), trial_id))
    app_conn.execute("DELETE FROM core.trial_location")
    result = CliRunner().invoke(cli.app, ["source", "locations", "ctis"])
    assert result.exit_code == 0 and "read the locations of 1 trial (5 sites and countries)" in result.stdout
    assert len(overview.trial_locations(app_conn, trial_id)) == 5


def test_the_public_finds_checked_trials_by_country_and_city_and_sees_the_sites(app_conn):
    madrid = seed(app_conn)
    other_ct = "2023-500099-11-00"
    gent = seed(app_conn, ct_number=other_ct)
    core.act_as(app_conn, "test-person")
    core.set_trial_locations(app_conn, madrid, "ctis", ctis.locations(with_sites(RECORD)))
    core.set_trial_locations(app_conn, gent, "ctis", [core.Location("Belgium", "Gent", "9000", "UZ Gent", "Ended")])
    judged(app_conn, madrid)
    judged(app_conn, gent)
    public = TestClient(web.app)

    def shown(query=""):
        home = public.get("/" + query).text
        return {ct for ct in (CT, other_ct) if ct in home}

    assert overview.published_trial_countries(app_conn, "eu-ctr-results") == ["Belgium", "Canada", "Poland", "Spain"]
    assert shown("?country=Spain") == {CT}
    assert shown("?country=Belgium") == {CT, other_ct}  # listed for both, with no site for the first
    assert shown("?city=gen") == {other_ct}
    assert shown("?city=madrid") == {CT}
    assert shown("?country=Belgium&city=Madrid") == set()  # the city in that country
    assert shown("?country=Atlantis") == {CT, other_ct}  # not a country we know: ignored
    home = public.get("/?city=madrid").text
    assert "<b>Madrid</b>" in home and "Belgium, Canada, Poland, Spain" in home

    page = public.get(f"/trials/{madrid}").text
    assert "Where it runs" in page and "Hospital La Paz" in page and "Madrid" in page and "28046" in page
    assert "Ongoing, recruiting" in page and "no site listed" in page
    assert EMAIL not in page
    staff = signed_in_client(app_conn, "moderator").get(f"/trials/{gent}").text
    assert "UZ Gent" in staff
