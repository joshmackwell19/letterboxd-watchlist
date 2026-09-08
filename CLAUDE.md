# letterboxd-watchlist

Cross-checks Josh's (and additively, Sarah's) Letterboxd watchlist against
JustWatch streaming availability, publishing a single-page dashboard and
sending email notifications. No official Letterboxd/JustWatch API exists —
both are scraped.

## Data flow

```
Letterboxd (scrape) ─┐
JustWatch (offers)  ─┼─► main.py run() ─► Postgres (Neon) ─► dashboard.py ─► dashboard.html
TMDB (enrich/discover)┘        │                                    │
                                │                                    ├─► GitHub Pages (public site)
                                └─► Resend email (diff/digest)        └─► iCloud (local sync script)

Dashboard UI writes (Settings save / dismiss / Review tab)
        │
        ▼
Cloudflare Worker (worker/) ──► GitHub Actions workflow_dispatch ──► main.py (one-off flag) ──► Postgres
```

`main.py`'s `run()` is the only thing that scrapes or writes most of
Postgres. Everything downstream of the database (the dashboard itself, the
Worker) only ever reads it or makes small, targeted writes to specific
tables — never a full rescan.

## Source layout (`src/watchlist_justwatch/`)

| File | Responsibility |
|---|---|
| `main.py` | CLI entrypoint; `run()` is the daily pipeline, everything else is a one-off flag (see below) |
| `letterboxd.py` | Scrapes Letterboxd (watchlist, diary, film detail pages) via `curl_cffi` browser impersonation |
| `justwatch_client.py` | Wraps `simple-justwatch-python-api`; `resolve_and_fetch` matches a film to JustWatch and pulls its offers |
| `tmdb_client.py` | Thin TMDB API wrapper — search, similar/recommended, discover, person credits, `original_language` |
| `config.py` | Loads `config/*.yaml`; the have/free_tier/subscription classification logic |
| `db.py` | All Postgres reads/writes — schema lives in the `SCHEMA` string at the top, applied idempotently on every connect |
| `state.py` | `StateDoc` — the in-memory shape of everything in Postgres for one run |
| `models.py` | `FilmState`/`OfferRecord`/`WatchlistFilm` — the core data shapes |
| `diff.py` | Classifies what's new since yesterday (`have`/`free_tier`/`new_possible`/new films/unmatched) for the daily email |
| `similar.py` | TMDB-correlated discovery (`because_you_watched`, by director/cast/genre, hidden gems, popular, rewatch) |
| `dashboard.py` | Builds the dashboard's JSON payload from `StateDoc` and renders `dashboard.html` (template + embedded JS live in this one file) |
| `report.py` / `html_email.py` / `weekly_digest.py` | Email rendering (plain text / HTML / the Friday digest) |
| `notify.py` | Resend API wrapper |
| `analysis.py` | `--rank-services`/`--recommend-favorites` standalone analyses |
| `languages.py` | ISO 639-1 code → name, and the subtitled-film heuristic |
| `brands.py`, `countries.py`, `availability.py` | Small lookup/normalization helpers |

## Database (Postgres, Neon free tier — currently ~14MB total)

