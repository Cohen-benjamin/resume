"""Stage 5b: the short 'how to best get this job' brief.

Written only for the top N roles, because this is the expensive part of a run
and advice about a role you won't apply to is worthless. Batched like the
scoring, against the same cached prefix.

The brief is deliberately specific and short. Generic advice ("tailor your
resume", "network!") is worse than no advice, so the prompt forbids it and the
schema forces every field to name something concrete from the posting.
"""

from __future__ import annotations

import json
import logging

from ..config import Config, ModelConfig
from ..llm import BatchRequest, cached_system, make_client, run_batch
from ..models import Job, Playbook, ResumeProfile
from ..store import Store

log = logging.getLogger(__name__)

_MAX_DESCRIPTION_CHARS = 6000

_SCHEMA = {
    "type": "object",
    "properties": {
        "angle": {
            "type": "string",
            "description": (
                "Two or three sentences: the single strongest case this candidate "
                "can make for this specific role. Name the employer's problem."
            ),
        },
        "lead_with": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
            "description": (
                "Up to 3 specific things from the candidate's background to put "
                "first, each tied to something this posting asks for."
            ),
        },
        "close_gaps": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
            "description": (
                "Up to 3 gaps, each with a concrete action that is realistic "
                "before applying or answerable in an interview."
            ),
        },
        "outreach": {
            "type": "string",
            "description": (
                "Who to contact and what to say, in one or two sentences. Name a "
                "role or team, not a person."
            ),
        },
    },
    "required": ["angle", "lead_with", "close_gaps", "outreach"],
    "additionalProperties": False,
}

_INSTRUCTIONS = """You write a short, specific brief on how one candidate should go \
after one job. The candidate will read this and act on it the same day.

Requirements:

- Every claim must be grounded in the candidate's actual background or in the \
posting's actual text. Never invent experience.
- Be concrete. "Lead with the capacity model that found the bottleneck at \
Raytheon" is useful; "highlight your analytical skills" is not.
- For gaps, give an action, not a diagnosis. "You have not used Minitab -- work \
through their free trial's DOE tutorial and say so plainly" beats "lacks Minitab".
- Outreach names a role and an opening line, not a platitude. If the company is \
small enough that the hiring manager is findable, say to go direct.
- Assume the candidate is competent and busy. No pep talk, no restating the job \
description back to them, no praise.
- If this role is genuinely a stretch, say which single thing would have to go \
right rather than pretending it isn't."""


def write_playbooks(
    jobs: list[Job],
    profile: ResumeProfile,
    config: Config,
    *,
    store: Store,
    api_key: str = "",
    offline: bool = False,
    force: bool = False,
) -> tuple[list[Job], list[str]]:
    """Attach a Playbook to the top N jobs by fit score."""
    problems: list[str] = []

    ranked = sorted(jobs, key=lambda j: j.match.fit_score if j.match else 0, reverse=True)
    top = ranked[: config.report.top_n]
    if not top:
        return jobs, problems

    pending: list[Job] = []
    for job in top:
        cached = None if force else store.get("playbook", _cache_key(job, profile))
        if cached:
            job.playbook = Playbook.model_validate(cached)
        else:
            pending.append(job)

    if not pending:
        return jobs, problems

    if offline or not api_key:
        problems.append(
            "offline: no how-to-land-it briefs were written"
            if offline
            else "ANTHROPIC_API_KEY not set: no how-to-land-it briefs were written"
        )
        return jobs, problems

    system = cached_system(_build_context(profile, config), label=_INSTRUCTIONS)
    requests = [
        BatchRequest(
            custom_id=job.id,
            system=system,
            user=_render_job(job),
            schema=_SCHEMA,
            max_tokens=2000,
        )
        for job in pending
    ]

    try:
        client = make_client(api_key)
        results = run_batch(client, model=ModelConfig.PLAYBOOK, requests=requests)
    except Exception as exc:  # noqa: BLE001
        log.warning("playbook batch failed: %s", exc)
        problems.append(f"briefs unavailable ({type(exc).__name__})")
        return jobs, problems

    for job in pending:
        data = results.get(job.id)
        if not data:
            continue
        job.playbook = Playbook.model_validate(data)
        store.set("playbook", _cache_key(job, profile), job.playbook.model_dump(mode="json"))

    missing = len(pending) - sum(1 for j in pending if j.id in results)
    if missing:
        problems.append(f"{missing} brief(s) could not be written")

    return jobs, problems


def _cache_key(job: Job, profile: ResumeProfile) -> str:
    return f"{profile.source_hash}:{job.id}"


def _build_context(profile: ResumeProfile, config: Config) -> str:
    parts = [
        "<candidate>",
        json.dumps(
            {
                "name": profile.name,
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
        parts += ["", "<what_they_want>", config.search.intent.strip(), "</what_they_want>"]
    return "\n".join(parts)


def _render_job(job: Job) -> str:
    description = job.description[:_MAX_DESCRIPTION_CHARS]
    lines = [
        "<posting>",
        f"Title: {job.title}",
        f"Company: {job.company}",
        f"Location: {job.location}{' (remote)' if job.remote else ''}",
    ]
    if job.salary.best:
        lines.append(f"Pay (from {job.salary.best.source.value}): {job.salary.best.display()}")
    lines += ["", description or "[no description available]", "</posting>"]

    if job.match:
        lines += [
            "",
            "<prior_assessment>",
            f"fit score: {job.match.fit_score}/100 ({job.match.seniority_fit})",
            f"matched: {', '.join(job.match.matched_skills) or 'none identified'}",
            f"gaps: {', '.join(job.match.gaps) or 'none identified'}",
            "</prior_assessment>",
        ]

    lines += ["", "Write the brief for this role."]
    return "\n".join(lines)
