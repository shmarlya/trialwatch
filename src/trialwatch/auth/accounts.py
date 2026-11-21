"""Email addresses as sign-in names, and the admin seeded from the environment (ADR-006).

TRIALWATCH_ADMIN_EMAIL and TRIALWATCH_ADMIN_PASSWORD (in .env, git-ignored) name an admin that
`trialwatch web` creates when it starts, if no user has that email yet. An existing user is never
changed: a password set later in .env does nothing; change it with `trialwatch user password` or on
/admin/users.
"""

import logging
import os
import re

import psycopg

from trialwatch.auth import actions, passwords
from trialwatch.db import auth as auth_db

ADMIN_EMAIL = "TRIALWATCH_ADMIN_EMAIL"
ADMIN_PASSWORD = "TRIALWATCH_ADMIN_PASSWORD"
# The same rule as the auth.app_user check: something@something.something, no spaces or slashes
EMAIL = re.compile(r"[^@\s/]+@[^@\s/]+\.[^@\s/]+")
LONGEST_EMAIL = 254
# Who the audit log names for the seeded admin's row
SEED_ACTOR = "admin-seed"

log = logging.getLogger("trialwatch.auth")


def normalised_email(text: str) -> str:
    """An email as it is stored and compared: without surrounding spaces, lower case."""
    return text.strip().lower()


def email_problem(email: str) -> str | None:
    if len(email) > LONGEST_EMAIL or EMAIL.fullmatch(email) is None:
        return f"not an email address: {email!r}"
    return None


class SeedProblem(Exception):
    """The seed settings in the environment are incomplete or invalid."""


def seed_settings(environment: dict) -> tuple[str, str] | None:
    """The admin to seed (email, password) from the environment, or None when neither is set.
    Raises SeedProblem when only one is set or a value is not acceptable."""
    email = normalised_email(environment.get(ADMIN_EMAIL, ""))
    password = environment.get(ADMIN_PASSWORD, "")
    if not email and not password:
        return None
    if not email or not password:
        raise SeedProblem(f"set both {ADMIN_EMAIL} and {ADMIN_PASSWORD}, or neither")
    problem = email_problem(email)
    if problem is None:
        problem = passwords.password_problem(password)
    if problem is not None:
        raise SeedProblem(f"{ADMIN_EMAIL} / {ADMIN_PASSWORD}: {problem}")
    return email, password


def seed_admin(conn: psycopg.Connection, environment: dict) -> str | None:
    """Add the admin the environment names unless a user with that email exists; returns the
    email when one was created. The connection should write as SEED_ACTOR."""
    settings = seed_settings(environment)
    if settings is None:
        return None
    email, password = settings
    if auth_db.user_by_email(conn, email) is not None:
        return None
    auth_db.add_user(conn, email, passwords.hashed(password), actions.ADMIN)
    log.warning("added admin %s from %s", email, ADMIN_EMAIL)
    return email


def seed_from_environment(connect) -> str | None:
    """seed_admin with this process's environment, on a connection from `connect` (trialwatch.db.connect)."""
    environment = {}
    for key in (ADMIN_EMAIL, ADMIN_PASSWORD):
        environment[key] = os.environ.get(key, "")
    with connect(actor=SEED_ACTOR, actor_kind="process") as conn:
        return seed_admin(conn, environment)
