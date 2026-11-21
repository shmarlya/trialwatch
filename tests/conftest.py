"""Tests run against a database of their own, never the working one: <database>_test on the same
Postgres server, created afresh for each test session from db/migrations/*.sql (their migrate:up
sections, in order, each in a transaction, as dbmate applies them).

The server is the one DATABASE_URL names, or, outside compose, the db container on the localhost port
.env publishes. Without a reachable server the database tests are skipped. While the tests run,
DATABASE_URL points at the test database, so code that connects by itself (the web interface, the
CLI) uses it too.
"""

import dataclasses
import os
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from trialwatch import db, env
from trialwatch.checks import settings as check_settings
from trialwatch.db import SESSION_OPTIONS
from trialwatch.env import ENV_FILE, read_env_file

ROOT = Path(__file__).parent.parent
MIGRATIONS = ROOT / "db" / "migrations"


def server_url() -> str | None:
    """The working database's URL: DATABASE_URL, else built from the POSTGRES_* settings (the
    environment's, then .env's). Nothing else from .env is loaded: tests never get a real API key."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    values = read_env_file(ENV_FILE)
    for key, value in os.environ.items():
        if key.startswith("POSTGRES_") and value:
            values[key] = value
    # env.database_url, not the fixture below that shares its name
    return env.database_url(values)


def up_section(path: Path) -> str:
    """The SQL of a migration's migrate:up section."""
    text = path.read_text()
    after_up = text.split("-- migrate:up", 1)[1]
    return after_up.split("-- migrate:down", 1)[0]


def create_test_database(url: str) -> str:
    """Drop and create <database>_test next to the database `url` names and apply every migration;
    returns the test database's URL."""
    info = psycopg.conninfo.conninfo_to_dict(url)
    test_name = f"{info['dbname']}_test"
    with psycopg.connect(url, autocommit=True, connect_timeout=3) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(test_name)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(test_name)))
    info["dbname"] = test_name
    test_url = psycopg.conninfo.make_conninfo(**info)  # the prepared dictionary as keyword arguments
    with psycopg.connect(test_url, autocommit=True) as conn:
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            with conn.transaction():
                conn.execute(up_section(migration))
    return test_url


@pytest.fixture(scope="session")
def database_url():
    """The test database's URL, with DATABASE_URL pointing at it for the whole session."""
    url = server_url()
    if url is None:
        pytest.skip("no database configured")
    try:
        test_url = create_test_database(url)
    except psycopg.OperationalError as exc:
        pytest.skip(f"database not reachable: {exc}")
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_url
    yield test_url
    if previous is None:
        del os.environ["DATABASE_URL"]
    else:
        os.environ["DATABASE_URL"] = previous


@pytest.fixture
def conn(database_url):
    """A connection inside a transaction that is rolled back afterwards, so tests leave no rows behind.
    The code's own `conn.transaction()` blocks become savepoints inside it."""
    connection = psycopg.connect(database_url, options=SESSION_OPTIONS)
    connection.execute("SELECT 1")  # opens the outer transaction
    yield connection
    connection.rollback()
    connection.close()


class SameConnection:
    """What `with db.connect() as conn:` gets in a test: the test's own connection, left open and in
    its transaction when the block ends."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> psycopg.Connection:
        return self.connection

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        return False


class ConnectsTo:
    """A stand-in for trialwatch.db.connect that always gives the same connection."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def __call__(self, actor: str | None = None, actor_kind: str = "person") -> SameConnection:
        return SameConnection(self.connection)


@pytest.fixture
def app_conn(conn, monkeypatch):
    """The test's connection, also for code that opens its own (the web interface, the CLI), so it
    reads the rows the test wrote; they are rolled back afterwards like any test's."""
    monkeypatch.setattr(db, "connect", ConnectsTo(conn))
    return conn


@pytest.fixture
def other_session(database_url):
    """A second session on the test database, in autocommit as the program's own are: what another
    process sees and holds (advisory locks)."""
    connection = psycopg.connect(database_url, autocommit=True, options=SESSION_OPTIONS)
    yield connection
    connection.close()


def default_settings(changes: dict | None = None) -> check_settings.Settings:
    """The check's default setup (checks/configs/default.json), with some values changed for a test."""
    chosen = check_settings.load(check_settings.DEFAULT)
    if changes:
        # the prepared changes are passed as keyword arguments to the library call
        chosen = dataclasses.replace(chosen, **changes)
    return chosen
