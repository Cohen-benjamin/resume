# JobScout

Takes a resume plus a description of the roles and area you want, then each week:

1. **Discovers** matching openings across job APIs and company boards
2. **Verifies** each role is genuinely still open on the employer's own site
3. **Prices** it, using pay data matched to the role and your experience level
4. **Ranks** the results against your resume with Claude
5. **Briefs** you on how best to land each of the top roles
6. **Emails** you the round-up

## Quick start

```bash
cd jobscout
uv venv && uv pip install -e ".[dev]"

cp config.example.yaml config.yaml     # edit: roles, locations, seniority, filters
cp companies.example.yaml companies.yaml
cp .env.example .env                   # add your API keys

# Full pipeline against bundled fixtures -- no keys, no network:
uv run jobscout run --offline --no-email

# For real:
uv run jobscout run --no-email
open digest.html
```

## Configuration

`config.yaml` is where you say what you want. The shipped
`config.example.yaml` is fully commented; the parts that matter most:

| Key | What it does |
|---|---|
| `search.roles` | Titles to search for. Add adjacent titles you'd take, not just ones you've held. |
| `search.locations` | Each entry is `name` + `radius_km`. Use `remote: true` for location-free searches. |
| `search.seniority` | Anchors salary percentiles and the fit rubric. Leave unset to infer from the resume. |
| `filters` | Hard excludes — title keywords, companies, minimum salary, maximum age of posting. |
| `report.top_n` | How many roles get a written brief. Briefs cost money; the rest still get scored. |
| `salary.scrapers.enabled` | Kill switch for the Glassdoor/Levels.fyi scrapers. |

Every key can be overridden per-run from the CLI, and secrets come from the
environment rather than the config file.

## Stages

Each stage is separately invocable and caches to `.jobscout/cache.db`, so a run
that dies in stage 4 resumes without re-paying for stages 1–3.

```
jobscout profile     # resume -> structured profile
jobscout discover    # config -> candidate postings
jobscout verify      # postings -> still-open check against the employer's ATS
jobscout salary      # postings -> pay band with provenance
jobscout score       # postings -> fit score, then briefs for the top N
jobscout report      # -> digest.html (+ email, unless --no-email)
jobscout run         # all of the above
```

Useful flags: `--offline` (fixtures only), `--no-email`, `--limit N`,
`--explain` (on `salary`, prints every source's answer and which won),
`--force` (ignore cache).

## Where the data comes from

**Listings** — [Adzuna](https://developer.adzuna.com/) (free key),
[The Muse](https://www.themuse.com/developers/api/v2) (free, no key), and the
public JSON boards of employers listed in `companies.yaml`.

**Verification** — the employer's own applicant tracking system. Greenhouse,
Lever, Ashby, SmartRecruiters and Workable all expose the same JSON their
careers page renders from, which makes "is this still open?" both authoritative
and free. Anything unrecognized falls back to fetching the apply URL and looking
for a 404, a redirect to the board root, or a closed-phrase match.

**Salary** — four sources, merged by confidence, each tagged with its provenance:

| Source | Confidence | Notes |
|---|---|---|
| Range in the posting | Highest | Increasingly mandated by state pay-transparency law |
| Levels.fyi / Glassdoor | High | Scraped; see the caveat below |
| DOL H-1B (LCA) disclosures | Medium | Real employer-filed salaries, by employer and worksite |
| BLS OES | Baseline | Official wage percentiles by occupation × metro |

The digest always shows which source produced the number.

> **On the scrapers.** Glassdoor and Levels.fyi are Cloudflare-protected and
> their terms of service prohibit scraping. They are enabled by default but
> treated as one input among four, so when a fetch is blocked the number
> degrades in confidence rather than disappearing. Set
> `salary.scrapers.enabled: false` to turn them off; nothing else changes.

## Secrets

| Variable | Required | For |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Profile extraction, scoring, briefs |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | yes | Adzuna search ([free](https://developer.adzuna.com/)) |
| `RESEND_API_KEY` | for email | Sending the digest |
| `DIGEST_TO_EMAIL` / `DIGEST_FROM_EMAIL` | for email | Envelope addresses |
| `BLS_API_KEY` | optional | Raises BLS rate limits; unregistered access works |

In GitHub Actions these live in repository secrets.

## Scheduled runs

`.github/workflows/jobscout.yml` runs the pipeline every Monday and emails the
digest, and can be triggered by hand from the Actions tab. It commits
`jobscout/state/seen.json` back to the branch so the next run knows which roles
you've already been shown.

## Cost

Roughly **$1–2 per run** — about 150 jobs scored through the Message Batches API
(half price) against a cached prompt prefix, plus a brief for each of the top 10.
At weekly cadence that's a few dollars a month. `jobscout run --dry-run` prints
the token estimate before spending anything.

## Development

```bash
uv run pytest          # unit + golden-file tests, all offline
uv run ruff check src tests
```

Tests never touch the network: HTTP is mocked with `respx` against recorded
fixtures in `fixtures/`, and `--offline` runs the whole pipeline from them.
