"""Salary extraction and the precedence rules that merge sources."""

from __future__ import annotations

import pytest

from jobscout.models import SALARY_SOURCE_RANK, SalaryEstimate, SalarySourceKind, Seniority
from jobscout.salary.base import SalaryQuery
from jobscout.salary.bls import BLSSource
from jobscout.salary.from_posting import PostingSalarySource
from jobscout.salary.h1b import H1BSource, build_index
from jobscout.salary.resolve import _merge


def parse(text: str) -> SalaryEstimate | None:
    return PostingSalarySource().lookup(
        SalaryQuery(title="Industrial Engineer", company="Acme", description=text)
    )


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [
        ("The salary range for this position is $85,000 - $110,000 per year.", 85000, 110000),
        ("Compensation: $95k to $120k annually plus bonus.", 95000, 120000),
        ("Pay range of $70,000-$88,000", 70000, 88000),
        ("Base salary $105,000", 105000, 105000),
        ("Salary: $92,000 – $118,000", 92000, 118000),
    ],
)
def test_parses_stated_ranges(text: str, low: float, high: float) -> None:
    estimate = parse(text)
    assert estimate is not None
    assert (estimate.low, estimate.high) == (low, high)


def test_annualizes_hourly_rates() -> None:
    estimate = parse("This role pays $38.50 - $46.00 per hour.")
    assert estimate is not None
    assert estimate.low == pytest.approx(38.50 * 2080)
    assert estimate.high == pytest.approx(46.00 * 2080)
    assert "annualized" in estimate.note


@pytest.mark.parametrize(
    "text",
    [
        "We offer a 401k match of $3,000 - $5,000 and great benefits.",
        "Relocation assistance up to $10,000 available.",
        "No pay information whatsoever in this posting.",
        "",
    ],
)
def test_rejects_non_salary_figures(text: str) -> None:
    """A wrong number is worse than no number."""
    assert parse(text) is None


def test_posted_range_outranks_higher_confidence_estimate() -> None:
    """Specificity beats confidence: the employer's own range always wins."""
    estimates = [
        SalaryEstimate(source=SalarySourceKind.BLS_OES, low=80500, high=99380, confidence=0.95),
        SalaryEstimate(source=SalarySourceKind.POSTING, low=92000, high=104000, confidence=0.70),
    ]
    resolution = _merge(estimates)
    assert resolution.best.source is SalarySourceKind.POSTING
    assert len(resolution.alternates) == 1


def test_merge_with_no_estimates() -> None:
    assert _merge([]).best is None


def test_source_ranking_is_total() -> None:
    assert len(set(SALARY_SOURCE_RANK.values())) == len(SalarySourceKind)


def test_bls_bands_track_seniority(offline_http) -> None:
    source = BLSSource(offline_http)
    early = source.lookup(
        SalaryQuery(title="IE", company="X", seniority=Seniority.EARLY, soc_code="17-2112")
    )
    senior = source.lookup(
        SalaryQuery(title="IE", company="X", seniority=Seniority.SENIOR, soc_code="17-2112")
    )
    assert early is not None and senior is not None
    assert senior.low > early.low


def test_bls_declines_unknown_occupation(offline_http) -> None:
    source = BLSSource(offline_http)
    assert source.lookup(SalaryQuery(title="X", company="X", soc_code="99-9999")) is None
    assert source.lookup(SalaryQuery(title="X", company="X", soc_code=None)) is None


def _lca_csv(rows: int = 12) -> bytes:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "CASE_STATUS", "EMPLOYER_NAME", "JOB_TITLE",
            "WAGE_RATE_OF_PAY_FROM", "WAGE_RATE_OF_PAY_TO",
            "WAGE_UNIT_OF_PAY", "WORKSITE_STATE",
        ]
    )
    for i in range(rows):
        writer.writerow(
            ["CERTIFIED", "Raytheon Technologies, Inc.", "Industrial Engineer II",
             str(88000 + i * 2500), str(95000 + i * 2500), "Year", "MA"]
        )
    writer.writerow(
        ["DENIED", "Raytheon Technologies, Inc.", "Industrial Engineer II",
         "9000000", "9000000", "Year", "MA"]
    )
    return buf.getvalue().encode()


def test_h1b_indexes_and_looks_up(store) -> None:
    stats = build_index(_lca_csv(), store, source_label="test")
    assert stats["kept"] == 12  # the DENIED row is excluded

    source = H1BSource(store)
    assert source.is_indexed()
    estimate = source.lookup(
        SalaryQuery(
            title="Industrial Engineer", company="Raytheon Technologies", seniority=Seniority.EARLY
        )
    )
    assert estimate is not None
    assert estimate.source is SalarySourceKind.H1B_LCA
    # The 9,000,000 denied filing must not have leaked into the distribution.
    assert estimate.high < 200_000


def test_h1b_silent_when_not_indexed(store) -> None:
    assert H1BSource(store).lookup(SalaryQuery(title="X", company="Y")) is None


def test_h1b_rejects_unrecognized_file(store) -> None:
    with pytest.raises(ValueError, match="unrecognized disclosure file"):
        build_index(b"col_a,col_b\n1,2\n", store)
