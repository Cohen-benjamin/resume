"""Stage 3: is this role actually still open on the company's own site?

Aggregators lag. A posting can sit in Adzuna's index for days after the employer
pulled it, and applying to a closed role is the single most wasteful thing a job
search does. So every posting is re-checked against the employer's own system
before it reaches the digest.

Three outcomes, and the distinction between the last two matters:

* ``open``       -- found on the employer's board, or the apply URL still serves a page
* ``closed``     -- the board no longer lists it, or the page is gone
* ``unverified`` -- we could not tell

A transport failure is always ``unverified``, never ``closed``. Treating a
network blip as a closure would silently delete live roles from the digest,
which is the one failure mode the user would never notice.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from .config import Config
from .http import HttpClient, OfflineError
from .models import Job, Verification, VerificationStatus, normalize_title
from .sources.ats.clients import get_client
from .sources.ats.detect import ATSRef, detect
from .store import Store

log = logging.getLogger(__name__)

_VERIFY_TTL_SECONDS = 3600

#: Phrases that mean "this posting is over" on a page that still returns 200.
_CLOSED_PHRASES = (
    "no longer accepting applications",
    "no longer available",
    "position has been filled",
    "this job is closed",
    "job posting has expired",
    "this posting has expired",
    "we are no longer accepting",
    "position is closed",
    "job has been filled",
    "requisition is closed",
)

#: Statuses that positively mean "gone", as opposed to "something went wrong".
_GONE_STATUSES = {404, 410}


def verify_all(
    jobs: list[Job],
    config: Config,
    *,
    http: HttpClient,
    store: Store,
    force: bool = False,
) -> list[Job]:
    """Attach a Verification to every job. Mutates and returns the list."""
    # Group by board so a company with 12 matching roles costs one fetch, not 12.
    board_cache: dict[tuple[str, str], dict[str, object] | None] = {}
    curated = {
        (e.ats.casefold(), e.token.casefold()): e.name for e in config.companies
    }

    for job in jobs:
        key = f"{job.id}:{job.apply_url or job.url}"
        with store.cached("verify", key, ttl_seconds=_VERIFY_TTL_SECONDS, force=force) as box:
            if box[0] is None:
                result = _verify_one(job, http, board_cache, curated)
                box[0] = result.model_dump(mode="json")
        job.verification = Verification.model_validate(box[0])

    return jobs


def _verify_one(
    job: Job,
    http: HttpClient,
    board_cache: dict[tuple[str, str], dict[str, object] | None],
    curated: dict[tuple[str, str], str],
) -> Verification:
    now = datetime.now(UTC)
    url = job.apply_url or job.url

    # A job discovered by polling a company board was, by definition, on that
    # board this run. Re-fetching it would prove nothing new.
    if job.source.startswith("board:"):
        return Verification(
            status=VerificationStatus.OPEN,
            method=job.source,
            checked_at=now,
            detail="listed on the employer's board during this run",
        )

    ref = detect(url)
    if ref is not None:
        result = _verify_via_ats(job, ref, http, board_cache, now)
        if result is not None:
            return result

    return _verify_via_http(job, url, http, now)


def _verify_via_ats(
    job: Job,
    ref: ATSRef,
    http: HttpClient,
    board_cache: dict[tuple[str, str], dict[str, object] | None],
    now: datetime,
) -> Verification | None:
    """Check the employer's board. Returns None if the board is unusable."""
    client = get_client(ref.kind, http)
    if client is None:
        return None

    cache_key = (ref.kind, ref.token)
    if cache_key not in board_cache:
        try:
            postings = client.list_postings(ref.token)
            board_cache[cache_key] = {
                "ids": {p.external_id for p in postings},
                "titles": {normalize_title(p.title) for p in postings},
                "count": len(postings),
            }
        except OfflineError:
            board_cache[cache_key] = None
        except Exception as exc:  # noqa: BLE001
            log.info("board %s/%s unavailable: %s", ref.kind, ref.token, exc)
            board_cache[cache_key] = None

    board = board_cache[cache_key]
    if board is None:
        return None

    # An empty board is far more likely to be a wrong token than an employer
    # with zero openings, so decline to conclude anything from it.
    if not board["count"]:
        return None

    if ref.external_id and ref.external_id in board["ids"]:
        return Verification(
            status=VerificationStatus.OPEN,
            method=ref.kind,
            checked_at=now,
            detail=f"job id {ref.external_id} present on {ref.kind} board {ref.token}",
        )

    # Without a usable id, fall back to title matching. Employers routinely
    # repost the same role under a new id, and that is still an open role.
    if normalize_title(job.title) in board["titles"]:
        return Verification(
            status=VerificationStatus.OPEN,
            method=f"{ref.kind}:title",
            checked_at=now,
            detail=f"title matched on {ref.kind} board {ref.token}",
        )

    if ref.external_id:
        return Verification(
            status=VerificationStatus.CLOSED,
            method=ref.kind,
            checked_at=now,
            detail=f"job id {ref.external_id} absent from {ref.kind} board {ref.token}",
        )

    return None


