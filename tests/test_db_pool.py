"""The web server's connection pool (trialwatch.db): a connection goes back to the pool with nothing of
the request that used it, so the next request never writes as someone else or holds their locks."""

import pytest

from trialwatch import db
from trialwatch.db import core

SOME_LOCK = 424242


@pytest.fixture
def one_connection_pool(database_url):
    # a single connection, so the second borrow gets the same session the first one used
    db.open_pool(max_size=1)
    yield
    db.close_pool()


def session_note(conn):
    return conn.execute("SELECT current_setting('trialwatch.note', true)").fetchone()[0]


def advisory_locks_held(conn):
    return conn.execute(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
    ).fetchone()[0]


def test_a_pooled_connection_comes_back_without_the_last_requests_session(one_connection_pool):
    with db.connect_system() as conn:
        first_session = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        conn.execute("SELECT set_config('trialwatch.note', 'request one', false)")
        conn.execute("SELECT pg_advisory_lock(%s)", (SOME_LOCK,))
        assert core.current_actor(conn) == db.SYSTEM_ACTOR_ID
        assert advisory_locks_held(conn) == 1
    # borrowed as it is, without naming an actor
    with db._pool.connection() as conn:
        assert conn.execute("SELECT pg_backend_pid()").fetchone()[0] == first_session
        assert core.current_actor(conn) is None
        assert session_note(conn) in (None, "")
        assert advisory_locks_held(conn) == 0
        # what the connection was opened with stays: sessions run in UTC
        assert conn.execute("SHOW TimeZone").fetchone()[0] == "UTC"


def test_the_migration_version_is_read_with_or_without_the_pool(database_url, one_connection_pool):
    # the tests' database is built from the migrations' SQL, without dbmate's table
    assert db.migration_version() is None
    db.close_pool()
    assert db.migration_version() is None
