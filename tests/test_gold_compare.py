from datetime import date

import pytest
from psycopg.types.json import Jsonb

from trialwatch.ctis.store import start_run
from trialwatch.gold import compare

# Valid format but not real trials, so tests never collide with seeded or fetched data
CT_A, CT_B = "1999-000000-00-01", "1999-000000-00-02"


def test_reproduces_bruckner_verdicts_on_their_export(conn):
    # ADR-001's gate for the due-date rules; needs the export, PIP list and verdicts (CLI or dev seed)
    try:
        result = compare.compare_due(conn)
    except ValueError:
        pytest.skip("Bruckner et al.'s data is not loaded")
    assert len(result.due_ours) == len(result.due_theirs) == 277
    assert result.disagreements == []


def test_disagreements_are_reported_per_trial(conn, monkeypatch):
    # A day and file names of our own, so only these rows are "the export"
    monkeypatch.setattr(compare, "SNAPSHOT_DAY", date(1999, 6, 1))
    monkeypatch.setattr(compare, "EXPORT_FILE", "test_export.csv")
    monkeypatch.setattr(compare, "PIP_FILE", "test_pip.csv")
    run = start_run(conn, "seed", {"test": True})
    export = {
        # adult, completed 17 months before: due, nothing posted
        CT_A: {"Overall trial status": "Ended", "Age group": "18-64 years", "End date": "01/01/1998",
               "Global end of the trial": "N/A"},
        # children, completed 3 months before: not due yet
        CT_B: {"Overall trial status": "Ended", "Age group": "0-17 years", "End date": "01/03/1999",
               "Global end of the trial": "N/A"},
    }
    for ct_number, payload in export.items():
        conn.execute("INSERT INTO ctis.trial (ct_number) VALUES (%s)", (ct_number,))
        # Only CT_B is in the PIP export (it is paediatric by age anyway)
        for source_file in ("test_export.csv", "test_pip.csv") if ct_number == CT_B else ("test_export.csv",):
            conn.execute(
                "INSERT INTO ctis.search_export_snapshot (ct_number, fetch_run_id, captured_on, source_file, "
                "content_hash, payload) VALUES (%s, %s, '1999-06-01', %s, %s, %s)",
                (ct_number, run, source_file, bytes(32), Jsonb(payload)),
            )
    for ct_number, verdict, document, status, completion in (
        (CT_A, "not fully reported", "none", "adult", "1998-01-01"),
        (CT_B, "extended", "none", "paediatric", "1999-03-01"),  # they counted it as due
    ):
        conn.execute(
            "INSERT INTO gold.bruckner_verdict (ct_number, fetch_run_id, source_file, verdict, results_document, "
            "lay_summary, content_hash, payload) VALUES (%s, %s, 'test.csv', %s, %s, false, %s, %s)",
            (ct_number, run, verdict, document, bytes(32),
             Jsonb({"regulatory_status": status, "completion_date": completion, "extended_not_due_yet": "NA"})),
        )

    result = compare.compare_due(conn)

    assert result.due_ours == {CT_A}
    # CT_A agrees on everything (due, adult, completion, verdict); CT_B only they counted as due.
    # (The real 277 verdicts show up as "due only for them" here too; they're not this test's rows.)
    mine = [d for d in result.disagreements if d.ct_number in (CT_A, CT_B)]
    assert mine == [compare.Disagreement(CT_B, "due", False, True)]
    assert result.fields["verdict"][0] >= 1
