from datetime import UTC

import psycopg
import pytest

from trialwatch import db
from conftest import _database_url


def test_connect_runs_sessions_in_utc(monkeypatch):
    url = _database_url()
    if not url:
        pytest.skip("no database configured")
    monkeypatch.setenv("DATABASE_URL", url)
    try:
        conn = db.connect()
    except psycopg.OperationalError as exc:
        pytest.skip(f"database not reachable: {exc}")
    with conn:
        assert conn.execute("SHOW TimeZone").fetchone()[0] == "UTC"
        # timestamptz values come back as aware datetimes in UTC
        now = conn.execute("SELECT now()").fetchone()[0]
        assert now.utcoffset().total_seconds() == 0 and now.tzinfo is not None
        assert now.astimezone(UTC) == now


def test_database_default_is_utc(conn):
    # Set by the init migration (ALTER DATABASE ... SET TimeZone), so even clients that don't ask for UTC get it
    setting = conn.execute(
        """
        SELECT s.setconfig FROM pg_db_role_setting s
        JOIN pg_database d ON d.oid = s.setdatabase
        WHERE d.datname = current_database() AND s.setrole = 0
        """
    ).fetchone()
    assert setting is not None and "TimeZone=UTC" in setting[0]
