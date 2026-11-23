"""AI tags with checked quotes (ADR-008, phase 2): each tag names a field of the trial's profile and
quotes it; tags whose quote is not in that field, or whose value was not asked for, are dropped with
why; each call is priced and recorded; the search uses what was kept; automatic runs are held by the
spending cap."""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from places import load_places
from recruiting_trials import ManyCtis, listing, record
from test_cli import Calls
from test_jobs import queue, work
from test_sources_ctis import transfer
from typer.testing import CliRunner
from vocabulary import load_mesh

from trialwatch import cli, db
from trialwatch.db import core
from trialwatch.db import costs as costs_db
from trialwatch.db import jobs as jobs_db
from trialwatch.db import search as search_db
from trialwatch.jobs import refresh, steps
from trialwatch.search import annotate
from trialwatch.sources import ctis, profile
from trialwatch.trials import ingest

CT = "2026-500021-11-00"
ENDED = "2026-500022-11-00"

ANSWER = {
    "categories": [{"category": "metabolism", "field": "condition-1", "quote": "Type 2 Diabetes Mellitus"}],
    "interventions": [{"kind": "small-molecule", "field": "product-1", "quote": "FILM-COATED TABLET"}],
    "purposes": [{"purpose": "treatment", "field": "objective", "quote": "To compare the new medicine with placebo"}],
    "synonyms": [
        {"term": "sugar diabetes", "language": "en", "field": "condition-1", "quote": "Type 2 Diabetes Mellitus"},
        {"term": "diabète sucré", "language": "fr", "field": "condition-1-fr", "quote": "Diabète de type 2"},
        # German was not asked for: the trial recruits in France only
        {"term": "Zuckerkrankheit", "language": "de", "field": "condition-1", "quote": "Type 2 Diabetes Mellitus"},
        # a quote that is not in its field
        {"term": "made up", "language": "en", "field": "condition-1", "quote": "Type 3 Diabetes Mellitus"},
        # from a criterion, not a condition
        {"term": "adults", "language": "en", "field": "inclusion-1", "quote": "Adults aged 18 to 75 years"},
    ],
    "concepts": [{"ui": "D003924", "field": "condition-1", "quote": "Type 2 Diabetes Mellitus"},
                 {"ui": "D999999", "field": "condition-1", "quote": "Type 2 Diabetes Mellitus"}],
    "age_limits": {
        "minimum": {"value": 18, "unit": "years", "field": "inclusion-1", "quote": "aged 18 to 75 years"},
        # 80 is not in its quote
        "maximum": {"value": 80, "unit": "years", "field": "inclusion-1", "quote": "aged 18 to 75 years"},
    },
}


