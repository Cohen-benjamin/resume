"""Stage 5a: score every posting against the resume.

One Message Batches request per job, at half price, sharing a cached prompt
prefix that carries the resume profile and the rubric -- so the expensive,
identical part of the prompt is billed once for the batch rather than once per
job. The per-job description is the only thing that varies, and it goes in the
user turn, after the cache breakpoint.
"""

from __future__ import annotations

import json
import logging

from ..config import Config, ModelConfig
from ..llm import BatchRequest, cached_system, make_client, run_batch
from ..models import Job, MatchResult, ResumeProfile, Seniority
from ..store import Store

log = logging.getLogger(__name__)

#: Postings are long and repetitive; the top of one carries the requirements.
_MAX_DESCRIPTION_CHARS = 6000

_SCHEMA = {
    "type": "object",
    "properties": {
        "fit_score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "How well this candidate fits this role.",
        },
        "matched_skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Requirements the resume genuinely evidences.",
        },
        "gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Requirements the resume does not evidence.",
        },
        "seniority_fit": {
            "type": "string",
            "enum": ["under", "match", "over"],
        },
        "verdict": {
            "type": "string",
            "description": "One sentence, concrete, addressed to the candidate.",
        },
    },
    "required": ["fit_score", "matched_skills", "gaps", "seniority_fit", "verdict"],
    "additionalProperties": False,
}

_RUBRIC = """You score how well one candidate fits a job posting. Your scores decide \
which roles the candidate spends their limited application time on, so both \
false positives and false negatives are expensive.

Score 0-100 on this scale:

- 85-100: strong fit. Meets the core requirements and the seniority band; an \
application would be competitive today.
- 70-84: good fit with a real but closeable gap -- one missing tool, or slightly \
light on years.
- 55-69: plausible stretch. Several gaps, or an adjacent-industry move that \
needs the cover letter to do work.
- 30-54: weak. Would need requirements to be soft.
- 0-29: not a fit, or the seniority is wrong in either direction.

Rules:

- Judge against what the resume *evidences*, not against what the title implies. \
A title of "Industrial Engineer" is not evidence of Six Sigma certification.
- Over-seniority is a real mismatch, not a bonus. A role well below the \
candidate's level scores low and `seniority_fit` is "over".
- Transferable experience counts, but say so in `gaps` rather than pretending the \
direct experience exists.
- `matched_skills` and `gaps` must name requirements from *this* posting. Do not \
list generic strengths.
- The `verdict` is one sentence the candidate can act on. No preamble, no \
restating the job title."""


def score_all(
    jobs: list[Job],
    profile: ResumeProfile,
    config: Config,
    *,
    store: Store,
    api_key: str = "",
    offline: bool = False,
    force: bool = False,
) -> tuple[list[Job], list[str]]:
    """Attach a MatchResult to every job."""
    problems: list[str] = []
    pending: list[Job] = []

    for job in jobs:
        cached = None if force else store.get("score", _cache_key(job, profile))
        if cached:
            job.match = MatchResult.model_validate(cached)
        else:
            pending.append(job)

    if not pending:
        return jobs, problems

    if offline or not api_key:
        for job in pending:
            job.match = _heuristic_score(job, profile)
        problems.append(
            "offline: fit scores are keyword-based, not Claude-scored"
            if offline
            else "ANTHROPIC_API_KEY not set: fit scores are keyword-based"
        )
        return jobs, problems

    system = cached_system(_build_context(profile, config), label=_RUBRIC)
    requests = [
        BatchRequest(
            custom_id=job.id,
            system=system,
            user=_render_job(job),
            schema=_SCHEMA,
            max_tokens=1500,
        )
        for job in pending
    ]

    try:
        client = make_client(api_key)
        results = run_batch(client, model=ModelConfig.SCORING, requests=requests)
    except Exception as exc:  # noqa: BLE001 - a failed batch degrades, never aborts
        log.warning("scoring batch failed: %s", exc)
        problems.append(f"Claude scoring unavailable ({type(exc).__name__}); used keyword fallback")
        for job in pending:
            job.match = _heuristic_score(job, profile)
        return jobs, problems

    for job in pending:
        data = results.get(job.id)
        if not data:
            job.match = _heuristic_score(job, profile)
            continue
        job.match = MatchResult.model_validate(data)
        store.set("score", _cache_key(job, profile), job.match.model_dump(mode="json"))

    missing = len(pending) - sum(1 for j in pending if j.id in results)
    if missing:
        problems.append(f"{missing} role(s) fell back to keyword scoring")

    return jobs, problems


def _cache_key(job: Job, profile: ResumeProfile) -> str:
    # Keyed on the profile too: a new resume must invalidate every score.
    return f"{profile.source_hash}:{job.id}"


def _build_context(profile: ResumeProfile, config: Config) -> str:
    """The stable half of the prompt -- identical for every job in the batch."""
    parts = [
        "<candidate>",
        json.dumps(
            {
                "headline": profile.headline,
                "seniority": str(profile.seniority),
                "years_experience": profile.years_experience,
                "skills": profile.skills,
                "domains": profile.domains,
                "education": profile.education,
                "summary": profile.summary,
            },
            indent=2,
        ),
        "</candidate>",
    ]
    if config.search.intent:
        parts += [
            "",
            "<what_they_want>",
            config.search.intent.strip(),
            "</what_they_want>",
            "",
            "Weigh the stated preferences above: a role that fits the resume but "
            "contradicts what they say they want is not a strong fit.",
        ]
    return "\n".join(parts)


def _render_job(job: Job) -> str:
    description = job.description[:_MAX_DESCRIPTION_CHARS]
    if len(job.description) > _MAX_DESCRIPTION_CHARS:
        description += "\n[description truncated]"
    return "\n".join(
        (
            "<posting>",
            f"Title: {job.title}",
            f"Company: {job.company}",
            f"Location: {job.location}{' (remote)' if job.remote else ''}",
            "",
            description or "[no description available]",
            "</posting>",
            "",
            "Score this posting for the candidate.",
        )
    )


def _heuristic_score(job: Job, profile: ResumeProfile) -> MatchResult:
    """Keyword overlap. Used offline and whenever the API is unreachable.

    Crude on purpose: it keeps the pipeline runnable and testable without
    credentials. It is not trying to approximate the model's judgement, and the
    digest says so wherever these scores appear.
    """
    haystack = f"{job.title} {job.description}".casefold()
    matched = [s for s in profile.skills if s.casefold() in haystack]
    title_words = set(job.title.casefold().split())
    synonym_hit = any(
        title_words & set(syn.casefold().split()) for syn in profile.target_title_synonyms
    )

    score = 25
    if profile.skills:
        score += int(45 * len(matched) / max(len(profile.skills), 1))
    if synonym_hit:
        score += 20
    if job.description:
        score += 5

    seniority_fit = "match"
    lowered = job.title.casefold()
    if profile.seniority in (Seniority.EARLY, Seniority.ENTRY) and any(
        w in lowered for w in ("senior", "principal", "staff", "lead", "director", "manager")
    ):
        seniority_fit = "under"
        score = max(0, score - 25)

    return MatchResult(
        fit_score=min(score, 100),
        matched_skills=matched[:8],
        gaps=[],
        seniority_fit=seniority_fit,
        verdict="Keyword match only — no Claude scoring available for this run.",
    )
