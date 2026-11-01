import os

import psycopg

# Sessions run in UTC so timestamps come back as UTC-aware datetimes, whatever the server default
SESSION_OPTIONS = "-c TimeZone=UTC"


def connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(url, autocommit=True, options=SESSION_OPTIONS)
