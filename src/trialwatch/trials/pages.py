"""Page text for the central model: extract each stored file's text once (core.page) and its text
fingerprint (core.file.text_fingerprint), whatever source the file came from.

The fingerprint is the sha256 of the normalised text of all pages, so the same document saved again
as different bytes (re-exported, re-uploaded) is recognised as the same content.

Each file is read in a child process of its own (read_in_child). pypdf needs about ten times a file's
size while it reads it (a 38 MB CTIS study report of 3,000 pages: 420 MB), and a process keeps memory
it once used: in the worker, a few such files took it past its memory limit, and the kernel killed it
in the middle of a transfer (2026-11-24). A child gives its memory back when it ends; if it runs out
or takes too long, only that file fails, recorded like any broken file.

Several files are read at once (read_in_children): pypdf is pure Python, so one process reads on one CPU,
and a trial's dozens of documents read one after another kept the worker on one of its CPUs. Up to
config.pdf_readers() children run together, and a file starts only when its memory estimate fits beside
those being read (config.pdf_memory_mb()); the text is saved here, in the calling thread, as each ends.
"""

import hashlib
import io
import logging
import multiprocessing
import re
import resource
import sys
import time
import contextvars
import functools
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from importlib.metadata import version
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path

import psycopg
import pypdf
from pypdf import PdfReader

from trialwatch import config, db
from trialwatch.db import core
from trialwatch.progress import Progress, report


EXTRACTOR = f"pypdf {version('pypdf')}"
# pypdf logs a warning for every font it can't fully decode; the text is still usable
logging.getLogger("pypdf").setLevel(logging.ERROR)
# One line per file read and per file that could not be, in the worker's log (Coolify shows it)
log = logging.getLogger("trialwatch.pages")

# pypdf stops parsing a page whose content stream costs more than a budget (a guard against hostile
# files), 2,000,000 by default: two pages of dense tables in a CTIS study report needed more
# (2022-502844-11-00, 799 pages, 2026-11-24), and failed the whole file. Five times the default reads
# them; the guard still holds, and the child process bounds what a file can cost anyway.
STREAM_PARSING_BUDGET = 10_000_000

# Read each file in a child process (see the module's docstring). The tests turn it off to read in
# the process, where their fake extract_pages applies (tests/conftest.py).
READ_IN_CHILD = True
# The longest a child may read one file: the 3,000-page report took 80 s on a laptop
READ_TIMEOUT_SECONDS = 20 * 60
# "spawn", not "fork": the worker has threads (the job's heartbeat) and open connections, which a
# forked child would inherit in whatever state they were
CHILDREN = multiprocessing.get_context("spawn")
# A child's memory while it reads a file, for deciding how many read at once: about ten times the file
# plus the interpreter and pypdf it starts with (the 47 MB report: 550 MB), and never less than a small
# file of dense pages has taken (5.7 MB: child peak 590 MB)
CHILD_BASE_MB = 150
CHILD_MB_PER_FILE_MB = 10
CHILD_MIN_MB = 600
# How often a thread waiting for its child looks whether the reading was interrupted
STOP_CHECK_SECONDS = 1.0


class FileNotRead(Exception):
    """The child reading a file ended without its text: killed (out of memory) or too slow."""


class UnreadableFile(ValueError):
    """The file was read and found to be unreadable; the message names pypdf's error, and says so when
    the file is cut short. Reading the same bytes again fails the same way, so it is a lasting failure:
    only other bytes (a new version, or the source mending its file) can be read."""


# A PDF ends with %%EOF; a linearized one gives its full length near its start ("/Linearized 1 /L 32666300")
PDF_END_BYTES = 2048
LINEARIZED_LENGTH = re.compile(rb"/Linearized\s+[\d.]+\s*/L\s+(\d+)")


def why_unreadable(content: bytes, exc: Exception) -> str:
    """pypdf's error, and before it whether the file is cut short: a file the source publishes cut short
    (2022-501485-22-00's 32.7 MB safety narratives, stored by CTIS as their first 17.8 MB, 2026-11-25)
    fails as "Stream has ended unexpectedly", which alone doesn't say whose fault it is."""
    error = f"{type(exc).__name__}: {exc}"
    if not content.startswith(b"%PDF") or b"%%EOF" in content[-PDF_END_BYTES:]:
        return error
    found = LINEARIZED_LENGTH.search(content[:2048])
    if found is not None and int(found.group(1)) > len(content):
        return (
            f"the file is cut short: {_mb(len(content))} of the {_mb(int(found.group(1)))} its header gives ({error})"
        )
    # without its full length, only a likelihood: a whole PDF may have bytes after its %%EOF
    return f"the file may be cut short: no %%EOF in its last {PDF_END_BYTES // 1024} KB ({error})"