| Table | Written by | Notes |
|---|---|---|
| `films` | `run()`, full replace each run | Josh's watchlist **union** Sarah's — `josh_watchlist`/`sarah_watchlist` say whose list a slug came from, since this table alone can't |
| `diary` | `run()`, full replace | Every film ever logged as watched; backfilled once via `--backfill-diary` (must run locally — Letterboxd blocks GH Actions' IPs from `/username/films/`) |
| `discovery_films` | `run()`, full replace | TMDB-correlated films surfaced by `similar.py` that aren't on the watchlist itself |
| `recommendation_sections` | `run()`, full replace | Which slugs go in which home-page discovery section |
| `josh_watchlist` / `sarah_watchlist` | `run()`, full replace | Membership only (slugs) — offer data lives in `films` regardless of which list |
| `meta` | `run()`, full replace | `last_run_at`, `last_justwatch_check_date`, `last_seen_diary_guid`, `recent_watches`, `recent_additions` |
| `watch_together` | **Incrementally** — `seed_pending_watch_together` (new pending rows) / `set_watch_together_statuses_batch` (Review tab decisions) | Deliberately *not* part of the full-replace — see `db.py`'s own comment on `save_state` |

## GitHub Actions workflows (`.github/workflows/`)

| Workflow | Trigger | What it does |
|---|---|---|
| `daily.yml` | Cron ~08:00 UK + manual | The full pipeline: scrape, JustWatch-check (stale-rotation, ~20%/day), regenerate + deploy dashboard, send the diff email |
| `check-for-new-log.yml` | Cron hourly | Cheap RSS check for a new Letterboxd log entry; dispatches `daily.yml` early if one's found |
| `regenerate-dashboard.yml` | Dispatched by the Worker | Network-free `--dashboard` regen + deploy, optionally preceded by `--set-watch-together-statuses-batch` |
| `deploy-worker.yml` | Push to `worker/**` | Deploys `worker/` via Wrangler |
| `weekly-digest.yml` | Cron Friday ~17:00 UK | Read-only — sends the weekly roundup email from already-stored state |

`daily.yml` and `regenerate-dashboard.yml` don't commit `dashboard.html` to
git — Pages deploys straight from each run's own build artifact. No
concurrency group links `regenerate-dashboard.yml` runs together
(deliberately — see its own file comment: sharing one with `daily.yml` used
to silently cancel rapid Review-tab decisions before their DB write ever
ran).

## Cloudflare Worker (`worker/`)

`letterboxd-refresh-trigger` — the dashboard's only write path, holding the
real GitHub PAT server-side so the public page never sees it. Deployed via
`deploy-worker.yml` on push (see above); its two secrets (`GITHUB_TOKEN`,
`TRIGGER_SECRET`) live on Cloudflare's side and aren't in `wrangler.toml`.

| Endpoint | Does |
|---|---|
| `POST /` | Triggers `daily.yml` (the dashboard's "Refresh data" button) |
| `POST /update-services` | Commits `config/services.yaml`, triggers `regenerate-dashboard.yml` |
| `POST /dismiss-recommendation` | Commits `config/dismissed_recommendations.yaml`, triggers `regenerate-dashboard.yml` |
| `POST /tag-film` | Takes `{decisions: [{slug, status}]}` (a debounce-batched set from the Review tab), triggers `regenerate-dashboard.yml` with them |

## `main.py` CLI flags

**The daily run**: plain `python -m watchlist_justwatch.main` (needs
`--username`/`LETTERBOXD_USERNAME`, `--database-url`/`DATABASE_URL`;
`--sarah-username`/`SARAH_LETTERBOXD_USERNAME` optional).

**What the dashboard itself calls** (all network-free): `--dashboard`,
`--set-watch-together-status SLUG STATUS`, `--set-watch-together-statuses-batch JSON`.

**One-off local backfills** (Letterboxd blocks GH Actions' IPs from
`/username/films/`, so these run on a Mac): `--backfill-diary`,
`--backfill-diary-ratings`, `--backfill-language` (TMDB-only, no
JustWatch/Letterboxd calls).

**Standalone analyses** (network-free, read already-stored state):
`--rank-services`, `--recommend-favorites`, `--similar-to TITLE`,
`--email-audit`, `--email-audit-by-country`, `--weekly-digest`.

**Automation-only**: `--check-for-new-log` (the hourly workflow),
`--migrate-json-to-db` (one-time legacy import, long since obsolete).

## Known constraints

- **Letterboxd blocks GitHub Actions' IP range** from anything under
  `/username/films/` (diary included) — those backfills must run locally.
- **Neon's free-tier data-transfer quota has been exceeded twice before**
  (both fixed by reading less per run, not by removing the underlying
  pattern) — current usage is small (~14MB DB, batched writes since the
  Review tab's debounce), but worth watching if usage patterns change.
- **`.git` history carries ~400MB of old `dashboard.html` blobs** from
  before it stopped being committed — growth is stopped, the historical
  weight itself needs a deliberate `git filter-repo` + force-push to
  reclaim (see git log around September 2026 for when this was last
  discussed/attempted).
- **GitHub Actions cron has no DST awareness** — `daily.yml`/
  `weekly-digest.yml`'s schedules drift an hour during UK summer time,
  accepted and documented in those files rather than worked around.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

Covers the pure classification/section-building logic (`config.py`,
`diff.py`, `languages.py`, `dashboard.py`'s home-section builders) — not an
integration suite against a real database, which would need a Postgres
fixture and is a bigger lift for less immediate value than covering the
logic most likely to silently regress.

## Local development

```bash
source .venv/bin/activate
pip install -e .
python -m watchlist_justwatch.main --dashboard   # network-free regen, safe to run anytime
```

`.env` (see `.env.example`) needs `DATABASE_URL` at minimum; most flags
need nothing else network-facing beyond that.
