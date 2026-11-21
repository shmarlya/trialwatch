"""Gold datasets (ADR-003): hand-checked answers we compare our checks against, kept as versioned
files in git, never in the working database.

Every dataset version is a folder data/gold/<name>/<version>/ with two files:
- dataset.json: what the dataset is (title, citation), which requirement set its labels answer,
  its parts and labels, which documents its reviewers read (the evidence scope), and the input
  files it was built from with their sha256;
- items.jsonl: one item per line, one trial each: its identifiers (any scheme), the expected parts
  and label, each reviewer's answers and comments, and the dataset's own details as plain fields.

A released version never changes: data/gold/versions.lock holds the sha256 of both files of every
version, and a test fails when a file no longer matches. A corrected or larger dataset is a new
version. Each source of gold labels has a converter that builds a version from its own files
(gold/bruckner.py for Bruckner et al.); everything else here works on any dataset.
"""

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from trialwatch.db.core import Identifier, normalised

DATASET_FILE = "dataset.json"
ITEMS_FILE = "items.jsonl"
LOCK_FILE = "versions.lock"
# The repository's data/gold/; TRIALWATCH_GOLD_DIR points elsewhere (tests, another checkout)
DEFAULT_DIR = Path(__file__).parent.parent.parent.parent / "data" / "gold"


def gold_dir() -> Path:
    configured = os.environ.get("TRIALWATCH_GOLD_DIR")
    if configured:
        return Path(configured)
    return DEFAULT_DIR


@dataclass(frozen=True)
class Review:
    """One reviewer's answers for an item: part -> True / False / None (not answered)."""

    reviewer: str
    parts: dict[str, bool | None]
    asked_to_check: bool | None
    comment: str | None


@dataclass(frozen=True)
class Item:
    key: str
    identifiers: list[Identifier]
    title: str | None
    # What our check should conclude: part -> True / False / None (the reviewers didn't settle it)
    expected_parts: dict[str, bool | None]
    # One of the dataset's labels, or None when the dataset gives none for this item
    expected_label: str | None
    reviews: list[Review]
    reviewers_agreed: bool | None
    comment: str | None
    # The documents the reviewers read for this item: kind, and when it was posted if known
    evidence: list[dict]
    # The source's own fields, shown as they are (e.g. a verdict, a deadline)
    details: dict


@dataclass(frozen=True)
class Dataset:
    name: str
    version: str
    title: str
    description: str
    citation: str | None
    url: str | None
    # The requirement set whose parts and label the items' expected answers use
    requirements: str
    parts: list[str]
    labels: list[str]
    evidence: dict
    built_from: list[dict]
    built_with: str
    items: list[Item] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.name}/{self.version}"


# --- reading -------------------------------------------------------------------------------------

def dataset_refs(root: Path | None = None) -> list[str]:
    """Every dataset version on disk, as "<name>/<version>", sorted (newest version last)."""
    if root is None:
        root = gold_dir()
    refs = []
    if not root.is_dir():
        return refs
    for dataset_file in root.glob(f"*/*/{DATASET_FILE}"):
        version_dir = dataset_file.parent
        refs.append(f"{version_dir.parent.name}/{version_dir.name}")
    return sorted(refs)


def _review(data: dict) -> Review:
    return Review(data["reviewer"], data["parts"], data.get("asked_to_check"), data.get("comment"))


def item_from_data(data: dict) -> Item:
    identifiers = []
    for identifier in data["identifiers"]:
        identifiers.append(Identifier(identifier["scheme"], identifier["value"]))
    reviews = []
    for review in data.get("reviews", []):
        reviews.append(_review(review))
    expected = data["expected"]
    return Item(
        key=data["key"], identifiers=identifiers, title=data.get("title"), expected_parts=expected["parts"],
        expected_label=expected.get("label"), reviews=reviews, reviewers_agreed=data.get("reviewers_agreed"),
        comment=data.get("comment"), evidence=data.get("evidence", []), details=data.get("details", {}),
    )


