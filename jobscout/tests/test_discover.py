"""Discovery: sources, filtering and deduplication."""

from __future__ import annotations

from datetime import date, timedelta

from jobscout.config import Secrets
from jobscout.discover import _dedupe, _passes_filters, discover
from jobscout.sources.adzuna import AdzunaSource
from jobscout.sources.base import SearchQuery
from jobscout.sources.muse import MuseSource


def test_adzuna_parses_fixture(offline_http) -> None:
    source = AdzunaSource(offline_http, app_id="x", app_key="y")
    jobs = source.search(SearchQuery(role="Industrial Engineer", location="Boston, MA"))
    assert jobs
    first = next(j for j in jobs if j.company == "Formlabs")
    assert first.title == "Industrial Engineer"
    assert first.source == "adzuna"
    assert "capacity" in first.description.casefold()


def test_adzuna_ignores_predicted_salaries(offline_http) -> None:
    """A predicted range is Adzuna's guess, not the employer's commitment."""
    source = AdzunaSource(offline_http, app_id="x", app_key="y")
    jobs = source.search(SearchQuery(role="Industrial Engineer"))
    amazon = next(j for j in jobs if j.company == "Amazon Robotics")
    assert amazon.salary_text == ""


def test_muse_filters_by_role_keyword(offline_http) -> None:
    """The Muse has no keyword parameter, so filtering happens client-side."""
    source = MuseSource(offline_http)
    jobs = source.search(SearchQuery(role="Process Engineer"))
    assert jobs
    assert all("process" in j.title.casefold() for j in jobs)


def test_dedupe_prefers_the_company_board_copy(job_factory) -> None:
    """The board copy has the canonical URL and the full description."""
    aggregated = job_factory(source="adzuna", description="short snippet")
    from_board = job_factory(source="board:greenhouse", description="the full posting text " * 20)
    assert aggregated.id == from_board.id

    result = _dedupe([aggregated, from_board])
    assert len(result) == 1
    assert result[0].source == "board:greenhouse"


def test_filters_exclude_by_title_word_boundary(config, job_factory) -> None:
    """Excluding 'manager' must not also drop 'Management Systems Engineer'."""
    config.filters.exclude_title_keywords = ["manager"]
    assert not _passes_filters(job_factory(title="Engineering Manager"), config)
    assert _passes_filters(job_factory(title="Management Systems Engineer"), config)


def test_filters_drop_stale_postings(config, job_factory) -> None:
    config.filters.max_age_days = 30
    fresh = job_factory(posted_at=date.today() - timedelta(days=5))
    stale = job_factory(posted_at=date.today() - timedelta(days=90))
    assert _passes_filters(fresh, config)
    assert not _passes_filters(stale, config)


def test_filters_keep_postings_with_no_date(config, job_factory) -> None:
    """Missing a date is not evidence of being stale."""
    config.filters.max_age_days = 30
    assert _passes_filters(job_factory(posted_at=None), config)


def test_end_to_end_discovery_offline(config, offline_http, store) -> None:
    jobs, problems = discover(config, Secrets(), http=offline_http, store=store)

    assert jobs, "offline discovery should find postings in the fixtures"
    assert len(jobs) == len({j.id for j in jobs}), "results must be deduplicated"

    titles = {j.title for j in jobs}
    assert "Senior Industrial Engineer" not in titles, "excluded keyword leaked through"
    assert "Industrial Engineering Manager" not in titles, "excluded keyword leaked through"
    assert not any(j.company == "Stale Corp" for j in jobs), "stale posting leaked through"
    assert problems == [] or all(isinstance(p, str) for p in problems)