def _mb(size: int) -> str:
    return f"{size / 10**6:.1f} MB"


def extract_pages(content: bytes) -> list[str]:
    """Text of each page, in order. Encrypted files with an empty password (common in CTIS) are opened.
    A file that is not a PDF, or a PDF without pages, raises ValueError: its text was not read, and a
    check must not take it as read."""
    if not content.startswith(b"%PDF"):
        raise ValueError("not a PDF: only PDF files can be read")
    # set on each call: the setting belongs to the calling thread's context
    pypdf.overwrite_configuration(stream_parsing_work_maximum_count=STREAM_PARSING_BUDGET)
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
    sha256: bytes,
    timeout: float = READ_TIMEOUT_SECONDS,
    child_reads: Callable[[bytes, Connection], None] | None = None,
    stop: "threading.Event | _Either | None" = None,
) -> list[str]:
    """The pages of a stored file, read by a child process that fetches the file itself (so this
    process never holds its bytes). A file that can't be read raises UnreadableFile (a ValueError, as
    from extract_pages); a child that is killed or too slow raises FileNotRead. Each outcome is logged.
    `child_reads` is what the child runs (default _child_reads; tests give their own, a module-level
    function the child can import). Once `stop` is set (read_in_children, when its caller is
    interrupted), the child is killed and FileNotRead raised within a second."""
    if child_reads is None:
        child_reads = _child_reads
    short_sha = sha256.hex()[:12]
    started = time.monotonic()
    receiving_end, sending_end = CHILDREN.Pipe(duplex=False)
    child = CHILDREN.Process(target=child_reads, args=(sha256, sending_end), name="trialwatch-pages", daemon=True)
    child.start()
    # Only the child writes to the pipe: with this end closed here, the child's death closes it
    sending_end.close()
    try:
        # The answer is read before waiting for the child: a long text would fill the pipe and the
        # child would wait for it to be read. Polled a second at a time, so `stop` is seen.
        while not receiving_end.poll(min(STOP_CHECK_SECONDS, max(0.0, started + timeout - time.monotonic()))):
            if stop is not None and stop.is_set():
                raise FileNotRead("stopped: the reading was interrupted")
            if time.monotonic() - started >= timeout:
                problem = f"reading its text took longer than {timeout / 60:.0f} min"
                log.warning("file %s not read: %s", short_sha, problem)
                raise FileNotRead(problem)
        try:
            outcome, value, measures = receiving_end.recv()
        except EOFError:
            child.join()
            problem = _ended_without_text(child.exitcode)
            log.warning("file %s not read after %.0f s: %s", short_sha, time.monotonic() - started, problem)
            raise FileNotRead(problem) from None
    finally:
        receiving_end.close()
        if child.is_alive():
            child.kill()
        child.join()
    if outcome == "error":
        log.warning("file %s can't be read (%s): %s", short_sha, _measured(measures), value)
        raise UnreadableFile(value)
    if outcome == "failed":
        log.warning("file %s not read: %s", short_sha, value)
        raise FileNotRead(value)
    log.info("file %s read: %d pages (%s)", short_sha, len(value), _measured(measures))
    return value


def _measured(measures: dict) -> str:
    """What the child measured, for the log: the file's size, how long it read, its peak memory."""
    size = f"{measures['megabytes']:.1f} MB"
    took = f"{measures['seconds']:.0f} s"
    peak = f"child peak {measures['peak_megabytes']:.0f} MB"
    return f"{size}, {took}, {peak}"


def _ended_without_text(exitcode: int | None) -> str:
    # -9: SIGKILL, which is what the kernel sends a process when the container's memory runs out.
    # None: another thread starting a child reaped this one first (multiprocessing's cleanup), so its
    # exit code is lost; reading files at once, that is most often a child killed for memory too.
    if exitcode == -9:
        return "the process reading its text was killed, most likely out of memory"
    if exitcode is None:
        return "the process reading its text ended without it (exit code lost), possibly killed out of memory"
    return f"the process reading its text ended without it (exit code {exitcode})"


