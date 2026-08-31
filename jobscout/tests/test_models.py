"""Normalization and identity -- the basis of deduplication."""

from __future__ import annotations

import pytest

from jobscout.models import (
    SENIORITY_PERCENTILES,
    Seniority,
    normalize_company,
    normalize_title,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Senior Industrial Engineer III (Remote)", "senior industrial engineer"),
        ("Industrial Engineer - Req #12345", "industrial engineer"),
        ("Process Engineer II | Full Time", "process engineer"),
        ("  Manufacturing   Engineer  ", "manufacturing engineer"),
        ("Industrial Engineer (Andover, MA)", "industrial engineer"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Raytheon Technologies, Inc.", "raytheon technologies"),
        ("Formlabs LLC", "formlabs"),
        ("Desktop Metal Corp.", "desktop metal"),
    ],
)
def test_normalize_company(raw: str, expected: str) -> None:
    assert normalize_company(raw) == expected


def test_same_role_from_two_sources_dedupes(job_factory) -> None:
    """The whole point of the fingerprint: one job, one row."""
    a = job_factory(title="Industrial Engineer II", company="RTX Corp", url="https://a")
    b = job_factory(title="Industrial Engineer (Andover)", company="RTX Corporation", url="https://b")
    assert a.id == b.id


def test_different_roles_do_not_collide(job_factory) -> None:
    a = job_factory(title="Industrial Engineer")
    b = job_factory(title="Quality Engineer")
    assert a.id != b.id


def test_same_title_different_company_does_not_collide(job_factory) -> None:
    a = job_factory(company="Formlabs")
    b = job_factory(company="Markforged")
    assert a.id != b.id


def test_seniority_percentiles_ascend() -> None:
    """A more senior band must never sit below a less senior one."""
    order = [Seniority.ENTRY, Seniority.EARLY, Seniority.MID, Seniority.SENIOR]
    lows = [SENIORITY_PERCENTILES[s][0] for s in order]
    assert lows == sorted(lows)
