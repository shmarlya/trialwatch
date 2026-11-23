"""Keeping searches private (ADR-008): what someone searches for can be about their health, and it is in
the address of the search page (/find?q=…). Nothing about a search is stored; the server's access log
records each request without its query string; no request carries a page's address in its Referer,
only the site's origin (Referrer-Policy: strict-origin, set for every page in app.py), and links to a
registry send no Referer at all (rel="noreferrer"); and the search page is neither cached nor indexed.
A reverse proxy in front of the site must leave query strings out of its own logs too (README)."""

import copy
import logging

import uvicorn.config


class StripQueryFilter(logging.Filter):
    """Drops the query string from uvicorn's access log lines: "GET /find?q=diabetes" is logged as
    "GET /find". uvicorn passes (client, method, path with query, HTTP version, status) as the record's args."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            changed = list(args)
            changed[2] = args[2].split("?", 1)[0]
            record.args = tuple(changed)
        return True


def log_config() -> dict:
    """uvicorn's own logging setup with the access log's query strings left out."""
    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    if "filters" not in config:
        config["filters"] = {}
    config["filters"]["strip_query"] = {"()": "trialwatch.web.privacy.StripQueryFilter"}
    access = config["handlers"]["access"]
    if "filters" not in access:
        access["filters"] = []
    access["filters"].append("strip_query")
    return config
