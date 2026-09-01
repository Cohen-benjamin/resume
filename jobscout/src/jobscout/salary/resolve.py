"""Stage 4: merge every salary source into one answer, with its provenance.

The rule is that specificity wins. A range the employer published for this exact
opening beats a crowd-sourced average for the company, which beats what the
company filed for similar roles, which beats the occupational average for the
metro. Where a source is blocked or silent, the next one down carries the digest
and the confidence drops visibly rather than the field going blank.
"""

from __future__ import annotations

import logging

from ..config import Config, Secrets
from ..http import HttpClient
from ..models import (
    SALARY_SOURCE_RANK,
    Job,
    ResumeProfile,
    SalaryEstimate,
    SalaryResolution,
    Seniority,
)
from ..store import Store
from .base import SalaryQuery
from .bls import BLSSource
from .from_posting import PostingSalarySource
from .h1b import H1BSource
from .scrapers import BrowserPool, GlassdoorScraper, LevelsFyiScraper

log = logging.getLogger(__name__)


def resolve_all(
    jobs: list[Job],
    config: Config,
    secrets: Secrets,
    profile: ResumeProfile,
    *,
    http: HttpClient,
    store: Store,
    explain: bool = False,
) -> tuple[list[Job], bool, str]:
    """Attach a SalaryResolution to every job.

    Returns the jobs plus whether the run was degraded and why, so the digest
    can say "the scrapers were blocked" instead of quietly showing worse
    numbers.
    """
    seniority = config.search.seniority or profile.seniority or Seniority.UNKNOWN

    sources: list[object] = [PostingSalarySource()]
    pool: BrowserPool | None = None

    if config.salary.scrapers.enabled and not http.offline:
        pool = BrowserPool(config.salary.scrapers)
        sources.append(LevelsFyiScraper(pool, store, config.salary.scrapers))
        sources.append(GlassdoorScraper(pool, store, config.salary.scrapers))

    h1b = H1BSource(store) if config.salary.h1b_enabled else None
    if h1b is not None:
        sources.append(h1b)

    if config.salary.bls_enabled:
        sources.append(
            BLSSource(
                http,
                api_key=secrets.bls_api_key,
                area_code=config.salary.bls_area_code,
                area_name=config.salary.bls_area_name,
            )
        )

    try:
        for job in jobs:
            query = SalaryQuery(
                title=job.title,
                company=job.company,
                location=job.location,
                seniority=seniority,
                soc_code=profile.soc_code,
                description=job.description,
                salary_text=job.salary_text,
            )
            estimates: list[SalaryEstimate] = []
            for source in sources:
                try:
                    estimate = source.lookup(query)
                except Exception as exc:  # noqa: BLE001 - one source must not sink the rest
                    log.info("salary source %s failed: %s", getattr(source, "name", "?"), exc)
                    continue
                if estimate:
                    estimates.append(estimate)

            job.salary = _merge(estimates)
            if explain:
                _explain(job, estimates)
    finally:
        if pool is not None:
            pool.close()

    degraded, reason = _degradation(config, sources, h1b, profile)
    for job in jobs:
        job.salary.degraded = degraded
        job.salary.degraded_reason = reason

    return jobs, degraded, reason


def _merge(estimates: list[SalaryEstimate]) -> SalaryResolution:
    if not estimates:
        return SalaryResolution()

    # Rank first, confidence second. A low-confidence posted range still beats a
    # high-confidence occupational average, because it is about this job.
    ordered = sorted(
        estimates,
        key=lambda e: (SALARY_SOURCE_RANK.get(e.source, 0), e.confidence),
        reverse=True,
    )
    return SalaryResolution(best=ordered[0], alternates=ordered[1:])


def _degradation(
    config: Config, sources: list[object], h1b: H1BSource | None, profile: ResumeProfile
) -> tuple[bool, str]:
    """Explain any reason the salary data this run is worse than it could be."""
    reasons: list[str] = []

    for source in sources:
        breaker = getattr(source, "breaker", None)
        if breaker is not None and breaker.open:
            reasons.append(f"{source.name} blocked ({breaker.reason})")

    pool_reasons = {
        s.pool.unavailable_reason
        for s in sources
        if getattr(s, "pool", None) is not None and s.pool.unavailable_reason
    }
    reasons.extend(sorted(pool_reasons))

    if config.salary.h1b_enabled and h1b is not None and not h1b.is_indexed():
        reasons.append("H-1B data not indexed (run `jobscout fetch-h1b`)")

    if config.salary.bls_enabled and not profile.soc_code:
        reasons.append("no SOC code on the profile, so BLS wage data was skipped")

    return bool(reasons), "; ".join(reasons)


def _explain(job: Job, estimates: list[SalaryEstimate]) -> None:
    print(f"\n{job.title} @ {job.company} ({job.location})")
    if not estimates:
        print("  no source produced an estimate")
        return
    best_id = id(job.salary.best)
    for estimate in sorted(
        estimates, key=lambda e: SALARY_SOURCE_RANK.get(e.source, 0), reverse=True
    ):
        marker = "->" if id(estimate) == best_id else "  "
        print(
            f"  {marker} {estimate.source.value:12s} {estimate.display():>22s} "
            f"conf={estimate.confidence:.2f}  {estimate.note}"
        )
