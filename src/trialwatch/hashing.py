"""Content hashes of JSON-like records: the same content always gives the same sha256."""

import hashlib
import json


def storable(value: object) -> object:
    """The JSON-like value with every string made storable in Postgres jsonb: NUL characters removed,
    unpaired UTF-16 surrogates (which UTF-8 can't hold) replaced by U+FFFD."""
    if isinstance(value, str):
        text = value.replace("\x00", "")
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(value, dict):
        cleaned = {}
        for name, item in value.items():
            cleaned[storable(name)] = storable(item)
        return cleaned
    if isinstance(value, list):
        items = []
        for item in value:
            items.append(storable(item))
        return items
    return value


def json_hash(record: dict, leave_out: frozenset[str] = frozenset()) -> bytes:
    """sha256 of the record as canonical JSON (sorted keys, fixed separators), without the top-level
    fields in `leave_out` (e.g. a source's fields that change on every fetch)."""
    stable = {}
    for name, value in record.items():
        if name not in leave_out:
            stable[name] = value
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    canonical_bytes = canonical.encode()
    return hashlib.sha256(canonical_bytes).digest()
