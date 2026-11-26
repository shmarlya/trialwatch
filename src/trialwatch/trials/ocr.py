"""Text for pages that are pictures: OCR of a file's pages whose text layer has too few letters.

A page under MIN_LETTERS letters is looked at: if it is a picture (an image covers most of it: a scanned
page, a results table saved as an image; or it is drawn with many shapes: a chart, or text drawn as
outlines), the page is drawn (pypdfium2) and its text read from the picture (tesseract); otherwise it has
no other text than its own (a blank, cover, contents or signature page), and a check may count it as read. 94% of such image pages in prod's results documents gave 200 letters or
more, at about a second a page (evals/research/undecided-2026-11.md).

This runs where a file's text is read, in the same child process (trials/pages.py), and for files read
before it existed in `trialwatch pages ocr`. Without tesseract on the machine (`ocr_available()`),
nothing is looked at and the file is left for a later pass.
"""

import functools
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pypdfium2
import pypdfium2.raw as pdfium_raw

# Fewer letters than this and a page's text layer may not be its content (checks/verify.py: MIN_LETTERS)
MIN_LETTERS = 200
# An image over this share of the page makes it a picture
IMAGE_SHARE = 0.3
# So many drawn shapes make a page a picture too (a chart, or text drawn as outlines): a blank page has
# none, a cover or a table's rules a few
DRAWN_SHAPES = 50
# How finely a page is drawn for OCR: 200 dots per inch reads table text as well as 300, in half the time
DPI = 200
# The most image pages of one file read by OCR (about a second each): a file's reading must end well
# within the time a child may take (pages.READ_TIMEOUT_SECONDS); the others stay unread
MAX_IMAGE_PAGES = 400
# The longest one page's OCR may take
PAGE_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class Look:
    """What OCR made of one page under MIN_LETTERS letters: whether it is an image page, and the text
    read from the picture (None: not read, e.g. past MAX_IMAGE_PAGES, or not an image page)."""

    is_picture: bool
    text: str | None = None


def letters(text: str) -> int:
    """Letters in a text, as core.page counts them (its generated column `letters`)."""
    return len(re.sub(r"[^\w]|[\d_]", "", text))


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


@functools.cache
def extractor() -> str:
    """What core.page.extractor says of a page read by OCR: the engine and the renderer, with versions."""
    found = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, check=False)
    first = (found.stdout or found.stderr).splitlines()
    version = "tesseract"
    if first:
        version = first[0].strip()
    return f"{version} (pypdfium2 {pypdfium2.version.PYPDFIUM_INFO}, {DPI} dpi)"


def pages_to_look_at(texts: Sequence[str]) -> list[int]:
    """The pages (1-based) whose text has fewer than MIN_LETTERS letters."""
    wanted = []
    for page_no, text in enumerate(texts, start=1):
        if letters(text) < MIN_LETTERS:
            wanted.append(page_no)
    return wanted


def look_at_pages(
    content: bytes, page_nos: Sequence[int], languages: str, read_picture: Callable[[bytes, str], str] | None = None
) -> dict[int, Look]:
    """Each of these pages: whether it is an image page, and for image pages (up to MAX_IMAGE_PAGES) the
    text read from its picture. `read_picture` turns a page drawn as a PGM image into text (default:
    tesseract; tests give their own)."""
    if read_picture is None:
        read_picture = tesseract_text
    looks: dict[int, Look] = {}
    document = pypdfium2.PdfDocument(content)
    try:
        read = 0
        for page_no in page_nos:
            if page_no < 1 or page_no > len(document):
                continue
            page = document[page_no - 1]
            if not is_image_page(page):
                looks[page_no] = Look(is_picture=False)
                continue
            if read >= MAX_IMAGE_PAGES:
                looks[page_no] = Look(is_picture=True)
                continue
            read += 1
            text = read_picture(page_picture(page), languages)
            looks[page_no] = Look(is_picture=True, text=clean(text))
    finally:
        document.close()
    return looks


def is_image_page(page: pypdfium2.PdfPage) -> bool:
    """The page is a picture: an image (or images together) covers more than IMAGE_SHARE of it, or it is
    drawn with at least DRAWN_SHAPES shapes, as a chart is or text drawn as outlines (re-rendered or
    redacted reports have whole pages of text with no text layer and no image)."""
    shapes = 0
    for _ in page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_PATH], max_depth=3):
        shapes += 1
        if shapes >= DRAWN_SHAPES:
            return True
    width, height = page.get_size()
    covered = 0.0
    for image in page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE], max_depth=3):
        left, bottom, right, top = image.get_bounds()
        covered += max(0.0, right - left) * max(0.0, top - bottom)
    return covered > IMAGE_SHARE * width * height


def page_picture(page: pypdfium2.PdfPage) -> bytes:
    """The page drawn in grey at DPI, as a PGM image (which tesseract reads without other libraries)."""
    bitmap = page.render(scale=DPI / 72, grayscale=True)
    width = bitmap.width
    stride = bitmap.stride
    pixels = bytes(bitmap.buffer)
    rows = []
    for y in range(bitmap.height):
        rows.append(pixels[y * stride : y * stride + width])
    return f"P5\n{width} {bitmap.height}\n255\n".encode() + b"".join(rows)


def tesseract_text(picture: bytes, languages: str) -> str:
    """Tesseract's text of a picture, one thread (several files are read at once, each in its process)."""
    environment = dict(os.environ)
    environment["OMP_THREAD_LIMIT"] = "1"
    found = subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", languages, "--psm", "3"],
        input=picture,
        capture_output=True,
        env=environment,
        timeout=PAGE_TIMEOUT_SECONDS,
        check=False,
    )
    if found.returncode != 0:
        raise RuntimeError(f"tesseract failed: {found.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return found.stdout.decode("utf-8", "replace")


def clean(text: str) -> str:
    """As page text is stored (trials/pages.py): no NUL characters, which Postgres text can't hold."""
    return text.replace("\x00", "")
