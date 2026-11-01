import os
from pathlib import Path

import psycopg
import pytest

from trialwatch.db import SESSION_OPTIONS


def _database_url() -> str | None:
    if url := os.environ.get("DATABASE_URL"):
        return url
    # Outside compose: build it from .env and the localhost port the db container publishes
    env_file = Path(__file__).parent.parent / ".env"
    if not env_file.exists():
        return None
    env = dict(
        line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line and not line.startswith("#")
    )
    return (
        f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
        f"@127.0.0.1:{env.get('POSTGRES_PORT', '5432')}/{env['POSTGRES_DB']}"
    )


@pytest.fixture
def conn():
    """Connection inside a transaction that is rolled back afterwards, so tests leave no rows behind.

    The store's own `conn.transaction()` blocks become savepoints inside it.
    """
    url = _database_url()
    if not url:
        pytest.skip("no database configured")
    try:
        connection = psycopg.connect(url, connect_timeout=3, options=SESSION_OPTIONS)
    except psycopg.OperationalError as exc:
        pytest.skip(f"database not reachable: {exc}")
    connection.execute("SELECT 1")  # opens the outer transaction
    yield connection
    connection.rollback()
    connection.close()
