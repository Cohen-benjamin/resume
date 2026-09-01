"""The ATS contract.

An ATS client does two jobs, and that is the point: the endpoint that lists a
company's openings is the same endpoint that answers "is this one still open?".
Verification is therefore against the employer's own system of record rather
than against a scraped copy of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ...models import Job


@dataclass
class Posting:
    """One opening as the ATS reports it."""

    external_id: str
    title: str
    location: str
    url: str
    description: str = ""
    salary_text: str = ""
    updated_at: str = ""


@runtime_checkable
class ATSClient(Protocol):
    kind: str

    def list_postings(self, token: str) -> list[Posting]:
        """Every currently-open posting on the board.

        Raises on transport failure. Callers treat a raise as "cannot verify",
        never as "the job is closed" -- a network blip must not silently delete
        a live role from the digest.
        """
        ...


def posting_to_job(posting: Posting, company: str, source: str) -> Job:
    return Job(
        title=posting.title,
        company=company,
        location=posting.location,
        url=posting.url,
        apply_url=posting.url,
        description=posting.description,
        source=source,
        source_id=posting.external_id,
        salary_text=posting.salary_text,
        remote="remote" in posting.location.casefold(),
    )
