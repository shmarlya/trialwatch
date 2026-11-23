"""Queries on the vocab schema (ADR-008): MeSH descriptors, their entry terms and hierarchy. Names are
compared by geo.name_key, and word sets by vocab.words_key, in SQL."""

from collections.abc import Sequence
from dataclasses import dataclass

import psycopg
from psycopg.rows import class_row


def clear_mesh(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM vocab.mesh_descriptor")


def add_descriptors(conn: psycopg.Connection, descriptors: Sequence[tuple[str, str, list[str]]],
                    terms: Sequence[tuple[str, str]]) -> int:
    """Descriptors ((ui, name, tree numbers)) with their terms ((ui, term)); returns how many terms."""
    with conn.cursor() as cur:
        with cur.copy("COPY vocab.mesh_descriptor (ui, name, tree_numbers) FROM STDIN") as copy:
            for descriptor in descriptors:
                copy.write_row(descriptor)
    conn.execute("CREATE TEMPORARY TABLE mesh_term_in (ui text, term text) ON COMMIT DROP")
    with conn.cursor() as cur:
        with cur.copy("COPY mesh_term_in (ui, term) FROM STDIN") as copy:
            for term in terms:
                copy.write_row(term)
    cursor = conn.execute(
        """
        INSERT INTO vocab.mesh_term (name_key, ui, term, words_key)
        SELECT DISTINCT ON (geo.name_key(term), ui) geo.name_key(term), ui, term, vocab.words_key(term)
        FROM mesh_term_in WHERE geo.name_key(term) <> ''
        ORDER BY geo.name_key(term), ui, term
        """
    )
    conn.execute("DROP TABLE mesh_term_in")
    conn.execute(
        "INSERT INTO vocab.mesh_tree (tree_number, ui) "
        "SELECT unnest(tree_numbers), ui FROM vocab.mesh_descriptor ON CONFLICT DO NOTHING"
    )
    return cursor.rowcount


def loaded(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT EXISTS (SELECT FROM vocab.mesh_descriptor)").fetchone()[0]


@dataclass(frozen=True)
class TermMatch:
    """A MeSH descriptor one of the given names matched; `key` is the name given (or the words given)."""

    key: str
    ui: str
    name: str
    term: str


def terms_named(conn: psycopg.Connection, names: Sequence[str]) -> list[TermMatch]:
    """Descriptors with an entry term that is exactly one of these names (case and accents aside)."""
    with conn.cursor(row_factory=class_row(TermMatch)) as cur:
        return cur.execute(
            """
            SELECT given AS key, d.ui, d.name, t.term
            FROM unnest(%s::text[]) AS given
            JOIN vocab.mesh_term t ON t.name_key = geo.name_key(given)
            JOIN vocab.mesh_descriptor d ON d.ui = t.ui
            ORDER BY given, d.ui
            """,
            (list(names),),
        ).fetchall()


def terms_with_same_words(conn: psycopg.Connection, names: Sequence[str]) -> list[TermMatch]:
    """Descriptors with an entry term of the same words as one of these names, in any order
    ("Type 2 Diabetes Mellitus" and "Diabetes Mellitus, Type 2"), keyed by the words."""
    with conn.cursor(row_factory=class_row(TermMatch)) as cur:
        return cur.execute(
            """
            SELECT DISTINCT ON (given, d.ui) given AS key, d.ui, d.name, t.term
            FROM unnest(%s::text[]) AS given
            JOIN vocab.mesh_term t ON t.words_key = vocab.words_key(given) AND t.words_key <> ''
            JOIN vocab.mesh_descriptor d ON d.ui = t.ui
            ORDER BY given, d.ui, t.term
            """,
            (list(names),),
        ).fetchall()


def terms_with_words(conn: psycopg.Connection, phrase: str, extra: int) -> list[TermMatch]:
    """Descriptors whose own name (not a synonym) begins with the phrase and has at most `extra` more
    words ("diabetes": Diabetes Mellitus, Diabetes Insipidus). Synonyms are left out: MeSH writes
    them inverted too ("Diabetes, Bronze" is hemochromatosis, not a kind of diabetes)."""
    with conn.cursor(row_factory=class_row(TermMatch)) as cur:
        return cur.execute(
            """
            WITH given AS (SELECT geo.name_key(%(phrase)s) AS key)
            SELECT geo.name_key(d.name) AS key, d.ui, d.name, d.name AS term
            FROM given, vocab.mesh_descriptor d
            WHERE given.key <> '' AND geo.name_key(d.name) LIKE given.key || ' %%'
              AND cardinality(string_to_array(geo.name_key(d.name), ' '))
                  <= cardinality(string_to_array(given.key, ' ')) + %(extra)s
            ORDER BY d.ui
            """,
            {"phrase": phrase, "extra": extra},
        ).fetchall()


def tree_numbers(conn: psycopg.Connection, uis: Sequence[str]) -> dict[str, list[str]]:
    """Each descriptor's tree numbers."""
    found = {}
    for ui, numbers in conn.execute(
        "SELECT ui, tree_numbers FROM vocab.mesh_descriptor WHERE ui = ANY(%s) ORDER BY ui", (list(uis),)
    ).fetchall():
        found[ui] = numbers
    return found


def descriptors_at(conn: psycopg.Connection, numbers: Sequence[str]) -> dict[str, str]:
    """Tree number -> the descriptor there."""
    found = {}
    for tree_number, ui in conn.execute(
        "SELECT tree_number, ui FROM vocab.mesh_tree WHERE tree_number = ANY(%s) ORDER BY tree_number", (list(numbers),)
    ).fetchall():
        found[tree_number] = ui
    return found


# a MeSH term this alike to the words of a condition is offered to the AI tags as a candidate
CANDIDATE_SIMILARITY = 0.5


def similar_terms(conn: psycopg.Connection, texts: Sequence[str], limit: int) -> list[TermMatch]:
    """MeSH descriptors with an entry term most like some run of words of these texts (word
    similarity), the most alike first, each descriptor once: the shortlist the AI tags pick from."""
    with conn.cursor(row_factory=class_row(TermMatch)) as cur:
        return cur.execute(
            """
            WITH given AS (SELECT geo.name_key(text) AS key FROM unnest(%(texts)s::text[]) AS text),
            scored AS (
                SELECT t.ui, t.term, max(word_similarity(t.name_key, g.key)) AS score
                FROM vocab.mesh_term t, given g
                WHERE length(t.name_key) >= 4 AND word_similarity(t.name_key, g.key) >= %(least)s
                GROUP BY t.ui, t.term
            ),
            best AS (
                SELECT DISTINCT ON (s.ui) s.ui, s.term, s.score FROM scored s ORDER BY s.ui, s.score DESC, s.term
            )
            SELECT b.term AS key, b.ui, d.name, b.term
            FROM best b JOIN vocab.mesh_descriptor d ON d.ui = b.ui
            ORDER BY b.score DESC, length(b.term) DESC, b.ui
            LIMIT %(limit)s
            """,
            {"texts": list(texts), "least": CANDIDATE_SIMILARITY, "limit": limit},
        ).fetchall()
