"""Page text for the central model: extract each stored file's text once (core.page) and its text
fingerprint (core.file.text_fingerprint), whatever source the file came from.

The fingerprint is the sha256 of the normalised text of all pages, so the same document saved again
as different bytes (re-exported, re-uploaded) is recognised as the same content.

Each file is read in a child process of its own (read_in_child). pypdf needs about ten times a file's
size while it reads it (a 38 MB CTIS study report of 3,000 pages: 420 MB), and a process keeps memory
it once used: in the worker, a few such files took it past its memory limit, and the kernel killed it
in the middle of a transfer (2026-11-24). A child gives its memory back when it ends; if it runs out
or takes too long, only that file fails, recorded like any broken file.
"""

import hashlib
import io
import logging
import multiprocessing
from importlib.metadata import version
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path

import psycopg
from pypdf import PdfReader

from trialwatch import config
from trialwatch.db import core
from trialwatch.progress import Progress, report


EXTRACTOR = f"pypdf {version('pypdf')}"
# pypdf logs a warning for every font it can't fully decode; the text is still usable
logging.getLogger("pypdf").setLevel(logging.ERROR)

# Read each file in a child process (see the module's docstring). The tests turn it off to read in
# the process, where their fake extract_pages applies (tests/conftest.py).
READ_IN_CHILD = True
# The longest a child may read one file: the 3,000-page report took 80 s on a laptop
READ_TIMEOUT_SECONDS = 20 * 60
# "spawn", not "fork": the worker has threads (the job's heartbeat) and open connections, which a
# forked child would inherit in whatever state they were
CHILDREN = multiprocessing.get_context("spawn")


class FileNotRead(Exception):
    """The child reading a file ended without its text: killed (out of memory) or too slow."""


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


def read_in_child(
    sha256: bytes, timeout: float = READ_TIMEOUT_SECONDS, child_reads: Callable[[bytes, Connection], None] | None = None
) -> list[str]:
    """The pages of a stored file, read by a child process that fetches the file itself (so this
    process never holds its bytes). A file that can't be read raises ValueError, as extract_pages does;
    a child that is killed or too slow raises FileNotRead. `child_reads` is what the child runs
    (default _child_reads; tests give their own, a module-level function the child can import)."""
    if child_reads is None:
        child_reads = _child_reads
    receiving_end, sending_end = CHILDREN.Pipe(duplex=False)
    child = CHILDREN.Process(target=child_reads, args=(sha256, sending_end), name="trialwatch-pages", daemon=True)
    child.start()
    # Only the child writes to the pipe: with this end closed here, the child's death closes it
    sending_end.close()
    try:
        # The answer is read before waiting for the child: a long text would fill the pipe and the
        # child would wait for it to be read
        answered = receiving_end.poll(timeout)
        if not answered:
            raise FileNotRead(f"reading its text took longer than {timeout / 60:.0f} min")
        try:
            outcome, value = receiving_end.recv()
        except EOFError:
            child.join()
            raise FileNotRead(_ended_without_text(child.exitcode)) from None
    finally:
        receiving_end.close()
        if child.is_alive():
            child.kill()
        child.join()
    if outcome == "error":
        raise ValueError(value)
    return value


def _ended_without_text(exitcode: int | None) -> str:
    # -9: SIGKILL, which is what the kernel sends a process when the container's memory runs out
    if exitcode == -9:
        return "the process reading its text was killed, most likely out of memory"
    return f"the process reading its text ended without it (exit code {exitcode})"


def _child_reads(sha256: bytes, answer: Connection) -> None:
    """In the child: fetch the file, read its pages, send ("pages", texts) or ("error", message)."""
    _first_to_go()
    try:
        with psycopg.connect(config.database_url()) as conn:
            content = core.file_content(conn, sha256)
        texts = extract_pages(content)
    except Exception as exc:  # a broken file: the parent records the message
        answer.send(("error", f"{type(exc).__name__}: {exc}"))
        return
    answer.send(("pages", texts))


def _first_to_go() -> None:
    """When the container runs out of memory, the kernel kills the process it scores highest: make that
    this child rather than the worker, so only this file fails (Linux; elsewhere nothing to do)."""
    try:
        Path("/proc/self/oom_score_adj").write_text("1000")
    except OSError:
        pass


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
    or only these `kinds`). `extract` reads a file's pages here (tests give their own); without it, a
    child process reads each file (read_in_child)."""
    result = PagesResult()
    for sha in core.files_without_pages(conn, trial_ids, kinds):
        short_sha = sha.hex()[:12]
        try:
            texts = _read(conn, sha, extract)
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


def _read(conn: psycopg.Connection, sha256: bytes, extract: Callable[[bytes], list[str]] | None) -> list[str]:
    if extract is not None:
        return extract(core.file_content(conn, sha256))
    if READ_IN_CHILD:
        return read_in_child(sha256)
    return extract_pages(core.file_content(conn, sha256))
