"""The home page's charts: checked trials by result per country and per primary sponsor, from each
trial's current published judgement (withdrawn ones left out)."""

from fastapi.testclient import TestClient
from test_evidence import seed
from test_publication import judged

from trialwatch.db import checks as checks_db
from trialwatch.db import core, overview
from trialwatch.web import app as web

SECOND = "2023-500098-11-00"
THIRD = "2023-500097-11-00"
FOURTH = "2023-500096-11-00"


def checked_trials(conn):
    """Four checked trials: full in Spain and Belgium, not full in Spain, undecided in Belgium, and a
    withdrawn one in Spain. Acme sponsors all of them, Uni co-sponsors the first."""
    first = seed(conn)
    second = seed(conn, ct_number=SECOND)
    third = seed(conn, ct_number=THIRD)
    fourth = seed(conn, ct_number=FOURTH)
    core.act_as(conn, "test-person")
    core.set_trial_locations(conn, first, "ctis", [core.Location("Spain"), core.Location("Belgium", "Gent")])
    core.set_trial_locations(
        conn, second, "ctis", [core.Location("Spain", "Madrid"), core.Location("Spain", "Sevilla")]
    )
    core.set_trial_locations(conn, third, "ctis", [core.Location("Belgium")])
    core.set_trial_locations(conn, fourth, "ctis", [core.Location("Spain")])
    acme = core.Sponsor("Acme", primary=True)
    core.set_trial_sponsors(conn, first, "ctis", [acme, core.Sponsor("Uni", primary=False)])
    for trial_id in (second, third, fourth):
        core.set_trial_sponsors(conn, trial_id, "ctis", [acme])
    judged(conn, first, label="full")
    judged(conn, second, label="not full")
    judged(conn, third, label="undecided")
    withdrawn_run = judged(conn, fourth, label="full")
    checks_db.withdraw_judgement(conn, withdrawn_run, fourth, "test")


def counts(rows):
    found = {}
    for row in rows:
        found[row.name] = (row.full, row.not_full, row.undecided, row.total)
    return found


def test_by_country_counts_a_trial_once_per_country_and_leaves_out_withdrawn_ones(app_conn):
    checked_trials(app_conn)
    rows = overview.reported_by_country(app_conn, "eu-ctr-results")
    assert counts(rows) == {"Spain": (1, 1, 0, 2), "Belgium": (1, 0, 1, 2)}
    assert [row.reported for row in rows] == [50.0, 50.0]


def test_by_sponsor_counts_primary_sponsors_with_enough_trials(app_conn):
    checked_trials(app_conn)
    assert counts(overview.reported_by_sponsor(app_conn, "eu-ctr-results")) == {"Acme": (1, 1, 1, 3)}
    assert counts(overview.reported_by_sponsor(app_conn, "eu-ctr-results", at_least=4)) == {}


def test_the_home_page_draws_both_charts_with_links_to_the_filter(app_conn):
    checked_trials(app_conn)
    home = TestClient(web.app).get("/").text
    assert 'id="by-country"' in home and 'id="by-sponsor"' in home
    assert 'href="/?country=Spain#checked"' in home and 'href="/?q=Acme#checked"' in home
    assert "/static/charts.js" in home
    assert "style=" not in home


def test_with_nothing_checked_the_charts_say_so(app_conn):
    home = TestClient(web.app).get("/").text
    assert 'id="by-country"' in home and "data-bars" not in home
