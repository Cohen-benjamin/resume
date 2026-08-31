"""The Muse job search.

Free and unauthenticated. Smaller corpus than Adzuna but its listings carry a
structured experience level, which is a useful cross-check on seniority.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from ..http import HttpClient
from ..models import Job, Seniority
from .base import SearchQuery

log = logging.getLogger(__name__)

_ENDPOINT = "https://www.themuse.com/api/public/jobs"
_PAGE_SIZE = 20

#: Words too generic to identify a role. Matching on these alone would make a
#: search for "Process Engineer" return every engineering job on the board.
_GENERIC_WORDS = frozenset(
    {
        "engineer", "engineering", "specialist", "analyst", "associate",
        "manager", "lead", "senior", "junior", "staff", "principal",
        "i", "ii", "iii", "iv", "and", "of", "the",
    }
)

#: The Muse exposes a fixed vocabulary of levels; map ours onto theirs.
_LEVEL_MAP: dict[Seniority, list[str]] = {
    Seniority.INTERN: ["Internship"],
    Seniority.ENTRY: ["Entry Level"],
    Seniority.EARLY: ["Entry Level", "Mid Level"],
    Seniority.MID: ["Mid Level"],
    Seniority.SENIOR: ["Senior Level"],
    Seniority.STAFF: ["Senior Level", "Management"],
}


class MuseSource:
    name = "muse"

    def __init__(self, http: HttpClient, *, seniority: Seniority | None = None) -> None:
        self.http = http
        self.seniority = seniority

    def search(self, query: SearchQuery) -> list[Job]:
        params: dict[str, object] = {"page": 0}
        if query.location and not query.remote:
            params["location"] = query.location
        elif query.remote:
            params["location"] = "Flexible / Remote"
        if self.seniority and self.seniority in _LEVEL_MAP:
            params["level"] = _LEVEL_MAP[self.seniority]

        jobs: list[Job] = []
        pages = max(1, -(-query.max_results // _PAGE_SIZE))
        wanted = _distinctive_words(query.role)

        for page in range(pages):
            params["page"] = page
            fixture = f"muse/{_slug(query.role)}.json"
            try:
                data = self.http.get_json(_ENDPOINT, params=params, fixture=fixture)
            except Exception as exc:  # noqa: BLE001
                log.warning("muse search failed for %r: %s", query.role, exc)
                break

            results = data.get("results", []) if isinstance(data, dict) else []
            for item in results:
                job = self._to_job(item)
                # The Muse has no keyword parameter, so the role filter is
                # applied here rather than server-side.
                if job and _title_matches(job.title, wanted):
                    jobs.append(job)
            if page + 1 >= data.get("page_count", 1):
                break

        return jobs[: query.max_results]

    def _to_job(self, item: dict) -> Job | None:
        title = (item.get("name") or "").strip()
        company = ((item.get("company") or {}).get("name") or "").strip()
        refs = item.get("refs") or {}
        url = refs.get("landing_page") or ""
        if not (title and company and url):
            return None

        locations = [loc.get("name", "") for loc in item.get("locations", [])]
        location = locations[0] if locations else ""
        remote = any("remote" in loc.casefold() or "flexible" in loc.casefold() for loc in locations)

        return Job(
            title=title,
            company=company,
            location=location,
            url=url,
            apply_url=url,
            description=_strip_tags(item.get("contents") or ""),
            posted_at=_parse_date(item.get("publication_date")),
            source=self.name,
            source_id=str(item.get("id", "")),
            remote=remote,
        )


def _distinctive_words(role: str) -> set[str]:
    """The words that actually identify a role.

    Falls back to the full word set when a role is generic all the way through
    ("Engineer"), since something has to be matched on.
    """
    words = {w for w in role.casefold().split() if w}
    distinctive = words - _GENERIC_WORDS
    return distinctive or words


def _title_matches(title: str, wanted: set[str]) -> bool:
    return bool(wanted & set(title.casefold().split()))


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _strip_tags(text: str) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", text)
    for entity, char in (("&amp;", "&"), ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"')):
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
