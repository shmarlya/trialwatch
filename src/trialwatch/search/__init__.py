"""Patient search (ADR-008): find trials that recruit now near a place, for a condition, from a query
like "diabetes Paris". Each trial's search document is built from its profile at transfer
(index.py), with MeSH concepts its conditions name (concepts.py) and plain-language categories
(categories.py); a query is read by rules and a gazetteer, no model call (query.py)."""
