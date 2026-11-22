"""MkDocs hooks (`hooks:` in mkdocs.yml): the pages kept outside docs/, and links that leave the site.

- README.md is the Home page; STYLE.md and the benchmark data's README are pages at their repo paths. So a
  link written for GitHub (`docs/search.md` in README.md, `../STYLE.md` in a docs page) is rewritten to the
  page it means on the site.
- The decisions (docs/adr/) stay in the repo only (exclude_docs in mkdocs.yml). A link to one, or to any other
  repo file that is not a page, goes to that file on GitHub.
"""

import posixpath
import re
from pathlib import Path

from mkdocs.structure.files import File, Files

ROOT = Path(__file__).parent.parent
REPO_BLOB = "https://github.com/shmarlya/trialwatch/blob/main/"
# Markdown kept outside docs/ -> its page on the site
PAGES = {
    "README.md": "index.md",
    "STYLE.md": "STYLE.md",
    "data/bruckner/README.md": "data/bruckner/README.md",
}
# A markdown link with a relative target, and its #anchor if any (not http:, mailto:, #anchor or /path)
RELATIVE_LINK = re.compile(r"\]\((?![a-z]+:|#|/)([^)\s#]+)(#[^)\s]*)?\)")


def on_files(files: Files, *, config) -> Files:
    for source, page in PAGES.items():
        markdown = (ROOT / source).read_text()
        files.append(File.generated(config, page, content=markdown))
    return files


def on_page_markdown(markdown: str, *, page, config, files) -> str:
    """Every relative link in the page, pointed at what it means on the site (see the module docstring)."""
    page_site_path = page.file.src_uri
    page_repo_path = repo_path(page_site_path)
    pieces = []
    position = 0
    for match in RELATIVE_LINK.finditer(markdown):
        target = site_target(match[1], page_repo_path, page_site_path)
        anchor = ""
        if match[2]:
            anchor = match[2]
        pieces.append(markdown[position : match.start()])
        pieces.append(f"]({target}{anchor})")
        position = match.end()
    pieces.append(markdown[position:])
    return "".join(pieces)


def repo_path(site_path: str) -> str:
    """Where a page of the site lives in the repo."""
    for source, page in PAGES.items():
        if page == site_path:
            return source
    return "docs/" + site_path


def site_path(linked: str) -> str | None:
    """The page a repo file is on the site, or None if it is not on the site."""
    if linked in PAGES:
        return PAGES[linked]
    if linked.startswith("docs/") and not linked.startswith("docs/adr/"):
        return linked.removeprefix("docs/")
    return None


def site_target(target: str, page_repo_path: str, page_site_path: str) -> str:
    """A link target written relative to the page's repo path, as a target for the page on the site."""
    page_repo_folder = posixpath.dirname(page_repo_path)
    linked = posixpath.normpath(posixpath.join(page_repo_folder, target))
    linked_page = site_path(linked)
    if linked_page is None:
        return REPO_BLOB + linked
    page_site_folder = posixpath.dirname(page_site_path)
    if not page_site_folder:
        page_site_folder = "."
    return posixpath.relpath(linked_page, page_site_folder)
