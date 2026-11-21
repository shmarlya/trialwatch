"""Retrieval for the evidence check's text method (ADR-004): which pages each question is shown.

Postgres full-text search over the page text of a trial's documents ('simple' configuration: no
stemming, documents come in many languages), with the words of the requirement's search hints and of
the registered item it is about (e.g. the endpoint as registered). The best pages and their
neighbours are sent; a question the retrieved pages can't settle is asked again with every page
(evidence.py), so a retrieval miss never becomes "not reported".
"""

import re
from dataclasses import dataclass, field

import psycopg

from trialwatch.checks.requirements import Question
from trialwatch.db import core
from trialwatch.db.core import Page

WORD = re.compile(r"\w{4,}", re.UNICODE)


def search_terms(text: str) -> list[str]:
    # each word once, in the order it first appears; at most 40 so the search query stays small
    terms = []
    seen = set()
    for word in WORD.findall(text.lower()):
        if word not in seen:
            seen.add(word)
            terms.append(word)
    return terms[:40]


@dataclass
class Retrieval:
    """What the search did for one question: its terms, the ranked hits and the pages chosen."""

    terms: list[str] = field(default_factory=list)
    ranked: list[dict] = field(default_factory=list)
    chosen: list = field(default_factory=list)


def search(conn: psycopg.Connection, pages: list[Page], query: str, top_k: int) -> Retrieval:
    """The top_k pages for a query (any of its words, ranked by ts_rank) and their neighbours, with
    the terms and ranks for the trace."""
    result = Retrieval(terms=search_terms(query))
    if not result.terms or not pages:
        return result
    file_hashes = set()
    for page in pages:
        file_hashes.add(page.file)
    ranked = core.rank_pages(conn, sorted(file_hashes), result.terms, top_k)
    pages_by_file = {}
    for page in pages:
        pages_by_file[(page.file, page.page_no)] = page
    for file_hash, page_no, rank in ranked:
        hit = {"file": file_hash.hex(), "page": page_no, "rank": round(rank, 4), "terms_found": [], "snippet": ""}
        page = pages_by_file.get((file_hash, page_no))
        if page is not None:
            hit["terms_found"] = terms_on_page(result.terms, page.text)
            hit["snippet"] = snippet(page.text, hit["terms_found"])
        result.ranked.append(hit)
    # a result often starts or ends on the page next to the best match, so neighbours are sent too
    wanted = set()
    for file_hash, page_no, rank in ranked:
        wanted.add((file_hash, page_no - 1))
        wanted.add((file_hash, page_no))
        wanted.add((file_hash, page_no + 1))
    for page in pages:
        if (page.file, page.page_no) in wanted:
            result.chosen.append(page)
    return result


def terms_on_page(terms: list[str], text: str) -> list[str]:
    """The search terms that occur on the page (as whole words, ignoring case)."""
    words = set()
    for word in WORD.findall(text.lower()):
        words.add(word)
    found = []
    for term in terms:
        if term in words:
            found.append(term)
    return found


def snippet(text: str, terms: list[str], width: int = 160) -> str:
    """A short piece of the page around the first search term it contains, on one line."""
    lowered = text.lower()
    first = -1
    for term in terms:
        position = lowered.find(term)
        if position >= 0 and (first < 0 or position < first):
            first = position
    if first < 0:
        first = 0
    start = max(0, first - width // 2)
    piece = text[start:start + width]
    words = piece.split()
    joined = " ".join(words)
    if start > 0:
        joined = "… " + joined
    if start + width < len(text):
        joined = joined + " …"
    return joined




def search_query_for(question: Question) -> str:
    """Words to search the pages with: the requirement's search hints, plus the text of the registered
    item it is about (e.g. the endpoint as registered); the question itself when there are neither."""
    words = []
    for word in question.search:
        words.append(word)
    if question.item_text:
        words.append(question.item_text)
    if not words:
        return question.text
    return " ".join(words)


def pages_to_send_first(pages: list[Page], per_question: dict[str, list[Page]]) -> list[Page]:
    """Every page any question needs, each once, in document order."""
    needed = set()
    for question_pages in per_question.values():
        for page in question_pages:
            needed.add((page.doc, page.page_no))
    chosen = []
    for page in pages:
        if (page.doc, page.page_no) in needed:
            chosen.append(page)
    return chosen


