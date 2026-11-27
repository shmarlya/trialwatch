"""Database access. Every SQL statement in TrialWatch lives in this package, one module per schema:

- trialwatch.db.core: the central, source-agnostic model (ADR-003): sources, actors and the audit
  log, ingest runs, trials and their identifiers, records and sponsors, documents, their versions and
  pages, trial ↔ document links, and the advisory locks that keep processes apart
- trialwatch.db.checks: what our checks answered
- trialwatch.db.overview: read-only queries across both for the web interface

The rest of the code calls these functions and never writes SQL (tests/test_sql_lives_in_db.py
checks that). Values always travel as query parameters, never formatted into the SQL text.
"""

import os
import pwd
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager

import psycopg
from psycopg_pool import ConnectionPool

from trialwatch import config

# Sessions run in UTC so timestamps come back as UTC-aware datetimes, whatever the server default
SESSION_OPTIONS = "-c TimeZone=UTC"
# Every connection: give up connecting after 10 s, and notice a database that went away without a word
# (a restart, a network cut) within about 90 s, instead of waiting on it for ever
LIVENESS = {
    "connect_timeout": 10,
    "keepalives": 1,
    "keepalives_idle": 60,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}
# TrialWatch itself (core.actor 1): who writes what no person does, such as scheduled jobs, automatic
# checks and publishing, migrations and seeding the first admin
SYSTEM_ACTOR = "trialwatch"
SYSTEM_ACTOR_ID = 1


# The web interface's connections: a pool, open while its server runs (web/app.py opens and closes it).
# The worker and the command line open a connection of their own each time: they hold session state
# (advisory locks, who they write as) for a whole job.
_pool: ConnectionPool | None = None
# How long a request waits for a free connection before it fails (and shows an error page)
POOL_WAIT_SECONDS = 10


def open_pool(max_size: int = 10) -> None:
    """Open the pool connect() takes connections from. It fills in the background, so the server
    starts (and /healthz answers) even while the database is down."""
    global _pool
    if _pool is not None:
        return
    settings = {"autocommit": True, "options": SESSION_OPTIONS, **LIVENESS}
    _pool = ConnectionPool(
        config.database_url(),
        min_size=1,
        max_size=max_size,
        kwargs=settings,
        reset=_reset_session,
        check=ConnectionPool.check_connection,
        timeout=POOL_WAIT_SECONDS,
        name="trialwatch-web",
        open=False,
    )
    _pool.open(wait=False)


def close_pool() -> None:
    global _pool
    if _pool is None:
        return
    _pool.close()
    _pool = None


def _reset_session(conn: psycopg.Connection) -> None:
    """Run as a connection goes back to the pool: nothing one request set on its session reaches the
    next one (who it wrote as, its audit note, its advisory locks)."""
    conn.execute("RESET ALL")
    conn.execute("SELECT pg_advisory_unlock_all()")


@contextmanager
def connect(actor: str | None = None, actor_kind: str = "person") -> Iterator[psycopg.Connection]:
    """`with connect() as conn:` a connection whose writes are recorded as made by `actor` (core.actor,
    ADR-003): a person's name, or a process such as SYSTEM_ACTOR with actor_kind="process". Without a
    name, the person is TRIALWATCH_ACTOR or the login name of whoever runs the program. In the web
    server it comes from the pool and goes back to it at the end of the block; elsewhere it is opened
    and closed."""
    if actor is None:
        actor = default_actor()
    # Imported here, not at the top, so the package's modules can be imported in any order
    from trialwatch.db.core import act_as

    if _pool is not None:
        with _pool.connection() as conn:
            act_as(conn, actor, actor_kind)
            yield conn
        return
    with psycopg.connect(config.database_url(), autocommit=True, options=SESSION_OPTIONS, **LIVENESS) as conn:
        act_as(conn, actor, actor_kind)
        yield conn


def migration_version() -> str | None:
    """The newest migration applied to the database (dbmate's schema_migrations); raises when the
    database can't be reached. For /readyz: it writes nothing and names no actor."""
    if _pool is not None:
        with _pool.connection() as conn:
            return _newest_migration(conn)
    with psycopg.connect(
        config.database_url(), autocommit=True, options=SESSION_OPTIONS, connect_timeout=POOL_WAIT_SECONDS
    ) as conn:
        return _newest_migration(conn)


def _newest_migration(conn: psycopg.Connection) -> str | None:
    # a database the tests built from the migrations has no dbmate table
    exists = conn.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL").fetchone()[0]
    if not exists:
        return None
    return conn.execute("SELECT max(version) FROM public.schema_migrations").fetchone()[0]


def connect_system() -> AbstractContextManager[psycopg.Connection]:
    """A connection whose writes are recorded as TrialWatch's own (SYSTEM_ACTOR). Also used for
    reading, so a page view never adds the server's login name as a person."""
    return connect(actor=SYSTEM_ACTOR, actor_kind="process")


def default_actor() -> str:
    """Who is running TrialWatch: TRIALWATCH_ACTOR if set, else the account the program runs under
    (from the operating system, not the USER variable, which a launcher may set to anything)."""
    name = config.actor_name()
    if name is not None:
        return name
    return pwd.getpwuid(os.getuid()).pw_name
