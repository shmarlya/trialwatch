"""What reading a query looks up, from the database (search/query.py Lookups)."""

import psycopg

from trialwatch.db import geo as geo_db
from trialwatch.db import vocab as vocab_db
from trialwatch.search import concepts


class DatabaseLookups:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def places(self, names: list[str]) -> list:
        return geo_db.places_named(self.conn, names)

    def countries(self, names: list[str]) -> list:
        return geo_db.countries_named(self.conn, names)

    def postcodes(self, code: str) -> list:
        return geo_db.postcode_places(self.conn, code)

    def concepts(self, words: list[str]) -> list[tuple[str, str]]:
        return concepts.for_query(self.conn, words)

    def condition_names(self, names: list[str]) -> list[str]:
        """The names that are a MeSH entry term of a condition (so a word like "marburg" is read as one)."""
        found = []
        for match in vocab_db.terms_named(self.conn, names):
            if match.key not in found:
                found.append(match.key)
        return found
