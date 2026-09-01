"""The pipeline end to end, offline."""

from __future__ import annotations

from pathlib import Path

from jobscout.models import VerificationStatus
from jobscout.pipeline import Context, commit_seen, run_all
from jobscout.report import render
from jobscout.store import SeenLedger, Store

from .helpers import FIXTURES


def build_context(config, **kwargs) -> Context:
    return Context.build(config, offline=True, fixture_dir=FIXTURES, **kwargs)


def test_full_offline_run_produces_a_digest(config) -> None:
    ctx = build_context(config)
    try:
        result = run_all(ctx)
        path, html, text = render.write(result, config)
    finally:
        ctx.close()

    assert path.exists()
    assert html and text
    assert result.jobs, "the offline run found no jobs at all"
    assert result.stats.discovered > 0


def test_closed_roles_never_reach_the_digest(config) -> None:
    """The stale Quality Engineer posting is in the fixtures but not on the board."""
    ctx = build_context(config)
    try:
        result = run_all(ctx)
    finally:
        ctx.close()

    assert result.stats.verified_closed >= 1
    top, rest = render.split_jobs(result, config)
    assert all(j.verification.status is VerificationStatus.OPEN for j in top + rest)
    assert not any(j.title == "Quality Engineer" for j in top + rest)


def test_salary_is_resolved_with_provenance(config) -> None:
    ctx = build_context(config)
    try:
        result = run_all(ctx)
    finally:
        ctx.close()

    priced = [j for j in result.jobs if j.salary.best]
    assert priced, "no role got a salary at all"
    assert all(j.salary.best.source for j in priced)
    assert all(j.salary.best.note for j in priced), "every estimate must say where it came from"


def test_offline_run_says_scores_are_not_claude_scored(config) -> None:
    """The digest must never pass keyword scores off as model judgement."""
    ctx = build_context(config)
    try:
        result = run_all(ctx)
    finally:
        ctx.close()

    assert any("keyword" in note.casefold() for note in result.stats.errors)


def test_seen_ledger_marks_roles_new_only_once(config) -> None:
    ctx = build_context(config)
    try:
        first = run_all(ctx)
        assert all(j.is_new for j in first.jobs), "everything should be new on a first run"
        commit_seen(ctx, first)
    finally:
        ctx.close()

    ctx = build_context(config)
    try:
        second = run_all(ctx)
    finally:
        ctx.close()

    open_jobs = [j for j in second.jobs if j.verification.status is VerificationStatus.OPEN]
    assert open_jobs
    assert not any(j.is_new for j in open_jobs), "roles were re-flagged as new"


def test_ledger_survives_a_corrupt_file(tmp_path: Path) -> None:
    """A bad ledger costs the 'new' flag for one run, never the run itself."""
    path = tmp_path / "seen.json"
    path.write_text("{ not json at all")
    ledger = SeenLedger(path)
    assert ledger.is_new("anything")
    ledger.mark("anything", "2026-08-31")
    ledger.save()
    assert not SeenLedger(path).is_new("anything")


def test_second_run_uses_the_cache(config) -> None:
    """Reruns must be cheap: stage results are cached, not recomputed."""
    ctx = build_context(config)
    try:
        run_all(ctx)
    finally:
        ctx.close()

    store = Store(config.cache_dir / "cache.db")
    try:
        assert store.get("search", "adzuna:industrial engineer|boston, ma|50|False|50|30|None")
    finally:
        store.close()
