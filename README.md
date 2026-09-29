# Trend Engine

A small Python CLI for trade-the-news research: find emerging headlines and public
attention shifts, with Hacker News as the main information/tech news channel and
regional search/social signals in North America and Asia. It does not execute
trades, collect market prices, match tokens, or predict price moves.
Python 3.12+, with **Peewee as the only third-party runtime dependency**.

## Run it

From the repository root:

```bash
python3 -m venv ./venv
./venv/bin/python -m pip install -e .
./venv/bin/trend-engine doctor
./venv/bin/trend-engine run
```

All application files live directly under `src/`. You can run the CLI as a script
from the repository root:

```bash
./venv/bin/python src/cli.py run
```

The installed `trend-engine` command and `python -m cli` entry point also work.
Installation uses Hatchling as a build tool only.

Optional free YouTube key (export it yourself; `.env` is not loaded automatically):

```bash
export YOUTUBE_API_KEY='...'
```

YouTube is skipped without a key. Other collectors attempt public, keyless access.
No paid endpoints, paid scraper services, brokerage credentials, or wallets are used.

```bash
./venv/bin/trend-engine collect
./venv/bin/trend-engine report --window 24
./venv/bin/trend-engine run --sources hacker_news google_trends
./venv/bin/trend-engine run --sources hacker_news --json
./venv/bin/trend-engine run --config /path/to/config.toml
```

`collect` saves one snapshot. `report` reads history without changing the database.
`run` does both. `doctor` validates settings and reports key availability offline;
it does not imply that external sources are reachable. Without `--config`, doctor
checks a local `config.toml` if present; otherwise configuration is `not_checked`.
An explicitly supplied missing or invalid config is an error, not a skipped check.

Edit [config.toml](config.toml) for countries, sources, limits, and paths. Data paths
are relative to that file. Supported markets: US, CA, MX, JP, KR, IN, ID, SG.
Reports go to `reports/YYYY-MM-DD/HHMMSS-microseconds/report.md` and `report.json`.
Exit codes: 0 completed; 1 source failure or no data; 2 invalid input/local error.
Limited samples and optional missing keys are printed but do not fail a useful run.

Use `--json` on `report` or `run` to print the report to stdout instead of creating
report files. Status messages and errors go to stderr, so stdout can be redirected
or piped to another tool. `run --json` still saves the collected snapshot to SQLite.

```bash
./venv/bin/python src/cli.py report --window 24 --json
./venv/bin/python src/cli.py run --json > trends.json
```

The JSON has the same format as `report.json`. Exit codes are unchanged: an empty
report still produces JSON but exits with 1, as does a run with a source failure.

## Agents and OpenClaw

[AGENTS.md](AGENTS.md) is the agent-facing CLI guide: commands, side effects,
credentials, exit codes, freshness, and handling untrusted source text.
The CLI takes regular arguments validated by argparse, not JSON stdin. Only its
outputs have schemas: [reports](contracts/report.schema.json) (v3) and
[offline doctor checks](contracts/doctor.schema.json) (v2).

Offline discovery and compatibility checks:

```bash
./venv/bin/trend-engine --version
./venv/bin/trend-engine schema
./venv/bin/trend-engine schema doctor
./venv/bin/trend-engine doctor --json --min-version 0.4.0 --max-version 0.5.0 --require-report-schema 3 --require-doctor-schema 2
```

These commands are offline. `schema` prints the bundled report schema by default;
it and `--version` need no config. Doctor's version bounds are minimum inclusive,
maximum exclusive. It checks compatibility and key presence, not validity or
freshness; see [AGENTS.md](AGENTS.md) for output and error handling.

`skills/` is ignored by this repository and ready for its own repository. Its
`trend-analisis/SKILL.md` teaches OpenClaw to call the installed `trend-engine` on PATH;
`skills/README.md` explains discovery, installation paths, and environment setup.
Git initialization, OpenClaw installation, and live configuration are left to you.
The standalone skill includes copies of both output schemas,
`compatibility.json`, independent offline tests, and a manually triggered CI workflow
that accepts an explicit CLI repository and release revision. It does not import
this checkout. Supply a trusted absolute config path in the request or set
`TREND_ENGINE_CONFIG` for the skill to pass as `--config`; the CLI does not read
that environment variable itself.

Keys saved in `.env` are **not automatically loaded**. Export them into the CLI's
process environment or configure the agent's environment securely. Do not commit
keys to either repository. The offline `doctor` command reports presence only.

## Hacker News: main information and tech channel

