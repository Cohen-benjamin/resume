"""Official BLS wage data.

The baseline that is always available. Unlike the crowd-sourced sources this is
a government statistical product, so it never gets blocked and never goes stale
without warning -- but it describes an *occupation in a metro*, not a job at a
company, which is why it ranks below the others.

Two paths, in order:

1. The BLS Public Data API, which serves OEWS series by constructed series ID.
2. A small bundled table of national wage percentiles, so that a missing key,
   a rate limit, or offline mode still produces a defensible number.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from ..http import HttpClient
from ..models import SENIORITY_PERCENTILES, SalaryEstimate, SalarySourceKind, Seniority
from .base import SalaryQuery

log = logging.getLogger(__name__)

_API = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

#: OEWS datatype codes for annual wage percentiles.
_ANNUAL_PERCENTILE_CODES = {10: "15", 25: "16", 50: "17", 75: "18", 90: "19"}

_FALLBACK_PATH = Path(__file__).parent / "bls_national.json"


class BLSSource:
    name = "bls"

    def __init__(
        self,
        http: HttpClient,
        *,
        api_key: str = "",
        area_code: str = "0071650",
        area_name: str = "Boston-Cambridge-Nashua, MA-NH",
    ) -> None:
        self.http = http
        self.api_key = api_key
        self.area_code = area_code
        self.area_name = area_name
        self._fallback: dict[str, dict] | None = None

    def lookup(self, query: SalaryQuery) -> SalaryEstimate | None:
        soc = _normalize_soc(query.soc_code)
        if not soc:
            return None

        low_pct, high_pct = SENIORITY_PERCENTILES.get(
            query.seniority, SENIORITY_PERCENTILES[Seniority.UNKNOWN]
        )

        metro = self._from_api(soc, low_pct, high_pct)
        if metro:
            return metro
        return self._from_fallback(soc, low_pct, high_pct)

    # -- API ------------------------------------------------------------

    def _from_api(self, soc: str, low_pct: int, high_pct: int) -> SalaryEstimate | None:
        if self.http.offline:
            return None

        series = {
            pct: f"OEUM{self.area_code}000000{soc}{code}"
            for pct, code in _ANNUAL_PERCENTILE_CODES.items()
            if pct in (low_pct, high_pct)
        }
        if not series:
            return None

        body: dict[str, object] = {"seriesid": list(series.values())}
        if self.api_key:
            body["registrationkey"] = self.api_key

        try:
            resp = self.http.request("POST", _API, json_body=body)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - fall through to the bundled table
            log.info("BLS API unavailable: %s", exc)
            return None

        if data.get("status") != "REQUEST_SUCCEEDED":
            log.info("BLS API said %s", data.get("status"))
            return None

        values: dict[int, float] = {}
        by_id = {s.get("seriesID"): s for s in data.get("Results", {}).get("series", [])}
        for pct, series_id in series.items():
            entries = (by_id.get(series_id) or {}).get("data") or []
            if not entries:
                continue
            try:
                values[pct] = float(str(entries[0].get("value", "")).replace(",", ""))
            except ValueError:
                continue

        if len(values) < 2:
            return None

        return SalaryEstimate(
            source=SalarySourceKind.BLS_OES,
            low=values[low_pct],
            high=values[high_pct],
            confidence=0.6,
            as_of=date.today(),
            note=f"BLS OEWS {low_pct}th-{high_pct}th percentile, {self.area_name}",
            url="https://www.bls.gov/oes/",
        )

    # -- bundled fallback ----------------------------------------------

    def _from_fallback(self, soc: str, low_pct: int, high_pct: int) -> SalaryEstimate | None:
        if self._fallback is None:
            try:
                self._fallback = json.loads(_FALLBACK_PATH.read_text())
            except (OSError, json.JSONDecodeError):
                self._fallback = {}

        record = self._fallback.get(_dashed(soc))
        if not record:
            return None

        pcts = record.get("percentiles", {})
        lo, hi = pcts.get(str(low_pct)), pcts.get(str(high_pct))
        if lo is None or hi is None:
            return None

        return SalaryEstimate(
            source=SalarySourceKind.BLS_OES,
            low=float(lo),
            high=float(hi),
            confidence=0.45,
            as_of=date(record.get("year", 2024), 5, 1),
            note=(
                f"BLS OEWS {low_pct}th-{high_pct}th percentile, national "
                f"({record.get('title', '')}) — metro data unavailable"
            ),
            url="https://www.bls.gov/oes/",
        )


def _normalize_soc(soc: str | None) -> str | None:
    """'17-2112' -> '172112'."""
    if not soc:
        return None
    digits = "".join(ch for ch in soc if ch.isdigit())
    return digits if len(digits) == 6 else None


def _dashed(soc: str) -> str:
    return f"{soc[:2]}-{soc[2:]}"
