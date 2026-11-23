"""A trial's profile (ADR-008, patient search): read by the CTIS adapter from a record and its list row,
stored at every transfer, read again from stored records by `trialwatch source profiles`; never the
people at a site, and never part of what a check reads."""

import json

from recruiting_trials import DIABETES_TERM, EMAIL, MADRID, PARIS, VILLEJUIF, ManyCtis, listing, record
from test_cli import Calls
from test_sources_ctis import transfer
from typer.testing import CliRunner

from trialwatch import cli
from trialwatch.db import core
from trialwatch.sources import ctis, profile
from trialwatch.trials import ingest

CT = "2026-500001-11-00"


def test_ctis_reads_what_a_person_looking_for_a_trial_needs():
    read = ctis.trial_profile(record(CT, title_translations={"French (France)": "Une étude"}), listing(CT))
    assert read.public_title == "A study of a new medicine"
    assert read.title_translations == (profile.Text("Une étude", "fr"),)
    assert read.recruiting is True
    condition = read.conditions[0]
    assert condition.name == "Type 2 Diabetes Mellitus" and condition.rare is False
    assert condition.translations == (profile.Text("Diabète de type 2", "fr"), profile.Text("Diabetes tipo 2", "es"))
    assert read.coded_terms == (profile.CodedTerm("meddra", "10045242", "Type II diabetes mellitus", "LLT", "10027433"),)
    assert read.areas == (profile.Area("C18", "Diseases [C] - Nutritional and Metabolic Diseases [C18]"),)
    # the list's label, matched despite CTIS's double space
    assert (read.phase, read.phase_label) == ("3", "Therapeutic confirmatory (Phase III)")
    assert read.age_groups == ("18-64", "65+")
    assert (read.female, read.male, read.patients, read.healthy_volunteers) == (True, True, True, False)
    assert read.inclusion[0].text == "Adults aged 18 to 75 years with type 2 diabetes"
    assert read.exclusion[0].text == "Pregnancy"
    product = read.products[0]
    assert (product.name, product.role, product.substances, product.atc_code, product.origins) == (
        "SEMAGLUTIDE", "test", ("semaglutide",), "A10BJ06", ("Chemical",))
    assert read.countries == (profile.CountryStatus("France", "Ongoing, recruiting", True, "2026-01-15", None),)
    assert profile.recruiting_countries(read) == ["France"]
    # nobody at a site is read
    assert EMAIL not in repr(read) and "Investigator" not in repr(read)


def test_the_list_is_fresher_than_the_record():
    """The list is re-read at every sync, the record only when the trial changed: its status wins."""
    trial = record(CT, sites=(PARIS, MADRID))
    read = ctis.trial_profile(trial, listing(CT, status_code=5, countries={"France": 5, "Spain": 4}))
    assert read.recruiting is False
    assert read.countries[0] == profile.CountryStatus("France", "Ongoing, recruiting", False, "2026-01-15", None)
    assert read.countries[1].recruiting is True
    # without a list row: the record's own status code and texts
    alone = ctis.trial_profile(trial)
    assert alone.recruiting is True and profile.recruiting_countries(alone) == ["France", "Spain"]
    # the phase from the record's code when the list gives none
    assert (alone.phase, alone.phase_label) == ("3", "Therapeutic confirmatory (Phase III)")
    assert ctis.trial_profile({"ctNumber": CT}) == profile.TrialProfile()


def test_odd_values_are_left_out_not_guessed():
    trial = record(CT, groups=(), age_codes=(), female=None, male=None, phase_code="99",
                   terms=({"level": "SOC", "classificationCode": "10027433", "termName": "Metabolism and nutrition disorders",
                           "organClass": "6"},),
                   areas=("Not possible to specify",))
    read = ctis.trial_profile(trial, {"ageGroup": "0-17 years", "gender": "Female"})
    assert (read.healthy_volunteers, read.patients) == (None, None)
    assert read.age_groups == ("0-17",) and (read.female, read.male) == (True, False)
    assert (read.phase, read.phase_label) == (None, None)
    # an organ class that is not a MedDRA code (CTIS sometimes gives an internal id) is no group
    assert read.coded_terms[0].group is None
    assert read.areas == (profile.Area(None, "Not possible to specify"),)


def test_stored_json_reads_back_the_same_and_hashes_the_same():
    read = ctis.trial_profile(record(CT), listing(CT))
    data = json.loads(json.dumps(profile.profile_data(read)))
    assert profile.profile_from_data(data) == read
    assert profile.profile_hash(profile.profile_from_data(data)) == profile.profile_hash(read)


def test_a_transfer_stores_it_and_reading_it_again_changes_nothing(conn):
    core.act_as(conn, "test-person")
    fake = ManyCtis({CT: record(CT)}, [listing(CT)])
    ingest.sync_list(conn, core.registered_source(conn, "ctis"), ctis.CtisAdapter(fake))
    transfer(conn, fake, keys=(CT,))
    trial_id = core.trials_with_scheme(conn, "eu-ct", [CT])[CT]
    stored = core.trial_profile(conn, trial_id)
    assert stored.source == "ctis" and stored.recruiting is True
    assert stored.profile["conditions"][0]["name"] == "Type 2 Diabetes Mellitus"
    assert EMAIL not in json.dumps(stored.profile)
    # the profile is not what checks read: the record's `registered` reading holds none of it
    assert "conditions" not in ctis.registered(record(CT))
    audits = conn.execute("SELECT count(*) FROM core.audit_log WHERE table_name = 'core.trial_profile'").fetchone()[0]
    transfer(conn, fake, keys=(CT,))
    assert conn.execute("SELECT count(*) FROM core.audit_log WHERE table_name = 'core.trial_profile'").fetchone()[0] == audits
    # the list stops calling it recruiting: the next read follows
    conn.execute("UPDATE core.source_listing SET summary = %s WHERE source_key = %s",
                 (json.dumps(listing(CT, status_code=5, countries={"France": 5})), CT))
    transfer(conn, fake, keys=(CT,))
    assert core.trial_profile(conn, trial_id).recruiting is False


def test_the_command_reads_them_again_from_stored_records(app_conn, monkeypatch):
    monkeypatch.setattr(cli, "load_env_file", Calls())
    core.act_as(app_conn, "test-person")
    fake = ManyCtis({CT: record(CT, sites=(PARIS, VILLEJUIF))}, [listing(CT)])
    transfer(app_conn, fake, keys=(CT,))
    app_conn.execute("DELETE FROM core.trial_profile")
    result = CliRunner().invoke(cli.app, ["source", "profiles", "ctis"])
    assert result.exit_code == 0, result.stdout
    assert "read the profiles of 1 trial (1 new or changed)" in result.stdout
    trial_id = core.trials_with_scheme(app_conn, "eu-ct", [CT])[CT]
    assert core.trial_profile(app_conn, trial_id).profile["coded_terms"][0]["code"] == DIABETES_TERM["classificationCode"]
