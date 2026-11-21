"""Passwords are kept only as argon2id hashes (auth.app_user.password_hash)."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

HASHER = PasswordHasher()
# Shorter passwords are refused when a user is added or a password is set
MINIMUM_LENGTH = 12
# Checked against when there is no such user, so a wrong name takes as long as a wrong password
UNKNOWN_USER_HASH = HASHER.hash("no user has this password")


def hashed(password: str) -> str:
    problem = password_problem(password)
    if problem is not None:
        raise ValueError(problem)
    return HASHER.hash(password)


def password_problem(password: str) -> str | None:
    if len(password) < MINIMUM_LENGTH:
        return f"a password needs at least {MINIMUM_LENGTH} characters"
    return None


def matches(password_hash: str, password: str) -> bool:
    try:
        return HASHER.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Whether the hash was made with older settings than HASHER's (then it is replaced at sign-in)."""
    return HASHER.check_needs_rehash(password_hash)
