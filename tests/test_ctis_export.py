import csv
from datetime import date

import pytest

from trialwatch.ctis.export import captured_on_from_name, seed_export

# Valid format but not real trials, so tests never collide with seeded or fetched data
CT_A, CT_B = "1999-000000-00-01", "1999-000000-00-02"
HEADER = ["Trial number", "Overall trial status", "Trial results"]


def write_export(path, rows):
    # The portal writes UTF-8 with a byte-order mark
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(rows)
    return path


def stored(conn):
    return conn.execute(
        """
        SELECT ct_number, captured_on, payload->>'Overall trial status'
        FROM ctis.search_export_snapshot WHERE ct_number IN (%s, %s) ORDER BY ct_number
        """,
        (CT_A, CT_B),
    ).fetchall()


def test_date_comes_from_file_name(tmp_path):
    assert captured_on_from_name(tmp_path / "CTIS_trials_Phases_II-IV_20251110.csv") == date(2025, 11, 10)
    with pytest.raises(ValueError):
        captured_on_from_name(tmp_path / "export.csv")


def test_rows_stored_as_in_file_and_reload_adds_nothing(conn, tmp_path):
    path = write_export(tmp_path / "CTIS_trials_20251110.csv", [[CT_A, "Ended", "Yes"], [CT_B, "Ongoing, recruiting", "No"]])

    first = seed_export(conn, path, date(2025, 11, 10))
    again = seed_export(conn, path, date(2025, 11, 10))

    assert (first.fetched, first.changed, again.changed) == (2, 2, 0)
    assert stored(conn) == [(CT_A, date(2025, 11, 10), "Ended"), (CT_B, date(2025, 11, 10), "Ongoing, recruiting")]
    payload = conn.execute(
        "SELECT payload FROM ctis.search_export_snapshot WHERE ct_number = %s", (CT_A,)
    ).fetchone()[0]
    assert payload == {"Trial number": CT_A, "Overall trial status": "Ended", "Trial results": "Yes"}
    # The export is not a /retrieve response, so live-data queries don't see it
    assert conn.execute("SELECT count(*) FROM ctis.trial_latest WHERE ct_number = %s", (CT_A,)).fetchone() == (0,)


def test_bad_row_is_skipped_and_recorded(conn, tmp_path):
    path = write_export(tmp_path / "x_20251110.csv", [[CT_A, "Ended", "Yes"], ["not-a-number", "Ended", "No"]])

    result = seed_export(conn, path, date(2025, 11, 10))

    assert (result.fetched, result.changed, list(result.failed)) == (1, 1, ["line 3"])
    status, error = conn.execute(
        "SELECT status, error FROM ctis.fetch_run WHERE id = %s", (result.run_id,)
    ).fetchone()
    assert status == "failed" and "not-a-number" in error


def test_file_without_trial_number_column_is_rejected(conn, tmp_path):
    path = tmp_path / "other_20251110.csv"
    path.write_text("id,name\n1,x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Trial number"):
        seed_export(conn, path, date(2025, 11, 10))


def test_same_rows_in_another_export_are_kept_as_their_own_facts(conn, tmp_path):
    # e.g. a trial in both the Phase II-IV export and the PIP export of the same day
    rows = [[CT_A, "Ended", "Yes"]]
    main = seed_export(conn, write_export(tmp_path / "CTIS_trials_Phases_II-IV_20251110.csv", rows), date(2025, 11, 10))
    pip = seed_export(conn, write_export(tmp_path / "CTIS_trials_with_PIP_20251110.csv", rows), date(2025, 11, 10))

    assert (main.changed, pip.changed) == (1, 1)
    files = conn.execute(
        "SELECT source_file FROM ctis.search_export_snapshot WHERE ct_number = %s ORDER BY source_file", (CT_A,)
    ).fetchall()
    assert files == [("CTIS_trials_Phases_II-IV_20251110.csv",), ("CTIS_trials_with_PIP_20251110.csv",)]
