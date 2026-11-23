"""Keeping searches private (ADR-008): what someone searches for can be about their health, and it is in
the address of the search page (/find?q=…). Nothing about a search is stored; the server's access log
records each request without its query string (telemetry.log_config, this module's filter); no request carries a page's address in its Referer,
only the site's origin (Referrer-Policy: strict-origin, set for every page in app.py), and links to a
registry send no Referer at all (rel="noreferrer"); and the search page is neither cached nor indexed.
A reverse proxy in front of the site must leave query strings out of its own logs too (README)."""

import logging


class StripQueryFilter(logging.Filter):
    """Drops the query string and the visitor's address from uvicorn's access log lines:
    '203.0.113.7:51234 - "GET /find?q=diabetes HTTP/1.1" 200' is logged as '- - "GET /find HTTP/1.1" 200'
    (behind the proxy the address is the visitor's own: with a search's line it would say who searched
    when). uvicorn passes (client, method, path with query, HTTP version, status) as the record's args."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            changed = list(args)
            changed[0] = "-"
            changed[2] = args[2].split("?", 1)[0]
            record.args = tuple(changed)
        return True
