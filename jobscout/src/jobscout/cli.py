"""Command line interface."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import Config
from .models import RunResult, VerificationStatus
from .pipeline import (
    Context,
    commit_seen,
    run_all,
    run_discover,
    run_profile,
    run_salary,
    run_score,
    run_verify,
)
from .report import render
from .store import Store

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Resume-driven job discovery, verification, comp lookup and weekly digest.",
)
console = Console()

_DEFAULT_CONFIG = Path("config.yaml")
_FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _load(config_path: Path) -> Config:
    if not config_path.exists():
        console.print(
            f"[red]No config at {config_path}.[/red] "
            "Copy [bold]config.example.yaml[/bold] to [bold]config.yaml[/bold] and edit it."
        )
        raise typer.Exit(code=2)
    return Config.load(config_path)


def _context(config_path: Path, offline: bool, force: bool, limit: int | None) -> Context:
    config = _load(config_path)
    return Context.build(
        config,
        offline=offline,
        force=force,
        limit=limit,
        fixture_dir=_FIXTURES if offline else None,
    )


# -- shared options ----------------------------------------------------

ConfigOpt = typer.Option(_DEFAULT_CONFIG, "--config", "-c", help="Path to config.yaml.")
OfflineOpt = typer.Option(False, "--offline", help="Use bundled fixtures; make no network calls.")
ForceOpt = typer.Option(False, "--force", help="Ignore cached results and recompute.")
LimitOpt = typer.Option(None, "--limit", "-n", help="Cap how many postings are processed.")
VerboseOpt = typer.Option(False, "--verbose", "-v", help="Log progress to stderr.")


@app.command()
def profile(
    config: Path = ConfigOpt,
    offline: bool = OfflineOpt,
    force: bool = ForceOpt,
    verbose: bool = VerboseOpt,
) -> None:
    """Parse the resume into a structured profile."""
    _setup_logging(verbose)
    ctx = _context(config, offline, force, None)
    try:
        result = run_profile(ctx)
    finally:
        ctx.close()

    console.print(f"[bold]{result.name}[/bold] — {result.headline}")
    console.print(f"seniority: {result.seniority} ({result.years_experience:g} yrs)")
    console.print(f"SOC: {result.soc_code or '—'} {result.soc_title or ''}")
    console.print(f"skills: {', '.join(result.skills) or '—'}")
    console.print(f"search titles: {', '.join(result.target_title_synonyms) or '—'}")


@app.command()
def discover(
    config: Path = ConfigOpt,
    offline: bool = OfflineOpt,
    force: bool = ForceOpt,
    limit: int | None = LimitOpt,
    verbose: bool = VerboseOpt,
) -> None:
    """Search every configured source for candidate postings."""
    _setup_logging(verbose)
    ctx = _context(config, offline, force, limit)
    try:
        prof = run_profile(ctx)
        jobs = run_discover(ctx, prof)
    finally:
        ctx.close()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Title", overflow="fold")
    table.add_column("Company")
    table.add_column("Location")
    table.add_column("Source")
    for job in jobs:
        table.add_row(job.title, job.company, job.location or "—", job.source)
    console.print(table)
    console.print(f"[bold]{len(jobs)}[/bold] unique postings after filters")
    _print_problems(ctx)


@app.command()
def verify(
    config: Path = ConfigOpt,
    offline: bool = OfflineOpt,
    force: bool = ForceOpt,
    limit: int | None = LimitOpt,
    verbose: bool = VerboseOpt,
) -> None:
    """Check each posting is still open on the employer's own site."""
    _setup_logging(verbose)
    ctx = _context(config, offline, force, limit)
    try:
        prof = run_profile(ctx)
        jobs = run_verify(ctx, run_discover(ctx, prof))
    finally:
        ctx.close()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Status")
    table.add_column("Title", overflow="fold")
    table.add_column("Company")
    table.add_column("Method")
    colours = {
        VerificationStatus.OPEN: "green",
        VerificationStatus.CLOSED: "red",
        VerificationStatus.UNVERIFIED: "yellow",
    }
    for job in jobs:
        status = job.verification.status
        table.add_row(
            f"[{colours[status]}]{status.value}[/{colours[status]}]",
            job.title,
            job.company,
            job.verification.method or "—",
        )
    console.print(table)


@app.command()
def salary(
    config: Path = ConfigOpt,
    offline: bool = OfflineOpt,
    force: bool = ForceOpt,
    limit: int | None = LimitOpt,
    explain: bool = typer.Option(False, "--explain", help="Show every source's answer."),
    verbose: bool = VerboseOpt,
) -> None:
    """Resolve pay data for each posting."""
    _setup_logging(verbose)
    ctx = _context(config, offline, force, limit)
    try:
        prof = run_profile(ctx)
        jobs = run_verify(ctx, run_discover(ctx, prof))
        jobs, degraded, reason = run_salary(ctx, jobs, prof, explain=explain)
    finally:
        ctx.close()

    if not explain:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Title", overflow="fold")
        table.add_column("Company")
        table.add_column("Pay", justify="right")
        table.add_column("Source")
        for job in jobs:
            best = job.salary.best
            table.add_row(
                job.title,
                job.company,
                best.display() if best else "—",
                best.source.value if best else "—",
            )
        console.print(table)

    if degraded:
        console.print(f"[yellow]degraded:[/yellow] {reason}")


