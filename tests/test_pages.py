"""Reading a file's text in a child process (trials/pages.read_in_child): the answer comes back, and a
child that dies or hangs fails only that file."""

import os
import signal
import time

import pytest

from trialwatch.trials import pages

SHA = bytes(32)


# What the child runs in these tests: module-level, so the spawned child can import them


def answers_two_pages(sha256, answer):
    answer.send(("pages", ["page one", "page two"]))


def answers_an_error(sha256, answer):
    answer.send(("error", "PdfReadError: broken"))


def is_killed(sha256, answer):
    # what the kernel does to the process that takes the container past its memory limit
    os.kill(os.getpid(), signal.SIGKILL)


def hangs(sha256, answer):
    time.sleep(60)


def test_the_childs_pages_come_back():
    assert pages.read_in_child(SHA, child_reads=answers_two_pages) == ["page one", "page two"]


def test_a_file_the_child_cannot_read_is_a_broken_file():
    with pytest.raises(ValueError, match="PdfReadError: broken"):
        pages.read_in_child(SHA, child_reads=answers_an_error)


def test_a_killed_child_fails_the_file_not_the_caller():
    with pytest.raises(pages.FileNotRead, match="out of memory"):
        pages.read_in_child(SHA, child_reads=is_killed)


def test_a_child_that_takes_too_long_is_stopped():
    started = time.monotonic()
    with pytest.raises(pages.FileNotRead, match="took longer than"):
        pages.read_in_child(SHA, timeout=1, child_reads=hangs)
    assert time.monotonic() - started < 30


def test_the_child_fetches_the_file_from_the_database(database_url):
    # No such file: the child connected and asked, and says so as a broken file
    with pytest.raises(ValueError):
        pages.read_in_child(SHA)
