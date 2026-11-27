"""OCR for pages that are pictures (trials/ocr.py): which pages are looked at, which are pictures, and
what is stored of them (trials/pages.py, db/core.py)."""

import io

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from trialwatch.db import core
from trialwatch.trials import ocr, pages


def pdf_cover_then_picture() -> bytes:
    """Two pages: a cover with a few words, and a page that is one picture (a scanned table)."""
    writer = PdfWriter()
    cover = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    cover[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    words = DecodedStreamObject()
    words.set_data(b"BT /F1 12 Tf 20 100 Td (Clinical study report) Tj ET")
    cover[NameObject("/Contents")] = writer._add_object(words)
    scan = writer.add_blank_page(width=200, height=200)
    image = DecodedStreamObject()
    image.set_data(bytes([128]) * 64)
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(8),
            NameObject("/Height"): NumberObject(8),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
            NameObject("/BitsPerComponent"): NumberObject(8),
        }
    )
    scan[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): writer._add_object(image)})}
    )
    draw = DecodedStreamObject()
    draw.set_data(b"q 200 0 0 200 0 0 cm /Im1 Do Q")
    scan[NameObject("/Contents")] = writer._add_object(draw)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


TABLE = "Primary endpoint: change from baseline, treatment group versus placebo, mean difference. " * 4


def reads_a_table(picture: bytes, languages: str) -> str:
    assert picture.startswith(b"P5\n") and languages == "eng"
    return TABLE


def test_only_pages_under_the_letters_are_looked_at():
    assert ocr.pages_to_look_at(["Page 1 of 3", "word " * 50, ""]) == [1, 3]


def test_a_picture_is_read_and_a_page_of_few_words_is_marked_as_no_picture():
    looks = ocr.look_at_pages(pdf_cover_then_picture(), [1, 2], "eng", read_picture=reads_a_table)
    assert looks == {1: ocr.Look(is_picture=False), 2: ocr.Look(is_picture=True, text=TABLE)}


def pdf_of_drawings(shapes: int) -> bytes:
    """One page without text or images, drawn with this many line shapes (a chart, or text as outlines)."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    lines = DecodedStreamObject()
    lines.set_data(b"".join(f"{10 + i} 10 m {10 + i} 190 l S\n".encode() for i in range(shapes)))
    page[NameObject("/Contents")] = writer._add_object(lines)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_a_page_drawn_with_many_shapes_is_a_picture_and_a_few_rules_are_not():
    drawn = ocr.look_at_pages(pdf_of_drawings(ocr.DRAWN_SHAPES), [1], "eng", read_picture=reads_a_table)
    assert drawn == {1: ocr.Look(is_picture=True, text=TABLE)}
    ruled = ocr.look_at_pages(pdf_of_drawings(3), [1], "eng", read_picture=reads_a_table)
    assert ruled == {1: ocr.Look(is_picture=False)}


def test_past_the_most_image_pages_a_picture_is_marked_but_not_read(monkeypatch):
    monkeypatch.setattr(ocr, "MAX_IMAGE_PAGES", 0)
    looks = ocr.look_at_pages(pdf_cover_then_picture(), [2], "eng", read_picture=reads_a_table)
    assert looks == {2: ocr.Look(is_picture=True)}


def test_ocr_text_replaces_the_text_layer_only_when_it_has_more_letters():
    looks = {
        1: ocr.Look(is_picture=False),
        2: ocr.Look(is_picture=True, text=TABLE),
        3: ocr.Look(is_picture=True, text="x"),
    }
    stored = pages.page_looks(["Cover", "", "Figure 2: hazard ratio by subgroup"], looks, "tesseract 5")
    assert stored == {
        1: core.PageLook(False, None, "tesseract 5"),
        2: core.PageLook(True, TABLE, "tesseract 5"),
        3: core.PageLook(True, None, "tesseract 5"),
    }
    assert pages.page_looks(["Cover"], None, None) is None


def test_ocr_that_fails_leaves_the_text_layer(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)

    def broken(*args, **kwargs):
        raise RuntimeError("tesseract failed: no eng.traineddata")

    monkeypatch.setattr(ocr, "look_at_pages", broken)
    read = pages.read_pictures(b"%PDF", ["Cover", ""])
    assert read == pages.FileText(["Cover", ""])


@pytest.mark.skipif(not ocr.ocr_available(), reason="tesseract is not installed here")
def test_tesseract_reads_a_drawn_page():
    looks = ocr.look_at_pages(pdf_cover_then_picture(), [2], "eng")
    assert looks[2].is_picture and looks[2].text is not None
