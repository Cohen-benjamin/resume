"""Resume text -> structured ResumeProfile, via one Claude call.

Recomputed only when the resume's content hash changes, so this costs one
request the first time and nothing on subsequent runs.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import ModelConfig
from ..llm import make_client, structured_call
from ..models import ResumeProfile, Seniority
from ..store import Store
from .parse import content_hash, extract_text

log = logging.getLogger(__name__)

_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "headline": {"type": "string", "description": "Short professional headline."},
        "seniority": {
            "type": "string",
            "enum": ["intern", "entry", "early", "mid", "senior", "staff", "unknown"],
        },
        "years_experience": {"type": "number"},
        "skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete skills, tools and methods. Normalize names (AutoCAD, SAP, SQL).",
        },
        "target_title_synonyms": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Job titles worth searching for. Include adjacent titles this "
                "background genuinely supports, not only titles already held."
            ),
        },
        "domains": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Industries and problem areas, e.g. aerospace, capacity modeling.",
        },
        "education": {"type": "array", "items": {"type": "string"}},
        "soc_code": {
            "type": "string",
            "description": "Best-fit BLS SOC code, format 00-0000, e.g. 17-2112.",
        },
        "soc_title": {"type": "string"},
        "summary": {
            "type": "string",
            "description": "Two or three sentences a hiring manager would find useful.",
        },
    },
    "required": [
        "name",
        "headline",
        "seniority",
        "years_experience",
        "skills",
        "target_title_synonyms",
        "domains",
        "education",
        "soc_code",
        "soc_title",
        "summary",
    ],
    "additionalProperties": False,
}

_SYSTEM = """You read a resume and produce a structured profile used to search job \
boards and to score how well postings fit this candidate.

Be accurate and specific. Two fields carry most of the weight:

- `target_title_synonyms` drives what gets searched for at all. Include titles \
this background genuinely supports, including adjacent moves, but do not \
inflate seniority: an early-career engineer should not get "Director" here.
- `soc_code` anchors official wage data. Choose the closest BLS Standard \
Occupational Classification code for the candidate's actual occupation.

Judge seniority from time in full-time professional roles, counting internships \
and part-time research separately. Where a resume has inconsistent dates, take \
the most conservative reading."""


def build_profile(
    resume_path: Path,
    *,
    store: Store,
    api_key: str = "",
    force: bool = False,
    offline: bool = False,
) -> ResumeProfile:
    """Load, and only recompute when the resume itself changed."""
    text = extract_text(resume_path)
    digest = content_hash(text)

    cached = store.get("profile", digest)
    if cached and not force:
        log.info("profile cache hit for %s", digest)
        return ResumeProfile.model_validate(cached)

    if offline:
        profile = _heuristic_profile(text, digest)
        log.warning("offline: using heuristic profile, no Claude call")
        return profile

    try:
        client = make_client(api_key)
        data = structured_call(
            client,
            model=ModelConfig.EXTRACTION,
            system=_SYSTEM,
            user=f"<resume>\n{text}\n</resume>",
            schema=_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 - any failure here degrades, never aborts
        # A profile is required for everything downstream, so degrade to the
        # heuristic rather than aborting the run.
        log.warning("profile extraction failed (%s); falling back to heuristics", exc)
        return _heuristic_profile(text, digest)

    data["source_hash"] = digest
    profile = ResumeProfile.model_validate(data)
    store.set("profile", digest, profile.model_dump(mode="json"))
    return profile


#: Coarse headline -> SOC mapping for the offline/fallback path. The model does
#: this properly; this exists so the BLS wage layer still has something to key
#: on when there is no model.
_SOC_BY_KEYWORD: list[tuple[tuple[str, ...], str, str]] = [
    (("industrial engineer", "manufacturing engineer", "process engineer",
      "continuous improvement"), "17-2112", "Industrial Engineers"),
    (("mechanical engineer",), "17-2141", "Mechanical Engineers"),
    (("electrical engineer",), "17-2071", "Electrical Engineers"),
    (("aerospace engineer",), "17-2011", "Aerospace Engineers"),
    (("materials engineer",), "17-2131", "Materials Engineers"),
    (("supply chain", "logistician", "logistics"), "13-1081", "Logisticians"),
    (("operations research", "operations analyst"), "15-2031", "Operations Research Analysts"),
    (("data scientist", "data analyst"), "15-2051", "Data Scientists"),
    (("production manager",), "11-3051", "Industrial Production Managers"),
]


def _guess_soc(headline: str, text: str) -> tuple[str | None, str | None]:
    haystack = f"{headline} {text[:1500]}".casefold()
    for keywords, code, title in _SOC_BY_KEYWORD:
        if any(kw in haystack for kw in keywords):
            return code, title
    return None, None


def _heuristic_profile(text: str, digest: str) -> ResumeProfile:
    """Keyword-only fallback used offline and when the API is unreachable.

    Deliberately crude. It exists so the pipeline is testable end-to-end without
    credentials, not to compete with the model.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    name = lines[0] if lines else ""
    headline = lines[1] if len(lines) > 1 else ""

    known_skills = [
        "AutoCAD", "Inventor", "Arena Simulation", "SAP", "SQL", "Excel", "Python",
        "R", "C", "PHP", "Visio", "Minitab", "Tableau", "Power BI", "Lean",
        "Six Sigma", "Kaizen", "Capacity Modeling", "Value Stream Mapping",
    ]
    lowered = text.casefold()
    skills = [s for s in known_skills if s.casefold() in lowered]

    soc_code, soc_title = _guess_soc(headline, text)

    return ResumeProfile(
        name=name,
        headline=headline,
        seniority=Seniority.EARLY,
        years_experience=2.0,
        skills=skills,
        target_title_synonyms=[headline] if headline else [],
        domains=[],
        education=[line for line in lines if "University" in line or "B.S." in line][:3],
        soc_code=soc_code,
        soc_title=soc_title,
        summary=headline,
        source_hash=digest,
    )