class Model:
    """The model: every call answers `answer` (JSON), and is counted."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        return SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(input_tokens=4000, output_tokens=900),
                               content=[SimpleNamespace(type="text", text=json.dumps(self.answer))])

    def __call__(self):
        return self


@pytest.fixture
def recruiting(app_conn, tmp_path):
    core.act_as(app_conn, "test-person")
    load_places(app_conn, tmp_path)
    load_mesh(app_conn, tmp_path)
    records = {CT: record(CT), ENDED: record(ENDED, status="Ended", status_code=8)}
    fake = ManyCtis(records, [listing(CT), listing(ENDED, status_code=8, countries={"France": 8})])
    ingest.sync_list(app_conn, core.registered_source(app_conn, "ctis"), ctis.CtisAdapter(fake))
    transfer(app_conn, fake, keys=[CT, ENDED])
    found = core.trials_with_scheme(app_conn, "eu-ct", [CT, ENDED])
    app_conn.execute("UPDATE jobs.schedule SET enabled = false")
    return found


def stored_profile(conn, trial_id):
    return profile.current(conn, trial_id).read


def test_only_tags_quoted_from_their_field_and_asked_for_are_kept(app_conn, recruiting):
    read = stored_profile(app_conn, recruiting[CT])
    fields = annotate.fields_for(read)
    field_ids = []
    for item in fields:
        field_ids.append(item.id)
    # exclusion criteria are never sent
    assert "Pregnancy" not in json.dumps([item.text for item in fields])
    assert field_ids[:3] == ["condition-1", "condition-1-fr", "condition-1-es"] and "inclusion-1" in field_ids
    languages = annotate.languages_for(app_conn, read, 6)
    assert languages == ["en", "fr"]
    candidates = annotate.candidates_for(app_conn, read, 15)
    assert ("D003924", "Diabetes Mellitus, Type 2") in candidates
    checked = annotate.check_answer(ANSWER, fields, languages, candidates)
    terms = []
    for synonym in checked.facts["synonyms"]:
        terms.append((synonym["term"], synonym["language"]))
    assert terms == [("sugar diabetes", "en"), ("diabète sucré", "fr")]
    assert [concept["ui"] for concept in checked.facts["concepts"]] == ["D003924"]
    assert checked.facts["age_min"]["years"] == 18.0 and checked.facts["age_max"] is None
    assert checked.facts["interventions"][0]["kind"] == "small-molecule"
    reasons = []
    for dropped in checked.dropped:
        reasons.append((dropped["tag"], dropped["why"]))
    assert reasons == [
        ("synonym", "not in a language it was asked for"),
        ("synonym", "its quote is not in that field"),
        ("synonym", "not of a condition"),
        ("concept", "not one of the MeSH concepts it was given"),
        ("age maximum", "its number is not in its quote"),
    ]


def test_a_field_named_with_its_brackets_is_the_field():
    fields = [annotate.Field("condition-1", "Asthma")]
    answer = {"categories": [{"category": "lungs", "field": "[condition-1]", "quote": "Asthma"}]}
    checked = annotate.check_answer(answer, fields, ["en"], [])
    assert checked.facts["categories"] == [{"category": "lungs", "field": "condition-1", "quote": "Asthma"}]


def test_months_become_years_and_a_short_field_may_be_quoted_whole():
    fields = [annotate.Field("condition-1", "Asthma"), annotate.Field("inclusion-1", "Children aged 6 months to 11 years")]
    answer = {"synonyms": [{"term": "wheezing illness", "language": "en", "field": "condition-1", "quote": "Asthma"}],
              "age_limits": {"minimum": {"value": 6, "unit": "months", "field": "inclusion-1", "quote": "aged 6 months"},
                             "maximum": None}}
    checked = annotate.check_answer(answer, fields, ["en"], [])
    assert checked.facts["synonyms"][0]["term"] == "wheezing illness"
    assert checked.facts["age_min"]["years"] == 0.5 and checked.dropped == []


def test_a_run_records_its_prompt_and_cost_and_the_search_uses_the_kept_tags(app_conn, recruiting):
    model = Model(ANSWER)
    summary = annotate.run(app_conn, model, [recruiting[CT]])
    assert (summary.trials, summary.tagged, summary.kept, summary.dropped) == (1, 1, 7, 5)
    request = model.requests[0]
    assert request["model"] == "claude-haiku-5-5" and request["system"].startswith("You tag one clinical trial")
    assert "[condition-1] Type 2 Diabetes Mellitus" in request["messages"][0]["content"]
    run = app_conn.execute("SELECT status, trials, prompt, length(prompt_text) > 0 FROM "
                           "search.annotation_run WHERE id = %s", (summary.run_id,)).fetchone()
    assert run == ("succeeded", 1, "annotate", True)
    # the call: the request as sent (without the system prompt and schema), the answer, its cost
    calls = app_conn.execute("SELECT operation, trial_id, input_tokens, cost_usd > 0, request ? 'system', "
                             "request ? 'messages', response, stop_reason FROM core.llm_call WHERE annotation_run_id = %s",
                             (summary.run_id,)).fetchall()
    assert calls == [("annotate", recruiting[CT], 4000, True, False, True, json.dumps(ANSWER), "end_turn")]
    assert summary.cost_usd > 0
    # a synonym only the AI gave finds the trial; the exact age limit narrows it
    hits, _total = search_db.search_hits(app_conn, search_db.SearchFilter(tsquery="sugar:* & diabete:*"), 0, 10)
    assert [hit.trial_id for hit in hits] == [recruiting[CT]]
    too_young = search_db.SearchFilter(tsquery="sugar:*", age=16)
    assert search_db.search_hits(app_conn, too_young, 0, 10)[1] == 0
    document = app_conn.execute("SELECT age_min, interventions, purposes FROM search.document WHERE trial_id = %s",
                                (recruiting[CT],)).fetchone()
    assert (float(document[0]), document[1], document[2]) == (18.0, ["small-molecule"], ["treatment"])
    # tagged for its profile: nothing left to tag; the ended trial is never tagged
    assert search_db.trials_to_annotate(app_conn) == []


def test_the_page_shows_the_tags_as_ai_made_with_their_quotes(app_conn, recruiting):
    from fastapi.testclient import TestClient

    from trialwatch.web import app as web

    annotate.run(app_conn, Model(ANSWER), [recruiting[CT]])
    page = TestClient(web.app).get(f"/trials/{recruiting[CT]}").text
    assert "What AI read in the registry" in page and "AI · claude-haiku-5-5" in page
    assert "sugar diabetes" in page and "“aged 18 to 75 years” (inclusion-1)" in page
    assert "(5 dropped)" in page


def no_sources(conn, capability):
    """No source to sync or transfer from: the refresh goes straight to tagging."""
    return []


def test_the_refresh_tags_recruiting_trials_and_the_cap_holds_it(app_conn, recruiting, monkeypatch):
    model = Model(ANSWER)
    monkeypatch.setattr(steps, "anthropic_client", model)
    monkeypatch.setattr(refresh, "_sources", no_sources)
    core.act_as(app_conn, "test-person")
    costs_db.set_monthly_cap(app_conn, Decimal("0"))
    job_id = queue(app_conn, "refresh", {}, actor=db.SYSTEM_ACTOR, automatic=True)
    work(app_conn, monkeypatch)
    job = jobs_db.job(app_conn, job_id)
    assert (job.status, job.last_error, job.attempts) == ("held", steps.CAP_REACHED, 0)
    assert model.requests == []
    costs_db.set_monthly_cap(app_conn, None)
    work(app_conn, monkeypatch)
    job = jobs_db.job(app_conn, job_id)
    assert job.status == "succeeded", job.last_error
    assert len(model.requests) == 1 and job.result["annotate"]["tagged"] == 1


def test_the_command_dry_run_sends_nothing(app_conn, recruiting, monkeypatch):
    monkeypatch.setattr(cli, "load_env_file", Calls())
    result = CliRunner().invoke(cli.app, ["search", "annotate", "--all", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert "1 trials to tag with claude-haiku-5-5" in result.stdout
    assert "<languages>en, fr</languages>" in result.stdout and "dry run: nothing was sent" in result.stdout
