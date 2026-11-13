"""Docs must describe the code as it is.

The "as built" docs are checked strictly: every file, table and CLI command they name must exist, and
every table, command and compose service must appear in its diagram. The plan docs (decision map,
roadmap, ADRs) name files that are still to be written, so only their links to other diagrams are
checked. Text in a box starting "Not built yet" is a plan too. Every diagram must also have an
up-to-date image in docs/img/ (rendered by tools/diagrams/render.mjs). Runs with the suite and from
the Stop hook in .claude/settings.json.
"""

import hashlib
import json
import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path

from typer.main import get_command

from trialwatch.cli import app

ROOT = Path(__file__).parent.parent
AS_BUILT = [
    "docs/system-design.excalidraw",
    "docs/db.excalidraw",
    "docs/external-apis.excalidraw",
    "README.md",
    "GLOSSARY.md",
    "data/bruckner/README.md",
]
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache"}
# A file name with one of these extensions, optionally with directories: ctis/store.py, db/migrations/*.sql
FILE_REF = re.compile(r"(?<![\w.~/*-])((?:[\w.-]+/)*[\w*-][\w.*-]*\.(?:md|py|sql|ya?ml|toml|sh|excalidraw|json))(?![\w/])")
TABLE_REF = re.compile(r"\b((?:ctis|ref|gold|public)\.\w+)")
NOTATION = {"ref.x"}  # external-apis: "ref.x = code decoded by that table"
PLACEHOLDER = ("…", "<", "{", "NNN", "00n")
# First line of every image render.mjs writes: the sha256 of the .excalidraw it was rendered from
IMAGE_STAMP = re.compile(r"<!-- Rendered from (\S+) sha256=([0-9a-f]{64}) ")


def text_of(path: Path) -> str:
    if path.suffix != ".excalidraw":
        return path.read_text()
    elements = json.loads(path.read_text())["elements"]
    return "\n".join(
        e["text"] for e in elements if e["type"] == "text" and not e["text"].startswith("Not built yet")
    )


