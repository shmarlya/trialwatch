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


# Edge cases of a download: the network, the storage, the file


class Storage:
    """CTIS hands out numbered links; the storage answers each request with the next of `answers`
    (a callable taking the request), and remembers which link each request used."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.links_given = 0
        self.used: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "euclinicaltrials.eu":
            self.links_given += 1
            return httpx.Response(200, json={"url": f"{LINK}&n={self.links_given}"})
        self.used.append(request.url.params["n"])
        return self.answers.pop(0)(request)


def whole_file(request=None):
    return httpx.Response(200, content=b"%PDF-1.7 whole\n%%EOF")


def drops_mid_body(request):
    def body():
        yield b"%PDF-1.7 wh"
        raise httpx.ReadError("connection reset by peer", request=request)

    return httpx.Response(200, content=body())


def cut_short(request):
    # the storage says 21 bytes and sends 11: a body cut short
    return httpx.Response(200, headers={"Content-Length": "21"}, content=iter([b"%PDF-1.7 wh"]))


def expired(request):
    return httpx.Response(403, content=b"<Error><Code>AccessDenied</Code><Message>Request has expired</Message></Error>")


def times_out(request):
    raise httpx.ReadTimeout("timed out", request=request)


def test_a_download_is_spooled_and_hashed_on_the_way(monkeypatch):
    import hashlib

    monkeypatch.setattr(ctis_client, "SPOOL_IN_MEMORY_BYTES", 4)  # past it, the spool is a file on disk
    with client(Storage(whole_file)) as c, c.download_document(CT, UUID) as file:
        assert file.content == b"%PDF-1.7 whole\n%%EOF"
        assert file.sha256() == hashlib.sha256(b"%PDF-1.7 whole\n%%EOF").digest()
        assert (file.size, file.head(4), file.tail(5)) == (20, b"%PDF", b"%%EOF")


def test_a_connection_dropped_mid_body_is_tried_again():
    storage = Storage(drops_mid_body, whole_file)
    with client(storage) as c:
        assert c.download_document(CT, UUID).content == b"%PDF-1.7 whole\n%%EOF"
    assert storage.used == ["1", "1"]


def test_a_body_shorter_than_its_content_length_is_tried_again_never_kept():
    storage = Storage(cut_short, whole_file)
    with client(storage) as c:
        assert c.download_document(CT, UUID).content == b"%PDF-1.7 whole\n%%EOF"


def test_a_body_cut_short_every_time_fails():
    with client(Storage(*[cut_short] * 5)) as c, pytest.raises(ctis_client.IncompleteDownload, match="11 of 21"):
        c.download_document(CT, UUID)


def test_an_expired_link_is_replaced_by_a_fresh_one():
    storage = Storage(expired, whole_file)
    with client(storage) as c:
        assert c.download_document(CT, UUID).content == b"%PDF-1.7 whole\n%%EOF"
    assert (storage.links_given, storage.used) == (2, ["1", "2"])


def test_links_that_keep_expiring_fail_after_max_links():
    storage = Storage(*[expired] * ctis_client.MAX_LINKS)
    with client(storage) as c, pytest.raises(ctis_client.LinkUnusable):
        c.download_document(CT, UUID)
    assert storage.links_given == ctis_client.MAX_LINKS


def test_timeouts_on_every_attempt_fail_the_download_with_the_error():
    with client(Storage(*[times_out] * 5)) as c, pytest.raises(httpx.ReadTimeout):
        c.download_document(CT, UUID)


class Clock:
    """A clock that moves `step` seconds each time it is read."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_a_download_past_its_deadline_is_given_up_and_retried_with_a_fresh_link():
    def trickles(request):
        return httpx.Response(200, content=iter([b"%PDF"] * 5))

    storage = Storage(trickles, whole_file)
    # each read of the clock is 4 minutes: the first download passes its 10 minutes after a few chunks
    clock = Clock(step=4 * 60)
    c = CtisClient(min_interval=0, backoff=0, transport=httpx.MockTransport(storage), clock=clock, sleep=lambda s: None)
    with c:
        assert c.download_document(CT, UUID).content == b"%PDF-1.7 whole\n%%EOF"
    assert storage.used == ["1", "2"]


def test_a_rate_limit_waits_as_long_as_retry_after_asks():
    slept = []

    def busy(request):
        return httpx.Response(429, headers={"Retry-After": "7"})

    c = CtisClient(min_interval=0, backoff=0, transport=httpx.MockTransport(Storage(busy, whole_file)),
                   sleep=slept.append)
    with c:
        c.download_document(CT, UUID)
    assert 7 in slept


def test_a_retry_after_longer_than_the_cap_waits_only_the_cap():
    assert ctis_client._retry_after("7") == 7
    assert ctis_client._retry_after("soon") is None
    slept = []

    def far(request):
        return httpx.Response(503, headers={"Retry-After": "86400"})

    c = CtisClient(min_interval=0, backoff=0, transport=httpx.MockTransport(Storage(far, whole_file)),
                   sleep=slept.append)
    with c:
        c.download_document(CT, UUID)
    assert max(slept) == ctis_client.LONGEST_RETRY_AFTER
