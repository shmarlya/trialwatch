import httpx
import pytest

from trialwatch.ctis.client import CtisClient, TrialNotFound

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
