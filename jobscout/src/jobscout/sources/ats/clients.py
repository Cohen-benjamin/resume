"""Concrete ATS clients.

Each of these hits the same public JSON endpoint the employer's own careers page
uses, so no authentication and no scraping is involved.
"""

from __future__ import annotations

import logging
import re

from ...http import HttpClient
from .base import Posting

log = logging.getLogger(__name__)


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    for entity, char in (
        ("&amp;", "&"), ("&nbsp;", " "), ("&#39;", "'"),
        ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">"),
    ):
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()


class GreenhouseClient:
    kind = "greenhouse"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def list_postings(self, token: str) -> list[Posting]:
        data = self.http.get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
            params={"content": "true"},
            fixture=f"ats/greenhouse-{token}.json",
        )
        out = []
        for job in data.get("jobs", []):
            out.append(
                Posting(
                    external_id=str(job.get("id", "")),
                    title=job.get("title", ""),
                    location=(job.get("location") or {}).get("name", ""),
                    url=job.get("absolute_url", ""),
                    description=_strip_tags(job.get("content", "")),
                    updated_at=job.get("updated_at", ""),
                )
            )
        return out


class LeverClient:
    kind = "lever"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def list_postings(self, token: str) -> list[Posting]:
        data = self.http.get_json(
            f"https://api.lever.co/v0/postings/{token}",
            params={"mode": "json"},
            fixture=f"ats/lever-{token}.json",
        )
        out = []
        for job in data if isinstance(data, list) else []:
            cats = job.get("categories") or {}
            out.append(
                Posting(
                    external_id=str(job.get("id", "")),
                    title=job.get("text", ""),
                    location=cats.get("location", ""),
                    url=job.get("hostedUrl", ""),
                    description=_strip_tags(job.get("descriptionPlain") or job.get("description", "")),
                    salary_text=(job.get("salaryRange") or {}).get("text", "") if isinstance(job.get("salaryRange"), dict) else "",
                    updated_at=str(job.get("createdAt", "")),
                )
            )
        return out


class AshbyClient:
    kind = "ashby"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def list_postings(self, token: str) -> list[Posting]:
        data = self.http.get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{token}",
            params={"includeCompensation": "true"},
            fixture=f"ats/ashby-{token}.json",
        )
        out = []
        for job in data.get("jobs", []):
            comp = job.get("compensation") or {}
            summary = comp.get("compensationTierSummary", "") if isinstance(comp, dict) else ""
            out.append(
                Posting(
                    external_id=str(job.get("id", "")),
                    title=job.get("title", ""),
                    location=job.get("location", ""),
                    url=job.get("jobUrl", ""),
                    description=_strip_tags(job.get("descriptionPlain") or ""),
                    salary_text=summary or "",
                    updated_at=job.get("publishedAt", ""),
                )
            )
        return out


class SmartRecruitersClient:
    kind = "smartrecruiters"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def list_postings(self, token: str) -> list[Posting]:
        out: list[Posting] = []
        offset = 0
        while True:
            data = self.http.get_json(
                f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
                params={"limit": 100, "offset": offset},
                fixture=f"ats/smartrecruiters-{token}.json",
            )
            content = data.get("content", [])
            for job in content:
                loc = job.get("location") or {}
                city = loc.get("city", "")
                region = loc.get("region", "")
                out.append(
                    Posting(
                        external_id=str(job.get("id", "")),
                        title=job.get("name", ""),
                        location=", ".join(p for p in (city, region) if p),
                        url=(job.get("ref") or {}).get("jobAd", "")
                        or f"https://jobs.smartrecruiters.com/{token}/{job.get('id','')}",
                        updated_at=job.get("releasedDate", ""),
                    )
                )
            offset += len(content)
            # The fixture path is offset-independent, so offline mode would loop
            # forever without this guard.
            if not content or offset >= data.get("totalFound", 0) or self.http.offline:
                break
        return out


class WorkableClient:
    kind = "workable"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def list_postings(self, token: str) -> list[Posting]:
        data = self.http.get_json(
            f"https://apply.workable.com/api/v1/widget/accounts/{token}",
            params={"details": "true"},
            fixture=f"ats/workable-{token}.json",
        )
        out = []
        for job in data.get("jobs", []):
            out.append(
                Posting(
                    external_id=str(job.get("shortcode", "")),
                    title=job.get("title", ""),
                    location=", ".join(
                        p for p in (job.get("city", ""), job.get("state", "")) if p
                    ),
                    url=job.get("url", "") or job.get("application_url", ""),
                    description=_strip_tags(job.get("description", "")),
                    updated_at=job.get("published_on", ""),
                )
            )
        return out


CLIENTS: dict[str, type] = {
    "greenhouse": GreenhouseClient,
    "lever": LeverClient,
    "ashby": AshbyClient,
    "smartrecruiters": SmartRecruitersClient,
    "workable": WorkableClient,
}


def get_client(kind: str, http: HttpClient):
    """Instantiate the client for an ATS kind, or None if unrecognized."""
    cls = CLIENTS.get(kind.casefold())
    return cls(http) if cls else None
