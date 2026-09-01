"""The shared HTTP client: offline guarantees and retry behaviour."""

from __future__ import annotations

import httpx
import pytest
import respx

from jobscout.http import HttpClient, OfflineError, RateLimiter

from .helpers import FIXTURES


def test_offline_refuses_live_requests() -> None:
    """This is what makes an offline run trustworthy rather than merely quiet."""
    with HttpClient(offline=True) as client, pytest.raises(OfflineError):
        client.request("GET", "https://example.com")


def test_offline_serves_fixtures() -> None:
    with HttpClient(offline=True, fixture_dir=FIXTURES) as client:
        data = client.get_json("https://api.adzuna.com/anything", fixture="adzuna/industrial-engineer.json")
    assert data["results"]


def test_offline_raises_on_a_missing_fixture() -> None:
    with HttpClient(offline=True, fixture_dir=FIXTURES) as client, pytest.raises(OfflineError):
        client.get_json("https://api.adzuna.com/anything", fixture="adzuna/nope.json")


@respx.mock
def test_retries_transient_failures() -> None:
    route = respx.get("https://example.com/flaky").mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )
    with HttpClient(max_retries=3) as client:
        resp = client.request("GET", "https://example.com/flaky")
    assert resp.status_code == 200
    assert route.call_count == 3


@respx.mock
def test_does_not_retry_a_real_answer() -> None:
    """A 404 from an ATS means the job is gone -- that is information, not a fault."""
    route = respx.get("https://example.com/gone").mock(return_value=httpx.Response(404))
    with HttpClient(max_retries=3) as client:
        resp = client.request("GET", "https://example.com/gone", allow_status={404})
    assert resp.status_code == 404
    assert route.call_count == 1


def test_rate_limiter_spaces_requests_per_host() -> None:
    import time

    limiter = RateLimiter()
    start = time.monotonic()
    limiter.wait("api.adzuna.com")
    limiter.wait("api.adzuna.com")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.9  # the configured interval for this host is 1.0s