def item_data(item: Item) -> dict:
    """The item as one line of items.jsonl; keys in a fixed order so a rebuild gives the same bytes."""
    identifiers = []
    for identifier in item.identifiers:
        identifiers.append({"scheme": identifier.scheme, "value": identifier.value})
    reviews = []
    for review in item.reviews:
        reviews.append({"reviewer": review.reviewer, "parts": review.parts,
                        "asked_to_check": review.asked_to_check, "comment": review.comment})
    return {
        "key": item.key, "identifiers": identifiers, "title": item.title,
        "expected": {"parts": item.expected_parts, "label": item.expected_label},
        "reviews": reviews, "reviewers_agreed": item.reviewers_agreed, "comment": item.comment,
        "evidence": item.evidence, "details": item.details,
    }


def load(ref: str, root: Path | None = None) -> Dataset:
    """One dataset version with its items; FileNotFoundError if there is none."""
    if root is None:
        root = gold_dir()
    version_dir = _version_dir(root, ref)
    meta = json.loads((version_dir / DATASET_FILE).read_text())
    items = []
    with (version_dir / ITEMS_FILE).open() as lines:
        for line in lines:
            if line.strip():
                items.append(item_from_data(json.loads(line)))
    return Dataset(
        name=meta["name"], version=meta["version"], title=meta["title"], description=meta["description"],
        citation=meta.get("citation"), url=meta.get("url"), requirements=meta["requirements"],
        parts=meta["parts"], labels=meta["labels"], evidence=meta["evidence"], built_from=meta["built_from"],
        built_with=meta["built_with"], items=items,
    )


def _version_dir(root: Path, ref: str) -> Path:
    pieces = ref.split("/")
    # A ref is exactly "<name>/<version>": nothing else may reach outside the gold folder
    if len(pieces) != 2 or not _plain_name(pieces[0]) or not _plain_name(pieces[1]):
        raise FileNotFoundError(f"no gold dataset {ref!r}")
    version_dir = root / pieces[0] / pieces[1]
    if not (version_dir / DATASET_FILE).is_file():
        raise FileNotFoundError(f"no gold dataset {ref!r}")
    return version_dir


def _plain_name(text: str) -> bool:
    if not text or text.startswith("."):
        return False
    for character in text:
        if not (character.isalnum() or character in "-_."):
            return False
    return True


def find_item(dataset: Dataset, key: str) -> Item | None:
    for item in dataset.items:
        if item.key == key:
            return item
    return None


def items_for_identifiers(identifiers: list[Identifier], root: Path | None = None) -> list[tuple[Dataset, Item]]:
    """Every (dataset version, item) naming one of these identifiers, in all datasets on disk."""
    wanted = set()
    for identifier in identifiers:
        wanted.add(normalised(identifier))
    found = []
    for ref in dataset_refs(root):
        dataset = load(ref, root)
        for item in dataset.items:
            if _names_one_of(item, wanted):
                found.append((dataset, item))
    return found


def _names_one_of(item: Item, wanted: set[Identifier]) -> bool:
    for identifier in item.identifiers:
        if normalised(identifier) in wanted:
            return True
    return False


# --- searching -----------------------------------------------------------------------------------

def _searchable_text(item: Item) -> str:
    pieces = [item.key]
    for identifier in item.identifiers:
        pieces.append(identifier.value)
    if item.title:
        pieces.append(item.title)
    if item.comment:
        pieces.append(item.comment)
    for review in item.reviews:
        if review.comment:
            pieces.append(review.comment)
    for value in item.details.values():
        if value is not None:
            pieces.append(str(value))
    return " ".join(pieces).lower()


def matches_text(item: Item, text: str) -> bool:
    """Every word of the search is somewhere in the item: identifiers, title, comments, details."""
    searchable = _searchable_text(item)
    for word in text.lower().split():
        if word not in searchable:
            return False
    return True


def reviewers_disagreed(item: Item) -> bool:
    """The reviewers gave different answers to some part (also when the dataset says they agreed in the end)."""
    if item.reviewers_agreed is False:
        return True
    seen: dict[str, bool] = {}
    for review in item.reviews:
        for part, answer in review.parts.items():
            if answer is None:
                continue
            if part in seen and seen[part] != answer:
                return True
            seen[part] = answer
    return False


# --- comparing with a check ----------------------------------------------------------------------

