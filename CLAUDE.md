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

Dashboard quick search (any film on Letterboxd, watchlisted or not)
Dashboard film detail page (TMDB's own similar / director / cast)
Dashboard person page (a director's or actor's full filmography)
        │
        ▼
Cloudflare Worker (worker/) ──► TMDB + Letterboxd + JustWatch, live ──► rendered client-side
```

`main.py`'s `run()` is the only thing that scrapes or writes most of
Postgres. Everything downstream of the database (the dashboard itself, the
Worker) only ever reads it or makes small, targeted writes to specific
tables — never a full rescan. Quick search is the one path that touches the
database not at all: it reads live and renders, storing nothing, so a
searched film leaves no trace and costs no Neon quota. The film detail
page's live layer works the same way — it asks TMDB what else the director
made and what's similar, renders it alongside what's already tracked, and
stores none of it.

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
| `cinemas.py` | Scrapes showtimes for 4 London cinemas (Prince Charles, Barbican, Vue Fulham Broadway, Riverside Studios) — one fetcher per venue, each a different mechanism (plain HTML, a JSON API, an opaque-token AJAX endpoint). `clean_listing_title` strips what the venue added and the film doesn't have ("(10th Anniversary)", "- IMAX", a re-release year); `match_watchlist_film` matches a listing against the watchlist by title, and `resolve_listing_to_letterboxd` finds the Letterboxd film for everything else (TMDB for the id, then `/tmdb/<id>/` for the slug), with its two network calls injected so the matching judgement is testable without either |
| `dashboard.py` | Builds the dashboard's JSON payload from `StateDoc` and renders `dashboard.html` (template + embedded JS live in this one file); `_search_taxonomy` is what lets the page classify a *searched* film's offers without duplicating `brands.py`/`config.py` in JS. The film detail view (the long look behind quick look's "Full details") adds no payload of its own — its director/cast/genre relations are derived in JS from `films_by_slug`, which already carries every watchlist and discovery film — TMDB's own answer to the same questions arrives separately, from the Worker, and is merged in behind the local one. The person page (any director/actor name is a link to it) is the same shape one level up, and shares the film page's back-trail: `detailTrail` holds both kinds of stop, so film → director → another of their films unwinds one step at a time |
| `report.py` / `html_email.py` / `weekly_digest.py` | Email rendering (plain text / HTML / the Friday digest) |
| `notify.py` | Resend API wrapper |
| `analysis.py` | `--rank-services`/`--recommend-favorites` standalone analyses |
| `languages.py` | ISO 639-1 code → name, and the subtitled-film heuristic |
| `brands.py`, `countries.py`, `availability.py` | Small lookup/normalization helpers |

## Database (Postgres, Neon free tier — currently ~14MB total)

| Table | Written by | Notes |
|---|---|---|
| `films` | `run()`, full replace each run | Josh's watchlist **union** Sarah's — `josh_watchlist`/`sarah_watchlist` say whose list a slug came from, since this table alone can't. `tmdb_id` comes free from the same TMDB search `original_language` already makes, and is what lets the dashboard ask the Worker about a film directly; it fills in via the stale rotation (or `--backfill-language`), and a film without one just gets no live layer |
| `diary` | `run()`, full replace | Every film ever logged as watched; backfilled once via `--backfill-diary` (must run locally — Letterboxd blocks GH Actions' IPs from `/username/films/`) |
| `discovery_films` | `run()`, full replace | TMDB-correlated films surfaced by `similar.py` that aren't on the watchlist itself. Their offers are stored with `monetization_types`, so `dashboard.py` re-runs `_classify` on them at build time — a stored verdict would otherwise keep whatever config was current the day the film was discovered |
| `recommendation_sections` | `run()`, full replace | Which slugs go in which home-page discovery section |
| `josh_watchlist` / `sarah_watchlist` | `run()`, full replace | Membership only (slugs) — offer data lives in `films` regardless of which list |
| `meta` | `run()`, full replace | `last_run_at`, `last_justwatch_check_date`, `last_seen_diary_guid`, `recent_watches`, `recent_additions` |
| `watch_together` | **Incrementally** — `seed_pending_watch_together` (new pending rows) / `set_watch_together_statuses_batch` (Review tab decisions) | Deliberately *not* part of the full-replace — see `db.py`'s own comment on `save_state` |
| `cinema_showtimes` | `run()`, full replace | Raw scraped rows from `cinemas.py` only — matching against the watchlist happens fresh in `dashboard.py` at build time, not stored |
| `cinema_film_matches` | `run()`, full replace | The Letterboxd film each listing is showing, for the ~90% of the programme the watchlist can't name. Keyed by `cinemas.listing_match_key` (cleaned title + year), so the same film at three venues resolves once. Unlike the watchlist match this costs a TMDB search plus a Letterboxd page, so it's cached rather than recomputed at build time; a NULL slug is a listing with no Letterboxd film, remembered so it isn't retried daily |

## GitHub Actions workflows (`.github/workflows/`)

| Workflow | Trigger | What it does |
|---|---|---|
| `daily.yml` | Cron ~08:00 UK + manual | The full pipeline: scrape, JustWatch-check (stale-rotation, ~20%/day), regenerate + deploy dashboard, send the diff email |
| `check-for-new-log.yml` | Cron hourly | Cheap RSS check for a new Letterboxd log entry; dispatches `daily.yml` early if one's found |
| `regenerate-dashboard.yml` | Dispatched by the Worker | Network-free `--dashboard` regen + deploy, optionally preceded by `--set-watch-together-statuses-batch` |
| `deploy-worker.yml` | Push to `worker/**` | Deploys `worker/` via Wrangler |
| `weekly-digest.yml` | Cron Friday ~17:00 UK | Read-only — sends the weekly roundup email from already-stored state |
| `backfill-tmdb-ids.yml` | Manual only | `--backfill-language` (fills `original_language`/`tmdb_id` for every film missing either) + dashboard regen. Shares `daily.yml`'s concurrency group, since both replace the `films` table wholesale. Exists because the stale rotation takes ~5 days to fill a newly-added field across the whole watchlist |

`daily.yml` and `regenerate-dashboard.yml` don't commit `dashboard.html` to
git — Pages deploys straight from each run's own build artifact. No
concurrency group links `regenerate-dashboard.yml` runs together
(deliberately — see its own file comment: sharing one with `daily.yml` used
to silently cancel rapid Review-tab decisions before their DB write ever
ran).

## Cloudflare Worker (`worker/`)

`letterboxd-refresh-trigger` — the dashboard's only write path, holding the
real GitHub PAT server-side so the public page never sees it. Deployed via
`deploy-worker.yml` on push (see above); its secrets (`GITHUB_TOKEN`,
`TRIGGER_SECRET`, and `TMDB_API_KEY` for quick search) live on
Cloudflare's side and aren't in `wrangler.toml`.

| Endpoint | Does |
|---|---|
| `POST /` | Triggers `daily.yml` (the dashboard's "Refresh data" button) |
| `POST /update-services` | Commits `config/services.yaml`, triggers `regenerate-dashboard.yml` |
| `POST /dismiss-recommendation` | Commits `config/dismissed_recommendations.yaml`, triggers `regenerate-dashboard.yml` |
| `POST /tag-film` | Takes `{decisions: [{slug, status}]}` (a debounce-batched set from the Review tab), triggers `regenerate-dashboard.yml` with them |
| `POST /search-films` | Quick search, step 1: TMDB title search (+ a parallel credits call per row for the director) returning the picker list |
| `POST /film-lookup` | Quick search, step 2: for the picked film, Letterboxd details via `/tmdb/<id>/` and JustWatch offers for every country the page sends. Also what the film detail page calls when an untracked TMDB poster is tapped |
| `POST /film-relations` | The film detail page's live layer: TMDB's similar + recommendations for one film, plus the filmographies of up to 2 credited directors and 3 billed cast. Eight subrequests worst case (1 credits, then the rest in parallel), against Cloudflare's limit of 50 |
| `POST /person` | The person page: one director/actor's details and whole filmography, split into directing and acting credits. Takes `person_id` where the page has one (the relations payload carries them) or `name` where it doesn't. One subrequest by id — `append_to_response=movie_credits` folds the credits into the details call — two by name |

Anything else 404s. The base route used to be a catch-all, so a typo or a
call to an endpoint the deployed Worker didn't have yet silently kicked off
a full `daily.yml` run and answered as if it had worked.

The four TMDB-backed endpoints (`/search-films`, `/film-lookup`,
`/film-relations`, `/person`) are the only ones that touch neither GitHub
nor the database — they read live data and return it, and deliberately
don't classify anything. Turning raw JustWatch offers into have/free_tier/
could_get_again/subscription badges stays in Python (`brands.py` +
`config.py`), reaching the page as a lookup table rather than as logic
reimplemented in JS. `/film-relations` deliberately fetches no availability
at all: the page already knows it for every film it tracks, and a film it
doesn't track goes through `/film-lookup` when it's actually tapped, rather
than a hundred JustWatch lookups for posters nobody opens.

## `main.py` CLI flags

**The daily run**: plain `python -m watchlist_justwatch.main` (needs
`--username`/`LETTERBOXD_USERNAME`, `--database-url`/`DATABASE_URL`;
`--sarah-username`/`SARAH_LETTERBOXD_USERNAME` optional).

**What the dashboard itself calls** (all network-free): `--dashboard`,
`--set-watch-together-status SLUG STATUS`, `--set-watch-together-statuses-batch JSON`.

**One-off local backfills** (Letterboxd blocks GH Actions' IPs from
`/username/films/`, so these run on a Mac): `--backfill-diary`,
`--backfill-diary-ratings`.

`--backfill-language` fills `original_language` *and* `tmdb_id` (both come
from the same TMDB search) and is grouped with those historically, but it
never touches Letterboxd — so it also runs from Actions via
`backfill-tmdb-ids.yml`.

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
- **GitHub Actions cron has no DST awareness** — `daily.yml`/
  `weekly-digest.yml`'s schedules drift an hour during UK summer time,
  accepted and documented in those files rather than worked around.
- **Cinema listings resolve gradually.** `run()` resolves at most
  `CINEMA_RESOLVE_PER_RUN` (120) new listings a day, busiest film first,
  because each costs a TMDB search plus a Letterboxd page. A first run
  after a programme changes wholesale therefore leaves a tail unmatched
  until the next day or two; the cache means steady state is only the
  handful of newly announced titles.
- **Riverside Studios' showtimes come from a captured, opaque filter
  token** (see `cinemas.py`'s `RIVERSIDE_FILTER_TOKEN` comment) — their
  listing page only loads via a JS-encrypted filter widget with no
  derivable encoding, so if Riverside ever changes that widget the token
  stops working. Fails soft (that venue's fetch warns and carries
  forward yesterday's listing, same as any other cinema fetch failure)
  rather than breaking the run; fixing it for real means re-capturing
  the token by hand (open the site, click the Cinema filter, copy the
  new `/ajax/filter_stream/<token>/` request from devtools).

## Testing

```bash
pip install -e ".[dev]"
pytest                                  # the Python pipeline
node --test worker/test/*.test.mjs      # the Worker (no deps, no package.json)
```

Covers the pure classification/section-building logic (`config.py`,
`diff.py`, `languages.py`, `cinemas.py`'s title cleaning and listing
matching, `dashboard.py`'s home-section builders and its
`_search_taxonomy` — that one checks the table the page classifies searched
films from still agrees with `_classify` itself) — not an
integration suite against a real database, which would need a Postgres
fixture and is a bigger lift for less immediate value than covering the
logic most likely to silently regress.

On the Worker side, `/film-relations` and `/person` are covered against a
stubbed TMDB — including their subrequest counts, which are the thing that
would break them against Cloudflare's limit of 50 without any test
noticing. The other endpoints aren't covered yet; they were written before
there was anywhere to put a JS test.

## Local development

```bash
source .venv/bin/activate
pip install -e .
python -m watchlist_justwatch.main --dashboard   # network-free regen, safe to run anytime
```

`.env` (see `.env.example`) needs `DATABASE_URL` at minimum; most flags
need nothing else network-facing beyond that.
