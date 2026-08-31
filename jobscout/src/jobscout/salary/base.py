"""The SalarySource contract and the query it answers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import SalaryEstimate, Seniority


@dataclass
class SalaryQuery:
    title: str
    company: str
    location: str = ""
    seniority: Seniority = Seniority.UNKNOWN
    soc_code: str | None = None
    #: Full posting text, for sources that read the range out of the listing.
    description: str = ""
    salary_text: str = ""

    def cache_key(self) -> str:
        from ..models import normalize_company, normalize_title

        return "|".join(
            (
                normalize_title(self.title),
                normalize_company(self.company),
                self.location.casefold(),
                str(self.seniority),
            )
        )


@runtime_checkable
class SalarySource(Protocol):
    name: str

    def lookup(self, query: SalaryQuery) -> SalaryEstimate | None:
        """Return an estimate, or None when this source has nothing to say.

        Must not raise. A source that cannot answer returns None; the resolver
        moves on to the next one.
        """
        ...