@app.command()
def score(
    config: Path = ConfigOpt,
    offline: bool = OfflineOpt,
    force: bool = ForceOpt,
    limit: int | None = LimitOpt,
    verbose: bool = VerboseOpt,
) -> None:
    """Score postings against the resume and write briefs for the top N."""
    _setup_logging(verbose)
    ctx = _context(config, offline, force, limit)
    try:
        prof = run_profile(ctx)
        jobs = run_verify(ctx, run_discover(ctx, prof))
        jobs, _, _ = run_salary(ctx, jobs, prof)
        jobs = run_score(ctx, jobs, prof)
    finally:
        ctx.close()

    for job in sorted(jobs, key=lambda j: j.match.fit_score if j.match else 0, reverse=True):
        if not job.match:
            continue
        console.print(f"[bold]{job.match.fit_score:3d}[/bold] {job.title} — {job.company}")
        if job.match.verdict:
            console.print(f"    {job.match.verdict}")
    _print_problems(ctx)


@app.command()
def report(
    config: Path = ConfigOpt,
    offline: bool = OfflineOpt,
    force: bool = ForceOpt,
    limit: int | None = LimitOpt,
    no_email: bool = typer.Option(False, "--no-email", help="Write the file but don't send it."),
    verbose: bool = VerboseOpt,
) -> None:
    """Render the digest from the current cache and optionally email it."""
    run(
        config=config,
        offline=offline,
        force=force,
        limit=limit,
        no_email=no_email,
        explain_salary=False,
        verbose=verbose,
    )


@app.command()
def run(
    config: Path = ConfigOpt,
    offline: bool = OfflineOpt,
    force: bool = ForceOpt,
    limit: int | None = LimitOpt,
    no_email: bool = typer.Option(False, "--no-email", help="Write the file but don't send it."),
    explain_salary: bool = typer.Option(False, "--explain-salary", help="Show salary provenance."),
    verbose: bool = VerboseOpt,
) -> None:
    """Run the whole pipeline and produce the digest."""
    _setup_logging(verbose)
    ctx = _context(config, offline, force, limit)
    try:
        result = run_all(ctx, explain_salary=explain_salary)
        path, html, text = render.write(result, ctx.config)
        _summarize(result, ctx.config, path)

        sent = False
        if not no_email and ctx.config.email.enabled:
            sent = _send(ctx, result, html, text)

        # Only record roles as seen once the digest actually exists.
        commit_seen(ctx, result)
        if sent:
            console.print("[green]digest emailed[/green]")
    finally:
        ctx.close()


@app.command("fetch-h1b")
def fetch_h1b(
    source: str = typer.Argument(
        ...,
        help="Path to a downloaded DOL LCA disclosure CSV, or a URL to one.",
    ),
    config: Path = ConfigOpt,
    verbose: bool = VerboseOpt,
) -> None:
    """Index DOL H-1B disclosure data for employer-specific salary lookups.

    Download a quarterly file from
    https://www.dol.gov/agencies/eta/foreign-labor/performance and point this at
    it. Indexing is a one-time cost; lookups afterwards are local.
    """
    _setup_logging(verbose)
    cfg = _load(config)
    from .http import HttpClient
    from .salary.h1b import build_index

    if source.startswith(("http://", "https://")):
        console.print(f"downloading {source} …")
        with HttpClient(timeout=600) as http:
            resp = http.request("GET", source)
            resp.raise_for_status()
            payload = resp.content
        label = source
    else:
        path = Path(source)
        if not path.exists():
            console.print(f"[red]no such file: {path}[/red]")
            raise typer.Exit(code=2)
        payload = path.read_bytes()
        label = str(path)

    store = Store(cfg.cache_dir / "cache.db")
    try:
        stats = build_index(payload, store, source_label=label)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        store.close()

    console.print(
        f"indexed [bold]{stats['kept']}[/bold] wage records from {stats['rows']} rows "
        f"across {stats['keys']} employer/title keys"
    )


def _send(ctx: Context, result: RunResult, html: str, text: str) -> bool:
    from .notify.resend import EmailError, send_digest

    top, rest = render.split_jobs(result, ctx.config)
    subject = ctx.config.report.subject_template.format(
        n=len(top) + len(rest), date=result.generated_at.strftime("%d %b")
    )
    recipient = ctx.config.email.to or ctx.secrets.digest_to_email
    sender = ctx.secrets.digest_from_email or ctx.config.email.from_address

    try:
        send_digest(
            ctx.http,
            api_key=ctx.secrets.resend_api_key,
            to=recipient,
            from_address=sender,
            subject=subject,
            html=html,
            text=text,
        )
    except EmailError as exc:
        # Never fail the run over delivery: the digest is already on disk.
        console.print(f"[yellow]digest not emailed:[/yellow] {exc}")
        return False
    return True


def _summarize(result: RunResult, config: Config, path: Path) -> None:
    top, rest = render.split_jobs(result, config)
    console.print()
    console.print(f"[bold]{len(top) + len(rest)}[/bold] roles in the digest → [bold]{path}[/bold]")
    console.print(
        f"  {result.stats.discovered} discovered · "
        f"{result.stats.verified_open} verified open · "
        f"{result.stats.verified_closed} closed · "
        f"{result.stats.unverified} unverified · "
        f"{result.stats.salary_resolved} with pay data"
    )
    for job in top:
        score_value = job.match.fit_score if job.match else 0
        pay = job.salary.best.display() if job.salary.best else "no pay data"
        console.print(f"  [bold]{score_value:3d}[/bold]  {job.title} — {job.company}  ({pay})")
    for note in result.stats.errors:
        console.print(f"  [yellow]![/yellow] {note}")


def _print_problems(ctx: Context) -> None:
    for problem in dict.fromkeys(ctx.problems):
        console.print(f"[yellow]![/yellow] {problem}")


if __name__ == "__main__":
    app()
