"""MeSH (Medical Subject Headings), the US National Library of Medicine's vocabulary, for patient search
(ADR-008): the descriptors of diseases (tree C) and mental disorders (F03), each with its entry terms
(synonyms, many in lay words) and tree numbers (its place in the hierarchy).

Read from NLM's descriptor file (https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/,
desc<year>.gz), streamed: the file is about 300 MB of XML. Free to use under NLM's terms
(https://www.nlm.nih.gov/databases/download/terms_and_conditions_mesh.html), credited on the site.
"""

import gzip
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from trialwatch import reference


MESH_FILES = "https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh"
LICENSE = "MeSH, U.S. National Library of Medicine (https://www.nlm.nih.gov/mesh/)"
# the branches of the tree a trial's condition is in: diseases, and mental disorders
BRANCHES = ("C", "F03")


def url_of(year: int) -> str:
    return f"{MESH_FILES}/desc{year}.gz"


def download(year: int, path: Path) -> str:
    """Download a year's descriptor file to `path`; returns the address it came from. NLM answers a file
    it doesn't have (next year's, before it is out) with a redirect to an error page: FileNotFoundError."""
    url = url_of(year)
    if not reference.download(url, path, timeout=120.0, follow_redirects=False):
        raise FileNotFoundError(f"{url}: no such file")
    return url


@dataclass(frozen=True)
class Descriptor:
    ui: str
    name: str
    tree_numbers: tuple[str, ...]
    # its name and every entry term, each once
    terms: tuple[str, ...]


def _in_branches(tree_numbers: list[str]) -> bool:
    """Whether a descriptor sits in one of the wanted branches (C04.588 is in C; F03.600 in F03)."""
    for tree_number in tree_numbers:
        for branch in BRANCHES:
            if tree_number.startswith(branch):
                return True
    return False


def read_descriptors(path: Path) -> Iterator[Descriptor]:
    """The descriptors of the wanted branches in a descriptor file (.gz or .xml), one at a time. The
    file's DOCTYPE names a DTD on NLM's site; the parser never fetches it."""
    if path.suffix == ".gz":
        opened = gzip.open(path, "rb")
    else:
        opened = path.open("rb")
    with opened as file:
        for _event, element in ElementTree.iterparse(file, events=("end",)):
            if element.tag != "DescriptorRecord":
                continue
            tree_numbers = []
            for tree_number in element.findall("TreeNumberList/TreeNumber"):
                if tree_number.text:
                    tree_numbers.append(tree_number.text.strip())
            if _in_branches(tree_numbers):
                ui = element.findtext("DescriptorUI").strip()
                name = element.findtext("DescriptorName/String").strip()
                terms = [name]
                for term in element.findall("ConceptList/Concept/TermList/Term/String"):
                    if term.text and term.text.strip() not in terms:
                        terms.append(term.text.strip())
                yield Descriptor(ui, name, tuple(tree_numbers), tuple(terms))
            element.clear()


def ancestor_tree_numbers(tree_number: str) -> list[str]:
    """The tree numbers above this one, nearest first: C18.452.394.750 -> C18.452.394, C18.452, C18."""
    parts = tree_number.split(".")
    found = []
    for length in range(len(parts) - 1, 0, -1):
        found.append(".".join(parts[:length]))
    return found
