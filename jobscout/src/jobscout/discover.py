"""Stage 2: config in, deduplicated candidate postings out."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from .config import Config, Secrets
from .http import HttpClient
from .models import Job, Seniority
from .sources.adzuna import AdzunaSource
from .sources.ats.base import posting_to_job
from .sources.ats.clients import get_client
from .sources.base import SearchQuery
from .sources.muse import MuseSource
from .store import Store

log = logging.getLogger(__name__)

#: Cached search results expire after this long. Long enough that re-running the
#: pipeline after a mid-run failure is free; short enough that a scheduled weekly
#: run always sees fresh listings.
_SEARCH_TTL_SECONDS = 6 * 3600


def discover(
    config: Config,
    secrets: Secrets,
    *,
    http: HttpClient,
    store: Store,
    seniority: Seniority | None = None,
    force: bool = False,
    limit: int | None = None,
) -> tuple[list[Job], list[str]]:
    """Run every configured source and return unique, filtered postings.

    Returns the jobs plus any non-fatal problems worth surfacing in the digest
    footer, so a source that silently returned nothing is visible rather than
    just looking like a quiet week.
    """
    problems: list[str] = []
    raw: list[Job] = []

    queries = _build_queries(config)
    log.info("built %d queries across %d roles", len(queries), len(config.search.roles))

    sources: list[object] = []
    if secrets.has_adzuna or http.offline:
        sources.append(
            AdzunaSource(http, app_id=secrets.adzuna_app_id, app_key=secrets.adzuna_app_key)
        )
    else:
        problems.append("Adzuna skipped: ADZUNA_APP_ID/ADZUNA_APP_KEY not set")
    sources.append(MuseSource(http, seniority=seniority or config.search.seniority))

    for source in sources:
        for query in queries:
            key = f"{source.name}:{query.cache_key()}"
            with store.cached("search", key, ttl_seconds=_SEARCH_TTL_SECONDS, force=force) as box:
                if box[0] is None:
                    found = source.search(query)
                    box[0] = [j.model_dump(mode="json") for j in found]
            raw.extend(Job.model_validate(d) for d in box[0])

    board_jobs, board_problems = _poll_company_boards(config, http, store, force=force)
    raw.extend(board_jobs)
    problems.extend(board_problems)

    log.info("collected %d raw postings", len(raw))
    unique = _dedupe(raw)
    kept = [j for j in unique if _passes_filters(j, config)]
    log.info("%d unique, %d after filters", len(unique), len(kept))

    if limit:
        kept = kept[:limit]
    return kept, problems


def _build_queries(config: Config) -> list[SearchQuery]:
    """The cartesian product of roles and locations."""
    roles = config.search.roles or []
    locations = config.search.locations or []
    queries: list[SearchQuery] = []

    for role in roles:
        if not locations:
            queries.append(
                SearchQuery(
                    role=role,
                    max_results=config.search.max_results_per_query,
                    max_age_days=config.filters.max_age_days,
                    min_salary=config.filters.min_salary,
                )
            )
            continue
        for loc in locations:
            queries.append(
                SearchQuery(
                    role=role,
                    location="" if loc.remote else loc.name,
                    radius_km=loc.radius_km,
                    remote=loc.remote,
                    max_results=config.search.max_results_per_query,
                    max_age_days=config.filters.max_age_days,
                    min_salary=config.filters.min_salary,
                )
            )
    return queries


def _poll_company_boards(
    config: Config, http: HttpClient, store: Store, *, force: bool = False
) -> tuple[list[Job], list[str]]:
    """Poll each curated employer's board directly.

    This is how a company that never syndicates to an aggregator still shows up.
    """
    jobs: list[Job] = []
    problems: list[str] = []

    for entry in config.companies:
        client = get_client(entry.ats, http)
        if client is None:
            problems.append(f"{entry.name}: unknown ATS {entry.ats!r}")
            continue

        key = f"{entry.ats}:{entry.token}"
        try:
            with store.cached("board", key, ttl_seconds=_SEARCH_TTL_SECONDS, force=force) as box:
                if box[0] is None:
                    box[0] = [p.__dict__ for p in client.list_postings(entry.token)]
        except Exception as exc:  # noqa: BLE001 - one dead board must not kill discovery
            problems.append(f"{entry.name}: board poll failed ({type(exc).__name__})")
            log.warning("board poll failed for %s: %s", entry.name, exc)
            continue

        from .sources.ats.base import Posting

        for data in box[0]:
            posting = Posting(**data)
            if entry.match_titles and not _title_matches(posting.title, entry.match_titles):
                continue
            jobs.append(posting_to_job(posting, entry.name, f"board:{entry.ats}"))

    return jobs, problems


def _title_matches(title: str, keywords: list[str]) -> bool:
    lowered = title.casefold()
    return any(kw.casefold() in lowered for kw in keywords)


def _dedupe(jobs: list[Job]) -> list[Job]:
    """Collapse duplicates, preferring the copy with the most information.

    A posting found on the company's own board beats the aggregator's copy of
    it: the URL is canonical and the description is the full text rather than a
    truncated snippet.
    """
    best: dict[str, Job] = {}
    for job in jobs:
        existing = best.get(job.id)
        if existing is None or _richness(job) > _richness(existing):
            best[job.id] = job
    return list(best.values())


def _richness(job: Job) -> tuple[int, int, int]:
    return (
        1 if job.source.startswith("board:") else 0,
        len(job.description),
        1 if job.salary_text else 0,
    )


def _passes_filters(job: Job, config: Config) -> bool:
    f = config.filters
    title = job.title.casefold()

    if f.require_title_keywords and not any(k.casefold() in title for k in f.require_title_keywords):
        return False
    # Word-boundary match, so excluding "manager" doesn't also drop
    # "Engineering Management Systems".
    for keyword in f.exclude_title_keywords:
        if re.search(rf"\b{re.escape(keyword.casefold())}\b", title):
            return False
    if any(c.casefold() in job.company.casefold() for c in f.exclude_companies):
        return False
    if f.max_age_days and job.posted_at:
        cutoff = date.today() - timedelta(days=f.max_age_days)
        return job.posted_at >= cutoff
    return True
