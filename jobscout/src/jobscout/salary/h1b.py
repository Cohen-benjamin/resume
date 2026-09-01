"""Employer-specific salaries from DOL H-1B (LCA) disclosure data.

Every H-1B petition requires the employer to file the actual wage for the role,
by job title and worksite. The Department of Labor publishes the lot quarterly.
That makes it the only free source that says what *this employer* pays for
*this kind of role in this place* -- far more specific than an occupational
average, and unlike the crowd-sourced sites it is a legal filing rather than a
self-report.

The tradeoff is that it covers only sponsored roles, skews toward larger
employers, and reports the filed wage rather than total compensation. So it
ranks above the BLS baseline and below anything the employer published for this
specific opening.

The quarterly disclosure file is far too large to fetch per run, so it is
downloaded and indexed once by ``jobscout fetch-h1b`` and read from SQLite
thereafter.
"""

from __future__ import annotations

import csv
import io
import logging
import statistics
from datetime import date
from pathlib import Path

from ..models import (
    SENIORITY_PERCENTILES,
    SalaryEstimate,
    SalarySourceKind,
    Seniority,
    normalize_company,
    normalize_title,
)
from ..store import Store
from .base import SalaryQuery

log = logging.getLogger(__name__)

_NAMESPACE = "h1b"
_INDEX_KEY = "__index_meta__"

#: Column names vary between DOL release years; accept any of them.
_COLUMNS = {
    "employer": ("EMPLOYER_NAME", "LCA_CASE_EMPLOYER_NAME"),
    "title": ("JOB_TITLE", "LCA_CASE_JOB_TITLE"),
    "wage": ("WAGE_RATE_OF_PAY_FROM", "LCA_CASE_WAGE_RATE_FROM", "WAGE_RATE_OF_PAY"),
    "wage_to": ("WAGE_RATE_OF_PAY_TO", "LCA_CASE_WAGE_RATE_TO"),
    "unit": ("WAGE_UNIT_OF_PAY", "LCA_CASE_WAGE_RATE_UNIT", "PW_UNIT_OF_PAY"),
    "state": ("WORKSITE_STATE", "LCA_CASE_WORKLOC1_STATE", "STATE"),
    "city": ("WORKSITE_CITY", "LCA_CASE_WORKLOC1_CITY"),
    "status": ("CASE_STATUS",),
}

_UNIT_MULTIPLIERS = {
    "year": 1.0,
    "yr": 1.0,
    "hour": 2080.0,
    "hr": 2080.0,
    "week": 52.0,
    "wk": 52.0,
    "bi-weekly": 26.0,
    "biweekly": 26.0,
    "month": 12.0,
    "mth": 12.0,
}

_MIN_PLAUSIBLE = 20_000
_MAX_PLAUSIBLE = 2_000_000


class H1BSource:
    name = "h1b"

    def __init__(self, store: Store) -> None:
        self.store = store
        self._available: bool | None = None

    def is_indexed(self) -> bool:
        if self._available is None:
            self._available = self.store.get(_NAMESPACE, _INDEX_KEY) is not None
        return self._available

    def lookup(self, query: SalaryQuery) -> SalaryEstimate | None:
        if not self.is_indexed():
            return None

        company = normalize_company(query.company)
        title = normalize_title(query.title)

        # Exact employer+title first; fall back to the employer's overall
        # engineering-ish spread only if that misses.
        record = self.store.get(_NAMESPACE, f"{company}|{title}")
        specificity = "employer and title"
        if not record:
            record = self.store.get(_NAMESPACE, f"{company}|*")
            specificity = "employer, across titles"
        if not record:
            return None

        wages = record.get("wages") or []
        if len(wages) < 3:
            # Two data points is an anecdote, not a distribution.
            return None

        low_pct, high_pct = SENIORITY_PERCENTILES.get(
            query.seniority, SENIORITY_PERCENTILES[Seniority.UNKNOWN]
        )
        lo = _percentile(wages, low_pct)
        hi = _percentile(wages, high_pct)
        if lo is None or hi is None:
            return None

        meta = self.store.get(_NAMESPACE, _INDEX_KEY) or {}
        return SalaryEstimate(
            source=SalarySourceKind.H1B_LCA,
            low=lo,
            high=hi,
            confidence=0.65 if specificity.startswith("employer and") else 0.5,
            as_of=_meta_date(meta),
            note=(
                f"DOL H-1B filings ({specificity}), {len(wages)} records, "
                f"{low_pct}th-{high_pct}th percentile"
            ),
            url="https://www.dol.gov/agencies/eta/foreign-labor/performance",
        )


