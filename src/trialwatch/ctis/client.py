"""HTTP client for the CTIS public API (https://euclinicaltrials.eu/ctis-public-api).

The API is public and unauthenticated but undocumented. Be polite: one request at a time,
a minimum gap between requests, and backoff on errors.
"""

import re
import time
from importlib.metadata import version

import httpx

BASE_URL = "https://euclinicaltrials.eu/ctis-public-api"
USER_AGENT = f"TrialWatch/{version('trialwatch')} (+https://ivyar.com)"

RETRY_STATUSES = {429, 500, 502, 503, 504}
CT_NUMBER = re.compile(r"\d{4}-\d{6}-\d{2}-\d{2}")


class TrialNotFound(Exception):
    pass


class CtisClient:
    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        min_interval: float = 1.0,
        max_attempts: int = 5,
        backoff: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )
        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._last_request = 0.0

    def __enter__(self) -> "CtisClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self._http.close()

    def retrieve(self, ct_number: str) -> dict:
        """Full trial record. CTIS answers an unknown number with 200 and `{}`."""
        if not CT_NUMBER.fullmatch(ct_number):
            raise ValueError(f"not an EU CT number: {ct_number!r}")
        data = self._request("GET", f"/retrieve/{ct_number}").json()
        if not data:
            raise TrialNotFound(ct_number)
        return data

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(1, self._max_attempts + 1):
            self._wait_turn()
            try:
                response = self._http.request(method, url, **kwargs)
            except httpx.TransportError:
                if attempt == self._max_attempts:
                    raise
            else:
                if response.status_code not in RETRY_STATUSES or attempt == self._max_attempts:
                    response.raise_for_status()
                    return response
            time.sleep(self._backoff * 2 ** (attempt - 1))
        raise AssertionError("unreachable")

    def _wait_turn(self) -> None:
        wait = self._last_request + self._min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
