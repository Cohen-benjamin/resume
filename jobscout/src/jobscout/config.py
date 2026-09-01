"""Configuration: YAML for what you want, environment for secrets."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from .models import Seniority


class LocationSpec(BaseModel):
    name: str
    radius_km: int = 40
    remote: bool = False


class SearchConfig(BaseModel):
    roles: list[str] = Field(default_factory=list)
    locations: list[LocationSpec] = Field(default_factory=list)
    seniority: Seniority | None = None
    #: Free-text description of what the candidate is after. Fed to the scoring
    #: rubric verbatim, so it captures preferences the structured fields can't.
    intent: str = ""
    max_results_per_query: int = 50

    @field_validator("locations", mode="before")
    @classmethod
    def _coerce_locations(cls, v: Any) -> Any:
        """Allow `locations: [Boston, MA]` shorthand alongside the full form."""
        if isinstance(v, list):
            return [{"name": item} if isinstance(item, str) else item for item in v]
        return v


class FilterConfig(BaseModel):
    exclude_title_keywords: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)
    require_title_keywords: list[str] = Field(default_factory=list)
    min_salary: float | None = None
    max_age_days: int = 30
    #: Drop anything Claude scores below this before it reaches the digest.
    min_fit_score: int = 0


class ScraperConfig(BaseModel):
    enabled: bool = True
    #: Seconds between requests; jittered by +/-50% at call time.
    delay_seconds: float = 7.0
    #: Stop trying after this many consecutive blocks and mark the run degraded.
    max_consecutive_blocks: int = 3
    cache_ttl_days: int = 30
    timeout_seconds: float = 30.0


class SalaryConfig(BaseModel):
    scrapers: ScraperConfig = Field(default_factory=ScraperConfig)
    bls_enabled: bool = True
    h1b_enabled: bool = True
    #: BLS area code for wage lookups. Default is Boston-Cambridge-Nashua NECTA.
    bls_area_code: str = "0071650"
    bls_area_name: str = "Boston-Cambridge-Nashua, MA-NH"


class ModelConfig:
    """Model choices are deliberately not user-configurable knobs in YAML.

    Scoring and extraction run on the same model so their judgements stay
    comparable across a run.
    """

    SCORING = "claude-opus-5"
    EXTRACTION = "claude-opus-5"
    PLAYBOOK = "claude-opus-5"


class ReportConfig(BaseModel):
    top_n: int = 10
    #: Roles below this score are listed compactly without a written brief.
    max_listed: int = 40
    output_path: Path = Path("digest.html")
    subject_template: str = "JobScout: {n} roles worth a look ({date})"


class EmailConfig(BaseModel):
    enabled: bool = True
    to: str = ""
    from_address: str = "JobScout <onboarding@resend.dev>"


class CompanyEntry(BaseModel):
    name: str
    ats: str
    token: str
    #: Optional per-company title filter, for employers with huge boards.
    match_titles: list[str] = Field(default_factory=list)


class Config(BaseModel):
    resume_path: Path = Path("../index.html")
    search: SearchConfig = Field(default_factory=SearchConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    salary: SalaryConfig = Field(default_factory=SalaryConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    companies: list[CompanyEntry] = Field(default_factory=list)
    cache_dir: Path = Path(".jobscout")
    state_path: Path = Path("state/seen.json")

    @classmethod
    def load(cls, path: Path, companies_path: Path | None = None) -> Config:
        data = yaml.safe_load(path.read_text()) or {} if path.exists() else {}

        if companies_path is None:
            companies_path = path.parent / "companies.yaml"
        if companies_path.exists() and not data.get("companies"):
            loaded = yaml.safe_load(companies_path.read_text()) or {}
            data["companies"] = loaded.get("companies", [])

        cfg = cls.model_validate(data)

        # Resolve relative paths against the config file, not the shell's cwd,
        # so `jobscout run` behaves the same from any directory.
        base = path.parent.resolve()
        for attr in ("resume_path", "cache_dir", "state_path"):
            v = getattr(cfg, attr)
            if not v.is_absolute():
                setattr(cfg, attr, (base / v).resolve())
        if not cfg.report.output_path.is_absolute():
            cfg.report.output_path = (base / cfg.report.output_path).resolve()
        return cfg


class Secrets(BaseModel):
    """Read from the environment only. Never persisted, never logged."""

    anthropic_api_key: str = ""
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    resend_api_key: str = ""
    digest_to_email: str = ""
    digest_from_email: str = ""
    bls_api_key: str = ""

    @classmethod
    def from_env(cls) -> Secrets:
        return cls(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            adzuna_app_id=os.getenv("ADZUNA_APP_ID", ""),
            adzuna_app_key=os.getenv("ADZUNA_APP_KEY", ""),
            resend_api_key=os.getenv("RESEND_API_KEY", ""),
            digest_to_email=os.getenv("DIGEST_TO_EMAIL", ""),
            digest_from_email=os.getenv("DIGEST_FROM_EMAIL", ""),
            bls_api_key=os.getenv("BLS_API_KEY", ""),
        )

    @property
    def has_adzuna(self) -> bool:
        return bool(self.adzuna_app_id and self.adzuna_app_key)

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_resend(self) -> bool:
        return bool(self.resend_api_key)
