"""The search index (ADR-008): each trial's search document is built from its profile at transfer, with
the MeSH concepts its conditions name; what words weigh, what is never indexed, and how a search
finds trials by word, concept, category and translation."""

from places import load_places
from recruiting_trials import ManyCtis, listing, record
from test_cli import Calls
from test_sources_ctis import transfer
from typer.testing import CliRunner
from vocabulary import load_mesh, write_mesh

from trialwatch import cli
from trialwatch.db import core
from trialwatch.db import search as search_db
from trialwatch.db import vocab as vocab_db
from trialwatch.search import concepts, index
from trialwatch.sources import ctis, stages

DIABETES_CT = "2026-500001-11-00"
NIDDM_CT = "2026-500002-11-00"
ASTHMA_CT = "2026-500003-11-00"
CANCER_CT = "2026-500004-11-00"
ENDED_CT = "2026-500005-11-00"


def condition(name, translations=None):
    if translations is None:
        translations = {}
    return {"name": name, "translations": translations, "rare": False}


def trials():
    """Five trials: type 2 diabetes (French and Spanish translations), one that names it only as
    NIDDM, asthma excluding diabetes, cancers named inside a longer text, and one that has ended."""
    records = {
        DIABETES_CT: record(DIABETES_CT),
        NIDDM_CT: record(NIDDM_CT, conditions=(condition("NIDDM"),), terms=(), title="A sugar study",
                         inclusion=("Adults with NIDDM",)),
        ASTHMA_CT: record(ASTHMA_CT, conditions=(condition("Asthma"),), terms=(),
                          areas=("Diseases [C] - Respiratory Tract Diseases [C08]",), inclusion=("Adults with asthma",),
                          exclusion=("Type 2 diabetes", "Diabetes mellitus of any kind"), title="An inhaler study"),
        CANCER_CT: record(CANCER_CT, conditions=(condition("Advanced solid tumours (colorectal cancer, breast cancer)"),),
                          terms=(), areas=("Diseases [C] - Neoplasms [C04]",), title="A tumour study",
                          inclusion=("A measurable tumour",)),
        ENDED_CT: record(ENDED_CT, status="Ended", status_code=8),
    }
    rows = [listing(DIABETES_CT), listing(NIDDM_CT), listing(ASTHMA_CT), listing(CANCER_CT),
            listing(ENDED_CT, status_code=8, countries={"France": 8})]
    return records, rows


def bring_in(conn, tmp_path, mesh=True):
    core.act_as(conn, "test-person")
    if mesh:
        load_mesh(conn, tmp_path)
    records, rows = trials()
    fake = ManyCtis(records, rows)
    stages.sync_list(conn, core.registered_source(conn, "ctis"), ctis.CtisAdapter(fake))
    result = transfer(conn, fake, keys=list(records))
    assert result.failed == {}, result.failed
    found = {}
    for ct, trial_id in core.trials_with_scheme(conn, "eu-ct", list(records)).items():
        found[ct] = trial_id
    return found


def found_by(conn, tsquery, trial_ids):
    hits, _total = search_db.search_hits(conn, search_db.SearchFilter(tsquery=tsquery), 0, 50)
    names = {}
    for ct, trial_id in trial_ids.items():
        names[trial_id] = ct
    cts = []
    for hit in hits:
        cts.append(names[hit.trial_id])
    return cts


def test_mesh_loads_diseases_with_their_synonyms_and_hierarchy(conn, tmp_path):
    core.act_as(conn, "test-person")
    summary = load_mesh(conn, tmp_path)
    assert (summary.version, summary.descriptors) == ("2026", 18)
    assert [match.ui for match in vocab_db.terms_named(conn, ["heart attack"])] == ["D009203"]
    # a qualifier's name is no term
    assert vocab_db.terms_named(conn, ["therapy"]) == []
    assert sorted(concepts.with_ancestors(conn, ["D003924"])) == ["D003920", "D004700", "D008659", "D009750", "D044882"]


def test_conditions_name_concepts_without_ai(conn, tmp_path):
    core.act_as(conn, "test-person")
    load_mesh(conn, tmp_path)
    found = concepts.find(conn, [("condition-1", "Type 2 Diabetes Mellitus"), ("condition-2", "NIDDM"),
                                 ("condition-3", "Advanced solid tumours (colorectal cancer, breast cancer)"),
                                 ("meddra-1", "Type II diabetes mellitus"), ("condition-4", "Pain")])
    seen = []
    for concept in found:
        seen.append((concept.field, concept.ui, concept.method, concept.quote))
    assert seen == [
        ("condition-1", "D003924", "same-words", "Type 2 Diabetes Mellitus"),
        ("condition-2", "D003924", "exact-name", "NIDDM"),
        # the longest phrases, not the "cancer" inside them
        ("condition-3", "D015179", "phrase", "colorectal cancer"),
        ("condition-3", "D001943", "phrase", "breast cancer"),
        # "diabetes mellitus" inside the MedDRA name ("Type II" is no MeSH word)
        ("meddra-1", "D003920", "phrase", "diabetes mellitus"),
    ]


