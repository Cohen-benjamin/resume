"""Read the pay range out of the posting itself.

This is the highest-confidence source available, because it is not an estimate
at all -- it is the band the employer committed to in writing, for this exact
role. A growing number of US states require it, so the hit rate is far better
than it used to be.
"""

from __future__ import annotations

import re
from datetime import date

from ..models import SalaryEstimate, SalarySourceKind
from .base import SalaryQuery

_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK]\b|\d{4,7}(?:\.\d+)?)"
#: Hourly figures are small and often carry cents, so they need their own
#: pattern -- the annual one deliberately refuses 2-digit numbers.
_HOURLY_MONEY = r"\$\s?(\d{1,3}(?:\.\d{1,2})?)"

#: Ordered by how much context they prove. A number sitting next to the word
#: "salary" is far more likely to be pay than a bare number in the body text.
_RANGE_PATTERNS = [
    re.compile(
        rf"(?:salary|compensation|pay|base)(?:\s+\w+){{0,6}}?[:\s]+(?:range\s*)?(?:of\s*)?"
        rf"{_MONEY}\s*(?:-|–|—|to|through)\s*{_MONEY}",
        re.IGNORECASE,
    ),
    re.compile(rf"{_MONEY}\s*(?:-|–|—|to)\s*{_MONEY}\s*(?:per\s+year|/\s*(?:yr|year)|annually|USD)", re.IGNORECASE),
    re.compile(rf"{_MONEY}\s*(?:-|–|—|to)\s*{_MONEY}", re.IGNORECASE),
]

_HOURLY_PATTERN = re.compile(
    rf"{_HOURLY_MONEY}\s*(?:(?:-|–|—|to)\s*{_HOURLY_MONEY})?\s*(?:per\s+hour|/\s*(?:hr|hour)|hourly|an\s+hour)",
    re.IGNORECASE,
)

#: A single figure, but only when pay vocabulary vouches for it. One number is
#: worth reporting; one number with no context is noise.
_SINGLE_PATTERN = re.compile(
    rf"(?:salary|compensation|pay|base)(?:\s+\w+){{0,4}}?(?:\s+is|:)?\s+(?:of\s+)?{_MONEY}"
    rf"(?!\s*(?:-|–|—|to)\s*\$)",
    re.IGNORECASE,
)

#: Anything below this is not an annual salary -- it's an hourly rate, a bonus
#: percentage, or a 401k match that happened to sit near a dollar sign.
_MIN_PLAUSIBLE_ANNUAL = 15_000
_MAX_PLAUSIBLE_ANNUAL = 2_000_000
_HOURLY_FULL_TIME_HOURS = 2080


class PostingSalarySource:
    name = "posting"

    def lookup(self, query: SalaryQuery) -> SalaryEstimate | None:
        for text, source_label in (
            (query.salary_text, "structured field"),
            (query.description, "posting text"),
        ):
            if not text:
                continue
            estimate = _parse(text, source_label)
            if estimate:
                return estimate
        return None


def _parse(text: str, label: str) -> SalaryEstimate | None:
    hourly = _HOURLY_PATTERN.search(text)
    if hourly:
        values = [_to_number(g) for g in hourly.groups() if g]
        values = [v for v in values if v and 7 <= v <= 400]
        if values:
            lo = min(values) * _HOURLY_FULL_TIME_HOURS
            hi = max(values) * _HOURLY_FULL_TIME_HOURS
            return SalaryEstimate(
                source=SalarySourceKind.POSTING,
                low=lo,
                high=hi,
                confidence=0.85,
                as_of=date.today(),
                note=f"hourly rate in {label}, annualized at {_HOURLY_FULL_TIME_HOURS}h",
            )

    for index, pattern in enumerate(_RANGE_PATTERNS):
        match = pattern.search(text)
        if not match:
            continue
        values = [_to_number(g) for g in match.groups() if g]
        values = [v for v in values if v and _MIN_PLAUSIBLE_ANNUAL <= v <= _MAX_PLAUSIBLE_ANNUAL]
        if len(values) < 2:
            continue
        lo, hi = min(values), max(values)
        if hi < lo * 1.005:
            continue
        # A range with no surrounding pay vocabulary could be anything.
        confidence = (0.95, 0.9, 0.7)[index]
        return SalaryEstimate(
            source=SalarySourceKind.POSTING,
            low=lo,
            high=hi,
            confidence=confidence,
            as_of=date.today(),
            note=f"range stated in {label}",
        )

    single = _SINGLE_PATTERN.search(text)
    if single:
        value = _to_number(single.group(1))
        if value and _MIN_PLAUSIBLE_ANNUAL <= value <= _MAX_PLAUSIBLE_ANNUAL:
            return SalaryEstimate(
                source=SalarySourceKind.POSTING,
                low=value,
                high=value,
                confidence=0.8,
                as_of=date.today(),
                note=f"single figure stated in {label}",
            )

    return None


def _to_number(raw: str | None) -> float | None:
    if not raw:
        return None
    value = raw.strip().replace(",", "").replace("$", "")
    multiplier = 1.0
    if value.lower().endswith("k"):
        value = value[:-1].strip()
        multiplier = 1000.0
    try:
        return float(value) * multiplier
    except ValueError:
        return None
