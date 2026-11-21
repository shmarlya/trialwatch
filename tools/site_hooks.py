"""MkDocs hooks (`hooks:` in mkdocs.yml) that build every page of the docs site from the docs we keep anyway.

- Home from README.md, Glossary from GLOSSARY.md and the benchmark data's own README, with
  repo-relative links rewritten for the site.
- One page per docs/**/*.excalidraw: its title (the diagram's largest text), its image from docs/img/
  (rendered by tools/diagrams/render.mjs), a download link for the source, and all of its text in a
  collapsed block so the site search finds what's in the diagram.

Pages exist only in the build; nothing is written to docs/. A new diagram gets a page automatically but
still needs a nav entry in mkdocs.yml. Decisions (NOT_ON_SITE: the ADRs, their roadmap and the AI decision
map) stay in the repo only: they get no page, their files are left out of the site, and links to them go
to their image on GitHub.
"""

import html
import json
import re
from pathlib import Path

from mkdocs.structure.files import File, Files

ROOT = Path(__file__).parent.parent
# Markdown kept outside docs/ -> where it goes on the site. The data README keeps its repo path so
# that README.md's link to it resolves both on GitHub and here.
PAGES = {
    "README.md": "index.md",
    "GLOSSARY.md": "glossary.md",
    "STYLE.md": "STYLE.md",
    "data/bruckner/README.md": "data/bruckner/README.md",
}
# Diagrams kept in the repo but not on the site (paths relative to docs/; a trailing / is a folder)
NOT_ON_SITE = ("adr/", "adr-roadmap.excalidraw", "ai-architecture-decisions.excalidraw")
REPO_BLOB = "https://github.com/shmarlya/trialwatch/blob/main/"
# README.md links to docs/img/<name>.svg so the images show on GitHub; on the site, link the diagram's page.
IMAGE_LINK = re.compile(r"\]\(docs/img/([^)\s]+)\.svg\)")
DOCS_LINK = re.compile(r"\]\(docs/([^)\s]+)\)")


def shown_on_site(rel: str) -> bool:
    """False for a diagram, or its image under img/, that stays in the repo only (NOT_ON_SITE)."""
    if rel.startswith("img/"):
        rel = rel.removeprefix("img/").removesuffix(".svg") + ".excalidraw"
    for kept_out in NOT_ON_SITE:
        if rel == kept_out or (kept_out.endswith("/") and rel.startswith(kept_out)):
            return False
    return True


def on_files(files: Files, *, config) -> Files:
    for file in list(files):
        if not shown_on_site(file.src_uri):
            files.remove(file)
    for source, page in PAGES.items():
        markdown = (ROOT / source).read_text()
        content = site_links(markdown)
        files.append(File.generated(config, page, content=content))
    docs = Path(config.docs_dir)
    for diagram in sorted(docs.rglob("*.excalidraw")):
        rel = diagram.relative_to(docs)
        if not shown_on_site(rel.as_posix()):
            continue
        page = rel.with_suffix(".md").as_posix()
        content = diagram_page(diagram, rel)
        files.append(File.generated(config, page, content=content))
    return files


def site_links(markdown: str) -> str:
    # Image links first: DOCS_LINK would also match them and turn them into links to the SVG
    with_page_links = IMAGE_LINK.sub(image_link, markdown)
    return DOCS_LINK.sub(r"](\1)", with_page_links)


def image_link(match: re.Match) -> str:
    """A diagram's page on the site, or its image on GitHub if the diagram is not on the site."""
    name = match[1]
    if shown_on_site(name + ".excalidraw"):
        return f"]({name}.md)"
    return f"]({REPO_BLOB}docs/img/{name}.svg)"


def md_escape(text: str) -> str:
    """Literal text inside Markdown: no tags (`<decision>`), emphasis or link brackets."""
    html_escaped = html.escape(text, quote=False)
    return re.sub(r"([\\`*_\[\]])", r"\\\1", html_escaped)


def text_elements(diagram: Path) -> list[dict]:
    """The diagram's text elements, leaving out deleted ones (Excalidraw keeps them in the file)."""
    elements = json.loads(diagram.read_text())["elements"]
    texts = []
    for element in elements:
        if element["type"] == "text" and not element.get("isDeleted"):
            texts.append(element)
    return texts


def largest_text(texts: list[dict]) -> dict:
    """The text in the largest font; the first one if several share that size."""
    largest = texts[0]
    for text in texts:
        if text.get("fontSize", 0) > largest.get("fontSize", 0):
            largest = text
    return largest


def reading_position(element: dict) -> tuple:
    """Sort key: top to bottom, then left to right."""
    return (element["y"], element["x"])


def diagram_page(diagram: Path, rel: Path) -> str:
    texts = text_elements(diagram)
    # The title is the first line of the diagram's largest text, or the file name if it has no text
    if texts:
        title = largest_text(texts)["text"].splitlines()[0]
    else:
        title = rel.stem
    folder_depth = len(rel.parts) - 1
    up = "../" * folder_depth  # the page sits next to its diagram, e.g. adr/ADR-001-….md
    image = f"{up}img/{rel.with_suffix('.svg').as_posix()}"
    # Top to bottom, then left to right: roughly reading order for these diagrams. The newline after
    # <br> keeps words on either side of a line break apart in the search index.
    paragraph_lines = []
    for element in sorted(texts, key=reading_position):
        escaped = html.escape(element["text"])
        with_breaks = escaped.replace("\n", "<br>\n")
        paragraph_lines.append("<p>" + with_breaks + "</p>")
    paragraphs = "\n".join(paragraph_lines)
    return f"""---
hide: [toc]
---

# {md_escape(title)}

[![{md_escape(title)}]({image})]({image}){{ target=_blank }}

Click the diagram to open it full size. Source: [{rel.name}]({rel.name}){{ download="{rel.name}" }},
open it at [excalidraw.com](https://excalidraw.com) to edit; the image is re-rendered on commit.

<details class="abstract">
<summary>Text in this diagram</summary>
{paragraphs}
</details>
"""
