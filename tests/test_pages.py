"""Reading a file's text in a child process (trials/pages.read_in_child): the answer comes back, and a
child that dies or hangs fails only that file."""

import io
import logging
import os
import signal
import threading
import time

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from trialwatch.trials import pages

SHA = bytes(32)


# What the child runs in these tests: module-level, so the spawned child can import them


MEASURES = {"megabytes": 1.0, "seconds": 2.0, "peak_megabytes": 90.0}


def answers_two_pages(sha256, answer):
    answer.send(("pages", ["page one", "page two"], MEASURES))


def answers_an_error(sha256, answer):
    answer.send(("error", "PdfReadError: broken", MEASURES))


def is_killed(sha256, answer):
    # what the kernel does to the process that takes the container past its memory limit
    os.kill(os.getpid(), signal.SIGKILL)


def hangs(sha256, answer):
    time.sleep(60)


def answers_the_first_hangs_on_others(sha256, answer):
    if sha256 == bytes([1]) * 32:
        answer.send(("pages", ["page one"], MEASURES))
        return
    time.sleep(60)


def test_the_childs_pages_come_back_and_are_logged(caplog):
    caplog.set_level(logging.INFO, logger="trialwatch.pages")
    assert pages.read_in_child(SHA, child_reads=answers_two_pages) == ["page one", "page two"]
    assert "file 000000000000 read: 2 pages (1.0 MB, 2 s, child peak 90 MB)" in caplog.text


def test_a_file_the_child_cannot_read_is_a_broken_file(caplog):
    with pytest.raises(pages.UnreadableFile, match="^PdfReadError: broken$"):
        pages.read_in_child(SHA, child_reads=answers_an_error)
    assert "file 000000000000 can't be read (1.0 MB, 2 s, child peak 90 MB): PdfReadError: broken" in caplog.text


def test_a_killed_child_fails_the_file_not_the_caller(caplog):
    with pytest.raises(pages.FileNotRead, match="out of memory"):
        pages.read_in_child(SHA, child_reads=is_killed)
    assert "file 000000000000 not read after" in caplog.text


def test_a_child_that_takes_too_long_is_stopped():
    started = time.monotonic()
    with pytest.raises(pages.FileNotRead, match="took longer than"):
        pages.read_in_child(SHA, timeout=1, child_reads=hangs)
    assert time.monotonic() - started < 30


def test_the_child_fetches_the_file_from_the_database(database_url):
    # No such file: the child connected and asked, and says so; not as a broken file (UnreadableFile,
    # left to people), as the bytes were never read: another try may find them
    with pytest.raises(pages.FileNotRead, match="TypeError"):
        pages.read_in_child(SHA)


def test_pages_over_pypdfs_default_parsing_budget_are_read():
    # A page whose content stream costs more than pypdf's default budget (2,000,000) but less than
    # ours, as two pages of dense tables in a CTIS report did (2022-502844-11-00)
    operations = b"BT /F1 1 Tf " + b"(x) Tj " * 1_100_000 + b"ET"
    texts = pages.extract_pages(pdf_with_page(operations))
    assert len(texts) == 1


