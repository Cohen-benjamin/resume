"""The JobSource contract, and the query that drives it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..models import Job


@dataclass
class SearchQuery:
    """One (role, location) pair to search for."""

    role: str
    location: str = ""
    radius_km: int = 40
    remote: bool = False
    max_results: int = 50
    max_age_days: int = 30
    min_salary: float | None = None
    #: Extra title words that must appear, from filters.require_title_keywords.
    require_keywords: list[str] = field(default_factory=list)

    def cache_key(self) -> str:
        parts = [
            self.role.casefold(),
            self.location.casefold(),
            str(self.radius_km),
            str(self.remote),
            str(self.max_results),
            str(self.max_age_days),
            str(self.min_salary),
        ]
        return "|".join(parts)


@runtime_checkable
class JobSource(Protocol):
    """Anything that can turn a query into postings.

    Implementations must not raise on a bad response: a source that is down
    should cost you its results, not the run. Return an empty list and log.
    """

    name: str

    def search(self, query: SearchQuery) -> list[Job]:
        ...
