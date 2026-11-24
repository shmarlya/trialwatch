"""Reading a file's text in a child process (trials/pages.read_in_child): the answer comes back, and a
child that dies or hangs fails only that file."""

import io
import logging
import os
import signal
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
    # No such file: the child connected and asked, and says so as a broken file
    with pytest.raises(ValueError):
        pages.read_in_child(SHA)


def test_pages_over_pypdfs_default_parsing_budget_are_read():
    # A page whose content stream costs more than pypdf's default budget (2,000,000) but less than
    # ours, as two pages of dense tables in a CTIS report did (2022-502844-11-00)
    operations = b"BT /F1 1 Tf " + b"(x) Tj " * 1_100_000 + b"ET"
    texts = pages.extract_pages(pdf_with_page(operations))
    assert len(texts) == 1


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