def _verify_via_http(job: Job, url: str, http: HttpClient, now: datetime) -> Verification:
    """Last resort: fetch the apply URL and read what comes back."""
    if not url:
        return Verification(
            status=VerificationStatus.UNVERIFIED,
            method="none",
            checked_at=now,
            detail="no apply URL",
        )

    try:
        resp = http.request("GET", url, allow_status=_GONE_STATUSES | {403, 401})
    except OfflineError:
        return Verification(
            status=VerificationStatus.UNVERIFIED,
            method="offline",
            checked_at=now,
            detail="offline mode",
        )
    except Exception as exc:  # noqa: BLE001
        return Verification(
            status=VerificationStatus.UNVERIFIED,
            method="http:error",
            checked_at=now,
            detail=type(exc).__name__,
        )

    if resp.status_code in _GONE_STATUSES:
        return Verification(
            status=VerificationStatus.CLOSED,
            method=f"http:{resp.status_code}",
            checked_at=now,
            detail="apply URL returns gone",
        )

    # A bot wall tells us nothing about the job. Say so rather than guessing.
    if resp.status_code in {401, 403}:
        return Verification(
            status=VerificationStatus.UNVERIFIED,
            method=f"http:{resp.status_code}",
            checked_at=now,
            detail="blocked from reading the page",
        )

    if resp.status_code >= 400:
        return Verification(
            status=VerificationStatus.UNVERIFIED,
            method=f"http:{resp.status_code}",
            checked_at=now,
            detail="unexpected status",
        )

    body = resp.text.casefold()
    for phrase in _CLOSED_PHRASES:
        if phrase in body:
            return Verification(
                status=VerificationStatus.CLOSED,
                method="http:phrase",
                checked_at=now,
                detail=f"page says {phrase!r}",
            )

    # Bounced to the board root instead of the posting: the posting is gone.
    if _redirected_to_root(str(resp.url), url):
        return Verification(
            status=VerificationStatus.CLOSED,
            method="http:redirect",
            checked_at=now,
            detail=f"redirected to {resp.url}",
        )

    return Verification(
        status=VerificationStatus.OPEN,
        method=f"http:{resp.status_code}",
        checked_at=now,
        detail="apply URL still serves the posting",
    )


def _redirected_to_root(final_url: str, original_url: str) -> bool:
    from urllib.parse import urlsplit

    final, original = urlsplit(final_url), urlsplit(original_url)
    if final.netloc != original.netloc:
        return False
    final_depth = len([p for p in final.path.split("/") if p])
    original_depth = len([p for p in original.path.split("/") if p])
    # A real climb up the tree counts; a trailing-slash change does not, since
    # empty segments are filtered out before the depths are compared.
    return final_depth < original_depth and not re.search(r"\d{4,}", final.path)
