"""Stage 6: render the digest as HTML and as plain text."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import Config
from ..models import Job, RunResult, VerificationStatus

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _score_colour(job: Job) -> str:
    """Green/amber/grey by fit, so the digest is skimmable without reading."""
    score = job.match.fit_score if job.match else 0
    if score >= 85:
        return "#1e7e34"
    if score >= 70:
        return "#2e7d32"
    if score >= 55:
        return "#b8860b"
    return "#78848f"


def _salary_line(job: Job) -> str:
    return job.salary.best.display() if job.salary.best else "no pay data"


def build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["score_colour"] = _score_colour
    env.globals["salary_line"] = _salary_line
    return env


def split_jobs(result: RunResult, config: Config) -> tuple[list[Job], list[Job]]:
    """Split into the briefed top N and the compact remainder.

    Only verified-open roles are shown. An unverified role is deliberately
    excluded from the listing but counted in the header, so a coverage gap looks
    like a coverage gap rather than a quiet week.
    """
    open_jobs = [j for j in result.jobs if j.verification.status == VerificationStatus.OPEN]
    ranked = sorted(
        open_jobs,
        key=lambda j: (j.match.fit_score if j.match else 0, j.is_new),
        reverse=True,
    )
    ranked = [j for j in ranked if (j.match.fit_score if j.match else 0) >= config.filters.min_fit_score]

    top = [j for j in ranked if j.playbook is not None][: config.report.top_n]
    top_ids = {j.id for j in top}
    rest = [j for j in ranked if j.id not in top_ids][: config.report.max_listed]

    # With no briefs written (offline, or no key) nothing would appear in the
    # top section at all, which reads as "found nothing". Promote by score.
    if not top and ranked:
        top = ranked[: config.report.top_n]
        top_ids = {j.id for j in top}
        rest = [j for j in ranked if j.id not in top_ids][: config.report.max_listed]

    return top, rest


def render_html(result: RunResult, config: Config, *, notes: list[str] | None = None) -> str:
    top, rest = split_jobs(result, config)
    env = build_environment()
    template = env.get_template("digest.html.j2")

    subject = config.report.subject_template.format(
        n=len(top) + len(rest), date=result.generated_at.strftime("%d %b")
    )
    headline = _headline(len(top) + len(rest), result.generated_at)

    return template.render(
        subject=subject,
        headline=headline,
        generated_at=result.generated_at,
        profile=result.profile,
        stats=result.stats,
        top_jobs=top,
        rest_jobs=rest,
        new_count=sum(1 for j in top + rest if j.is_new),
        notes=notes or _collect_notes(result),
    )


def render_text(result: RunResult, config: Config, *, notes: list[str] | None = None) -> str:
    """Plain-text alternative, for mail clients that refuse HTML."""
    top, rest = split_jobs(result, config)
    lines: list[str] = [
        _headline(len(top) + len(rest), result.generated_at),
        result.generated_at.strftime("%A %d %B %Y"),
        "",
        f"{result.stats.discovered} postings searched, {result.stats.deduped} unique, "
        f"{result.stats.verified_open} verified open.",
        "",
    ]

    for index, job in enumerate(top, 1):
        score = job.match.fit_score if job.match else "-"
        lines += [
            f"{index}. {job.title} — {job.company}",
            f"   {job.location}{' (remote)' if job.remote else ''}",
            f"   fit {score}/100 · {_salary_line(job)}"
            + (f" ({job.salary.best.source.value})" if job.salary.best else ""),
            f"   {job.url}",
        ]
        if job.match and job.match.verdict:
            lines.append(f"   {job.match.verdict}")
        if job.playbook:
            if job.playbook.angle:
                lines += ["", f"   ANGLE: {job.playbook.angle}"]
            for item in job.playbook.lead_with:
                lines.append(f"   + lead with: {item}")
            for item in job.playbook.close_gaps:
                lines.append(f"   - close gap: {item}")
            if job.playbook.outreach:
                lines.append(f"   > outreach: {job.playbook.outreach}")
        lines.append("")

    if rest:
        lines += ["ALSO OPEN", ""]
        for job in rest:
            score = job.match.fit_score if job.match else "-"
            lines.append(f"  [{score}] {job.title} — {job.company} · {job.url}")
        lines.append("")

    if not top and not rest:
        lines += ["Nothing cleared the bar this run.", ""]

    active_notes = notes or _collect_notes(result)
    if active_notes:
        lines += ["WORTH KNOWING"] + [f"  - {n}" for n in active_notes] + [""]

    lines.append("Salary figures are estimates from the source named on each role.")
    return "\n".join(lines)


def _headline(count: int, when: datetime) -> str:
    if count == 0:
        return "No new roles cleared the bar"
    if count == 1:
        return "1 role worth a look"
    return f"{count} roles worth a look"


def _collect_notes(result: RunResult) -> list[str]:
    notes = list(result.stats.errors)
    if result.stats.salary_degraded and result.stats.salary_degraded_reason:
        notes.append(f"Salary data degraded: {result.stats.salary_degraded_reason}")
    if result.stats.unverified:
        notes.append(
            f"{result.stats.unverified} posting(s) could not be verified against an "
            "employer site and were left out of the listing."
        )
    return notes


def write(result: RunResult, config: Config) -> tuple[Path, str, str]:
    html = render_html(result, config)
    text = render_text(result, config)
    path = config.report.output_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    path.with_suffix(".txt").write_text(text, encoding="utf-8")
    return path, html, text
