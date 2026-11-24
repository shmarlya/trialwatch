"""HTTP client for the CTIS public API (https://euclinicaltrials.eu/ctis-public-api).

The API is public and unauthenticated but undocumented. Be polite: one request at a time,
a minimum gap between requests, and backoff on errors.
"""

import re
import time
from importlib.metadata import version

import httpx

# Shared with every source (the snapshot replays them for any adapter)
from trialwatch.sources.registry import DownloadedFile, TrialNotFound

BASE_URL = "https://euclinicaltrials.eu/ctis-public-api"
USER_AGENT = f"TrialWatch/{version('trialwatch')} (+https://github.com/shmarlya/trialwatch)"

RETRY_STATUSES = {429, 500, 502, 503, 504}
CT_NUMBER = re.compile(r"\d{4}-\d{6}-\d{2}-\d{2}")
# CTIS's search answers at most this many rows per query, whatever the page size (seen 2026-11-21)
SEARCH_LIMIT = 10_000
# The search page always sends its three text fields, empty when not used
SEARCH_TEXT_DEFAULTS = {"containAll": "", "containAny": "", "containNot": ""}
# The largest document downloaded: the largest of CTIS's documents seen so far is 47 MB (2026-11-24).
# A download costs the worker about three times the file's size for a moment, so 300 MB fits in its
# 2 GB (compose.coolify.yaml); a larger file fails as too large instead of taking the worker down.
MAX_DOCUMENT_BYTES = 300 * 1024 * 1024
DOCUMENT_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_https_link(link: object) -> bool:
    if not isinstance(link, str):
        return False
    return link.startswith("https://")


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

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self._http.close()

    def retrieve(self, ct_number: str) -> dict:
        """Full trial record. CTIS answers an unknown number with 200 and `{}`."""
        if not CT_NUMBER.fullmatch(ct_number):
            raise ValueError(f"not an EU CT number: {ct_number!r}")
        response = self._request("GET", f"/retrieve/{ct_number}")
        data = response.json()
        if not data:
            raise TrialNotFound(ct_number)
        return data

    def download_document(self, ct_number: str, uuid: str) -> DownloadedFile:
        """One published document (e.g. a results PDF), by the uuid in the trial's `documents[]`.

        Two steps: CTIS answers with a presigned storage link valid for about 5 minutes, and the
        file is fetched from that link straight away.
        """
        if not CT_NUMBER.fullmatch(ct_number):
            raise ValueError(f"not an EU CT number: {ct_number!r}")
        if not DOCUMENT_UUID.fullmatch(uuid):
            raise ValueError(f"not a document uuid: {uuid!r}")
        link_response = self._request("GET", f"/documents/{ct_number}/{uuid}/download")
        link_answer = link_response.json()
        link = link_answer.get("url")
        if not is_https_link(link):
            raise ValueError(f"CTIS gave no https download link for {ct_number}/{uuid}: {link!r}")
        response, content = self._download(link)
        media_type = response.headers.get("Content-Type")
        file_name = response.headers.get("x-amz-meta-filename")
        return DownloadedFile(content=content, media_type=media_type, file_name=file_name)

    def search(self, criteria: dict, page: int, size: int, direction: str = "ASC") -> dict:
        """One page of CTIS's public trial list (the search on euclinicaltrials.eu), sorted by EU CT
        number (ASC or DESC) so paging through it is stable. `criteria` are the search page's own
        fields, e.g. {"status": [8], "hasStudyResults": true}. CTIS answers at most SEARCH_LIMIT rows
        per query (beyond them: no rows, total 0). Returns {"pagination": {...}, "data": [...]}."""
        if page < 1 or size < 1:
            raise ValueError("page and size start at 1")
        if direction not in ("ASC", "DESC"):
            raise ValueError("direction is ASC or DESC")
        search_criteria = dict(SEARCH_TEXT_DEFAULTS)
        for name, value in criteria.items():
            search_criteria[name] = value
        body = {"pagination": {"page": page, "size": size}, "sort": {"property": "ctNumber", "direction": direction},
                "searchCriteria": search_criteria}
        response = self._request("POST", "/search", json=body)
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ValueError(f"CTIS search answered without a list of trials: {str(data)[:200]}")
        return data

    def _request(self, method: str, url: str, headers: dict | None = None, json: dict | None = None) -> httpx.Response:
        for attempt in range(1, self._max_attempts + 1):
            self._wait_turn()
            is_last_attempt = attempt == self._max_attempts
            try:
                response = self._http.request(method, url, headers=headers, json=json)
            except httpx.TransportError:
                # A dropped connection or a timeout is worth another try, unless this was the last one
                if is_last_attempt:
                    raise
                self._sleep_before_retry(attempt)
                continue
            # A rate limit or a server error is worth another try; any other answer is final
            if response.status_code not in RETRY_STATUSES or is_last_attempt:
                response.raise_for_status()
                return response
            self._sleep_before_retry(attempt)
        raise AssertionError("unreachable")

    def _download(self, link: str) -> tuple[httpx.Response, bytes]:
        """A file from a presigned link, read piece by piece so one larger than MAX_DOCUMENT_BYTES is
        refused (ValueError) before it is all in memory; retried like _request."""
        for attempt in range(1, self._max_attempts + 1):
            self._wait_turn()
            is_last_attempt = attempt == self._max_attempts
            try:
                # An absolute URL bypasses base_url; the link is presigned, so no CTIS headers are needed
                with self._http.stream("GET", link, headers={"Accept": "*/*"}) as response:
                    if response.status_code in RETRY_STATUSES and not is_last_attempt:
                        retry = True
                    else:
                        retry = False
                        response.raise_for_status()
                        content = _read_at_most(response, MAX_DOCUMENT_BYTES)
            except httpx.TransportError:
                if is_last_attempt:
                    raise
                retry = True
            if not retry:
                return response, content
            self._sleep_before_retry(attempt)
        raise AssertionError("unreachable")

    def _sleep_before_retry(self, attempt: int) -> None:
        # Exponential backoff: wait twice as long after each failed attempt
        seconds = self._backoff * 2 ** (attempt - 1)
        time.sleep(seconds)

    def _wait_turn(self) -> None:
        # Politeness towards an undocumented public API: at least min_interval between two requests
        earliest_next_request = self._last_request + self._min_interval
        seconds_to_wait = earliest_next_request - time.monotonic()
        if seconds_to_wait > 0:
            time.sleep(seconds_to_wait)
        self._last_request = time.monotonic()


def _read_at_most(response: httpx.Response, limit: int) -> bytes:
    """The body of a streamed response, or ValueError as soon as it is known to be over `limit` bytes."""
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ValueError(f"too large to download: {_megabytes(int(declared))} (the limit is {_megabytes(limit)})")
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            raise ValueError(f"too large to download: over {_megabytes(limit)}")
    return bytes(body)


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.0f} MB"