Enabled by default using the [official Hacker News API](https://github.com/HackerNews/API).
No API key, account, paid service, browser, or extra Python dependency is needed.
The API documentation currently states no rate limit; requests still use the
existing bounded worker pool, timeouts, and transient-error retries.

One request gets `topstories.json`, then one request per sampled item gets its
title, points, comment count, author, publication time, article URL, and HN discussion
URL. The existing `limit` setting caps the sample: 25 entries means at most 26
requests before retries, once per run, not once per country. No comments, user
profiles, or linked articles are crawled. Jobs, dead/deleted stories, and missing
items are skipped without filling the gaps or changing the original feed ranks.

HN leads the Markdown report in **Global information and tech trends**. JSON adds
`global_trends` and `platform_trends.hacker_news.global`; observations have
`country: null` and `region: "global"`. The feed has no country breakdown, so HN
stories are not counted as North American or Asian trends. This is a sample of HN
community interest, not a claim that every story is tech or globally popular.
Feed-rank changes compare successive snapshots; they do not predict market impact.

Failed individual requests preserve other stories and produce a `partial` health
status; useful partial samples exit with 0. A failed list request or no usable
stories after request failures produces `failed` and exit 1. Explicit `--sources`
or a custom configuration source list still selects only the named sources.

## Source audit

Reviewed September 25, 2026. Free access still has quotas and coverage limits.
Request counts below exclude retries and assume the eight default countries.
Search/social feeds are supporting attention signals, not news-only feeds or
verification that a headline is true. Entertainment and advertising may appear.

| Source | Free? / key? | Implementation and cost per run | Limits and decision |
|---|---|---|---|
| [Hacker News](https://github.com/HackerNews/API) | Free, no key | Official API, 1 top-list GET + up to `limit` item GETs (26 by default) | Main information/tech channel, enabled by default. Global only; no regional attribution. No published rate limit currently. RSS is a cheaper one-request alternative but lacks these structured metrics. |
| [Google Trends](https://support.google.com/trends/answer/3076011?hl=en) | Free, no key | Official RSS, 8 GETs | Keep. Search interest is a separate signal, not TikTok/Instagram activity. Feed order is not a view-count ranking. |
| [TikTok Creative Center](https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en) | Public samples, no key | 7 hashtag POSTs + 6 video GETs | Keep the existing public page requests as best effort. Undocumented endpoints; no published quota or availability guarantee. Samples cover 7 days and may include advertising. No login/browser fallback. |
| [YouTube Data API](https://developers.google.com/youtube/v3/docs/videos/list) | Free quota, API key required | 8 GETs, 1 quota unit each | Keep optional. Current [mostPopular coverage](https://developers.google.com/youtube/v3/guides/implementation/videos) is music, movies, and gaming. Not all YouTube trends. |
| Instagram | No suitable general regional feed verified | No requests | Leave disabled. A logged-in scraping stack or paid proxy service adds cost and maintenance without a dependable general trend feed. Manual checks remain an option. |

TikTok hashtags are attempted in every configured market except India; videos are
attempted only in US, JP, and ID. The refactor's live US check returned three
hashtags and four videos without login; this is an
observed limitation, not a guaranteed response size. Songs/creators are not collected.
A missing or failed source stays visible in the health section.

YouTube's current [default quota](https://developers.google.com/youtube/v3/getting-started)
includes 10,000 daily units for the endpoint group containing `videos.list`.
Use the Cloud console to check your project's actual quota. Eight countries four
times daily would use 32 units before retries.

Free collectors can still return 429/403. The app honors short `Retry-After`
values, stops on access denial, and records failure.
There is no proxy rotation or authentication bypass.

Free alternatives worth considering, **not integrated**:

- [Hacker News RSS](https://news.ycombinator.com/rss) is a single-request alternative
  to the integrated API when titles and links alone are sufficient.
- YouTube channel upload feeds can track a known channel list without an API key.
  They measure new uploads, not country-wide popularity.
- Google Trends RSS can provide broader topic discovery where a social source
  is unavailable. Reports must continue to label it Google search data.

For future integrations, first record cost, key requirements, quota, geographic
coverage, and an example response. Prefer one RSS/JSON request and a parser.
Only add a browser when a required public source cannot be read directly.

## How reports work

Only the **latest collection run within the requested window** supplies current
results. Earlier successful items are not carried forward after a failed run.
A source-only collection therefore produces a source-only report. Snapshots older
than six hours are labeled stale. The window selects stored history; it does not
change TikTok's 7-day sample or refresh upstream feeds.

Topics with the same normalized title are grouped (case/spacing/punctuation
ignored, so `#solareclipse` and `Solar eclipse` can group). There is no translation,
fuzzy matching, LLM, or embedding model. Regional score is the sum of reciprocal
feed positions, counting the best position once per source/country. This is a
transparent way to order sampled evidence, not a statistical measure of virality.

Rank change compares the same source/country/kind/item in the immediately previous
run within the window. The comparison timestamp is included; a first observation
has unknown movement. The tool does not claim an arbitrary interval is six hours.

Use evidence URLs, publication/observation times, and coverage gaps when reviewing
news. The CLI does not verify stories or establish that a headline moved a market.

The intended collection cadence remains six hours, using an external scheduler
if your chosen sources support your usage. No background scheduler is bundled.

## Small codebase

- `src/social.py`: HTTP calls and news/search/social parsers, including HN.
- `src/fetch.py`: requests, timeouts, retries, and numeric parsing.
- `src/collect.py`: build the work list, run collectors, combine their results.
- `src/models.py`: the Peewee `Snapshot` model for the existing SQLite schema.
- `src/storage.py`: transactional model writes and read-only history queries.
- `src/scoring.py`, `src/reporting.py`: ranking and Markdown/JSON output.
- `src/config.py`, `src/cli.py`: settings and command-line commands.
- `src/compatibility.py`: CLI version, bundled schemas, and offline agent checks.

These are plain scripts/modules, with no `src/trend_engine` package. Local imports
use module names directly, such as `from storage import save_snapshot`. `fetch.py`
avoids shadowing Python's built-in `http` package during direct script execution.
Keep functions untyped, use `"%s" % value` formatting, and add short comments for
the purpose of each functional block. The linter preserves percent formatting.

Readability is a hard rule for application code and tests: use ordinary loops,
named intermediate values, and separate sorting steps. Avoid nested comprehensions,
transformation pipelines inside arguments, and compact dictionary merges. Separate
logical blocks inside functions with blank lines and explain non-obvious rules
with short comments. More readable lines are better than a clever one-liner; see
[AGENTS.md](AGENTS.md#readability-is-a-hard-rule) for the review checklist.

Removed Typer, Pydantic, SQLAlchemy, HTTPX, BeautifulSoup, PyYAML, and Jinja2.
Removed the collector protocol/classes, ORM conversion layers, unused Playwright
fallback, sentence-transformer option, custom weighted heat score, duplicate report
tables, and nested thread pools. Collectors are functions; storage uses a Peewee model.

The earlier simplification removed about half of the original 2,502 application
lines. Peewee is now the sole runtime dependency; collectors and the CLI still
use the standard library.

The existing database and generated reports are preserved. New collections use a
single `snapshots` table with its existing text timestamp and JSON payload columns.
The `Snapshot` model retains the original table and index names, so switching to
Peewee requires no data rewrite. The reader uses Peewee's
[reflected models](https://docs.peewee-orm.com/en/latest/peewee/db_tools.html#reflection)
for the original history tables; those tables are never changed. Old snapshots
remain labeled legacy because they predate parser fixes. Connections close after
each operation, writes use a transaction, and reports open SQLite in read-only mode.

## Upgrading to the news-focused CLI (0.4.0)

Report JSON is now v3: `hot_robinhood_crypto`, `hot_solana_tokens`, and
`social_asset_matches` are removed. Doctor is v2 and no longer checks CoinGecko.
Update agent version checks and install the matching `trend-analisis` skill.

For older custom configs, remove `robinhood`/`solana` from `sources` and delete
`solana_min_liquidity_usd` and `solana_min_volume_24h_usd`. Other settings and paths
stay the same. Removed sources/options are rejected rather than silently enabled.
`COINGECKO_API_KEY` is no longer used; existing environment files are not modified.

Existing snapshots and generated reports are not rewritten. New reports ignore
historical market data, including its health rows. A latest snapshot with only
market data produces an empty news report and exit 1; it does not reuse older
headlines. Headlines mentioning crypto are still news, not token recommendations.

Tests use Python's built-in unittest runner. The development extra adds Ruff and
`jsonschema` for validating the agent contracts; neither is a runtime dependency:

```bash
./venv/bin/python -m pip install -e '.[dev]'
./venv/bin/python -m unittest discover -s tests -v
./venv/bin/ruff check src tests
./venv/bin/ruff format --check src tests
```

Tests cover recorded responses, source failures, key redaction, non-finite numbers,
latest-run semantics, legacy history, configuration, and CLI execution. They also
check that old market data stays stored but does not appear in new reports.
Storage tests also verify compatibility with existing snapshot files, read-only
access, transaction rollback, and isolation between database connections.
HN tests cover request caps, the shared worker pool, global attribution, removed
items, partial failures, rank changes, JSON/Markdown reports, and CLI source selection.
