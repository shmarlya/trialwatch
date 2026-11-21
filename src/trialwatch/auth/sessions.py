"""Signing in and out. A session is a random token in an HttpOnly cookie; the database keeps only its
SHA-256 (auth.session), so reading the table gives nobody a way in. A session ends after IDLE without
use, after LIFETIME in any case, at sign-out, and when the user's role or password changes or they
are disabled."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import psycopg

from trialwatch.auth import passwords
from trialwatch.db import auth as auth_db

COOKIE = "tw_session"
IDLE = timedelta(hours=12)
LIFETIME = timedelta(days=7)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def now() -> datetime:
    return datetime.now(UTC)


def sign_in(conn: psycopg.Connection, username: str, password: str) -> str | None:
    """A new session's token when the name and password are right and the user is enabled, else None.
    The answer never says which of them was wrong."""
    user = auth_db.user_by_name(conn, username.strip().lower())
    if user is None:
        # the same work as for a real user, so the time taken doesn't tell whether the name exists
        passwords.matches(passwords.UNKNOWN_USER_HASH, password)
        return None
    if not passwords.matches(user.password_hash, password):
        return None
    if user.disabled_at is not None:
        return None
    if passwords.needs_rehash(user.password_hash):
        auth_db.set_password_hash(conn, user.username, passwords.hashed(password))
    auth_db.drop_expired_sessions(conn, now() - IDLE)
    token = secrets.token_urlsafe(32)
    auth_db.add_session(conn, token_hash(token), user.id, now() + LIFETIME)
    return token


def user_of(conn: psycopg.Connection, token: str | None) -> auth_db.User | None:
    """The signed-in user of a session cookie, or None (no cookie, unknown, expired, user disabled)."""
    if not token:
        return None
    return auth_db.session_user(conn, token_hash(token), now() - IDLE)


def sign_out(conn: psycopg.Connection, token: str | None) -> None:
    if token:
        auth_db.drop_session(conn, token_hash(token))
