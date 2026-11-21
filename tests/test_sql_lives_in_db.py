"""All SQL lives in trialwatch.db (one module per schema); the rest of the code calls its functions."""

import re
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "trialwatch"
# Running SQL, or SQL text itself: a statement keyword at the start of a string. COPY only in capitals:
# in lower case it is a plain word too (e.g. the warning about a file copy, "copy <uuid>")
SQL = re.compile(r"\.execute\(|\.executemany\(|\.copy\(|\.cursor\(|[\"'](?:\s*)(?:(?i:SELECT|INSERT|UPDATE|DELETE|WITH|CREATE)|COPY)\s")


def test_no_sql_outside_the_db_package():
    found = []
    for path in SRC.rglob("*.py"):
        if "db" in path.relative_to(SRC).parts[:1]:
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if SQL.search(line):
                found.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")
    assert not found, "SQL outside trialwatch/db/:\n" + "\n".join(found)


def test_the_check_finds_sql_text_and_sql_calls():
    for line in ('conn.execute("SELECT 1")', "query = 'select id from core.trial'", '" UPDATE core.file SET',
                 'text = "COPY core.page FROM STDIN"', "with cur.copy(statement) as copy:"):
        assert SQL.search(line), line
    for line in ('fetched.warnings[f"copy {uuid}"] = message', 'label = "selected trials"'):
        assert not SQL.search(line), line
