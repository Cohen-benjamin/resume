"""The safety mechanisms around the opt-in scrapers.

These sites block automated access by design. What matters is not that the
scrapers succeed, but that failing is cheap, quiet, and never corrupts the
digest with a wrong number.
"""

from __future__ import annotations

import pytest

from jobscout.config import ScraperConfig
from jobscout.models import SalarySourceKind
from jobscout.salary.base import SalaryQuery
from jobscout.salary.scrapers import (
    BrowserPool,
    LevelsFyiScraper,
    ScraperBlocked,
    _CircuitBreaker,
    _dollar_amounts,
    _trimmed_range,
)


class FakePool:
    """Stands in for Playwright, so the tests never launch a browser."""

    def __init__(self, *, html: str = "", raises: Exception | None = None) -> None:
        self.html = html
        self.raises = raises
        self.calls = 0
        self.unavailable_reason = ""

    def fetch(self, url: str) -> str:
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.html

    def close(self) -> None:
        pass


def config() -> ScraperConfig:
    return ScraperConfig(enabled=True, delay_seconds=0, max_consecutive_blocks=2, cache_ttl_days=30)


def test_circuit_opens_after_repeated_blocks() -> None:
    breaker = _CircuitBreaker(3)
    assert not breaker.open
    for _ in range(3):
        breaker.record_block("challenge page")
    assert breaker.open
    breaker.record_success()
    assert not breaker.open


def test_blocked_scraper_returns_none_and_stops_trying(store) -> None:
    """A blocked site must not be hammered for the rest of the run."""
    pool = FakePool(raises=ScraperBlocked("challenge page"))
    scraper = LevelsFyiScraper(pool, store, config())

    for i in range(6):
        result = scraper.lookup(
            SalaryQuery(title=f"Engineer {i}", company=f"Company {i}")
        )
        assert result is None

    # Two blocks open the circuit; every later lookup short-circuits.
    assert pool.calls == 2


def test_a_block_is_never_cached_as_a_miss(store) -> None:
    """Being blocked must not poison the 30-day cache with 'no data'."""
    blocked = FakePool(raises=ScraperBlocked("challenge page"))
    scraper = LevelsFyiScraper(blocked, store, config())
    query = SalaryQuery(title="Industrial Engineer", company="Formlabs")
    assert scraper.lookup(query) is None

    working = FakePool(html="Median $105,000 and $120,000 and $95,000 reported")
    scraper2 = LevelsFyiScraper(working, store, config())
    assert scraper2.lookup(query) is not None, "a later run was blocked by a cached block"


def test_successful_scrape_is_cached(store) -> None:
    pool = FakePool(html="Median $105,000 and $120,000 and $95,000 reported")
    scraper = LevelsFyiScraper(pool, store, config())
    query = SalaryQuery(title="Industrial Engineer", company="Formlabs")

    first = scraper.lookup(query)
    second = scraper.lookup(query)
    assert first is not None and second is not None
    assert pool.calls == 1, "the second lookup should have come from the cache"
    assert first.source is SalarySourceKind.LEVELS_FYI


def test_disabled_scraper_never_fetches(store) -> None:
    pool = FakePool(html="$100,000 $120,000")
    scraper = LevelsFyiScraper(pool, store, ScraperConfig(enabled=False))
    assert scraper.lookup(SalaryQuery(title="X", company="Y")) is None
    assert pool.calls == 0


def test_page_with_no_usable_figures_yields_nothing(store) -> None:
    pool = FakePool(html="<html><body>No salary data available.</body></html>")
    scraper = LevelsFyiScraper(pool, store, config())
    assert scraper.lookup(SalaryQuery(title="X", company="Y")) is None


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("$95,000 $110,000 $135,000", [95000, 110000, 135000]),
        ("$5,000,000 stock grant", []),          # above the plausible ceiling
        ("$12 shipping fee", []),                # below the floor
        ("$120k and $95k", [95000, 120000]),
    ],
)
def test_amount_extraction_plausibility_window(html: str, expected: list[float]) -> None:
    assert _dollar_amounts(html, floor=40_000, ceiling=1_500_000) == expected


def test_trimming_discards_outliers() -> None:
    """One stray figure must not widen the band into uselessness."""
    values = [50_000] + [100_000] * 20 + [900_000]
    low, high = _trimmed_range(sorted(values))
    assert low == 100_000
    assert high == 100_000


def test_browser_pool_without_playwright_is_not_an_error() -> None:
    """Playwright is optional; its absence is a supported configuration."""
    pool = BrowserPool(config())
    try:
        pool.fetch("https://example.com")
    except ScraperBlocked:
        pass  # expected when chromium can't launch in the test environment
    finally:
        pool.close()
