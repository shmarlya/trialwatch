"""Loading MeSH into the vocab schema (ADR-008): one transaction, so a failed load leaves the earlier data."""

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import psycopg

from trialwatch import reference
from trialwatch.db import reference as reference_db
from trialwatch.db import vocab as vocab_db
from trialwatch.vocab import mesh

Progress = Callable[[str], None] | None


@dataclass
class LoadSummary:
    run_id: int
    version: str
    descriptors: int
    terms: int


def _version_of(path: Path, fallback: str) -> str:
    """The MeSH year in a file name like desc2026.gz."""
    digits = ""
    for character in path.name:
        if character.isdigit():
            digits += character
    if len(digits) == 4:
        return digits
    return fallback


def load_mesh(conn: psycopg.Connection, year: int | None, from_file: Path | None, progress: Progress = None) -> LoadSummary:
    """Replace the MeSH descriptors with a year's file: `from_file`, or downloaded from NLM (`year`, or
    this year's, else last year's when this year's is not out yet)."""
    with tempfile.TemporaryDirectory(prefix="trialwatch-mesh-") as work:
        if from_file is not None:
            path = from_file
            origin = str(from_file)
            version = _version_of(from_file, "unknown")
        else:
            years = []
            if year is not None:
                years.append(year)
            else:
                years.append(date.today().year)
                years.append(date.today().year - 1)
            path = None
            origin = ""
            version = ""
            for candidate in years:
                path = Path(work) / f"desc{candidate}.gz"
                try:
                    origin = mesh.download(candidate, path)
                except FileNotFoundError:
                    path = None
                    continue
                version = str(candidate)
                break
            if path is None:
                tried = []
                for candidate in years:
                    tried.append(str(candidate))
                raise ValueError(f"no MeSH descriptor file for {', '.join(tried)} at {mesh.MESH_FILES}")
        if progress is not None:
            progress(f"reading {origin}")
        with conn.transaction():
            run_id = reference_db.start_load(conn, "mesh", mesh.LICENSE, {})
            vocab_db.clear_mesh(conn)
            descriptors = []
            terms = []
            for descriptor in mesh.read_descriptors(path):
                descriptors.append((descriptor.ui, descriptor.name, list(descriptor.tree_numbers)))
                for term in descriptor.terms:
                    terms.append((descriptor.ui, term))
            stored_terms = vocab_db.add_descriptors(conn, descriptors, terms)
            files = [{"name": path.name, "from": origin, "sha256": reference.sha256_of(path)}]
            reference_db.finish_load(conn, run_id, files, {"descriptors": len(descriptors), "terms": stored_terms},
                                     version)
    return LoadSummary(run_id, version, len(descriptors), stored_terms)