def test_a_pdf_cut_short_says_so_and_by_how_much_when_its_header_gives_its_length():
    error = ValueError("Stream has ended unexpectedly")
    linearized = b"%PDF-1.7\n10691 0 obj\n<< /Linearized 1 /L 32666300 /H [ 2419 8093 ] /N 631 >>\nendobj\n"
    cut = linearized + b"x" * 17_000_000
    assert pages.why_unreadable(cut, error) == (
        "the file is cut short: 17.0 MB of the 32.7 MB its header gives (ValueError: Stream has ended unexpectedly)"
    )
    whole = pdf_with_page(b"BT /F1 12 Tf (x) Tj ET")
    assert pages.why_unreadable(whole[: len(whole) // 2], error) == (
        "the file is cut short: no %%EOF at its end (ValueError: Stream has ended unexpectedly)"
    )
    # a whole file, or one that is not a PDF: pypdf's error alone
    assert pages.why_unreadable(whole, error) == "ValueError: Stream has ended unexpectedly"
    assert pages.why_unreadable(b"<html>", error) == "ValueError: Stream has ended unexpectedly"


def pdf_with_page(operations: bytes) -> bytes:
    """A one-page PDF whose page draws `operations` with Helvetica."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    fonts = DictionaryObject()
    fonts[NameObject("/F1")] = font
    resources = DictionaryObject()
    resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources
    stream = DecodedStreamObject()
    stream.set_data(operations)
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# Several files at once (pages.read_in_children), with a `read` that only notes who reads together


class Readers:
    """A `read` for read_in_children that holds each file a moment and notes the most read at once."""

    def __init__(self, failing: bytes | None = None):
        self.lock = threading.Lock()
        self.now: set[bytes] = set()
        self.together: list[set[bytes]] = []
        self.failing = failing

    def __call__(self, sha256: bytes) -> list[str]:
        with self.lock:
            self.now.add(sha256)
            self.together.append(set(self.now))
        time.sleep(0.05)
        with self.lock:
            self.now.discard(sha256)
        if sha256 == self.failing:
            raise pages.UnreadableFile("PdfReadError: broken")
        return [sha256.hex()[:2]]

    def most_at_once(self) -> int:
        return max(len(group) for group in self.together)


def sha(number: int) -> bytes:
    return bytes([number]) * 32


MB = 2**20


def test_files_are_read_several_at_once_up_to_the_readers():
    files = [sha(n) for n in range(6)]
    readers = Readers()
    outcomes = list(pages.read_in_children(files, dict.fromkeys(files, MB), readers=3, memory_mb=10_000, read=readers))
    assert sorted(outcome.sha256 for outcome in outcomes) == files
    assert readers.most_at_once() == 3


def test_a_file_starts_only_when_its_memory_fits_beside_the_others():
    # 100 MB files: 150 + 1,000 MB each, so a budget of 2,000 MB holds one at a time
    files = [sha(n) for n in range(3)]
    readers = Readers()
    list(pages.read_in_children(files, dict.fromkeys(files, 100 * MB), readers=3, memory_mb=2_000, read=readers))
    assert readers.most_at_once() == 1


def test_small_files_go_beside_a_large_one_and_a_file_over_the_budget_is_still_read():
    large, small_one, small_two = sha(1), sha(2), sha(3)
    sizes = {large: 300 * MB, small_one: MB, small_two: MB}
    readers = Readers()
    outcomes = list(
        pages.read_in_children([large, small_one, small_two], sizes, readers=2, memory_mb=1_000, read=readers)
    )
    assert len(outcomes) == 3
    assert {large} in readers.together
    assert all(large not in group or len(group) == 1 for group in readers.together)


def test_a_file_that_cannot_be_read_is_yielded_with_its_error_and_the_rest_go_on():
    files = [sha(n) for n in range(4)]
    readers = Readers(failing=sha(2))
    outcomes = {o.sha256: o for o in pages.read_in_children(files, {}, readers=2, memory_mb=10_000, read=readers)}
    assert isinstance(outcomes[sha(2)].error, pages.UnreadableFile)
    assert outcomes[sha(0)].texts == ["00"]
    assert len(outcomes) == 4


def test_a_caller_that_stops_early_does_not_wait_for_the_children_still_reading(monkeypatch):
    # e.g. Ctrl-C, or `trialwatch web` stopping its worker: the child still reading is killed
    monkeypatch.setattr(pages, "_child_reads", answers_the_first_hangs_on_others)
    started = time.monotonic()
    for outcome in pages.read_in_children([sha(1), sha(2)], {}, readers=2, memory_mb=10_000):
        assert outcome.texts == ["page one"]
        break
    assert time.monotonic() - started < 20


def test_the_reading_threads_run_in_the_callers_context_for_its_trace():
    import contextvars

    where = contextvars.ContextVar("where", default="none")
    where.set("the job")
    seen = []

    def read(sha256):
        seen.append(where.get())
        return []

    list(pages.read_in_children([sha(1), sha(2)], {}, readers=2, memory_mb=10_000, read=read))
    assert seen == ["the job", "the job"]


def test_small_dense_files_are_not_estimated_below_what_one_took():
    assert pages.child_estimate_mb(int(5.7 * MB)) >= 590
    assert pages.child_estimate_mb(47 * MB) == 150 + 470


def test_a_reading_stops_starting_files_while_too_much_text_waits_to_be_saved(monkeypatch):
    # the cap of text held: the first file's text is over it, so the second waits until it is saved
    monkeypatch.setattr(pages, "HELD_TEXT_BYTES", 5)
    reading = pages.Reading.__new__(pages.Reading)
    reading._files = [sha(1), sha(2)]
    reading._outcomes = []
    reading._held = 0
    reading._ready = threading.Condition()
    reading._error = None
    reading._stop = threading.Event()
    reading.result = pages.PagesResult()
    started = []

    def read(sha256, stop=None):
        started.append(sha256)
        return ["a long page of text"]

    monkeypatch.setattr(pages, "read_in_child", read)
    thread = threading.Thread(target=reading._read, args=({}, 1, 10_000))
    thread.start()
    time.sleep(0.3)
    assert started == [sha(1)]  # held: the second has not started
    saved = []
    monkeypatch.setattr(pages, "_record", lambda conn, result, outcome, progress: saved.append(outcome.sha256))
    reading.drain(None)
    thread.join(5)
    reading.drain(None)
    assert started == [sha(1), sha(2)] and saved == [sha(1), sha(2)]
