"""HTTP client for the CTIS public API (https://euclinicaltrials.eu/ctis-public-api).

The API is public and unauthenticated but undocumented. Be polite: one request at a time,
a minimum gap between requests, and backoff on errors.
"""

import email.utils
import hashlib
import re
import tempfile
import time
from collections.abc import Callable
from importlib.metadata import version

import httpx

from trialwatch import progress

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
# A download is spooled to a temporary file as it arrives (only SPOOL_IN_MEMORY_BYTES of it in memory),
# and read into memory once, only when it must be stored; a larger file fails as too large.
MAX_DOCUMENT_BYTES = 300 * 1024 * 1024
SPOOL_IN_MEMORY_BYTES = 8 * 1024 * 1024
# The longest one download may take, whatever the server's pace (httpx's timeout bounds each read
# only): DOWNLOAD_BASE_SECONDS, plus a minute for each DOWNLOAD_BYTES_PER_MINUTE the file says it has
DOWNLOAD_BASE_SECONDS = 10 * 60
DOWNLOAD_BYTES_PER_MINUTE = 20 * 1024 * 1024
# A presigned link lives about 5 minutes: when it has expired (the storage answers 403 or 404), or a
# download was too slow or cut short, CTIS is asked for a fresh link, at most this many times in all
MAX_LINKS = 3
# The longest a Retry-After is waited for; a longer one is not worth holding a job for
LONGEST_RETRY_AFTER = 5 * 60
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
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
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
        # injected by tests: what waits, and what tells the time
        self._sleep = sleep
        self._clock = clock

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
        for link_number in range(1, MAX_LINKS + 1):
            link_response = self._request("GET", f"/documents/{ct_number}/{uuid}/download")
            link = link_response.json().get("url")
            if not is_https_link(link):
                raise ValueError(f"CTIS gave no https download link for {ct_number}/{uuid}: {link!r}")
            try:
                return self._download(link)
            except LinkUnusable:
                if link_number == MAX_LINKS:
                    raise
        raise AssertionError("unreachable")

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
            self._sleep_before_retry(attempt, response)
        raise AssertionError("unreachable")

    def _download(self, link: str) -> DownloadedFile:
        """A file from a presigned link, spooled to a temporary file as it arrives and hashed on the way.
        Refused (ValueError) as soon as it is known to be over MAX_DOCUMENT_BYTES. A dropped connection, a
        busy storage or a body cut short is tried again like _request; an expired link or a download
        over its deadline raises LinkUnusable, so the caller asks CTIS for a fresh link."""
        for attempt in range(1, self._max_attempts + 1):
            self._wait_turn()
            is_last_attempt = attempt == self._max_attempts
            busy = None
            try:
                # An absolute URL bypasses base_url; the link is presigned, so no CTIS headers are needed
                with self._http.stream("GET", link, headers={"Accept": "*/*"}) as response:
                    # S3 answers 403 to a link whose signature expired: a fresh link will do. A 404 is
                    # a file the storage no longer holds, which a fresh link to it would not change
                    if response.status_code == 403:
                        raise LinkUnusable("the download link answered 403 (expired?)")
                    if response.status_code in RETRY_STATUSES and not is_last_attempt:
                        busy = response
                    else:
                        response.raise_for_status()
                        return self._spooled(response)
            except (httpx.TransportError, IncompleteDownload):
                if is_last_attempt:
                    raise
            self._sleep_before_retry(attempt, busy)
        raise AssertionError("unreachable")

    def _spooled(self, response: httpx.Response) -> DownloadedFile:
        declared = _declared_size(response)
        if declared is not None and declared > MAX_DOCUMENT_BYTES:
            raise ValueError(f"too large to download: {_megabytes(declared)} (the limit is {_megabytes(MAX_DOCUMENT_BYTES)})")
        seconds = DOWNLOAD_BASE_SECONDS
        if declared is not None:
            seconds += 60 * declared / DOWNLOAD_BYTES_PER_MINUTE
        deadline = self._clock() + seconds
        spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_IN_MEMORY_BYTES, prefix="trialwatch-download-")
        try:
            digest = hashlib.sha256()
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_DOCUMENT_BYTES:
                    raise ValueError(f"too large to download: over {_megabytes(MAX_DOCUMENT_BYTES)}")
                if self._clock() > deadline:
                    raise LinkUnusable(f"the download took over {seconds / 60:.0f} min")
                digest.update(chunk)
                spool.write(chunk)
                # a download that is moving is work moving (the worker's watchdog)
                progress.beat("downloading a document")
            # the bytes received as sent (before any Content-Encoding is undone), as Content-Length counts
            # them; a body httpx had whole already (tests' transports) counts none, so its size stands
            received = response.num_bytes_downloaded or size
            if declared is not None and received != declared:
                raise IncompleteDownload(f"the download ended after {received} of {declared} bytes")
        except BaseException:
            spool.close()
            raise
        return DownloadedFile(
            media_type=response.headers.get("Content-Type"),
            file_name=response.headers.get("x-amz-meta-filename"),
            spool=spool,
            sha256=digest.digest(),
            size=size,
        )

    def _sleep_before_retry(self, attempt: int, response: httpx.Response | None = None) -> None:
        # Exponential backoff: wait twice as long after each failed attempt, or as long as the server
        # asks (Retry-After, in seconds or as a date), up to LONGEST_RETRY_AFTER
        seconds = self._backoff * 2 ** (attempt - 1)
        if response is not None:
            asked = _retry_after(response.headers.get("Retry-After"))
            if asked is not None:
                seconds = max(seconds, min(asked, LONGEST_RETRY_AFTER))
        self._sleep(seconds)

    def _wait_turn(self) -> None:
        # Politeness towards an undocumented public API: at least min_interval between two requests
        earliest_next_request = self._last_request + self._min_interval
        seconds_to_wait = earliest_next_request - self._clock()
        if seconds_to_wait > 0:
            self._sleep(seconds_to_wait)
        self._last_request = self._clock()


class LinkUnusable(Exception):
    """A presigned download link that can't give the file any more: expired, or too slow."""


class IncompleteDownload(Exception):
    """The file's storage ended the body before the size it declared."""


def _declared_size(response: httpx.Response) -> int | None:
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.isdigit():
        return int(declared)
    return None


def _retry_after(value: str | None) -> float | None:
    """Retry-After's seconds, from a number or an HTTP date; None when absent or unreadable."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.0f} MB"
