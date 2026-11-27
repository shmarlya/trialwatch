"""Every setting TrialWatch reads from its environment, in one place.

Inside compose, the containers get their settings from compose.yaml. On the host (`uv run trialwatch
...`), the same values come from the git-ignored .env file: the database is the compose db container
on the localhost port it publishes (POSTGRES_PORT). Values already set in the environment always win.

The settings (each read by a function below):
- DATABASE_URL, or POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB / POSTGRES_PORT to build it
- ANTHROPIC_API_KEY: read by Anthropic's SDK itself (trialwatch.llm); empty: no model calls
- TRIALWATCH_HOSTS: other names the web interface answers to, comma-separated
- TRIALWATCH_ADMIN_EMAIL / TRIALWATCH_ADMIN_PASSWORD: the first admin (trialwatch.auth.accounts)
- TRIALWATCH_ACTOR: who a command's writes are recorded as (default: the operating system account)
- TRIALWATCH_GOLD_DIR: the gold datasets (default: the repository's data/gold/)
- TRIALWATCH_COUNTRIES: whose places `trialwatch geo load` loads, ISO codes, comma-separated (default:
  the EU/EEA, where CTIS trials have sites)
- TRIALWATCH_PDF_READERS / TRIALWATCH_PDF_MEMORY_MB: how many PDFs the worker reads at once, and the
  memory their child processes may take together (trialwatch.trials.pages)
- TRIALWATCH_OCR_MAX_PAGES / TRIALWATCH_OCR_LANGUAGES / TRIALWATCH_OCR_MINUTES_PER_REFRESH: which files
  get OCR for their picture pages, in which languages, and how long a refresh spends on it
  (trialwatch.trials.ocr)
- TRIALWATCH_STUCK_MINUTES: how long a job may go without moving before the worker restarts
  (trialwatch.jobs.worker)
"""

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

REPOSITORY = Path(__file__).parent.parent.parent
ENV_FILE = REPOSITORY / ".env"
ADMIN_EMAIL = "TRIALWATCH_ADMIN_EMAIL"
ADMIN_PASSWORD = "TRIALWATCH_ADMIN_PASSWORD"


# --- reading .env ------------------------------------------------------------------------------


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


def compose_database_url(values: Mapping[str, str]) -> str | None:
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
    url = compose_database_url(os.environ)
    if url is not None:
        os.environ["DATABASE_URL"] = url


# --- the settings ------------------------------------------------------------------------------


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def extra_hosts() -> list[str]:
    """The names the web interface answers to besides this machine's own (TRIALWATCH_HOSTS)."""
    hosts = []
    for host in os.environ.get("TRIALWATCH_HOSTS", "").split(","):
        if host.strip():
            hosts.append(host.strip())
    return hosts


def admin_seed() -> dict[str, str]:
    """The first admin's email and password as set (each "" when not), by their variable names."""
    return {ADMIN_EMAIL: os.environ.get(ADMIN_EMAIL, ""), ADMIN_PASSWORD: os.environ.get(ADMIN_PASSWORD, "")}


def actor_name() -> str | None:
    """Who a command's writes are recorded as, when TRIALWATCH_ACTOR names someone."""
    name = os.environ.get("TRIALWATCH_ACTOR")
    if name:
        return name
    return None


def gazetteer_countries() -> list[str]:
    """The countries whose places patient search knows (TRIALWATCH_COUNTRIES), by ISO code."""
    configured = []
    for code in os.environ.get("TRIALWATCH_COUNTRIES", "").split(","):
        if code.strip():
            configured.append(code.strip().upper())
    if configured:
        return configured
    # imported here: config is read before anything else, and needs no geo code otherwise
    from trialwatch.geo import geonames

    return geonames.eea_countries()


def pdf_readers() -> int:
    """How many PDFs the worker reads at once, each in a child process (TRIALWATCH_PDF_READERS,
    default 2: one per CPU of the worker's limit)."""
    return _positive_int("TRIALWATCH_PDF_READERS", 2)


def pdf_memory_mb() -> int:
    """The memory, in MB, the children reading PDFs may take together (TRIALWATCH_PDF_MEMORY_MB,
    default 1400 of the worker's 2 GB): a file starts only when its estimate fits beside those being
    read. A file larger than the whole budget is still read, alone."""
    return _positive_int("TRIALWATCH_PDF_MEMORY_MB", 1400)


def ocr_max_pages() -> int:
    """Files with more pages than this get no OCR (TRIALWATCH_OCR_MAX_PAGES, default 1000): a study report
    of thousands of pages with its listings and forms as pictures would take hours, and a check never
    reads such a file whole anyway (checks/configs: max_text_chars)."""
    return _positive_int("TRIALWATCH_OCR_MAX_PAGES", 1000)


def ocr_minutes_per_refresh() -> int:
    """How long the refresh's OCR step may take before it leaves the other files to the next refresh
    (TRIALWATCH_OCR_MINUTES_PER_REFRESH, default 240: the backlog of files read before OCR, about 1,300
    files, took about that on two readers)."""
    return _positive_int("TRIALWATCH_OCR_MINUTES_PER_REFRESH", 240)


def ocr_languages() -> str:
    """Tesseract's languages for OCR, joined by + (TRIALWATCH_OCR_LANGUAGES, default "eng"; each must be
    installed, see the Dockerfile). Each one more makes a page slower to read."""
    configured = os.environ.get("TRIALWATCH_OCR_LANGUAGES", "").strip()
    if not configured:
        return "eng"
    return configured


def stuck_minutes() -> int:
    """How long a job may go without moving before the worker stops itself to be started afresh
    (TRIALWATCH_STUCK_MINUTES, default 45: longer than a PDF's 20 minutes and a download's deadline)."""
    return _positive_int("TRIALWATCH_STUCK_MINUTES", 45)


def _positive_int(name: str, default: int) -> int:
    configured = os.environ.get(name, "").strip()
    if not configured:
        return default
    try:
        value = int(configured)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, not {configured!r}") from None
    if value < 1:
        raise ValueError(f"{name} must be 1 or more, not {configured}")
    return value


def gold_dir() -> Path:
    """Where the gold datasets are: TRIALWATCH_GOLD_DIR (tests, a container), else the repository's."""
    configured = os.environ.get("TRIALWATCH_GOLD_DIR")
    if configured:
        return Path(configured)
    return REPOSITORY / "data" / "gold"
