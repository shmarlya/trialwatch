import httpx
import pytest

from trialwatch.sources import ctis_client
from trialwatch.sources.ctis_client import CtisClient, TrialNotFound

CT = "2025-522750-39-00"


def client(handler) -> CtisClient:
    return CtisClient(min_interval=0, backoff=0, transport=httpx.MockTransport(handler))


def test_retrieve_returns_trial():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ctNumber": CT})

    with client(handler) as c:
        assert c.retrieve(CT) == {"ctNumber": CT}
    assert seen[0].url.path == f"/ctis-public-api/retrieve/{CT}"
    assert seen[0].headers["User-Agent"].startswith("TrialWatch/")


def test_empty_body_means_not_found():
    with client(lambda r: httpx.Response(200, json={})) as c, pytest.raises(TrialNotFound):
        c.retrieve(CT)


def test_rejects_malformed_number_without_calling_api():
    def handler(request):
        raise AssertionError("should not be called")

    with client(handler) as c, pytest.raises(ValueError):
        c.retrieve("NCT01234567")


def test_retries_server_errors_then_succeeds():
    responses = iter([httpx.Response(503), httpx.Response(429), httpx.Response(200, json={"ctNumber": CT})])
    with client(lambda r: next(responses)) as c:
        assert c.retrieve(CT) == {"ctNumber": CT}


def test_gives_up_after_max_attempts():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    with client(handler) as c, pytest.raises(httpx.HTTPStatusError):
        c.retrieve(CT)
    assert len(calls) == 5


def test_does_not_retry_client_errors():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403)

    with client(handler) as c, pytest.raises(httpx.HTTPStatusError):
        c.retrieve(CT)
    assert len(calls) == 1


UUID = "c04dae35-287b-4570-9e56-971c5e91ab11"
LINK = "https://bucket.s3.eu-central-1.amazonaws.com/doc?X-Amz-Signature=abc"


def test_download_document_follows_the_presigned_link():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "euclinicaltrials.eu":
            return httpx.Response(200, json={"url": LINK})
        return httpx.Response(
            200, content=b"%PDF-1.7 ...",
            headers={"Content-Type": "application/pdf", "x-amz-meta-filename": "Results.pdf"},
        )

    with client(handler) as c:
        file = c.download_document(CT, UUID)
    assert (file.content, file.media_type, file.file_name) == (b"%PDF-1.7 ...", "application/pdf", "Results.pdf")
    assert seen[0].url.path == f"/ctis-public-api/documents/{CT}/{UUID}/download"
    assert str(seen[1].url) == LINK


def test_download_document_rejects_a_non_https_link():
    with client(lambda r: httpx.Response(200, json={"url": "http://example.org/x"})) as c, pytest.raises(ValueError):
        c.download_document(CT, UUID)


def test_download_document_rejects_a_malformed_uuid_without_calling_api():
    def handler(request):
        raise AssertionError("should not be called")

    with client(handler) as c, pytest.raises(ValueError):
        c.download_document(CT, "../retrieve")


class LinkThen:
    """A handler: CTIS answers with the presigned link, the link with `file_response()`."""

    def __init__(self, file_response) -> None:
        self.file_response = file_response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "euclinicaltrials.eu":
            return httpx.Response(200, json={"url": LINK})
        return self.file_response()


def ten_bytes_said():
    return httpx.Response(200, content=b"%PDF-1.7 .")


def ten_bytes_unsaid():
    # a streamed body without Content-Length: only reading it shows its size
    return httpx.Response(200, content=iter([b"%PDF-", b"1.7 ."]))


def test_download_refuses_a_file_said_to_be_too_large(monkeypatch):
    monkeypatch.setattr(ctis_client, "MAX_DOCUMENT_BYTES", 5)
    with client(LinkThen(ten_bytes_said)) as c, pytest.raises(ValueError, match="too large to download"):
        c.download_document(CT, UUID)


def test_download_stops_reading_a_file_once_too_large(monkeypatch):
    monkeypatch.setattr(ctis_client, "MAX_DOCUMENT_BYTES", 5)
    with client(LinkThen(ten_bytes_unsaid)) as c, pytest.raises(ValueError, match="too large to download"):
        c.download_document(CT, UUID)


def test_download_takes_a_file_up_to_the_limit(monkeypatch):
    monkeypatch.setattr(ctis_client, "MAX_DOCUMENT_BYTES", 10)
    with client(LinkThen(ten_bytes_unsaid)) as c:
        assert c.download_document(CT, UUID).content == b"%PDF-1.7 ."


class BusyOnce:
    """The file's storage answers 503 once, then the file."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> httpx.Response:
        self.calls += 1
        if self.calls == 1:
            return httpx.Response(503)
        return ten_bytes_said()


def test_download_retries_a_busy_file_storage():
    storage = BusyOnce()
    with client(LinkThen(storage)) as c:
        assert c.download_document(CT, UUID).content == b"%PDF-1.7 ."
    assert storage.calls == 2
