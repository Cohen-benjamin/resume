"""Crowd-sourced comp from Glassdoor and Levels.fyi.

**These scrape sites that do not want to be scraped.** Both sit behind Cloudflare
and both prohibit automated access in their terms of service. They are included
because they are the best available signal on what a specific company pays for a
specific role, and they are built to fail quietly:

* Every lookup is cached for 30 days, so a company/title pair is fetched at most
  once a month no matter how often the pipeline runs.
* Requests are strictly sequential with a jittered delay.
* After a few consecutive blocks the circuit opens and the rest of the run skips
  the scrapers entirely rather than hammering a site that is clearly refusing.
* A block is never an error. It marks the run degraded, and the other three
  salary sources carry the digest.

Set ``salary.scrapers.enabled: false`` to switch both off.
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import date

from ..config import ScraperConfig
from ..models import SalaryEstimate, SalarySourceKind, normalize_company
from ..store import Store
from .base import SalaryQuery

log = logging.getLogger(__name__)

_NAMESPACE = "scrape"

#: Signals that we were served a challenge rather than the page.
_BLOCK_MARKERS = (
    "just a moment",
    "attention required",
    "cf-browser-verification",
    "captcha",
    "unusual traffic",
    "access denied",
    "are you a robot",
    "verify you are human",
)


class ScraperBlocked(RuntimeError):
    """The site served a challenge instead of content."""


class BrowserPool:
    """Lazily-started Playwright browser, shared by both scrapers.

    Playwright is an optional dependency. If it isn't installed the scrapers
    simply never produce a result, which is a supported configuration rather
    than an error.
    """

    def __init__(self, config: ScraperConfig) -> None:
        self.config = config
        self._playwright = None
        self._browser = None
        self._unavailable_reason = ""

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    def _ensure(self):
        if self._browser is not None:
            return self._browser
        if self._unavailable_reason:
            return None
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._unavailable_reason = "playwright is not installed (pip install '.[scrapers]')"
            log.info(self._unavailable_reason)
            return None
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            self._unavailable_reason = f"could not launch chromium: {exc}"
            log.info(self._unavailable_reason)
            return None
        return self._browser

    def fetch(self, url: str) -> str:
        """Return page HTML, or raise ScraperBlocked."""
        browser = self._ensure()
        if browser is None:
            raise ScraperBlocked(self._unavailable_reason or "no browser")

        # Jittered spacing. Constant intervals are themselves a bot signal.
        delay = self.config.delay_seconds * random.uniform(0.5, 1.5)
        time.sleep(delay)

        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        try:
            page = context.new_page()
            page.goto(url, timeout=self.config.timeout_seconds * 1000, wait_until="domcontentloaded")
            html = page.content()
        except Exception as exc:  # noqa: BLE001
            raise ScraperBlocked(f"navigation failed: {type(exc).__name__}") from exc
        finally:
            context.close()

        lowered = html[:6000].casefold()
        for marker in _BLOCK_MARKERS:
            if marker in lowered:
                raise ScraperBlocked(f"challenge page ({marker})")
        return html

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass


class _CircuitBreaker:
    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self.consecutive = 0
        self.reason = ""

    @property
    def open(self) -> bool:
        return self.consecutive >= self.threshold

    def record_block(self, reason: str) -> None:
        self.consecutive += 1
        self.reason = reason

    def record_success(self) -> None:
        self.consecutive = 0


class _ScraperBase:
    source_kind: SalarySourceKind
    name: str

    def __init__(self, pool: BrowserPool, store: Store, config: ScraperConfig) -> None:
        self.pool = pool
        self.store = store
        self.config = config
        self.breaker = _CircuitBreaker(config.max_consecutive_blocks)

    def lookup(self, query: SalaryQuery) -> SalaryEstimate | None:
        if not self.config.enabled:
            return None

        cache_key = f"{self.name}:{query.cache_key()}"
        ttl = self.config.cache_ttl_days * 86400
        cached = self.store.get(_NAMESPACE, cache_key, ttl_seconds=ttl)
        if cached is not None:
            return SalaryEstimate.model_validate(cached) if cached else None

        if self.breaker.open:
            return None

        try:
            estimate = self._scrape(query)
        except ScraperBlocked as exc:
            self.breaker.record_block(str(exc))
            log.info("%s blocked for %s: %s", self.name, query.company, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            log.info("%s failed for %s: %s", self.name, query.company, exc)
            return None

        self.breaker.record_success()
        # A confirmed miss is cached as {} so we don't re-fetch a company that
        # genuinely has no data for another 30 days.
        self.store.set(_NAMESPACE, cache_key, estimate.model_dump(mode="json") if estimate else {})
        return estimate

    def _scrape(self, query: SalaryQuery) -> SalaryEstimate | None:
        raise NotImplementedError


class LevelsFyiScraper(_ScraperBase):
    name = "levels_fyi"
    source_kind = SalarySourceKind.LEVELS_FYI

    def _scrape(self, query: SalaryQuery) -> SalaryEstimate | None:
        company = _slug(query.company)
        title = _slug(query.title)
        html = self.pool.fetch(f"https://www.levels.fyi/companies/{company}/salaries/{title}")

        values = _dollar_amounts(html, floor=40_000, ceiling=1_500_000)
        if len(values) < 2:
            return None
        lo, hi = _trimmed_range(values)
        return SalaryEstimate(
            source=self.source_kind,
            low=lo,
            high=hi,
            confidence=0.7,
            as_of=date.today(),
            note="Levels.fyi self-reported total compensation",
            url=f"https://www.levels.fyi/companies/{company}/salaries/{title}",
        )


class GlassdoorScraper(_ScraperBase):
    name = "glassdoor"
    source_kind = SalarySourceKind.GLASSDOOR

    def _scrape(self, query: SalaryQuery) -> SalaryEstimate | None:
        company = _slug(query.company)
        title = _slug(query.title)
        url = f"https://www.glassdoor.com/Salaries/{company}-{title}-salary-SRCH.htm"
        html = self.pool.fetch(url)

        values = _dollar_amounts(html, floor=30_000, ceiling=1_000_000)
        if len(values) < 2:
            return None
        lo, hi = _trimmed_range(values)
        return SalaryEstimate(
            source=self.source_kind,
            low=lo,
            high=hi,
            confidence=0.6,
            as_of=date.today(),
            note="Glassdoor self-reported base pay",
            url=url,
        )


_AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{2,3}\s?[kK]\b)")


def _dollar_amounts(html: str, *, floor: float, ceiling: float) -> list[float]:
    """Pull plausible pay figures out of a page.

    Deliberately structure-agnostic. Both sites rewrite their markup often
    enough that a CSS-selector-based parser would break silently; a numeric
    sweep with a plausibility window degrades to "no data" instead of to a
    wrong number.
    """
    out: list[float] = []
    for match in _AMOUNT.finditer(html):
        raw = match.group(1).replace(",", "").strip()
        multiplier = 1000.0 if raw.lower().endswith("k") else 1.0
        if multiplier > 1:
            raw = raw[:-1].strip()
        try:
            value = float(raw) * multiplier
        except ValueError:
            continue
        if floor <= value <= ceiling:
            out.append(value)
    return sorted(out)


def _trimmed_range(values: list[float]) -> tuple[float, float]:
    """Discard the tails before taking a range.

    Salary pages carry unrelated figures -- other roles, other locations, stock
    totals. Trimming keeps one outlier from widening the band into uselessness.
    """
    if len(values) <= 3:
        return values[0], values[-1]
    trim = max(1, len(values) // 10)
    core = values[trim:-trim]
    return core[0], core[-1]


def _slug(text: str) -> str:
    cleaned = normalize_company(text) if " " in text else text.casefold()
    return re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
