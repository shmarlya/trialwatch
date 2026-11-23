"""Page text for the central model: extract each stored file's text once (core.page) and its text
fingerprint (core.file.text_fingerprint), whatever source the file came from.

The fingerprint is the sha256 of the normalised text of all pages, so the same document saved again
as different bytes (re-exported, re-uploaded) is recognised as the same content.
"""

import hashlib
import io
import logging
from importlib.metadata import version
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import psycopg
from pypdf import PdfReader

from trialwatch.db import core
from trialwatch.progress import Progress, report


EXTRACTOR = f"pypdf {version('pypdf')}"
# pypdf logs a warning for every font it can't fully decode; the text is still usable
logging.getLogger("pypdf").setLevel(logging.ERROR)


def extract_pages(content: bytes) -> list[str]:
    """Text of each page, in order. Encrypted files with an empty password (common in CTIS) are opened.
    A file that is not a PDF, or a PDF without pages, raises ValueError: its text was not read, and a
    check must not take it as read."""
    if not content.startswith(b"%PDF"):
        raise ValueError("not a PDF: only PDF files can be read")
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        reader.decrypt("")
    if len(reader.pages) == 0:
        raise ValueError("the PDF has no pages")
    texts = []
    for page in reader.pages:
        text = page.extract_text()
        # A page with no text layer (e.g. a scanned image) gives no text
        if not text:
            text = ""
        # Postgres text can't hold NUL characters
        text = text.replace("\x00", "")
        # pypdf can return unpaired UTF-16 surrogates, which Postgres (UTF-8) can't store: pairs are
        # joined into their character, an unpaired one becomes U+FFFD
        text = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        texts.append(text)
    return texts


def text_fingerprint(texts: Sequence[str]) -> bytes | None:
    """sha256 of the pages' words, lower case, single-spaced, pages joined by a form feed; None when
    there are no words at all (a scanned PDF), so files without text never match each other."""
    normalised_pages = []
    has_words = False
    for text in texts:
        words = text.lower().split()
        if words:
            has_words = True
        normalised_pages.append(" ".join(words))
    if not has_words:
        return None
    joined = "\f".join(normalised_pages)
    return hashlib.sha256(joined.encode()).digest()


@dataclass
class PagesResult:
    files: int = 0
    pages: int = 0
    # file sha256 (hex, short) -> files with the same text
    same_text: dict[str, list[str]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)


def extract_missing(
    conn: psycopg.Connection,
    trial_ids: Sequence[int] | None = None,
    progress: Progress = None,
    kinds: Sequence[str] | None = None,
    extract: Callable[[bytes], list[str]] | None = None,
) -> PagesResult:
    """Read the page text of the files not read yet (of these trials, or all; of every document kind,
    or only these `kinds`). `extract` reads a file's pages (default extract_pages; tests give their own)."""
    if extract is None:
        extract = extract_pages
    result = PagesResult()
    for sha in core.files_without_pages(conn, trial_ids, kinds):
        short_sha = sha.hex()[:12]
        try:
            texts = extract(core.file_content(conn, sha))
            same = core.save_pages(conn, sha, texts, EXTRACTOR, text_fingerprint(texts))
        except Exception as exc:  # a broken file is recorded, the others go on
            result.failed[short_sha] = f"{type(exc).__name__}: {exc}"
            report(progress, f"{short_sha}: failed: {result.failed[short_sha]}")
            continue
        result.files += 1
        result.pages += len(texts)
        if same:
            others = []
            for other in same:
                others.append(other.hex()[:12])
            result.same_text[short_sha] = others
        report(progress, f"{short_sha}: {len(texts)} pages")
    return result
