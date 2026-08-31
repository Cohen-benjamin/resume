from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from jobscout.config import Config
from jobscout.http import HttpClient
from jobscout.models import (
    Job,
    MatchResult,
    Playbook,
    ResumeProfile,
    RunResult,
    RunStats,
    SalaryEstimate,
    SalaryResolution,
    SalarySourceKind,
    Seniority,
    Verification,
    VerificationStatus,
)
from jobscout.store import Store

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "cache.db")
    yield s
    s.close()


@pytest.fixture
def offline_http() -> HttpClient:
    with HttpClient(offline=True, fixture_dir=FIXTURES) as client:
        yield client


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A real config, rooted in a temp dir so tests never touch the repo."""
    shutil.copy(REPO / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(REPO / "companies.example.yaml", tmp_path / "companies.yaml")
    cfg = Config.load(tmp_path / "config.yaml")
    cfg.resume_path = REPO.parent / "index.html"
    cfg.cache_dir = tmp_path / ".jobscout"
    cfg.state_path = tmp_path / "state" / "seen.json"
    cfg.report.output_path = tmp_path / "digest.html"
    cfg.salary.scrapers.enabled = False
    return cfg


@pytest.fixture
def profile() -> ResumeProfile:
    return ResumeProfile(
        name="Benjamin Cohen",
        headline="Industrial Engineer",
        seniority=Seniority.EARLY,
        years_experience=2.0,
        skills=["AutoCAD", "SAP", "SQL", "Arena Simulation", "Capacity Modeling"],
        target_title_synonyms=["Industrial Engineer", "Process Engineer"],
        domains=["aerospace", "manufacturing"],
        education=["B.S. Industrial Engineering, Purdue University"],
        soc_code="17-2112",
        soc_title="Industrial Engineers",
        summary="Early-career IE with capacity modelling and factory layout experience.",
        source_hash="testhash",
    )


def make_job(**overrides) -> Job:
    base = dict(
        title="Industrial Engineer",
        company="Formlabs",
        location="Somerville, MA",
        url="https://boards.greenhouse.io/formlabs/jobs/4501001",
        description="Own capacity planning. AutoCAD and simulation required.",
        posted_at=date.today(),
        source="adzuna",
    )
    base.update(overrides)
    # apply_url is what verification actually fetches, so it follows url unless
    # a test deliberately sets the two to differ.
    base.setdefault("apply_url", base["url"])
    return Job(**base)


@pytest.fixture
def job_factory():
    """Build a Job with sensible defaults; override any field by keyword."""
    return make_job


@pytest.fixture
def rendered_result(profile: ResumeProfile) -> RunResult:
    """A complete result including briefs, which offline runs never produce."""
    job = make_job()
    job.verification = Verification(
        status=VerificationStatus.OPEN,
        method="greenhouse",
        checked_at=datetime.now(UTC),
        detail="job id 4501001 present on greenhouse board formlabs",
    )
    job.salary = SalaryResolution(
        best=SalaryEstimate(
            source=SalarySourceKind.POSTING,
            low=88000,
            high=112000,
            confidence=0.95,
            note="range stated in posting text",
        ),
        alternates=[
            SalaryEstimate(
                source=SalarySourceKind.BLS_OES,
                low=80500,
                high=99380,
                confidence=0.45,
                note="BLS OEWS 25th-50th percentile",
            )
        ],
    )
    job.match = MatchResult(
        fit_score=88,
        matched_skills=["AutoCAD", "capacity modelling"],
        gaps=["No additive manufacturing exposure"],
        seniority_fit="match",
        verdict="Strong fit — your Raytheon capacity models map directly onto this role.",
    )
    job.playbook = Playbook(
        angle="Formlabs is scaling Somerville production and needs bottlenecks found fast.",
        lead_with=["The Raytheon capacity model", "The AutoCAD layout redesign"],
        close_gaps=["No additive experience — read their materials white paper"],
        outreach="Message the Manufacturing Engineering lead on LinkedIn.",
    )
    job.is_new = True

    second = make_job(
        title="Process Engineer",
        company="Vicor",
        location="Andover, MA",
        url="https://jobs.lever.co/vicor/dddd4444",
    )
    second.verification = Verification(status=VerificationStatus.OPEN, method="lever")
    second.match = MatchResult(fit_score=71, verdict="Good fit with a Six Sigma gap.")
    second.is_new = False

    return RunResult(
        generated_at=datetime(2026, 8, 31, 12, 0, tzinfo=UTC),
        profile=profile,
        jobs=[job, second],
        stats=RunStats(
            discovered=9,
            deduped=9,
            verified_open=2,
            verified_closed=1,
            unverified=2,
            scored=2,
            salary_resolved=1,
        ),
    )
