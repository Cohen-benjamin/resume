"""Core domain models shared across every pipeline stage.

Everything that crosses a stage boundary is one of these, so a stage can be run,
cached, and resumed independently of the ones around it.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Seniority(StrEnum):
    INTERN = "intern"
    ENTRY = "entry"
    EARLY = "early"          # roughly 1-3 years post-grad
    MID = "mid"              # 3-7
    SENIOR = "senior"        # 7-12
    STAFF = "staff"          # 12+
    UNKNOWN = "unknown"


#: Where in the wage distribution a given seniority is expected to land.
#: Used to turn a percentile table (BLS) into a band that means something for
#: this candidate rather than for the occupation as a whole.
SENIORITY_PERCENTILES: dict[Seniority, tuple[int, int]] = {
    Seniority.INTERN: (10, 25),
    Seniority.ENTRY: (10, 25),
    Seniority.EARLY: (25, 50),
    Seniority.MID: (50, 75),
    Seniority.SENIOR: (75, 90),
    Seniority.STAFF: (90, 90),
    Seniority.UNKNOWN: (25, 75),
}


class VerificationStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNVERIFIED = "unverified"


class SalarySourceKind(StrEnum):
    POSTING = "posting"
    LEVELS_FYI = "levels_fyi"
    GLASSDOOR = "glassdoor"
    BLS_OES = "bls_oes"
    H1B_LCA = "h1b_lca"


#: Higher wins when two sources disagree. A range printed in the posting itself is
#: the actual offer band for the actual job, so nothing outranks it.
SALARY_SOURCE_RANK: dict[SalarySourceKind, int] = {
    SalarySourceKind.POSTING: 100,
    SalarySourceKind.LEVELS_FYI: 70,
    SalarySourceKind.GLASSDOOR: 60,
    SalarySourceKind.H1B_LCA: 50,
    SalarySourceKind.BLS_OES: 40,
}


def normalize_title(title: str) -> str:
    """Collapse a job title to a comparable key.

    Strips the noise employers bolt onto otherwise identical roles -- req numbers,
    location suffixes, seniority roman numerals, remote tags -- so the same job
    posted to two boards dedupes to one row.
    """
    t = title.casefold()
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t)              # parenthetical asides
    t = re.sub(r"\b(?:req|requisition|job)\s*#?\s*\d+\b", " ", t)
    t = re.sub(r"[-–—,|/]+", " ", t)
    t = re.sub(r"\b(?:i{1,3}|iv|v|vi{1,3})\b", " ", t)  # level numerals
    t = re.sub(r"\b(?:remote|hybrid|onsite|on site|full time|part time|contract)\b", " ", t)
    t = re.sub(r"[^a-z0-9+ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_company(name: str) -> str:
    """Collapse a company name to a comparable key (drops legal suffixes)."""
    c = name.casefold()
    c = re.sub(r"[^a-z0-9& ]+", " ", c)
    c = re.sub(r"\b(?:inc|llc|ltd|corp|corporation|company|co|plc|gmbh|holdings|group)\b", " ", c)
    return re.sub(r"\s+", " ", c).strip()


class ResumeProfile(BaseModel):
    """Structured view of the candidate, derived once from the resume."""

    name: str = ""
    headline: str = ""
    seniority: Seniority = Seniority.UNKNOWN
    years_experience: float = 0.0
    skills: list[str] = Field(default_factory=list)
    #: Titles worth searching on -- includes adjacent titles the resume supports,
    #: not just ones the candidate has literally held.
    target_title_synonyms: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    #: O*NET/BLS Standard Occupational Classification code, e.g. "17-2112".
    soc_code: str | None = None
    soc_title: str | None = None
    summary: str = ""
    #: Hash of the source resume text; the stage recomputes only when this changes.
    source_hash: str = ""


class SalaryEstimate(BaseModel):
    source: SalarySourceKind
    currency: str = "USD"
    low: float | None = None
    high: float | None = None
    #: 0.0-1.0. Combines source rank with how well the record matched the query.
    confidence: float = 0.5
    period: str = "year"
    as_of: date | None = None
    note: str = ""
    url: str | None = None

    @property
    def midpoint(self) -> float | None:
        vals = [v for v in (self.low, self.high) if v is not None]
        return sum(vals) / len(vals) if vals else None

    def display(self) -> str:
        def fmt(v: float | None) -> str:
            return f"${v:,.0f}" if v is not None else "?"

        if self.low is not None and self.high is not None:
            return f"{fmt(self.low)} – {fmt(self.high)}"
        if self.low is not None:
            return f"{fmt(self.low)}+"
        if self.high is not None:
            return f"up to {fmt(self.high)}"
        return "unknown"


class SalaryResolution(BaseModel):
    """The estimate that won, plus everything that lost, so provenance is visible."""

    best: SalaryEstimate | None = None
    alternates: list[SalaryEstimate] = Field(default_factory=list)
    degraded: bool = False
    degraded_reason: str = ""


class Verification(BaseModel):
    status: VerificationStatus = VerificationStatus.UNVERIFIED
    #: Which mechanism produced the answer, e.g. "greenhouse", "http:404".
    method: str = ""
    checked_at: datetime | None = None
    detail: str = ""


class MatchResult(BaseModel):
    fit_score: int = 0
    matched_skills: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    seniority_fit: str = ""
    verdict: str = ""


class Playbook(BaseModel):
    """The short 'how to best get this job' brief."""

    angle: str = ""
    lead_with: list[str] = Field(default_factory=list)
    close_gaps: list[str] = Field(default_factory=list)
    outreach: str = ""


class Job(BaseModel):
    """A single posting as it moves through the pipeline."""

    id: str = ""
    title: str
    company: str
    location: str = ""
    remote: bool = False
    url: str
    apply_url: str | None = None
    description: str = ""
    posted_at: date | None = None
    #: Which JobSource produced it, e.g. "adzuna".
    source: str = ""
    source_id: str = ""
    salary_text: str = ""

    verification: Verification = Field(default_factory=Verification)
    salary: SalaryResolution = Field(default_factory=SalaryResolution)
    match: MatchResult | None = None
    playbook: Playbook | None = None
    is_new: bool = True

    def model_post_init(self, _context: object) -> None:
        if not self.id:
            self.id = self.fingerprint()

    def fingerprint(self) -> str:
        """Stable identity across sources and runs.

        Deliberately excludes the URL and description: the same role syndicated to
        Adzuna and to the company's own board has different URLs and lightly
        different body text, but it is one job and should be shown once.
        """
        key = "|".join(
            (
                normalize_title(self.title),
                normalize_company(self.company),
                (self.location or "").casefold().strip(),
            )
        )
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @property
    def normalized_title(self) -> str:
        return normalize_title(self.title)

    @property
    def normalized_company(self) -> str:
        return normalize_company(self.company)


class RunStats(BaseModel):
    """Counters surfaced in the digest footer, so coverage gaps are never silent."""

    discovered: int = 0
    deduped: int = 0
    verified_open: int = 0
    verified_closed: int = 0
    unverified: int = 0
    scored: int = 0
    salary_resolved: int = 0
    salary_degraded: bool = False
    salary_degraded_reason: str = ""
    errors: list[str] = Field(default_factory=list)


class RunResult(BaseModel):
    generated_at: datetime
    profile: ResumeProfile
    jobs: list[Job] = Field(default_factory=list)
    stats: RunStats = Field(default_factory=RunStats)
