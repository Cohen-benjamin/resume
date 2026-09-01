"""Verification: the guarantee that a listed role is actually still open."""

from __future__ import annotations

import httpx
import pytest
import respx

from jobscout.http import HttpClient
from jobscout.models import VerificationStatus
from jobscout.sources.ats.detect import detect
from jobscout.verify import _redirected_to_root, verify_all


@pytest.mark.parametrize(
    ("url", "kind", "token", "external_id"),
    [
        ("https://boards.greenhouse.io/formlabs/jobs/4501001", "greenhouse", "formlabs", "4501001"),
        ("https://job-boards.greenhouse.io/markforged/jobs/1234", "greenhouse", "markforged", "1234"),
        (
            "https://jobs.lever.co/desktopmetal/aaaa1111-bbbb-2222-cccc-333344445555",
            "lever",
            "desktopmetal",
            "aaaa1111-bbbb-2222-cccc-333344445555",
        ),
        ("https://jobs.smartrecruiters.com/BigCorp/743999123456", "smartrecruiters", "BigCorp", "743999123456"),
        ("https://apply.workable.com/acme/j/ABCDEF1234/", "workable", "acme", "ABCDEF1234"),
        ("https://boards.greenhouse.io/embed/job_app?for=formlabs&gh_jid=999", "greenhouse", "formlabs", "999"),
    ],
)
def test_detects_ats_from_apply_url(url: str, kind: str, token: str, external_id: str) -> None:
    ref = detect(url)
    assert ref is not None
    assert (ref.kind, ref.token, ref.external_id) == (kind, token, external_id)


@pytest.mark.parametrize("url", ["https://www.adzuna.com/details/123", "", "not a url"])
def test_declines_unknown_apply_urls(url: str) -> None:
    assert detect(url) is None


@pytest.mark.parametrize(
    ("final", "original", "expected"),
    [
        ("https://jobs.lever.co/acme", "https://jobs.lever.co/acme/abc-123", True),
        ("https://x.com/a/b/", "https://x.com/a/b", False),
        ("https://x.com/a/b/c", "https://x.com/a/b/c", False),
        ("https://x.com/jobs/998877", "https://x.com/careers/jobs/998877", False),
        ("https://other.com/", "https://x.com/a/b", False),
    ],
)
def test_redirect_to_board_root(final: str, original: str, expected: bool) -> None:
    assert _redirected_to_root(final, original) is expected


@respx.mock
def test_job_absent_from_board_is_closed(config, store, job_factory) -> None:
    """The stale-aggregator case: Adzuna still lists it, the board does not."""
    respx.get("https://boards-api.greenhouse.io/v1/boards/formlabs/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [{"id": 999, "title": "Other Role",
                                                        "location": {"name": "Somerville, MA"},
                                                        "absolute_url": "https://x", "content": ""}]})
    )
    job = job_factory(url="https://boards.greenhouse.io/formlabs/jobs/4501001")
    with HttpClient() as http:
        verify_all([job], config, http=http, store=store)
    assert job.verification.status is VerificationStatus.CLOSED
    assert job.verification.method == "greenhouse"


@respx.mock
def test_job_present_on_board_is_open(config, store, job_factory) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/formlabs/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [{"id": 4501001, "title": "Industrial Engineer",
                                                         "location": {"name": "Somerville, MA"},
                                                         "absolute_url": "https://x", "content": ""}]})
    )
    job = job_factory(url="https://boards.greenhouse.io/formlabs/jobs/4501001")
    with HttpClient() as http:
        verify_all([job], config, http=http, store=store)
    assert job.verification.status is VerificationStatus.OPEN


@respx.mock
def test_network_failure_is_unverified_never_closed(config, store, job_factory) -> None:
    """The critical safety property: a blip must not delete a live role."""
    respx.get("https://boards-api.greenhouse.io/v1/boards/formlabs/jobs").mock(
        side_effect=httpx.ConnectError("boom")
    )
    respx.get("https://boards.greenhouse.io/formlabs/jobs/4501001").mock(
        side_effect=httpx.ConnectError("boom")
    )
    job = job_factory(url="https://boards.greenhouse.io/formlabs/jobs/4501001")
    with HttpClient(max_retries=0) as http:
        verify_all([job], config, http=http, store=store)
    assert job.verification.status is VerificationStatus.UNVERIFIED


@respx.mock
def test_empty_board_does_not_close_everything(config, store, job_factory) -> None:
    """An empty board means a bad token far more often than a shut-down employer."""
    respx.get("https://boards-api.greenhouse.io/v1/boards/formlabs/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    respx.get("https://boards.greenhouse.io/formlabs/jobs/4501001").mock(
        return_value=httpx.Response(200, text="<html>Industrial Engineer at Formlabs</html>")
    )
    job = job_factory(url="https://boards.greenhouse.io/formlabs/jobs/4501001")
    with HttpClient() as http:
        verify_all([job], config, http=http, store=store)
    assert job.verification.status is not VerificationStatus.CLOSED


@respx.mock
def test_404_apply_url_is_closed(config, store, job_factory) -> None:
    respx.get("https://careers.example.com/jobs/1").mock(return_value=httpx.Response(404))
    job = job_factory(url="https://careers.example.com/jobs/1")
    with HttpClient() as http:
        verify_all([job], config, http=http, store=store)
    assert job.verification.status is VerificationStatus.CLOSED
    assert job.verification.method == "http:404"


@respx.mock
def test_closed_phrase_on_a_200_page_is_closed(config, store, job_factory) -> None:
    respx.get("https://careers.example.com/jobs/2").mock(
        return_value=httpx.Response(
            200, text="<html><body>We are no longer accepting applications.</body></html>"
        )
    )
    job = job_factory(url="https://careers.example.com/jobs/2")
    with HttpClient() as http:
        verify_all([job], config, http=http, store=store)
    assert job.verification.status is VerificationStatus.CLOSED
    assert job.verification.method == "http:phrase"


@respx.mock
def test_bot_wall_is_unverified(config, store, job_factory) -> None:
    """A 403 tells us about the bot defences, not about the job."""
    respx.get("https://careers.example.com/jobs/3").mock(return_value=httpx.Response(403))
    job = job_factory(url="https://careers.example.com/jobs/3")
    with HttpClient() as http:
        verify_all([job], config, http=http, store=store)
    assert job.verification.status is VerificationStatus.UNVERIFIED
