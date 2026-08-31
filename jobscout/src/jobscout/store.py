"""SQLite run cache plus the cross-run 'already shown you this' ledger.

Two distinct kinds of state, deliberately stored differently:

* The **cache** is ephemeral and per-run. It exists so a pipeline that dies in
  stage 5 can resume without re-paying for stages 1-4. Losing it costs money and
  time but never correctness.
* The **seen ledger** is durable and must survive a wiped CI runner, so it lives
  in a small JSON file committed back to the repo rather than in the database.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    namespace TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT NOT NULL,
    stored_at REAL NOT NULL,
    PRIMARY KEY (namespace, key)
);
CREATE INDEX IF NOT EXISTS kv_ns_time ON kv (namespace, stored_at);
"""


class Store:
    """Namespaced key/value cache with per-entry TTL."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, namespace: str, key: str, *, ttl_seconds: float | None = None) -> Any | None:
        row = self._conn.execute(
            "SELECT value, stored_at FROM kv WHERE namespace=? AND key=?", (namespace, key)
        ).fetchone()
        if row is None:
            return None
        value, stored_at = row
        if ttl_seconds is not None and time.time() - stored_at > ttl_seconds:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None

    def set(self, namespace: str, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO kv (namespace, key, value, stored_at) VALUES (?,?,?,?)",
            (namespace, key, json.dumps(value, default=str), time.time()),
        )
        self._conn.commit()

    def clear(self, namespace: str | None = None) -> None:
        if namespace:
            self._conn.execute("DELETE FROM kv WHERE namespace=?", (namespace,))
        else:
            self._conn.execute("DELETE FROM kv")
        self._conn.commit()

    @contextmanager
    def cached(
        self, namespace: str, key: str, *, ttl_seconds: float | None = None, force: bool = False
    ) -> Iterator[list[Any]]:
        """Cache-aside helper.

        Yields a single-element list; if it still holds `None` on exit the block
        computed nothing and nothing is written. Callers read `box[0]` after the
        block for the value, whether it was cached or freshly computed.

            with store.cached("ats", key) as box:
                if box[0] is None:
                    box[0] = expensive()
            result = box[0]
        """
        box: list[Any] = [None if force else self.get(namespace, key, ttl_seconds=ttl_seconds)]
        had = box[0] is not None
        yield box
        if not had and box[0] is not None:
            self.set(namespace, key, box[0])


class SeenLedger:
    """Which job fingerprints have already appeared in a digest.

    Kept as sorted JSON so the file diffs cleanly when the workflow commits it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seen: dict[str, str] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
                self._seen = dict(data.get("seen", {}))
            except (json.JSONDecodeError, AttributeError):
                # A corrupt ledger costs us the "new" flag for one run; it must
                # never take down the run itself.
                self._seen = {}

    def is_new(self, job_id: str) -> bool:
        return job_id not in self._seen

    def mark(self, job_id: str, when: str) -> None:
        self._seen.setdefault(job_id, when)

    def prune(self, keep_days: int = 180) -> None:
        """Drop entries old enough that re-showing the role would be fine."""
        import datetime as _dt

        cutoff = _dt.date.today() - _dt.timedelta(days=keep_days)
        self._seen = {
            k: v for k, v in self._seen.items() if _safe_date(v) is None or _safe_date(v) >= cutoff
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"seen": dict(sorted(self._seen.items()))}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def __len__(self) -> int:
        return len(self._seen)


def _safe_date(value: str):
    import datetime as _dt

    try:
        return _dt.date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None
