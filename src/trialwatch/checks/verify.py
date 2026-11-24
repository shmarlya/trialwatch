"""Grounding for the evidence check (ADR-004): is a quote really on the page it cites, and could the
model have read a page at all.

Only verified quotes count for a judgement, and "not found" counts as "not reported" only when the
model read every page of the documents. Both tests lean towards "undecided" (a person decides):
- a quote must be at least MIN_QUOTE_CHARS long and occur on the cited page as whole words (ignoring
  case, spacing, dash and quote-mark variants, and hyphens that only break a word at a line end), so a
  token such as "NA" or "0" never verifies by turning up inside another word or number;
- a page counts as read only when its text layer holds at least MIN_LETTERS letters: a scanned page
  whose only text is a header or "Page 4 of 10" has a table the model never saw.
"""

import re
import unicodedata

from trialwatch.db.core import Page

# Shorter quotes ("NA", "0", "N/A") occur almost anywhere: they prove nothing
MIN_QUOTE_CHARS = 10
# Fewer letters than this on a page (a header, a page number, a signature line): its content was an
# image the text layer does not have
MIN_LETTERS = 200


def replacement_table(characters: str, replacement: str) -> dict[int, str]:
    """A str.translate table that turns each of `characters` into `replacement`."""
    table = {}
    for character in characters:
        table[ord(character)] = replacement
    return table


def _quote_mark_table() -> dict[int, str]:
    """Curly and low single quotes become ', double ones become "."""
    table = replacement_table("‘’‚‛", "'")
    double_quotes = replacement_table("“”„‟", '"')
    for code, replacement in double_quotes.items():
        table[code] = replacement
    return table


# PDF text extraction and the model write dashes and quote marks in different Unicode forms
_DASHES = replacement_table("‐‑‒–—−", "-")
_QUOTES = _quote_mark_table()


def normalise(text: str) -> str:
    # NFKC folds compatibility forms such as ligatures and non-breaking spaces into plain letters and spaces
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_DASHES)
    text = text.translate(_QUOTES)
    text = text.lower()
    words = text.split()
    return " ".join(words)


# A hyphen at the end of a line, between two word characters: "cross-\nover", "partici-\npants". The
# spaces around the line break are spaces and tabs only, so a long run of blank lines can't make the
# search slow.
LINE_BREAK_HYPHEN = re.compile(r"(?<=\w)[-‐‑‒–—−][ \t\r]*\n[ \t\r]*(?=\w)")


def page_text_variants(text: str) -> list[str]:
    """The page text as extracted, plus two readings of hyphens at line breaks: a compound word
    kept whole ("cross-over") and a word split only by the layout ("participants")."""
    variants = [text]
    if LINE_BREAK_HYPHEN.search(text):
        variants.append(LINE_BREAK_HYPHEN.sub("-", text))
        variants.append(LINE_BREAK_HYPHEN.sub("", text))
    return variants


def _edge_is_open(quote_character: str, text_character: str) -> bool:
    """A quote that starts or ends with a letter or digit may not continue a word of the page."""
    if not quote_character.isalnum():
        return True
    return not text_character.isalnum()


def occurs_as_words(quote: str, text: str) -> bool:
    """`quote` occurs in `text` without starting or ending inside a word or number of it."""
    start = text.find(quote)
    while start >= 0:
        end = start + len(quote)
        open_before = start == 0 or _edge_is_open(quote[0], text[start - 1])
        open_after = end == len(text) or _edge_is_open(quote[-1], text[end])
        if open_before and open_after:
            return True
        start = text.find(quote, start + 1)
    return False


def verify(quote: str, page: Page | None) -> bool:
    """The quote is long enough to prove something and occurs on the page (see the module's docstring)."""
    if page is None:
        return False
    quote = normalise(quote)
    if len(quote) < MIN_QUOTE_CHARS:
        return False
    for variant in page_text_variants(page.text):
        if occurs_as_words(quote, normalise(variant)):
            return True
    return False


def verify_in_text(quote: str, text: str) -> bool:
    """The quote occurs in a short text (a registry field, not a page) as whole words: at least
    MIN_QUOTE_CHARS long, or the whole field (a condition can be one short word, "Asthma")."""
    quote = normalise(quote)
    if not quote:
        return False
    whole = normalise(text)
    if quote == whole:
        return True
    if len(quote) < MIN_QUOTE_CHARS:
        return False
    return occurs_as_words(quote, whole)


def letters(text: str) -> int:
    count = 0
    for character in text:
        if character.isalpha():
            count += 1
    return count


def has_readable_text(page: Page) -> bool:
    """The page's text layer holds enough letters for the model to have read its content."""
    return letters(page.text) >= MIN_LETTERS


def every_page_readable(pages: list[Page]) -> bool:
    for page in pages:
        if not has_readable_text(page):
            return False
    return True


def read_enough(pages: list[Page], unreadable_allowed: int) -> bool:
    """Answers from these pages count as reading the whole document: some page is readable, and at
    most `unreadable_allowed` are not (a cover, signature or contents page has too few letters, though
    nothing on it can hold a result)."""
    unreadable = 0
    for page in pages:
        if not has_readable_text(page):
            unreadable += 1
    return unreadable <= unreadable_allowed and unreadable < len(pages)


def some_page_readable(pages: list[Page]) -> bool:
    for page in pages:
        if has_readable_text(page):
            return True
    return False