def build_index(csv_bytes: bytes, store: Store, *, source_label: str = "") -> dict[str, int]:
    """Index a DOL disclosure CSV into the store.

    Streams rather than loading the whole file into memory as rows, because the
    quarterly release runs to hundreds of megabytes.
    """
    text = io.TextIOWrapper(io.BytesIO(csv_bytes), encoding="utf-8", errors="replace")
    reader = csv.DictReader(text)

    fields = reader.fieldnames or []
    resolved = {key: _pick(fields, names) for key, names in _COLUMNS.items()}
    if not resolved["employer"] or not resolved["wage"]:
        raise ValueError(
            "unrecognized disclosure file: no employer/wage column found "
            f"(saw {fields[:8]}...)"
        )

    by_key: dict[str, list[float]] = {}
    rows = 0
    kept = 0

    for row in reader:
        rows += 1
        if resolved["status"]:
            status = (row.get(resolved["status"]) or "").strip().upper()
            # Withdrawn and denied filings never reflected a real offer.
            if status and status not in {"CERTIFIED", "CERTIFIED-WITHDRAWN"}:
                continue

        wage = _annualize(
            row.get(resolved["wage"] or "", ""),
            row.get(resolved["wage_to"] or "", ""),
            row.get(resolved["unit"] or "", ""),
        )
        if wage is None:
            continue

        employer = normalize_company(row.get(resolved["employer"] or "", ""))
        title = normalize_title(row.get(resolved["title"] or "", ""))
        if not employer:
            continue

        kept += 1
        by_key.setdefault(f"{employer}|*", []).append(wage)
        if title:
            by_key.setdefault(f"{employer}|{title}", []).append(wage)

    for key, wages in by_key.items():
        # Cap the stored sample: the percentile is stable well before this, and
        # a few large employers would otherwise dominate the database size.
        wages.sort()
        if len(wages) > 500:
            step = len(wages) / 500
            wages = [wages[int(i * step)] for i in range(500)]
        store.set(_NAMESPACE, key, {"wages": wages})

    store.set(
        _NAMESPACE,
        _INDEX_KEY,
        {
            "rows": rows,
            "kept": kept,
            "keys": len(by_key),
            "indexed_on": date.today().isoformat(),
            "source": source_label,
        },
    )
    return {"rows": rows, "kept": kept, "keys": len(by_key)}


def _pick(fields: list[str], candidates: tuple[str, ...]) -> str | None:
    upper = {f.upper().strip(): f for f in fields}
    for candidate in candidates:
        if candidate in upper:
            return upper[candidate]
    return None


def _annualize(raw_from: str, raw_to: str, unit: str) -> float | None:
    value = _to_float(raw_from)
    if value is None:
        return None
    high = _to_float(raw_to)
    if high and high > value:
        value = (value + high) / 2

    multiplier = _UNIT_MULTIPLIERS.get((unit or "year").strip().casefold(), 1.0)
    annual = value * multiplier
    return annual if _MIN_PLAUSIBLE <= annual <= _MAX_PLAUSIBLE else None


def _to_float(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = str(raw).replace(",", "").replace("$", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _percentile(sorted_wages: list[float], pct: int) -> float | None:
    if not sorted_wages:
        return None
    try:
        return float(statistics.quantiles(sorted_wages, n=100, method="inclusive")[pct - 1])
    except (statistics.StatisticsError, IndexError):
        return float(statistics.median(sorted_wages))


def _meta_date(meta: dict) -> date | None:
    try:
        return date.fromisoformat(meta.get("indexed_on", ""))
    except (ValueError, TypeError):
        return None


def load_local_file(path: Path) -> bytes:
    return path.read_bytes()
