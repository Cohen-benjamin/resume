"""Adzuna job search.

Free developer key, aggregates a large share of US listings, and returns
structured salary fields when the employer supplied them.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from ..http import HttpClient
from ..models import Job
from .base import SearchQuery

log = logging.getLogger(__name__)

_ENDPOINT = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
_PAGE_SIZE = 50


class AdzunaSource:
    name = "adzuna"

    def __init__(
        self,
        http: HttpClient,
        *,
        app_id: str,
        app_key: str,
        country: str = "us",
    ) -> None:
        self.http = http
        self.app_id = app_id
        self.app_key = app_key
        self.country = country

    def search(self, query: SearchQuery) -> list[Job]:
        jobs: list[Job] = []
        pages = max(1, -(-query.max_results // _PAGE_SIZE))  # ceil

        for page in range(1, pages + 1):
            params: dict[str, object] = {
                "app_id": self.app_id,
                "app_key": self.app_key,
                "results_per_page": min(_PAGE_SIZE, query.max_results),
                "what": query.role,
                "max_days_old": query.max_age_days,
                "content-type": "application/json",
            }
            if query.location and not query.remote:
                params["where"] = query.location
                params["distance"] = max(1, round(query.radius_km * 0.621371))  # API wants miles
            if query.min_salary:
                params["salary_min"] = int(query.min_salary)

            url = _ENDPOINT.format(country=self.country, page=page)
            fixture = f"adzuna/{_slug(query.role)}.json"
            try:
                data = self.http.get_json(url, params=params, fixture=fixture)
            except Exception as exc:  # noqa: BLE001 - a dead source must not kill the run
                log.warning("adzuna search failed for %r: %s", query.role, exc)
                break

            results = data.get("results", []) if isinstance(data, dict) else []
            for item in results:
                job = self._to_job(item)
                if job:
                    jobs.append(job)
            if len(results) < _PAGE_SIZE:
                break

        return jobs[: query.max_results]

    def _to_job(self, item: dict) -> Job | None:
        title = (item.get("title") or "").strip()
        company = ((item.get("company") or {}).get("display_name") or "").strip()
        url = item.get("redirect_url") or ""
        if not (title and company and url):
            return None

        location = ((item.get("location") or {}).get("display_name") or "").strip()
        description = _strip_tags(item.get("description") or "")

        salary_text = ""
        lo, hi = item.get("salary_min"), item.get("salary_max")
        if lo or hi:
            # Adzuna predicts a range when the employer didn't state one; that
            # guess must not be mistaken for a posted range downstream.
            predicted = str(item.get("salary_is_predicted", "0")) == "1"
            if not predicted:
                salary_text = f"{lo or ''}-{hi or ''}".strip("-")

        return Job(
            title=title,
            company=company,
            location=location,
            url=url,
            apply_url=url,
            description=description,
            posted_at=_parse_date(item.get("created")),
            source=self.name,
            source_id=str(item.get("id", "")),
            salary_text=salary_text,
            remote="remote" in location.casefold() or "remote" in title.casefold(),
        )


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _strip_tags(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", " ", text).replace("&amp;", "&").strip()


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
