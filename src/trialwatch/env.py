"""Settings from the git-ignored .env file, for running outside compose (`uv run trialwatch ...`).

Inside compose, the containers get DATABASE_URL and ANTHROPIC_API_KEY from compose.yaml. On the host,
the same values come from .env: the database is the compose db container on the localhost port it
publishes (POSTGRES_PORT). As with compose, values already set in the environment always win.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

ENV_FILE = Path(__file__).parent.parent.parent / ".env"


def _unquoted(text: str) -> str:
    """A value as compose reads it: without the quotes around it."""
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines of a .env file. Blank lines and # comments are skipped; an `export ` prefix and
    quotes around a value are dropped."""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ")
        key, value = line.split("=", 1)
        values[key.strip()] = _unquoted(value)
    return values


def database_url(values: Mapping[str, str]) -> str | None:
    """The URL of the compose database on this machine, from POSTGRES_USER, POSTGRES_PASSWORD,
    POSTGRES_DB and POSTGRES_PORT; None when one of the first three is missing."""
    for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        if not values.get(key):
            return None
    port = values.get("POSTGRES_PORT")
    if not port:
        port = "5432"
    # a password may hold characters that mean something in a URL (@, :, /)
    user = quote(values["POSTGRES_USER"], safe="")
    password = quote(values["POSTGRES_PASSWORD"], safe="")
    database = quote(values["POSTGRES_DB"], safe="")
    return f"postgresql://{user}:{password}@127.0.0.1:{port}/{database}"


def load_env_file(path: Path = ENV_FILE) -> None:
    """Fill in the environment variables missing here from .env, and DATABASE_URL from the POSTGRES_*
    values (the environment's own first)."""
    for key, value in read_env_file(path).items():
        # an empty value in the environment counts as missing (compose passes ANTHROPIC_API_KEY as "")
        if not os.environ.get(key) and value:
            os.environ[key] = value
    if os.environ.get("DATABASE_URL"):
        return
    url = database_url(os.environ)
    if url is not None:
        os.environ["DATABASE_URL"] = url
