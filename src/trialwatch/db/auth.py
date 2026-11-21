"""Queries on the auth schema: the people who can sign in to the web interface, and their sessions
(see db/migrations/*_auth.sql and ADR-006). What a role may do is the chart in trialwatch.auth.actions,
not data. Each user is also a core.actor (a person with the same name), so the audit log records their
writes under their own name."""

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row

from trialwatch.db.core import actor_id


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    password_hash: str
    disabled_at: datetime | None
    created_at: datetime


USER_COLUMNS = "id, username, role, password_hash, disabled_at, created_at"


def add_user(conn: psycopg.Connection, username: str, password_hash: str, role: str) -> int:
    """A new user with their core.actor (made if new). Raises psycopg.errors.UniqueViolation when the
    name is taken."""
    # the writer stays whoever adds them (the session's actor); the new user only gets an actor
    user_actor = actor_id(conn, username, "person")
    row = conn.execute(
        "INSERT INTO auth.app_user (username, password_hash, role, actor_id) VALUES (%s, %s, %s, %s) RETURNING id",
        (username, password_hash, role, user_actor),
    ).fetchone()
    return row[0]


def user_by_name(conn: psycopg.Connection, username: str) -> User | None:
    with conn.cursor(row_factory=class_row(User)) as cur:
        cur.execute(f"SELECT {USER_COLUMNS} FROM auth.app_user WHERE username = %s", (username,))
        return cur.fetchone()


def users(conn: psycopg.Connection) -> list[User]:
    with conn.cursor(row_factory=class_row(User)) as cur:
        cur.execute(f"SELECT {USER_COLUMNS} FROM auth.app_user ORDER BY username")
        return cur.fetchall()


def active_admins(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT count(*) FROM auth.app_user WHERE role = 'admin' AND disabled_at IS NULL"
    ).fetchone()
    return row[0]


def set_role(conn: psycopg.Connection, username: str, role: str) -> bool:
    """Change a user's role; False when there is no such user."""
    cur = conn.execute("UPDATE auth.app_user SET role = %s WHERE username = %s", (role, username))
    return cur.rowcount == 1


def set_password_hash(conn: psycopg.Connection, username: str, password_hash: str) -> bool:
    cur = conn.execute("UPDATE auth.app_user SET password_hash = %s WHERE username = %s", (password_hash, username))
    return cur.rowcount == 1


def set_disabled(conn: psycopg.Connection, username: str, disabled: bool) -> bool:
    """Disable a user (they can't sign in) or enable them again; False when there is no such user."""
    if disabled:
        cur = conn.execute(
            "UPDATE auth.app_user SET disabled_at = coalesce(disabled_at, clock_timestamp()) WHERE username = %s",
            (username,),
        )
    else:
        cur = conn.execute("UPDATE auth.app_user SET disabled_at = NULL WHERE username = %s", (username,))
    return cur.rowcount == 1


# --- sessions ------------------------------------------------------------------------------------

def add_session(conn: psycopg.Connection, token_sha256: bytes, user_id: int, expires_at: datetime) -> None:
    conn.execute(
        "INSERT INTO auth.session (token_sha256, user_id, expires_at) VALUES (%s, %s, %s)",
        (token_sha256, user_id, expires_at),
    )


def session_user(conn: psycopg.Connection, token_sha256: bytes, idle_since: datetime) -> User | None:
    """The enabled user of a session that has not expired and was used after `idle_since`; marks it
    used now. None for any other session."""
    with conn.cursor(row_factory=class_row(User)) as cur:
        cur.execute(
            "UPDATE auth.session s SET last_seen_at = clock_timestamp() FROM auth.app_user u"
            " WHERE s.token_sha256 = %s AND u.id = s.user_id AND u.disabled_at IS NULL"
            " AND s.expires_at > clock_timestamp() AND s.last_seen_at > %s"
            " RETURNING u.id, u.username, u.role, u.password_hash, u.disabled_at, u.created_at",
            (token_sha256, idle_since),
        )
        return cur.fetchone()


def drop_session(conn: psycopg.Connection, token_sha256: bytes) -> None:
    conn.execute("DELETE FROM auth.session WHERE token_sha256 = %s", (token_sha256,))


def drop_sessions_of(conn: psycopg.Connection, username: str) -> None:
    """Sign a user out everywhere (after a change of role, password or a disable)."""
    conn.execute(
        "DELETE FROM auth.session WHERE user_id = (SELECT id FROM auth.app_user WHERE username = %s)", (username,)
    )


def drop_expired_sessions(conn: psycopg.Connection, idle_since: datetime) -> None:
    conn.execute(
        "DELETE FROM auth.session WHERE expires_at <= clock_timestamp() OR last_seen_at <= %s", (idle_since,)
    )
