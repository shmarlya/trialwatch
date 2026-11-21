from datetime import UTC

import pytest

from trialwatch import db
from trialwatch.db import core


def test_connect_runs_sessions_in_utc(database_url):
    with db.connect() as conn:
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


def test_connect_names_the_actor_of_every_write(database_url, monkeypatch):
    monkeypatch.setenv("TRIALWATCH_ACTOR", "test-runner")
    with db.connect() as conn:
        actor = core.current_actor(conn)
        row = conn.execute("SELECT kind, name FROM core.actor WHERE id = %s", (actor,)).fetchone()
    assert row == ("person", "test-runner")
    with db.connect(actor="test-adapter", actor_kind="process") as conn:
        actor = core.current_actor(conn)
        row = conn.execute("SELECT kind, name FROM core.actor WHERE id = %s", (actor,)).fetchone()
    assert row == ("process", "test-adapter")


class ActorTableMissing:
    """act_as that fails, keeping the connection it was given."""

    def __init__(self):
        self.connections = []

    def __call__(self, conn, name, kind="person"):
        self.connections.append(conn)
        raise RuntimeError("no core.actor table")


def test_connect_closes_the_connection_when_it_cannot_name_the_actor(database_url, monkeypatch):
    failing = ActorTableMissing()
    monkeypatch.setattr(core, "act_as", failing)
    with pytest.raises(RuntimeError, match="no core.actor table"):
        db.connect()
    # the caller never got the connection, so connect closed it
    assert len(failing.connections) == 1 and failing.connections[0].closed


def test_connect_needs_a_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        db.connect()