def _child_reads(sha256: bytes, answer: Connection) -> None:
    """In the child: fetch the file, read its pages, send ("pages", texts, measures) or ("error",
    message, measures); measures: the file's size, the seconds it took, the child's peak memory."""
    _first_to_go()
    started = time.monotonic()
    size = 0
    try:
        with psycopg.connect(config.database_url(), **db.LIVENESS) as conn:
            content = core.file_content(conn, sha256)
        size = len(content)
    except Exception as exc:  # the database, not the file: the parent records the message
        answer.send(("failed", f"{type(exc).__name__}: {exc}", _measures(size, started)))
        return
    try:
        texts = extract_pages(content)
    except MemoryError as exc:  # not the file's fault: another try, with fewer files read beside it, may do
        answer.send(("failed", f"{type(exc).__name__}: {exc}", _measures(size, started)))
        return
    except Exception as exc:  # a broken file: the parent records the message
        answer.send(("error", why_unreadable(content, exc), _measures(size, started)))
        return
    answer.send(("pages", texts, _measures(size, started)))


def _measures(size: int, started: float) -> dict:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is in kilobytes on Linux, in bytes on macOS (where development runs)
    if sys.platform == "darwin":
        peak_megabytes = peak / 2**20
    else:
        peak_megabytes = peak / 1024
    return {"megabytes": size / 2**20, "seconds": time.monotonic() - started, "peak_megabytes": peak_megabytes}


def _first_to_go() -> None:
    """When the container runs out of memory, the kernel kills the process it scores highest: make that
    this child rather than the worker, so only this file fails (Linux; elsewhere nothing to do)."""
    try:
        Path("/proc/self/oom_score_adj").write_text("1000")
    except OSError:
        pass


def child_estimate_mb(size_bytes: int) -> float:
    """The memory a child is expected to take reading a file of this size."""
    return max(CHILD_MIN_MB, CHILD_BASE_MB + CHILD_MB_PER_FILE_MB * size_bytes / 2**20)


@dataclass
class Read:
    """A file's outcome from read_in_children: its pages, or the exception reading it raised."""

    sha256: bytes
    texts: list[str] | None = None
    error: Exception | None = None


def read_in_children(
    files: Sequence[bytes],
    sizes: Mapping[bytes, int],
    readers: int,
    memory_mb: float,
    read: Callable[[bytes], list[str]] | None = None,
    stop: threading.Event | None = None,
) -> Iterator[Read]:
    """Read these files, up to `readers` at once, each by `read` (default: a child process each,
    read_in_child) in a thread of its own that only waits for it; yield each file's outcome as it ends.
    The next file to start is the first, in the order given, whose estimate fits in `memory_mb` beside
    the files being read; with none being read, the first starts whatever its size.

    If the caller stops early (an exception, Ctrl-C), or sets `stop`, the children still reading are
    killed and their threads end within a second, rather than the caller waiting for their files; no
    other file starts once `stop` is set."""
    halt = threading.Event()
    either = _Either(halt, stop)
    if read is None:
        read = functools.partial(read_in_child, stop=either)
    waiting = list(files)
    running: dict[Future, tuple[bytes, float]] = {}
    in_use = 0.0
    pool = ThreadPoolExecutor(max_workers=readers, thread_name_prefix="trialwatch-pages")
    try:
        while waiting or running:
            if either.is_set():
                waiting.clear()
            while waiting and len(running) < readers:
                chosen = None
                for index, sha in enumerate(waiting):
                    cost = child_estimate_mb(sizes.get(sha, 0))
                    if not running or in_use + cost <= memory_mb:
                        chosen = index
                        break
                if chosen is None:
                    break
                sha = waiting.pop(chosen)
                # the thread runs in this one's context, so its log lines carry the job's trace
                running[pool.submit(contextvars.copy_context().run, read, sha)] = (sha, cost)
                in_use += cost
            ended, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in ended:
                sha, cost = running.pop(future)
                in_use -= cost
                try:
                    texts = future.result()
                except Exception as exc:  # recorded by the caller, as any file that can't be read
                    yield Read(sha, error=exc)
                    continue
                yield Read(sha, texts=texts)
    finally:
        halt.set()
        pool.shutdown(wait=True, cancel_futures=True)


