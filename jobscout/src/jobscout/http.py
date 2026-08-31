"""Shared HTTP client.

Every outbound request in the app goes through here so that rate limiting,
retries and caching are properties of the system rather than something each
adapter has to remember.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "jobscout/0.1 (personal job search; +https://github.com/Cohen-benjamin/resume)"

#: Minimum seconds between requests to the same host. Public JSON boards are
#: generous; anything not listed gets the default.
_HOST_MIN_INTERVAL: dict[str, float] = {
    "default": 0.25,
    "boards-api.greenhouse.io": 0.5,
    "api.lever.co": 0.5,
    "api.ashbyhq.com": 0.5,
    "api.smartrecruiters.com": 0.5,
    "apply.workable.com": 0.5,
    "api.adzuna.com": 1.0,
    "www.themuse.com": 1.0,
    "api.bls.gov": 1.0,
}

_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class OfflineError(RuntimeError):
    """Raised when a network call is attempted in --offline mode."""


class RateLimiter:
    """Per-host minimum spacing, shared across threads."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        interval = _HOST_MIN_INTERVAL.get(host, _HOST_MIN_INTERVAL["default"])
        with self._lock:
            now = time.monotonic()
            earliest = self._last.get(host, 0.0) + interval
            delay = max(0.0, earliest - now)
            self._last[host] = now + delay
        if delay > 0:
            time.sleep(delay)


class HttpClient:
    """httpx wrapper with retries, per-host rate limiting and an on-disk cache.

    In offline mode every request must be served from the fixture directory or
    the cache; a miss raises rather than silently reaching the network, which is
    what makes the offline test run trustworthy.
    """

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        offline: bool = False,
        fixture_dir: Path | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.offline = offline
        self.fixture_dir = fixture_dir
        self.cache_dir = cache_dir
        self.max_retries = max_retries
        self._limiter = RateLimiter()
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- fixtures -------------------------------------------------------

    def _fixture_path(self, fixture: str) -> Path | None:
        if not self.fixture_dir:
            return None
        p = self.fixture_dir / fixture
        return p if p.exists() else None

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        fixture: str | None = None,
    ) -> Any:
        """GET and parse JSON.

        `fixture` names a file under the fixture directory used in offline mode
        and as a fallback when a live call fails.
        """
        if self.offline:
            p = self._fixture_path(fixture) if fixture else None
            if p is None:
                raise OfflineError(f"offline: no fixture {fixture!r} for {url}")
            return json.loads(p.read_text())

        resp = self.request("GET", url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        allow_status: set[int] | None = None,
    ) -> httpx.Response:
        """Issue a request with rate limiting and retry-with-backoff.

        `allow_status` names statuses that are a real answer rather than a
        failure -- a 404 from an ATS means "this job is gone", which is
        information, not an error.
        """
        if self.offline:
            raise OfflineError(f"offline: refusing live {method} {url}")

        host = urlsplit(url).netloc
        allow_status = allow_status or set()
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._limiter.wait(host)
            try:
                resp = self._client.request(
                    method, url, params=params, headers=headers, json=json_body
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    raise
                self._backoff(attempt)
                continue

            if resp.status_code in allow_status or resp.status_code not in _RETRY_STATUS:
                return resp

            if attempt == self.max_retries:
                return resp

            # Honour Retry-After when the server tells us how long to wait.
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                time.sleep(min(float(retry_after), 60.0))
            else:
                self._backoff(attempt)

        if last_exc:
            raise last_exc
        raise httpx.HTTPError(f"exhausted retries for {url}")

    @staticmethod
    def _backoff(attempt: int) -> None:
        # Full jitter: spreads retries out instead of synchronising them.
        time.sleep(random.uniform(0, min(2.0**attempt, 30.0)))