def repo_files() -> list[str]:
    """Files git would publish (tracked, or new and not ignored): a doc must not point at a file that
    exists only on this machine, like the git-ignored db/dev/ seed."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split("\0")
    return [f for f in listed if f and (ROOT / f).is_file() and not SKIP_DIRS.intersection(Path(f).parts)]


def missing_files(text: str, files: list[str]) -> set[str]:
    """File references in `text` that match no file in the repo (by path suffix, globs allowed)."""
    missing = set()
    for ref in FILE_REF.findall(text):
        if any(mark in ref for mark in PLACEHOLDER):
            continue
        if not any(f == ref or fnmatch(f, ref) or fnmatch(f, f"*/{ref}") for f in files):
            missing.add(ref)
    return missing


def schema_objects() -> set[str]:
    return set(re.findall(r"CREATE (?:TABLE|VIEW) (\w+\.\w+)", (ROOT / "db/schema.sql").read_text()))


def cli_commands() -> set[str]:
    """Every command as typed after `trialwatch`, e.g. {"version", "ctis fetch", "ctis seed"}."""
    found = set()

    def walk(cmd, prefix):
        for name, sub in getattr(cmd, "commands", {}).items():
            path = f"{prefix} {name}".strip()
            if getattr(sub, "commands", None):
                walk(sub, path)
            else:
                found.add(path)

    walk(get_command(app), "")
    return found


def compose_services() -> set[str]:
    compose = "\n" + (ROOT / "compose.yaml").read_text()
    # top-level keys start at column 0; service-level `volumes:` is indented
    services = compose.split("\nservices:\n", 1)[1].split("\nvolumes:", 1)[0]
    return set(re.findall(r"^  ([\w-]+):\s*$", services, re.MULTILINE))


def test_checker_catches_a_missing_file():
    assert missing_files("Not here (see ROADMAP.md), but ctis/store.py and db/migrations/*.sql are", repo_files()) == {"ROADMAP.md"}


def test_as_built_docs_name_only_files_that_exist():
    files = repo_files()
    problems = {doc: sorted(missing_files(text_of(ROOT / doc), files)) for doc in AS_BUILT}
    assert not {d: m for d, m in problems.items() if m}, "docs name files that don't exist"


def test_as_built_docs_name_only_tables_that_exist():
    objects = schema_objects()
    problems = {}
    for doc in AS_BUILT:
        unknown = set(TABLE_REF.findall(text_of(ROOT / doc))) - objects - NOTATION
        if unknown:
            problems[doc] = sorted(unknown)
    assert not problems, "docs name tables that aren't in db/schema.sql"


def test_db_diagram_shows_every_table_and_the_latest_migration():
    text = text_of(ROOT / "docs/db.excalidraw")
    assert not sorted(o for o in schema_objects() if o not in text), "tables missing from db.excalidraw"
    latest = max(p.name[:14] for p in (ROOT / "db/migrations").glob("*.sql"))
    assert f"as of migration {latest}" in text, f"db.excalidraw should say 'as of migration {latest}'"


def test_system_design_shows_every_command_and_service_and_no_others():
    text = text_of(ROOT / "docs/system-design.excalidraw")
    commands = cli_commands()
    groups = "|".join(sorted({c.split()[0] for c in commands if " " in c}))  # ctis, gold, ...
    mentioned = {f"{g} {c}" for g, c in re.findall(rf"\b({groups}) ([a-z][\w-]*)", text)}
    assert mentioned <= commands, f"system-design names commands that don't exist: {sorted(mentioned - commands)}"
    assert not sorted(c for c in commands if c not in text), "commands missing from system-design.excalidraw"
    assert not sorted(s for s in compose_services() if f"{s} — " not in text), "compose services missing from system-design.excalidraw"


def test_every_diagram_has_an_up_to_date_image():
    docs, img = ROOT / "docs", ROOT / "docs/img"
    diagrams = {p.relative_to(docs).with_suffix(".svg"): p for p in docs.rglob("*.excalidraw")}
    stale = []
    for name, diagram in sorted(diagrams.items()):
        image = img / name
        stamp = IMAGE_STAMP.match(image.read_text()) if image.is_file() else None
        if not stamp or stamp[2] != hashlib.sha256(diagram.read_bytes()).hexdigest():
            stale.append(diagram.relative_to(ROOT).as_posix())
    orphans = sorted(p.relative_to(ROOT).as_posix() for p in img.rglob("*.svg") if p.relative_to(img) not in diagrams)
    assert not stale, f"images missing or stale, run `node tools/diagrams/render.mjs`: {stale}"
    assert not orphans, f"images whose diagram is gone, run `node tools/diagrams/render.mjs`: {orphans}"


def test_every_diagram_is_in_the_site_nav():
    """tools/site_hooks.py makes a page for each diagram; mkdocs.yml must list it (nav or not_in_nav)."""
    config = (ROOT / "mkdocs.yml").read_text()
    docs = ROOT / "docs"
    pages = (p.relative_to(docs).with_suffix(".md").as_posix() for p in docs.rglob("*.excalidraw"))
    assert not sorted(p for p in pages if not re.search(rf"(?<![\w-]){re.escape(p)}\b", config)), "diagrams missing from mkdocs.yml nav"


def test_links_between_diagrams_resolve():
    diagrams = {p.name for p in (ROOT / "docs").rglob("*.excalidraw")}
    docs = [p for p in (ROOT / "docs").rglob("*.excalidraw")] + [ROOT / "README.md", ROOT / "GLOSSARY.md"]
    problems = {}
    for doc in docs:
        links = {m for m in re.findall(r"[\w-]+\.excalidraw", text_of(doc)) if not any(x in m for x in PLACEHOLDER)}
        if links - diagrams:
            problems[doc.relative_to(ROOT).as_posix()] = sorted(links - diagrams)
    assert not problems, "docs link to diagrams that don't exist"
