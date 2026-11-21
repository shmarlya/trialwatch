"""Settings from .env for running outside compose (trialwatch.env)."""

import os

from trialwatch import env

ENV_TEXT = """
# the compose database
export POSTGRES_USER=trial watch
POSTGRES_PASSWORD="p@ss:w/rd#1"
POSTGRES_DB='trialwatch'

TW_TEST_KEPT=from-file
TW_TEST_FILLED = from-file
TW_TEST_EMPTY=from-file
TW_TEST_EQUALS=a=b
not a setting
TW_TEST_NOTHING=
"""
# every variable load_env_file may set from ENV_TEXT, so each test leaves the environment as it was
SET_FROM_THE_FILE = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_PORT", "DATABASE_URL",
                     "TW_TEST_KEPT", "TW_TEST_FILLED", "TW_TEST_EMPTY", "TW_TEST_EQUALS", "TW_TEST_NOTHING")


def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(ENV_TEXT)
    return path


def clear(monkeypatch):
    for key in SET_FROM_THE_FILE:
        monkeypatch.delenv(key, raising=False)


def test_a_env_file_is_read_as_compose_reads_it(tmp_path):
    values = env.read_env_file(env_file(tmp_path))
    # comments, blank lines and lines without "=" are skipped; `export` and quotes are dropped
    assert values == {
        "POSTGRES_USER": "trial watch", "POSTGRES_PASSWORD": "p@ss:w/rd#1", "POSTGRES_DB": "trialwatch",
        "TW_TEST_KEPT": "from-file", "TW_TEST_FILLED": "from-file", "TW_TEST_EMPTY": "from-file",
        "TW_TEST_EQUALS": "a=b", "TW_TEST_NOTHING": "",
    }
    assert env.read_env_file(tmp_path / "missing.env") == {}


def test_the_database_url_is_built_with_its_parts_percent_encoded():
    values = {"POSTGRES_USER": "trial watch", "POSTGRES_PASSWORD": "p@ss:w/rd#1", "POSTGRES_DB": "trialwatch"}
    assert env.database_url(values) == "postgresql://trial%20watch:p%40ss%3Aw%2Frd%231@127.0.0.1:5432/trialwatch"
    values["POSTGRES_PORT"] = "5544"
    assert env.database_url(values).endswith("@127.0.0.1:5544/trialwatch")
    del values["POSTGRES_PASSWORD"]
    assert env.database_url(values) is None


def test_the_environment_wins_over_the_file(tmp_path, monkeypatch):
    clear(monkeypatch)
    monkeypatch.setenv("TW_TEST_KEPT", "from-environment")
    # compose passes an unset key as "": that counts as missing
    monkeypatch.setenv("TW_TEST_EMPTY", "")
    monkeypatch.setenv("POSTGRES_DB", "other")
    env.load_env_file(env_file(tmp_path))
    assert os.environ["TW_TEST_KEPT"] == "from-environment"
    assert os.environ["TW_TEST_FILLED"] == "from-file"
    assert os.environ["TW_TEST_EMPTY"] == "from-file"
    assert "TW_TEST_NOTHING" not in os.environ
    # the database URL from the POSTGRES_* values, the environment's own first
    assert os.environ["DATABASE_URL"] == "postgresql://trial%20watch:p%40ss%3Aw%2Frd%231@127.0.0.1:5432/other"


def test_a_database_url_already_set_is_kept(tmp_path, monkeypatch):
    clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://elsewhere/db")
    env.load_env_file(env_file(tmp_path))
    assert os.environ["DATABASE_URL"] == "postgresql://elsewhere/db"