AGREE = "agree"
DISAGREE = "disagree"
# Ours is unsettled (None, or "undecided": left to a person): not counted either way
OURS_OPEN = "ours open"
# The dataset has no answer: nothing to compare with
NO_GOLD = "no gold"


@dataclass(frozen=True)
class Comparison:
    parts: dict[str, str]
    label: str
    # Ours "not full" where the gold label is "full": the costliest error (ADR-001)
    false_accusation: bool


def compare(item: Item, parts: list[str], our_parts: dict, our_label: str | None) -> Comparison:
    """Our judgement (one check's parts and label) next to the item's expected answers."""
    outcome_by_part = {}
    for part in parts:
        outcome_by_part[part] = _outcome(item.expected_parts.get(part), our_parts.get(part), None)
    label_outcome = _outcome(item.expected_label, our_label, "undecided")
    false_accusation = our_label == "not full" and item.expected_label == "full"
    return Comparison(outcome_by_part, label_outcome, false_accusation)


def _outcome(expected: object, ours: object, open_value: str | None) -> str:
    if expected is None:
        return NO_GOLD
    if ours is None or ours == open_value:
        return OURS_OPEN
    if ours == expected:
        return AGREE
    return DISAGREE


# --- writing and locking -------------------------------------------------------------------------

def _json_line(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=False)


def files_of(meta: dict, items: list[Item]) -> dict[str, bytes]:
    """The bytes of dataset.json and items.jsonl for a version, exactly as they are written."""
    dataset_text = json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
    lines = []
    for item in items:
        lines.append(_json_line(item_data(item)) + "\n")
    return {DATASET_FILE: dataset_text.encode(), ITEMS_FILE: "".join(lines).encode()}


def file_hashes(files: dict[str, bytes]) -> dict[str, str]:
    hashes = {}
    for name, content in files.items():
        hashes[name] = hashlib.sha256(content).hexdigest()
    return hashes


def read_lock(root: Path) -> dict[str, dict[str, str]]:
    lock_path = root / LOCK_FILE
    if not lock_path.exists():
        return {}
    return json.loads(lock_path.read_text())


def _write_lock(root: Path, lock: dict[str, dict[str, str]]) -> None:
    ordered = {}
    for ref in sorted(lock):
        ordered[ref] = lock[ref]
    (root / LOCK_FILE).write_text(json.dumps(ordered, indent=2) + "\n")


class VersionIsFixed(Exception):
    """A released version would change: build a new version instead."""


def write_version(root: Path, meta: dict, items: list[Item]) -> tuple[str, bool]:
    """Write a version's two files and lock them. Returns (ref, written): a version already on disk
    with the same bytes is left alone; one with other bytes raises VersionIsFixed."""
    ref = f"{meta['name']}/{meta['version']}"
    if not _plain_name(meta["name"]) or not _plain_name(meta["version"]):
        raise ValueError(f"not a plain dataset name and version: {ref!r}")
    files = files_of(meta, items)
    hashes = file_hashes(files)
    lock = read_lock(root)
    if ref in lock:
        if lock[ref] == hashes:
            return ref, False
        raise VersionIsFixed(f"{ref} is already released with other content; build it as a new version")
    version_dir = root / meta["name"] / meta["version"]
    version_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (version_dir / name).write_bytes(content)
    lock[ref] = hashes
    _write_lock(root, lock)
    return ref, True


def lock_problems(root: Path | None = None) -> list[str]:
    """Versions whose files don't match the lock, or that are on disk without being locked (or the reverse)."""
    if root is None:
        root = gold_dir()
    lock = read_lock(root)
    problems = []
    on_disk = dataset_refs(root)
    for ref in on_disk:
        if ref not in lock:
            problems.append(f"{ref}: not in {LOCK_FILE}")
            continue
        version_dir = root / ref
        for name, expected in lock[ref].items():
            path = version_dir / name
            if not path.is_file():
                problems.append(f"{ref}: {name} is missing")
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                problems.append(f"{ref}: {name} changed since it was released")
    for ref in lock:
        if ref not in on_disk:
            problems.append(f"{ref}: in {LOCK_FILE} but not on disk")
    return problems
