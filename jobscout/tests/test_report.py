"""Rendering: the digest must be self-contained and never silently empty."""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from jobscout.report.render import render_html, render_text, split_jobs


def test_html_is_self_contained(rendered_result, config) -> None:
    """It has to survive an email client, so no external assets at all."""
    html = render_html(rendered_result, config)
    assert "<script" not in html.casefold()
    assert not re.search(r'src=["\']', html)
    assert not re.search(r'<link\b', html, re.IGNORECASE)


def test_html_has_no_unrendered_template_syntax(rendered_result, config) -> None:
    html = render_html(rendered_result, config)
    assert "{{" not in html
    assert "{%" not in html


def test_html_contains_the_brief(rendered_result, config) -> None:
    html = render_html(rendered_result, config)
    assert "How to land it" in html
    assert "Formlabs is scaling Somerville production" in html
    assert "The Raytheon capacity model" in html
    assert "Message the Manufacturing Engineering lead" in html


def test_html_shows_salary_with_its_provenance(rendered_result, config) -> None:
    """A number without a source is not actionable."""
    html = render_html(rendered_result, config)
    assert "$88,000 – $112,000" in html
    assert "posting" in html


def test_html_marks_new_roles(rendered_result, config) -> None:
    html = render_html(rendered_result, config)
    assert html.count(">NEW<") == 1  # only the first job is flagged new


def test_html_parses_and_links_out(rendered_result, config) -> None:
    tree = HTMLParser(render_html(rendered_result, config))
    hrefs = [a.attributes.get("href") for a in tree.css("a")]
    assert "https://boards.greenhouse.io/formlabs/jobs/4501001" in hrefs


def test_unverified_roles_are_counted_but_not_listed(rendered_result, config) -> None:
    """A coverage gap must look like a gap, not like a quiet week."""
    html = render_html(rendered_result, config)
    assert "could not be verified" in html
    top, rest = split_jobs(rendered_result, config)
    assert all(j.verification.status.value == "open" for j in top + rest)


def test_empty_result_explains_itself(rendered_result, config) -> None:
    rendered_result.jobs = []
    html = render_html(rendered_result, config)
    assert "Nothing cleared the bar" in html
    assert "widening" in html or "misconfigured" in html


def test_roles_are_promoted_when_no_briefs_exist(rendered_result, config) -> None:
    """Offline runs write no briefs; the digest must still show the roles."""
    for job in rendered_result.jobs:
        job.playbook = None
    top, _ = split_jobs(rendered_result, config)
    assert top, "roles disappeared from the digest when briefs were unavailable"


def test_text_version_covers_the_same_roles(rendered_result, config) -> None:
    text = render_text(rendered_result, config)
    assert "Industrial Engineer" in text
    assert "Formlabs" in text
    assert "https://boards.greenhouse.io/formlabs/jobs/4501001" in text
    assert "ANGLE:" in text


def test_min_fit_score_filters_the_digest(rendered_result, config) -> None:
    config.filters.min_fit_score = 80
    top, rest = split_jobs(rendered_result, config)
    scores = [j.match.fit_score for j in top + rest if j.match]
    assert all(s >= 80 for s in scores)
    assert len(scores) == 1
