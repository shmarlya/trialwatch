"""MkDocs hooks (`hooks:` in mkdocs.yml) that build every page of the docs site from the docs we keep anyway.

- Home from README.md and Glossary from GLOSSARY.md, with repo-relative links rewritten for the site.
- One page per docs/**/*.excalidraw: its title (the diagram's largest text), its image from docs/img/
  (rendered by tools/diagrams/render.mjs), a download link for the source, and all of its text in a
  collapsed block so the site search finds what's in the diagram.

Pages exist only in the build; nothing is written to docs/. A new diagram gets a page automatically but
still needs a nav entry in mkdocs.yml.
"""

import html
import json
import re
from pathlib import Path

from mkdocs.structure.files import File, Files

ROOT = Path(__file__).parent.parent
# README.md links to docs/img/<name>.svg so the images show on GitHub; on the site, link the diagram's page.
IMAGE_LINK = re.compile(r"\]\(docs/img/([^)\s]+)\.svg\)")
DOCS_LINK = re.compile(r"\]\(docs/([^)\s]+)\)")


def on_files(files: Files, *, config) -> Files:
    files.append(File.generated(config, "index.md", content=site_links((ROOT / "README.md").read_text())))
    files.append(File.generated(config, "glossary.md", content=site_links((ROOT / "GLOSSARY.md").read_text())))
    docs = Path(config.docs_dir)
    for diagram in sorted(docs.rglob("*.excalidraw")):
        rel = diagram.relative_to(docs)
        files.append(File.generated(config, rel.with_suffix(".md").as_posix(), content=diagram_page(diagram, rel)))
    return files


def site_links(markdown: str) -> str:
    return DOCS_LINK.sub(r"](\1)", IMAGE_LINK.sub(r"](\1.md)", markdown))


def md_escape(text: str) -> str:
    """Literal text inside Markdown: no tags (`<decision>`), emphasis or link brackets."""
    return re.sub(r"([\\`*_\[\]])", r"\\\1", html.escape(text, quote=False))


def diagram_page(diagram: Path, rel: Path) -> str:
    texts = [e for e in json.loads(diagram.read_text())["elements"] if e["type"] == "text" and not e.get("isDeleted")]
    title = max(texts, key=lambda e: e.get("fontSize", 0))["text"].splitlines()[0] if texts else rel.stem
    up = "../" * (len(rel.parts) - 1)  # the page sits next to its diagram, e.g. adr/ADR-001-….md
    image = f"{up}img/{rel.with_suffix('.svg').as_posix()}"
    # Top to bottom, then left to right: roughly reading order for these diagrams. The newline after
    # <br> keeps words on either side of a line break apart in the search index.
    paragraphs = "\n".join(
        "<p>" + html.escape(e["text"]).replace("\n", "<br>\n") + "</p>"
        for e in sorted(texts, key=lambda e: (e["y"], e["x"]))
    )
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