def test_a_query_names_concepts_the_broadest_first(conn, tmp_path):
    core.act_as(conn, "test-person")
    load_mesh(conn, tmp_path)
    assert concepts.for_query(conn, ["heart", "attack"]) == [("D009203", "Myocardial Infarction")]
    assert concepts.for_query(conn, ["breast", "cancer"]) == [("D001943", "Breast Neoplasms")]
    # not a term itself: the broadest descriptors with the word and one more
    assert concepts.for_query(conn, ["diabetes"]) == [("D003919", "Diabetes Insipidus"), ("D003920", "Diabetes Mellitus")]
    assert concepts.for_query(conn, ["sunburn"]) == []


def test_a_transfer_makes_each_trial_findable(conn, tmp_path):
    trial_ids = bring_in(conn, tmp_path)
    # by its words, beginning like the query's
    assert found_by(conn, "diabet:*", trial_ids) == [DIABETES_CT]
    # through a translation, accents aside
    assert found_by(conn, "diabète:*", trial_ids) == [DIABETES_CT]
    # NIDDM's concept (type 2) sits under Diabetes Mellitus: the concept finds it
    assert sorted(found_by(conn, f"diabet:* | {concepts.token('D003920')}", trial_ids)) == [DIABETES_CT, NIDDM_CT]
    # exclusion criteria are never indexed: the asthma trial excludes diabetes, it is not about it
    assert ASTHMA_CT not in found_by(conn, "diabet:*", trial_ids)
    # a category from the therapeutic area, and concepts named inside a longer condition
    assert found_by(conn, "zcatcancer", trial_ids) == [CANCER_CT]
    assert found_by(conn, concepts.token("D001943"), trial_ids) == [CANCER_CT]
    assert found_by(conn, concepts.token("D009369"), trial_ids) == [CANCER_CT]
    # an ended trial is indexed, never found
    assert ENDED_CT not in found_by(conn, "diabet:*", trial_ids)
    assert search_db.document_hash(conn, trial_ids[ENDED_CT]) is not None
    # the concepts shown with a trial say where they were found
    shown = search_db.trial_concepts(conn, trial_ids[NIDDM_CT])
    assert [(item.name, item.method, item.field, item.quote) for item in shown] == [
        ("Diabetes Mellitus, Type 2", "exact-name", "condition-1", "NIDDM")]


def test_condition_words_weigh_more_than_title_words(conn, tmp_path):
    trial_ids = bring_in(conn, tmp_path)
    # "study" is only in titles; "tumour" is the cancer trial's condition and title: it ranks first
    hits, total = search_db.search_hits(conn, search_db.SearchFilter(tsquery="tumour:* | study:*"), 0, 10)
    assert total == 4 and hits[0].trial_id == trial_ids[CANCER_CT]


def test_the_list_saying_it_stopped_recruiting_hides_it_at_once(conn, tmp_path):
    trial_ids = bring_in(conn, tmp_path)
    conn.execute("UPDATE core.source_listing SET recruiting = false WHERE source_key = %s", (DIABETES_CT,))
    assert found_by(conn, "diabet:*", trial_ids) == []
    assert search_db.public_recruiting(conn, trial_ids[DIABETES_CT]) is False
    assert search_db.public_recruiting(conn, trial_ids[NIDDM_CT]) is True


def test_did_you_mean_offers_known_words(conn, tmp_path):
    bring_in(conn, tmp_path)
    assert search_db.known_word(conn, "diab") is True
    assert search_db.known_word(conn, "diabtes") is False
    assert search_db.suggestion(conn, "diabtes") == "diabetes"
    assert search_db.suggestion(conn, "xyzzy") is None


def test_an_unchanged_profile_is_not_rebuilt_and_reindex_rebuilds_them_all(app_conn, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_env_file", Calls())
    trial_ids = bring_in(app_conn, tmp_path, mesh=False)
    # without MeSH no concept is found; the trial is found by its words
    assert found_by(app_conn, concepts.token("D003920"), trial_ids) == []
    assert index.rebuild_trial(app_conn, trial_ids[DIABETES_CT]) is False
    load_mesh(app_conn, tmp_path)
    result = CliRunner().invoke(cli.app, ["search", "reindex", "--all"])
    assert result.exit_code == 0, result.stdout
    assert "5 trials with a profile, 5 documents built" in result.stdout
    assert "4 recruiting" in result.stdout
    assert sorted(found_by(app_conn, concepts.token("D003920"), trial_ids)) == [DIABETES_CT, NIDDM_CT]


def test_vocab_load_reads_a_file_and_rebuilds_the_index(app_conn, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_env_file", Calls())
    bring_in(app_conn, tmp_path, mesh=False)
    path = write_mesh(tmp_path)
    result = CliRunner().invoke(cli.app, ["vocab", "load", "--from", str(path)])
    assert result.exit_code == 0, result.stdout
    assert "MeSH 2026, 18 descriptors" in result.stdout and "rebuilt 5 search documents" in result.stdout


def test_sites_get_points_during_the_transfer_once_places_are_loaded(conn, tmp_path):
    core.act_as(conn, "test-person")
    load_places(conn, tmp_path)
    bring_in(conn, tmp_path, mesh=False)
    rows = conn.execute("SELECT precision, count(*) FROM geo.address_point GROUP BY precision").fetchall()
    assert rows == [("postcode", 2)]
