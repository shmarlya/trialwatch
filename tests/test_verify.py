"""Grounding for the evidence check (checks/verify.py): a quote counts only when it is on the page it
cites, as whole words and long enough to prove something; a page counts as read only when its text
layer holds enough letters."""

from trialwatch.checks import verify
from trialwatch.db.core import Page


def a_page(text):
    return Page(b"x", 1, "", 1, text, 1, "results-summary")


def test_verify_ignores_case_spacing_and_dashes():
    page = a_page("Mean change –3.5\n(SD 1.2) in the  ACTIVE arm")
    assert verify.verify("mean change -3.5 (SD 1.2) in the active arm", page)
    assert not verify.verify("mean change -3.6", page)
    assert not verify.verify("", page)
    assert not verify.verify("mean change -3.5", None)


def test_verify_accepts_a_hyphen_at_a_line_break():
    page = a_page("a placebo-controlled, cross-\nover study in 20 partici-\npants")
    assert verify.verify("cross-over study in 20", page)
    assert verify.verify("in 20 participants", page)
    assert verify.verify("cross- over study", page)


def test_a_quote_too_short_to_prove_anything_never_verifies():
    page = a_page("Outcome: NA. Deaths: 0. Withdrawn: N/A. Serious adverse events: none.")
    for quote in ("NA", "0", "N/A", "none.", "Deaths: 0"):
        assert not verify.verify(quote, page), quote
    # MIN_QUOTE_CHARS characters, as they are compared (normalised)
    assert len("Deaths: 0.") == verify.MIN_QUOTE_CHARS
    assert verify.verify("Deaths: 0.", page)


def test_a_quote_must_not_start_or_end_inside_a_word_or_number_of_the_page():
    page = a_page("In total 112 participants were randomised; 4 participants withdrew.")
    assert not verify.verify("12 participants", page)
    assert verify.verify("112 participants", page)
    assert not verify.verify("112 participant", page)
    assert not verify.verify("otal 112 participants", page)
    assert verify.verify("4 participants withdrew", page)
    # punctuation at the quote's edge may stand next to anything
    assert verify.verify("; 4 participants", page)
    assert verify.occurs_as_words("12 participants", "112 participants and 12 participants")
    assert not verify.occurs_as_words("na", "not analysed: n/a, nan")


def test_a_page_counts_as_read_only_with_enough_letters():
    assert not verify.has_readable_text(a_page("Page 4 of 10"))
    assert not verify.has_readable_text(a_page("Confidential - Example Pharma - Page 4 of 10\n12.3 45.6 78.9 101.2"))
    letters = "word " * 50
    assert verify.letters(letters) == verify.MIN_LETTERS
    assert verify.has_readable_text(a_page(letters))
    # one letter short: digits and punctuation are not letters
    almost = "word " * 49 + "wor 1234567890 ... ;;;"
    assert verify.letters(almost) == verify.MIN_LETTERS - 1
    assert not verify.has_readable_text(a_page(almost))
    pages = [a_page(letters), a_page("Page 4 of 10")]
    assert verify.some_page_readable(pages) and not verify.every_page_readable(pages)
    assert verify.every_page_readable([a_page(letters)])
    assert not verify.some_page_readable([])


def test_text_is_compared_in_one_normal_form():
    # a ligature and a no-break space from a PDF, typographic quotes and dashes from the model
    assert verify.normalise("Efﬁcacy “primary” end–point ‘a’") == "efficacy \"primary\" end-point 'a'"
    assert verify.page_text_variants("no hyphen at a line end") == ["no hyphen at a line end"]
    assert verify.page_text_variants("cross-\nover") == ["cross-\nover", "cross-over", "crossover"]


def test_a_quote_of_a_table_cell_broken_across_lines_verifies():
    """A narrow table cell comes out of the PDF in pieces; the model quotes the number whole."""
    page = a_page("Flu A1 Assay 1\n469.\n49 \n(300\n.51 \nto \n733.\n47) \n335.")
    assert verify.verify("469.49 (300.51 to 733.47)", page)
    # still whole words and numbers of the page as it is spaced
    page = a_page("In total 112 participants were randomised")
    assert not verify.verify("12 participants were", page)
    assert verify.occurs_as_words_ignoring_spaces("112participants", "in total 112 participants")
    assert not verify.occurs_as_words_ignoring_spaces("12participants", "in total 112 participants")
    # without its spaces, a quote must still be long enough to prove something
    assert not verify.verify("1 2 3 4 5 6", a_page("123456 and more"))
