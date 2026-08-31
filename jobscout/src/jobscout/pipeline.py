"""Stage orchestration.

Kept separate from the CLI so each stage can be driven from a test, or from a
scheduled run, without going through argument parsing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import Config, Secrets
from .discover import discover
from .http import HttpClient
from .match.playbook import write_playbooks
from .match.score import score_all
from .models import Job, ResumeProfile, RunResult, RunStats, VerificationStatus
from .profile.extract import build_profile
from .salary.resolve import resolve_all
from .store import SeenLedger, Store
from .verify import verify_all

log = logging.getLogger(__name__)


@dataclass
class Context:
    config: Config
    secrets: Secrets
    http: HttpClient
    store: Store
    offline: bool = False
    force: bool = False
    limit: int | None = None
    problems: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        config: Config,
        *,
        offline: bool = False,
        force: bool = False,
        limit: int | None = None,
        fixture_dir: Path | None = None,
    ) -> Context:
        secrets = Secrets.from_env()
        http = HttpClient(
            cache_dir=config.cache_dir,
            offline=offline,
            fixture_dir=fixture_dir,
        )
        store = Store(config.cache_dir / "cache.db")
        return cls(
            config=config,
            secrets=secrets,
            http=http,
            store=store,
            offline=offline,
            force=force,
            limit=limit,
        )

    def close(self) -> None:
        self.http.close()
        self.store.close()


def run_profile(ctx: Context) -> ResumeProfile:
    return build_profile(
        ctx.config.resume_path,
        store=ctx.store,
        api_key=ctx.secrets.anthropic_api_key,
        force=ctx.force,
        offline=ctx.offline,
    )


def run_discover(ctx: Context, profile: ResumeProfile) -> list[Job]:
    jobs, problems = discover(
        ctx.config,
        ctx.secrets,
        http=ctx.http,
        store=ctx.store,
        seniority=profile.seniority,
        force=ctx.force,
        limit=ctx.limit,
    )
    ctx.problems.extend(problems)
    return jobs


def run_verify(ctx: Context, jobs: list[Job]) -> list[Job]:
    return verify_all(jobs, ctx.config, http=ctx.http, store=ctx.store, force=ctx.force)


def run_salary(
    ctx: Context, jobs: list[Job], profile: ResumeProfile, *, explain: bool = False
) -> tuple[list[Job], bool, str]:
    return resolve_all(
        jobs,
        ctx.config,
        ctx.secrets,
        profile,
        http=ctx.http,
        store=ctx.store,
        explain=explain,
    )


def run_score(ctx: Context, jobs: list[Job], profile: ResumeProfile) -> list[Job]:
    jobs, problems = score_all(
        jobs,
        profile,
        ctx.config,
        store=ctx.store,
        api_key=ctx.secrets.anthropic_api_key,
        offline=ctx.offline,
        force=ctx.force,
    )
    ctx.problems.extend(problems)

    jobs, problems = write_playbooks(
        jobs,
        profile,
        ctx.config,
        store=ctx.store,
        api_key=ctx.secrets.anthropic_api_key,
        offline=ctx.offline,
        force=ctx.force,
    )
    ctx.problems.extend(problems)
    return jobs


def run_all(ctx: Context, *, explain_salary: bool = False) -> RunResult:
    """The whole pipeline. Every stage is cached, so a rerun is cheap."""
    stats = RunStats()

    profile = run_profile(ctx)
    log.info("profile: %s (%s)", profile.headline, profile.seniority)

    jobs = run_discover(ctx, profile)
    stats.discovered = len(jobs)
    stats.deduped = len(jobs)

    jobs = run_verify(ctx, jobs)
    stats.verified_open = sum(1 for j in jobs if j.verification.status == VerificationStatus.OPEN)
    stats.verified_closed = sum(
        1 for j in jobs if j.verification.status == VerificationStatus.CLOSED
    )
    stats.unverified = sum(
        1 for j in jobs if j.verification.status == VerificationStatus.UNVERIFIED
    )

    # Everything after this point costs money or time per job, so closed roles
    # are dropped here rather than being scored and then hidden.
    live = [j for j in jobs if j.verification.status != VerificationStatus.CLOSED]

    live, degraded, reason = run_salary(ctx, live, profile, explain=explain_salary)
    stats.salary_resolved = sum(1 for j in live if j.salary.best is not None)
    stats.salary_degraded = degraded
    stats.salary_degraded_reason = reason

    live = run_score(ctx, live, profile)
    stats.scored = sum(1 for j in live if j.match is not None)

    ledger = SeenLedger(ctx.config.state_path)
    now = datetime.now(UTC)
    for job in live:
        job.is_new = ledger.is_new(job.id)

    stats.errors = list(dict.fromkeys(ctx.problems))

    return RunResult(generated_at=now, profile=profile, jobs=live, stats=stats)


def commit_seen(ctx: Context, result: RunResult) -> None:
    """Record what was shown, after the digest is safely written.

    Deliberately not done during the run: if rendering or sending fails, the
    roles must still count as unseen so the next run surfaces them.
    """
    ledger = SeenLedger(ctx.config.state_path)
    stamp = result.generated_at.date().isoformat()
    for job in result.jobs:
        if job.verification.status == VerificationStatus.OPEN:
            ledger.mark(job.id, stamp)
    ledger.prune()
    ledger.save()
