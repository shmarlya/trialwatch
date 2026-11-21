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

import psycopg

# Sessions run in UTC so timestamps come back as UTC-aware datetimes, whatever the server default
SESSION_OPTIONS = "-c TimeZone=UTC"
# TrialWatch itself (core.actor 1): who writes what no person does, such as scheduled jobs, automatic
# checks and publishing, migrations and seeding the first admin
SYSTEM_ACTOR = "trialwatch"
SYSTEM_ACTOR_ID = 1


def connect(actor: str | None = None, actor_kind: str = "person") -> psycopg.Connection:
    """A connection whose writes are recorded as made by `actor` (core.actor, ADR-003): a person's
    name, or a process such as SYSTEM_ACTOR with actor_kind="process". Without a name, the person
    is TRIALWATCH_ACTOR or the login name of whoever runs the program."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    if actor is None:
        actor = default_actor()
    # Imported here, not at the top, so the package's modules can be imported in any order
    from trialwatch.db.core import act_as

    conn = psycopg.connect(url, autocommit=True, options=SESSION_OPTIONS)
    try:
        act_as(conn, actor, actor_kind)
    except BaseException:
        # the caller never gets the connection, so it is closed here
        conn.close()
        raise
    return conn


def connect_system() -> psycopg.Connection:
    """A connection whose writes are recorded as TrialWatch's own (SYSTEM_ACTOR). Also used for
    reading, so a page view never adds the server's login name as a person."""
    return connect(actor=SYSTEM_ACTOR, actor_kind="process")


def default_actor() -> str:
    """Who is running TrialWatch: TRIALWATCH_ACTOR if set, else the account the program runs under
    (from the operating system, not the USER variable, which a launcher may set to anything)."""
    name = os.environ.get("TRIALWATCH_ACTOR")
    if name:
        return name
    return pwd.getpwuid(os.getuid()).pw_name
