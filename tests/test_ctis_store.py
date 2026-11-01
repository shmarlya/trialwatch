from trialwatch.ctis.client import TrialNotFound
from trialwatch.ctis.store import content_hash, fetch_trials

# Valid format but not a real trial, so tests never collide with snapshots fetched from CTIS
CT = "1999-000000-00-00"
TRIAL = {"ctNumber": CT, "ctStatus": "Ended", "publishDate": "2026-10-30T02:33:59"}


def test_hash_ignores_key_order_and_publish_date():
    reordered = {"publishDate": "2026-11-06T02:10:00", "ctStatus": "Ended", "ctNumber": CT}
    assert content_hash(TRIAL) == content_hash(reordered)


def test_hash_changes_with_content():
    assert content_hash(TRIAL) != content_hash({**TRIAL, "ctStatus": "Authorised"})


def snapshots(conn, ct=CT):
    return conn.execute(
        "SELECT payload->>'ctStatus' FROM ctis.trial_snapshot WHERE ct_number = %s ORDER BY id", (ct,)
    ).fetchall()


def test_snapshot_only_when_content_changes(conn):
    run = lambda payload: fetch_trials(conn, lambda ct: payload, [CT], kind="fetch", args={})

    first = run(TRIAL)
    same = run({**TRIAL, "publishDate": "2026-11-06T02:10:00"})
    changed = run({**TRIAL, "ctStatus": "Revoked"})

    assert (first.changed, same.changed, changed.changed) == (1, 0, 1)
    assert snapshots(conn) == [("Ended",), ("Revoked",)]
    latest = conn.execute("SELECT payload->>'ctStatus' FROM ctis.trial_latest WHERE ct_number = %s", (CT,))
    assert latest.fetchone() == ("Revoked",)


def test_failed_trial_is_recorded_and_run_continues(conn):
    missing = "2099-000000-00-00"

    def retrieve(ct):
        if ct == missing:
            raise TrialNotFound(ct)
        return TRIAL

    result = fetch_trials(conn, retrieve, [missing, CT], kind="fetch", args={})

    assert (result.fetched, result.changed, list(result.failed)) == (1, 1, [missing])
    status, failed, error = conn.execute(
        "SELECT status, trials_failed, error FROM ctis.fetch_run WHERE id = %s", (result.run_id,)
    ).fetchone()
    assert (status, failed) == ("failed", 1)
    assert missing in error
    assert snapshots(conn, missing) == []