class _Either:
    """Set when either event is (the second may be None)."""

    def __init__(self, first: threading.Event, second: threading.Event | None) -> None:
        self.first = first
        self.second = second

    def is_set(self) -> bool:
        return self.first.is_set() or (self.second is not None and self.second.is_set())


@dataclass
class PagesResult:
    files: int = 0
    pages: int = 0
    # file sha256 (hex, short) -> files with the same text
    same_text: dict[str, list[str]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    # of the failed, those whose bytes can't be read (UnreadableFile): reading them again fails the same way
    unreadable: set[str] = field(default_factory=set)


def extract_missing(
    conn: psycopg.Connection,
    trial_ids: Sequence[int] | None = None,
    progress: Progress = None,
    kinds: Sequence[str] | None = None,
    extract: Callable[[bytes], list[str]] | None = None,
    readers: int | None = None,
) -> PagesResult:
    """Read the page text of the files not read yet (of these trials, or all; of every document kind,
    or only these `kinds`). `extract` reads a file's pages here (tests give their own); without it,
    child processes read the files, `readers` at once (default config.pdf_readers(); the web server
    reads one at a time, see sources/manual.py)."""
    result = PagesResult()
    files = core.files_without_pages(conn, trial_ids, kinds)
    if not files:
        return result
    if extract is not None or not READ_IN_CHILD:
        for sha in files:
            try:
                texts = _read_here(conn, sha, extract)
            except Exception as exc:  # recorded below, as any file that can't be read
                _record(conn, result, Read(sha, error=exc), progress)
                continue
            _record(conn, result, Read(sha, texts=texts), progress)
        return result
    if readers is None:
        readers = config.pdf_readers()
    sizes = core.file_sizes(conn, files)
    for outcome in read_in_children(files, sizes, readers, config.pdf_memory_mb()):
        _record(conn, result, outcome, progress)
    return result


def _record(conn: psycopg.Connection, result: PagesResult, outcome: Read, progress: Progress) -> None:
    """Save a file's pages and count them, or record why it has none; a broken file never stops the rest."""
    short_sha = outcome.sha256.hex()[:12]
    try:
        if outcome.error is not None:
            raise outcome.error
        texts = outcome.texts
        same = core.save_pages(conn, outcome.sha256, texts, EXTRACTOR, text_fingerprint(texts))
    except (UnreadableFile, FileNotRead) as exc:  # the message already says what went wrong
        result.failed[short_sha] = str(exc)
        if isinstance(exc, UnreadableFile):
            result.unreadable.add(short_sha)
        report(progress, f"{short_sha}: failed: {result.failed[short_sha]}")
        return
    except Exception as exc:  # a broken file is recorded, the others go on
        result.failed[short_sha] = f"{type(exc).__name__}: {exc}"
        log.warning("file %s not read: %s", short_sha, result.failed[short_sha])
        report(progress, f"{short_sha}: failed: {result.failed[short_sha]}")
        return
    result.files += 1
    result.pages += len(texts)
    if same:
        others = []
        for other in same:
            others.append(other.hex()[:12])
        result.same_text[short_sha] = others
    report(progress, f"{short_sha}: {len(texts)} pages")


def _read_here(conn: psycopg.Connection, sha256: bytes, extract: Callable[[bytes], list[str]] | None) -> list[str]:
    if extract is None:
        extract = extract_pages
    content = core.file_content(conn, sha256)
    try:
        return extract(content)
    except MemoryError as exc:  # as in a child (_child_reads): not the file's fault
        raise FileNotRead(f"{type(exc).__name__}: {exc}") from exc
    except Exception as exc:  # the file's bytes, as in a child (_child_reads)
        raise UnreadableFile(why_unreadable(content, exc)) from exc


# The most page text read but not saved yet that a Reading holds: past it, no other file starts until
# the caller saves what is held (drain), so a trial of many long reports never piles up in memory
HELD_TEXT_BYTES = 64 * 1024 * 1024


class Reading:
    """A trial's files being read in the background while the caller does something else (a transfer
    fetches the next trial): child processes read them (read_in_children) and a thread only waits for
    them and keeps their outcomes. The caller saves what is read so far whenever it can (drain(), e.g.
    between the next trial's documents) and the rest at the end (finish()), on its own connection and in
    its own thread, so every database write stays on one connection. Once HELD_TEXT_BYTES of text wait
    to be saved, the thread starts no other file until they are."""

    def __init__(self, conn: psycopg.Connection, trial_ids: Sequence[int], kinds: Sequence[str] | None) -> None:
        self._files = core.files_without_pages(conn, trial_ids, kinds)
        self._outcomes: list[Read] = []
        self._held = 0
        self._ready = threading.Condition()
        self._error: BaseException | None = None
        self._stop = threading.Event()
        self._thread = None
        self._started = time.monotonic()
        self._ended: float | None = None
        self.result = PagesResult()
        if self._files:
            sizes = core.file_sizes(conn, self._files)
            context = contextvars.copy_context()
            self._thread = threading.Thread(
                target=context.run,
                args=(self._read, sizes, config.pdf_readers(), config.pdf_memory_mb()),
                name="trialwatch-reading",
                daemon=True,
            )
            self._thread.start()

    @property
    def files(self) -> list[bytes]:
        """The files this reading set out to read."""
        return list(self._files)

    def _read(self, sizes: Mapping[bytes, int], readers: int, memory_mb: int) -> None:
        try:
            for outcome in read_in_children(self._files, sizes, readers, memory_mb, stop=self._stop):
                with self._ready:
                    self._outcomes.append(outcome)
                    self._held += _text_bytes(outcome)
                    # the generator starts the next file only when asked for its next outcome: wait
                    # here while too much text waits to be saved
                    while self._held > HELD_TEXT_BYTES and not self._stop.is_set():
                        self._ready.wait(1.0)
                # each file read is the work moving (the worker's watchdog), while the caller is busy
                report(None, f"{outcome.sha256.hex()[:12]}: read")
        except BaseException as exc:  # given to the caller by finish()
            self._error = exc
        finally:
            self._ended = time.monotonic()

    @property
    def seconds(self) -> float:
        """How long its files took to read, so far: from its start until its last file was read."""
        if self._thread is None:
            return 0.0
        ended = self._ended
        if ended is None:
            ended = time.monotonic()
        return ended - self._started

    def drain(self, conn: psycopg.Connection, progress: Progress = None) -> None:
        """Save the files read so far (their pages, or why they have none)."""
        with self._ready:
            outcomes = self._outcomes
            self._outcomes = []
            self._held = 0
            self._ready.notify_all()
        for outcome in outcomes:
            _record(conn, self.result, outcome, progress)

    def finish(self, conn: psycopg.Connection, progress: Progress = None) -> PagesResult:
        """Wait for the reading to end, saving each file as it comes; returns every file's outcome."""
        while self._thread is not None and self._thread.is_alive():
            self.drain(conn, progress)
            self._thread.join(0.5)
        if self._error is not None:
            raise self._error
        self.drain(conn, progress)
        return self.result

    def cancel(self) -> None:
        """Stop reading: the children still reading are killed, within a second or so."""
        self._stop.set()
        with self._ready:
            self._ready.notify_all()
        if self._thread is not None:
            self._thread.join()


def _text_bytes(outcome: Read) -> int:
    if outcome.texts is None:
        return 0
    size = 0
    for text in outcome.texts:
        size += len(text)
    return size


class ReadingHere:
    """Reading's stand-in where files are read in this process (tests, or `extract`): finish() reads
    and saves them then, one after another (extract_missing)."""

    def __init__(
        self, trial_ids: Sequence[int], kinds: Sequence[str] | None, extract: Callable[[bytes], list[str]] | None
    ) -> None:
        self._trial_ids = list(trial_ids)
        self._kinds = kinds
        self._extract = extract
        self.seconds = 0.0

    @property
    def files(self) -> None:
        """Not known ahead: read at the end."""
        return None

    def drain(self, conn: psycopg.Connection, progress: Progress = None) -> None:
        pass

    def finish(self, conn: psycopg.Connection, progress: Progress = None) -> PagesResult:
        started = time.monotonic()
        try:
            return extract_missing(conn, self._trial_ids, progress, self._kinds, self._extract)
        finally:
            self.seconds = time.monotonic() - started

    def cancel(self) -> None:
        pass


def start_reading(
    conn: psycopg.Connection,
    trial_ids: Sequence[int],
    kinds: Sequence[str] | None = None,
    extract: Callable[[bytes], list[str]] | None = None,
) -> "Reading | ReadingHere":
    """Start reading the page text these trials' files still lack, to save it later with finish()."""
    if extract is not None or not READ_IN_CHILD:
        return ReadingHere(trial_ids, kinds, extract)
    return Reading(conn, trial_ids, kinds)
