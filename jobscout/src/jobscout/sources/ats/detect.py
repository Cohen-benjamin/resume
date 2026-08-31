"""Infer which ATS a posting lives in, from its apply URL.

This is what lets verification work on jobs discovered through an aggregator:
the aggregator gives us a link, the link names the ATS and the board, and the
board's API tells us authoritatively whether the role is still open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass
class ATSRef:
    kind: str
    token: str
    external_id: str = ""


#: host suffix -> (ats kind, regex over the path capturing token and optional job id)
_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("greenhouse.io", "greenhouse", re.compile(r"^/(?:embed/job_app\?for=)?([^/?]+)(?:/jobs/(\d+))?")),
    ("boards.greenhouse.io", "greenhouse", re.compile(r"^/([^/?]+)(?:/jobs/(\d+))?")),
    ("job-boards.greenhouse.io", "greenhouse", re.compile(r"^/([^/?]+)(?:/jobs/(\d+))?")),
    ("lever.co", "lever", re.compile(r"^/([^/?]+)(?:/([0-9a-f-]{16,}))?")),
    ("ashbyhq.com", "ashby", re.compile(r"^/([^/?]+)(?:/([0-9a-f-]{16,}))?")),
    ("smartrecruiters.com", "smartrecruiters", re.compile(r"^/([^/?]+)(?:/(\d+))?")),
    ("workable.com", "workable", re.compile(r"^/([^/?]+)(?:/j/([A-Z0-9]+))?")),
]

#: Boards that host many companies under one domain, where the first path
#: segment is a section name rather than a company token.
_SKIP_SEGMENTS = {"embed", "api", "v1", "jobs", "search", "companies"}


def detect(url: str) -> ATSRef | None:
    """Return the ATS reference for an apply URL, or None if unrecognized."""
    if not url:
        return None

    parts = urlsplit(url)
    host = parts.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or "/"

    for suffix, kind, pattern in _PATTERNS:
        if not (host == suffix or host.endswith("." + suffix)):
            continue

        # Greenhouse's embed form carries the token in the query string.
        if kind == "greenhouse" and "for=" in (parts.query or ""):
            token = re.search(r"for=([^&]+)", parts.query)
            if token:
                job_id = re.search(r"gh_jid=(\d+)", parts.query)
                return ATSRef(kind, token.group(1), job_id.group(1) if job_id else "")

        match = pattern.match(path)
        if not match:
            continue
        token = match.group(1)
        if not token or token.casefold() in _SKIP_SEGMENTS:
            continue
        external_id = match.group(2) if match.lastindex and match.lastindex >= 2 else ""
        return ATSRef(kind, token, external_id or "")

    return None
