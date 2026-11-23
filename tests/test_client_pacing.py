"""The client's reaction to rate limits, against a fake transport (no mint needed)."""
import httpx
import pytest

from nutcheck.client import MintClient, MintUnavailable, RateLimited


def _client(handler):
    c = MintClient("https://mint.test")
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    c.sleeps = []
    c._sleep = c.sleeps.append  # record instead of waiting
    return c


def test_429_waits_a_quiet_minute_then_goes_slow():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(429 if len(calls) == 1 else 200, json={"signatures": []})

    c = _client(handler)
    assert c.swap([], []) == []
    assert any(s >= 60 for s in c.sleeps), "must wait out Nutshell's elastic-expiry window"
    assert c.slow and c.rate_limited == 1 and len(calls) == 2


def test_honours_longer_retry_after():
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(429, headers={"Retry-After": "90"}) if len(n) == 1 else httpx.Response(200, json={})

    c = _client(handler)
    c.info()
    assert 90 in c.sleeps


def test_persistent_429_is_inconclusive_not_a_refusal():
    c = _client(lambda req: httpx.Response(429, text="<h1>429</h1>"))
    with pytest.raises(RateLimited):
        c.swap([], [])
    assert len(c.sleeps) >= len(MintClient.backoff)


def test_post_5xx_is_never_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(502)

    c = _client(handler)
    with pytest.raises(MintUnavailable):
        c.swap([], [])
    assert len(calls) == 1, "a POST that may have been applied must not be sent twice"


def test_timeouts_are_inconclusive_not_crashes(monkeypatch):
    """mint.sharegap.net (run #48): a read timeout crashed the run instead of marking it incomplete."""
    import httpx
    import pytest
    from nutcheck.client import Inconclusive, MintClient, MintTimeout
    c = MintClient("https://mint.example")
    calls = []

    def slow(method, path, body, timeout):
        calls.append(method)
        raise httpx.ReadTimeout("The read operation timed out")
    monkeypatch.setattr(c, "_send", slow)
    with pytest.raises(MintTimeout) as e:
        c.get("/v1/keys")
    assert isinstance(e.value, Inconclusive) and calls == ["GET", "GET"]  # a GET is retried once
    calls.clear()
    with pytest.raises(MintTimeout):
        c.post("/v1/swap", {})
    assert calls == ["POST"]  # a POST is never repeated: the mint may have acted on it

    def refused(method, path, body, timeout):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(c, "_send", refused)
    with pytest.raises(Inconclusive):
        c.get("/v1/info")
