import argparse
import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from .analysis import (
    films_not_on_favorite,
    films_not_on_favorite_by_country,
    rank_missing_services,
    recommend_extra_countries,
    recommend_new_favorites,
    render_favorite_recommendations,
    render_ranking,
)
from .cinemas import (
    fetch_barbican,
    fetch_prince_charles,
    fetch_riverside,
    fetch_vue,
    MATCHER_VERSION,
    drop_past_showings,
    listing_match_key,
    match_watchlist_film,
    resolve_listing_to_letterboxd,
)
from .config import (
    load_config, load_dismissed_recommendations, load_favorites, load_global_subscriptions,
    load_main_services, load_revisitable_services,
)
from .custom_lists import all_source_paths, load_custom_lists, total_groups
from .dashboard import build_dashboard_data, compute_offer_snapshot, render_dashboard_html
from .db import (
    connect, custom_list_source_fetch_times, custom_list_source_totals, get_meta_value,
    load_custom_list_memberships, load_state, load_taste_inputs, load_watch_together, save_custom_list_source,
    save_state, seed_pending_watch_together, set_watch_together_status, set_watch_together_statuses_batch,
)
from .diff import build_report
from .html_email import (
    render_country_audit_html,
    render_country_audit_text,
    render_film_audit_html,
    render_film_audit_text,
    render_report_html,
    render_weekly_digest_html,
    render_weekly_digest_text,
)
from .justwatch_client import resolve_and_fetch
from .letterboxd import (
    LetterboxdFetchError,
    fetch_diary_ratings,
    fetch_list_slugs,
    fetch_new_diary_entries,
    fetch_rated_films,
    fetch_recent_watches,
    fetch_watched_films,
    fetch_watchlist,
    get_film_details_by_slug,
    get_film_details_by_tmdb_id,
)
from .models import FilmState, OfferRecord, WatchlistFilm
from .notify import send_if_configured
from .report import render_report
from .similar import (
    discover_because_watched,
    discover_by_cast_members,
    discover_by_directors,
    discover_by_genre,
    discover_hidden_gems,
    discover_popular_now,
    discover_rewatch,
    find_similar,
    render_similar,
)
from .state import StateDoc, get_cached_entry_id
from .taste import (
    PoliteFetcher, community_ratings_from_diary, evaluate, my_ratings_from_diary, recommend,
    render_evaluation, render_recommendations, scrape_raters,
)
from .tmdb_client import search_movie as _tmdb_search_movie
from .weekly_digest import compute_weekly_digest

DEFAULT_CONFIG_PATH = Path("config/services.yaml")
DEFAULT_FAVORITES_PATH = Path("config/favorites.yaml")
DEFAULT_REVISITABLE_PATH = Path("config/revisitable_services.yaml")
DEFAULT_DISMISSED_PATH = Path("config/dismissed_recommendations.yaml")
DEFAULT_MAIN_SERVICES_PATH = Path("config/main_services.yaml")
DEFAULT_CUSTOM_LISTS_PATH = Path("config/custom_lists.yaml")
DEFAULT_DASHBOARD_PATH = Path("dashboard.html")
# Source lists (award nominees, festival lineups) change rarely — no need
# to re-walk a 600-film list's pages every single day.
CUSTOM_LIST_SOURCE_REFRESH_DAYS = 7
# Per daily run — festival sources number in the hundreds once per-year
# lineups are included (TIFF and NYFF alone are ~115 lists), so each run
# refreshes only the stalest few rather than hammering Letterboxd with
# every one on the same day. 30/day cycles ~200 sources inside the week.
CUSTOM_LIST_SOURCES_PER_RUN = 30

# JustWatch offers are the slow, rate-limit-fragile part of each run (see
# justwatch_client.resolve_and_fetch's mandatory pacing sleep) and rarely
# change day to day, so most of the watchlist is checked on a rotation
# instead of every film every day. 0.20 means a ~5-day full cycle, within
# the 10-25%/day (4-10 day cycle) range that felt safe for how often a
# watchlist film's availability actually shifts.
STALE_BATCH_FRACTION = 0.20

# recent_additions used to be capped by count (newest 200), which meant a
# single busy day (e.g. a forced full re-check, or just a lot of offers
# landing at once) could evict genuinely-this-week entries before a weekly
# digest ever saw them — silent under-reporting with no error. Retention is
# by age instead: comfortably longer than the 7-day window anything reads
# today, so a slow week never gets truncated regardless of how many entries
# a single busy day produces.
RECENT_ADDITIONS_RETENTION_DAYS = 35


def _fetch_tmdb_facts(title: str, year: int | None) -> tuple[int | None, str | None]:
    """(tmdb_id, original_language) from one TMDB search.

    The language is for the "is this subtitled" flag (see
    tmdb_client.search_movie for why it isn't Letterboxd's own
    inLanguage); the id is what the dashboard hands the Worker to ask TMDB
    about a film directly. They come from the same search result, so taking
    both costs nothing over taking one.

    Best-effort: a title/year mismatch or a TMDB hiccup shouldn't cost the
    film's whole enrichment, same reasoning as every other best-effort
    fetch here.
    """
    try:
        movie = _tmdb_search_movie(title, year)
    except Exception as exc:
        print(f"warning: failed to fetch TMDB facts for {title!r}, leaving unset ({exc})", file=sys.stderr)
        return None, None
    if movie is None:
        return None, None
    return movie.get("id"), movie.get("original_language")


def _fetch_original_language(title: str, year: int | None) -> str | None:
    """The language half alone, for the diary/discovery rows that have
    nowhere to put an id."""
    return _fetch_tmdb_facts(title, year)[1]


# A first run has every listing to resolve at two network calls each; after
# that it's only what's newly announced. Capped so the first one doesn't add
# ten minutes to the pipeline — the rest resolve the next day, and the day
# after, which is soon enough for a listing that runs for weeks.
CINEMA_RESOLVE_PER_RUN = 120
# A listing that resolved to nothing is usually event cinema and always will
# be, but occasionally it's a film TMDB hadn't indexed yet on announcement.
# Re-asking a few weeks later costs almost nothing and catches those.
CINEMA_NEGATIVE_RETRY_DAYS = 30


def _resolved_cinema_matches(
    showtimes: list[dict], films: dict, previous: dict[str, dict | None], *, warn,
) -> dict[str, dict | None]:
    """Letterboxd films for the cinema listings the watchlist can't identify.

    The watchlist answers for about a tenth of what's on — everything else
    was showing as a bare title with no poster of ours, no rating and
    nowhere to click through to. This resolves the rest through TMDB and
    Letterboxd and caches the answer, since unlike the watchlist match it's
    far too expensive to redo at dashboard-build time.
    """
    today = datetime.now(timezone.utc).date()
    resolved: dict[str, dict | None] = {}
    budget = CINEMA_RESOLVE_PER_RUN

    # Busiest film first, so on the runs where the budget binds — the first
    # couple, when nothing is cached — it's spent on the films with thirty
    # showings rather than the one-off matinee that happens to scrape first.
    showings_per_key: dict[str, int] = {}
    for showing in showtimes:
        showings_per_key[listing_match_key(showing["title"], showing["year"])] = (
            showings_per_key.get(listing_match_key(showing["title"], showing["year"]), 0) + 1)
    showtimes = sorted(
        showtimes,
        key=lambda s: -showings_per_key[listing_match_key(s["title"], s["year"])])

    for showing in showtimes:
        # Already on the watchlist: that match is better than anything this
        # could find, and it's recomputed at build time anyway.
        if match_watchlist_film(showing["title"], showing["year"], films):
            continue
        key = listing_match_key(showing["title"], showing["year"])
        if key in resolved:
            continue

        cached = previous.get(key, "missing")
        if cached != "missing":
            stale = False
            if cached is None or not cached.get("slug"):
                # Retried either because the rules that failed it have since
                # changed, or because enough time has passed that TMDB may
                # have indexed a title it hadn't on announcement.
                if (cached or {}).get("matcher_version", 1) != MATCHER_VERSION:
                    stale = True
                else:
                    stamp = (cached or {}).get("resolved_at", "")[:10]
                    try:
                        stale = (today - date.fromisoformat(stamp)).days >= CINEMA_NEGATIVE_RETRY_DAYS
                    except ValueError:
                        stale = True
            if not stale:
                resolved[key] = cached
                continue

        if budget <= 0:
            continue
        budget -= 1
        try:
            match = resolve_listing_to_letterboxd(
                showing["title"], showing["year"],
                search_movie=_tmdb_search_movie,
                film_details_by_tmdb_id=get_film_details_by_tmdb_id,
            )
        except Exception as exc:
            # Not cached: a failure here is about the network, not about the
            # listing, so it shouldn't be remembered as "this isn't a film".
            warn(f"cinema listing {showing['title']!r} could not be resolved this run ({exc})")
            continue
        resolved[key] = {**match, "resolved_at": today.isoformat()} if match else {
            "slug": None, "resolved_at": today.isoformat(), "matcher_version": MATCHER_VERSION,
        }
        time.sleep(0.2)

    return resolved


def _refresh_custom_list_sources(database_url: str, custom_lists, warn, *, force: bool = False,
                                 limit: int | None = None) -> dict[str, int]:
    """Fetches each Letterboxd list a custom list sources from, skipping any
    fetched within CUSTOM_LIST_SOURCE_REFRESH_DAYS unless forced, stalest
    (never-fetched first) up to `limit` per call. A failed or empty fetch
    warns and keeps the previously cached copy (same carry-forward-on-
    failure pattern as the cinema fetchers). Returns source -> film count
    for whatever was actually fetched."""
    fetch_times = custom_list_source_fetch_times(database_url)
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=CUSTOM_LIST_SOURCE_REFRESH_DAYS)
    due = [
        path for path in all_source_paths(custom_lists)
        if force or path not in fetch_times or datetime.fromisoformat(fetch_times[path]) < stale_cutoff
    ]
    due.sort(key=lambda path: fetch_times.get(path, ""))
    if limit is not None:
        due = due[:limit]

    fetched: dict[str, int] = {}
    for path in due:
        try:
            slugs = fetch_list_slugs(path)
        except LetterboxdFetchError as exc:
            warn(f"custom list source {path!r} fetch failed, keeping cached copy ({exc})")
            continue
        if not slugs:
            # An empty result is far more likely a markup change or a
            # renamed list than a list that's genuinely been emptied.
            warn(f"custom list source {path!r} returned no films, keeping cached copy")
            continue
        save_custom_list_source(database_url, path, slugs, now.isoformat())
        fetched[path] = len(slugs)
    return fetched


def _custom_list_inputs(database_url: str, custom_lists, state: StateDoc) -> dict:
    """build_dashboard_data's custom-list kwargs, read from the cached
    sources — only watchlist/diary members and per-list totals, never the
    full source lists (see db.load_custom_list_memberships)."""
    if not custom_lists:
        return {"custom_lists": []}
    return {
        "custom_lists": custom_lists,
        "list_sources": load_custom_list_memberships(database_url, set(state.films) | set(state.diary)),
        "list_totals": custom_list_source_totals(database_url, total_groups(custom_lists)),
    }


def run(username: str, config_path: Path, database_url: str, *, sarah_username: str | None = None,
        progress: bool = True) -> int:
    # Collects the same messages already printed to stderr on a partial
    # failure (a discovery section, a per-film check, Sarah's watchlist
    # fetch) — those are all caught and carried-forward-on-failure by
    # design, so the run still reports "success", and stderr alone is easy
    # to never actually look at. Surfaced at the end of run() via
    # $GITHUB_STEP_SUMMARY and prepended to the day's email, so a quietly
    # degraded run doesn't look identical to a clean one.
    run_warnings: list[str] = []

    def _warn(msg: str) -> None:
        print(f"warning: {msg}", file=sys.stderr)
        run_warnings.append(msg)

    config = load_config(config_path)
    global_subscriptions = load_global_subscriptions(config_path)
    favorites = load_favorites(DEFAULT_FAVORITES_PATH)
    revisitable = load_revisitable_services(DEFAULT_REVISITABLE_PATH)
    dismissed = load_dismissed_recommendations(DEFAULT_DISMISSED_PATH)
    previous_state = load_state(database_url)

    films = fetch_watchlist(username)
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    recent_watch_films = fetch_recent_watches(username, limit=4)
    recent_watches = []
    # The full watch-history backfill (see --backfill-diary) has to run
    # locally — Letterboxd blocks anything under /username/films/ (diary
    # included) from GitHub Actions' IP range. Once backfilled, this run
    # just merges the last few watches in each day (already fetched above
    # for recent_watches, so no extra requests), which keeps state.diary
    # reasonably current between full backfills without ever touching the
    # blocked endpoint from here.
    current_state_diary = dict(previous_state.diary)
    for w in recent_watch_films:
        details = get_film_details_by_slug(w.slug)
        recent_watches.append({
            "slug": w.slug, "title": w.title, "year": w.year,
            "director": details["director"], "starring": details["starring"],
        })
        if w.slug not in current_state_diary:
            current_state_diary[w.slug] = {
                "title": w.title, "year": w.year, "rating": details["rating"],
                "poster_url": details["poster_url"],
                "director": ", ".join(details["director"]) if details["director"] else None,
                "starring": details["starring"], "synopsis": details["synopsis"],
                "genre": details["genre"], "original_language": _fetch_original_language(w.title, w.year),
            }

    # Sarah's own watchlist — additive (shown in its own dashboard tab),
    # never implies watch_together status. Optional: no-ops entirely if
    # she isn't configured. Films exclusive to her list now go through the
    # exact same JustWatch/enrichment pipeline as Josh's own (see
    # combined_films below) so quick-look/services work identically for her
    # films too — only the membership (whose list a slug came from) is
    # tracked separately, in sarah_watchlist_slugs / josh_watchlist_slugs.
    # A fetch failure just carries forward yesterday's list (reconstructed
    # from already-known films, so those still get their normal stale-
    # rotation recheck rather than silently dropping out for the day).
    josh_watchlist_slugs = {f.slug for f in films}
    sarah_watchlist_slugs = set(previous_state.sarah_watchlist)
    sarah_films: list[WatchlistFilm] = []
    if sarah_username:
        try:
            sarah_films = fetch_watchlist(sarah_username)
            sarah_watchlist_slugs = {f.slug for f in sarah_films}
        except Exception as exc:
            _warn(f"failed to fetch Sarah's watchlist, carrying forward yesterday's ({exc})")
            sarah_films = [
                WatchlistFilm(slug=slug, title=previous_state.films[slug].title, year=previous_state.films[slug].year)
                for slug in sarah_watchlist_slugs if slug in previous_state.films
            ]

    # Correlated across all of TMDB, not just the watchlist, so these can
    # surface films worth discovering rather than only re-surfacing what's
    # already tracked. Done early (like the recent-watches fetch above),
    # before the per-film loop's ~600+ requests pile up.
    #
    # because_you_watched/director:*/cast:*/by_genre/rewatch are all seeded
    # from recent_watches (+ the diary, which itself only grows when
    # recent_watches does) — if neither changed since yesterday, recomputing
    # them would just spend several minutes of TMDB/Letterboxd/JustWatch
    # calls to land on the same films, so they're carried forward instead.
    # hidden_gems/popular_now aren't seeded from recent_watches at all
    # (trending/highly-rated-overall) and are genuinely time-varying, so
    # those still run every time.
    def _is_recent_watch_seeded(key: str) -> bool:
        return key in {"because_you_watched", "by_genre", "rewatch"} or key.startswith(("director:", "cast:"))

    recent_watches_unchanged = (
        bool(recent_watches)
        and [w["slug"] for w in recent_watches] == [w["slug"] for w in previous_state.recent_watches]
    )

    recommendation_sections: list[dict] = []
    discovery_films: dict[str, dict] = {}
    if not recent_watches:
        # Fetch failed entirely — carry forward everything rather than lose
        # the whole home page's discovery content for the day.
        recommendation_sections = list(previous_state.recommendation_sections)
        discovery_films = dict(previous_state.discovery_films)
    else:
        if recent_watches_unchanged:
            for section in previous_state.recommendation_sections:
                if _is_recent_watch_seeded(section["key"]):
                    recommendation_sections.append(section)
                    for slug in section["slugs"]:
                        if slug in previous_state.discovery_films:
                            discovery_films[slug] = previous_state.discovery_films[slug]

        already_seen = set(current_state_diary.keys())
        exclude_slugs = {w["slug"] for w in recent_watches} | already_seen | set(discovery_films.keys()) | dismissed

        # Each discoverer returns a *list* of (key, header, slugs, films_map)
        # sections rather than always exactly one — director/cast produce
        # one section per unique person (however many that turns out to be)
        # so each header names exactly one director/actor, instead of one
        # combined section whose header couldn't say which recommended film
        # came from which person. Called one at a time (not pre-built into a
        # tuple) so exclude_slugs.update() below actually takes effect
        # between them — otherwise every section would be computed against
        # the same starting exclusion set and could independently pick the
        # same film.
        def _single(key: str, result: tuple[str, list[str], dict]) -> list[tuple[str, str, list[str], dict]]:
            header, slugs, films_map = result
            return [(key, header, slugs, films_map)] if slugs else []

        discoverers = []
        if not recent_watches_unchanged:
            discoverers += [
                ("because_you_watched", lambda ex: _single("because_you_watched", discover_because_watched(
                    recent_watches, now_iso, config, global_subscriptions, revisitable, ex))),
                ("director sections", lambda ex: discover_by_directors(
                    recent_watches, now_iso, config, global_subscriptions, revisitable, ex)),
                ("cast sections", lambda ex: discover_by_cast_members(
                    recent_watches, now_iso, config, global_subscriptions, revisitable, ex)),
                ("by_genre", lambda ex: _single("by_genre", discover_by_genre(
                    recent_watches, now_iso, config, global_subscriptions, revisitable, ex))),
            ]
        discoverers += [
            ("hidden_gems", lambda ex: _single("hidden_gems", discover_hidden_gems(
                now_iso, config, global_subscriptions, revisitable, ex))),
            ("popular_now", lambda ex: _single("popular_now", discover_popular_now(
                now_iso, config, global_subscriptions, revisitable, ex))),
        ]
        for name, discoverer in discoverers:
            # Each section is a nice-to-have on top of the core watchlist
            # refresh below, not essential to it — an API hiccup (TMDB rate
            # limit, JustWatch timeout) in one section shouldn't cost the
            # whole daily run, so it's skipped rather than left to crash out.
            try:
                results = discoverer(exclude_slugs)
            except Exception as exc:
                _warn(f"discovery section {name!r} failed, skipping it ({exc})")
                continue
            for key, header, slugs, films_map in results:
                recommendation_sections.append({"key": key, "header": header, "slugs": slugs})
                discovery_films.update(films_map)
                exclude_slugs.update(slugs)

        if not recent_watches_unchanged:
            # Rewatch pulls FROM the diary on purpose, so it can't receive
            # the full diary as an exclusion set like the sections above —
            # only whatever they already claimed, so nothing shows twice.
            rewatch_exclude = exclude_slugs - already_seen
            try:
                header, slugs, films_map = discover_rewatch(
                    current_state_diary, recent_watches, now_iso, config, global_subscriptions, revisitable,
                    rewatch_exclude)
            except Exception as exc:
                _warn(f"discovery section 'rewatch' failed, skipping it ({exc})")
                slugs = []
            if slugs:
                recommendation_sections.append({"key": "rewatch", "header": header, "slugs": slugs})
                discovery_films.update(films_map)

    # The actual pool the JustWatch/enrichment loop below processes: Josh's
    # watchlist UNION Sarah's — a film on both only needs resolving once.
    # Order favors Josh's own WatchlistFilm object when a slug is on both
    # (arbitrary — the two lists always agree on title/year for the same
    # slug anyway).
    combined_films_by_slug: dict[str, WatchlistFilm] = {f.slug: f for f in sarah_films}
    combined_films_by_slug.update({f.slug: f for f in films})
    combined_films = list(combined_films_by_slug.values())

    # New watchlist additions have no cached offers yet, so they're always
    # checked live; films dropped from both watchlists just aren't in
    # `combined_films` any more and fall out of current_state.films
    # naturally. Everything else is checked on a stale-first rotation (see
    # STALE_BATCH_FRACTION), except on the 1st of the month, when service
    # libraries most often change, so the whole pool gets a live check
    # regardless of how recently each film was last checked.
    #
    # That rotation is capped to once per calendar day regardless of how
    # many times the workflow actually runs that day — re-triggering
    # manually to test something (a dashboard tweak, the pipeline itself)
    # shouldn't repeat several minutes of JustWatch checks that already
    # happened earlier the same day.
    today_str = now_dt.date().isoformat()
    already_refreshed_today = previous_state.last_justwatch_check_date == today_str
    new_slugs = {film.slug for film in combined_films if film.slug not in previous_state.films}
    if already_refreshed_today:
        checked_today = new_slugs
    elif now_dt.day == 1:
        checked_today = {film.slug for film in combined_films}
    else:
        existing = [film for film in combined_films if film.slug not in new_slugs]
        existing.sort(key=lambda film: previous_state.films[film.slug].last_checked)
        batch_size = max(0, round(len(combined_films) * STALE_BATCH_FRACTION) - len(new_slugs))
        checked_today = new_slugs | {film.slug for film in existing[:batch_size]}

    current_state = StateDoc(last_run_at=now_iso, last_justwatch_check_date=today_str,
                              last_seen_diary_guid=previous_state.last_seen_diary_guid)
    for i, film in enumerate(combined_films, start=1):
        if progress and i % 25 == 0:
            print(f"...processed {i}/{len(combined_films)} films", file=sys.stderr)

        previous_film = previous_state.films.get(film.slug)
        if film.slug not in checked_today and previous_film is not None:
            current_state.films[film.slug] = previous_film
            continue

        # A JustWatch/Letterboxd hiccup on one film shouldn't cost the whole
        # day's refresh — carry forward its previous state (same as an
        # unchecked-today film above) rather than let the exception escape
        # and abandon every other film's already-completed work along with
        # it (nothing gets saved until the end of this loop).
        try:
            cached_entry_id, cached_confidence = get_cached_entry_id(previous_state, film.slug)
            film_state = resolve_and_fetch(film, cached_entry_id, cached_confidence, now_iso=now_iso)

            # original_language/runtime_minutes were added after most films
            # were already enriched — checking for them here (not just
            # poster_url) means every film missing either gets backfilled
            # the next time its own stale-rotation turn comes up (see
            # STALE_BATCH_FRACTION above), rather than needing a separate
            # one-off backfill pass.
            if (previous_film is not None and previous_film.poster_url is not None
                    and previous_film.original_language is not None
                    and previous_film.runtime_minutes is not None
                    and previous_film.tmdb_id is not None):
                film_state.rating = previous_film.rating
                film_state.poster_url = previous_film.poster_url
                film_state.director = previous_film.director
                film_state.starring = previous_film.starring
                film_state.synopsis = previous_film.synopsis
                film_state.genre = previous_film.genre
                film_state.original_language = previous_film.original_language
                film_state.runtime_minutes = previous_film.runtime_minutes
                film_state.tmdb_id = previous_film.tmdb_id
            else:
                details = get_film_details_by_slug(film.slug)
                film_state.rating = details["rating"]
                film_state.poster_url = details["poster_url"]
                film_state.director = details["director"]
                film_state.starring = details["starring"]
                film_state.synopsis = details["synopsis"]
                film_state.genre = details["genre"]
                film_state.tmdb_id, film_state.original_language = _fetch_tmdb_facts(film.title, film.year)
                film_state.runtime_minutes = details["runtime_minutes"]
        except Exception as exc:
            _warn(f"failed to check {film.slug!r}, skipping it this run ({exc})")
            if previous_film is not None:
                current_state.films[film.slug] = previous_film
            continue

        current_state.films[film.slug] = film_state

    current_state.recent_watches = recent_watches
    current_state.recommendation_sections = recommendation_sections
    current_state.discovery_films = discovery_films
    current_state.diary = current_state_diary
    current_state.josh_watchlist = josh_watchlist_slugs
    current_state.sarah_watchlist = sarah_watchlist_slugs

    # Cinema showtimes: independent of the watchlist refresh above, and
    # each venue's own site is a separate point of failure — one venue's
    # markup changing shouldn't cost the other three, so each gets its own
    # try/except and falls back to yesterday's listing for just that venue
    # rather than the whole feature going blank for a day.
    cinema_fetchers = [
        ("Prince Charles Cinema", fetch_prince_charles),
        ("Barbican", fetch_barbican),
        ("Vue Fulham Broadway", fetch_vue),
        ("Riverside Studios", fetch_riverside),
    ]
    cinema_showtimes: list[dict] = []
    for cinema_name, fetcher in cinema_fetchers:
        try:
            cinema_showtimes.extend(fetcher())
        except Exception as exc:
            _warn(f"cinema showtimes fetch failed for {cinema_name!r}, carrying forward yesterday's ({exc})")
            cinema_showtimes.extend(s for s in previous_state.cinema_showtimes if s["cinema"] == cinema_name)
    # Showings that have already started are no use to anyone — and without
    # this, a venue that keeps failing would carry the same stale listing
    # forward every day indefinitely rather than letting it run out.
    cinema_showtimes = drop_past_showings(cinema_showtimes)
    current_state.cinema_showtimes = cinema_showtimes
    current_state.cinema_matches = _resolved_cinema_matches(
        cinema_showtimes, current_state.films, previous_state.cinema_matches, warn=_warn)

    # Auto-queue every watchlist film missing a watch-together decision for
    # Sarah's review — covers both the one-time backfill of the existing
    # watchlist (nothing has an entry yet the first time this runs after
    # shipping) and future additions (only the new slug is missing) through
    # the same call, no separate backfill step needed.
    existing_watch_together = load_watch_together(database_url)
    missing_watch_together = {slug for slug in current_state.films if slug not in existing_watch_together}
    seed_pending_watch_together(database_url, missing_watch_together, today_str)

    # A single day's diff is usually too small to fill a "recently added"
    # section on its own, so newly-detected have/free offers accumulate into
    # a rolling log instead of a one-day snapshot. Skipped on a true first
    # run, where every offer would otherwise look "new".
    if previous_state.films:
        today = now_iso[:10]
        previous_snapshot = compute_offer_snapshot(previous_state, config, global_subscriptions, revisitable)
        current_snapshot = compute_offer_snapshot(current_state, config, global_subscriptions, revisitable)
        new_additions = [
            {"slug": slug, "brand": brand, "country": country, "classification": classification, "added_at": today}
            for slug, offers in current_snapshot.items()
            for (brand, country), classification in offers.items()
            if classification in ("have", "free")
            and previous_snapshot.get(slug, {}).get((brand, country)) != classification
        ]
        retention_cutoff = (date.fromisoformat(today) - timedelta(days=RECENT_ADDITIONS_RETENTION_DAYS)).isoformat()
        current_state.recent_additions = [
            a for a in new_additions + previous_state.recent_additions if a["added_at"] >= retention_cutoff
        ]

    report = build_report(previous_state, current_state, config)
    text = render_report(report, config, global_subscriptions, revisitable)

    # Persist before emailing — everything the day's run actually computed
    # (JustWatch refresh, discovery, diary) shouldn't be lost just because
    # Resend is having an outage; the email is a nice-to-have on top.
    save_state(database_url, current_state)
    watch_together = load_watch_together(database_url)
    custom_lists = load_custom_lists(DEFAULT_CUSTOM_LISTS_PATH)
    _refresh_custom_list_sources(database_url, custom_lists, _warn, limit=CUSTOM_LIST_SOURCES_PER_RUN)
    dashboard_data = build_dashboard_data(current_state, favorites, config, global_subscriptions, revisitable,
                                          dismissed, watch_together=watch_together,
                                          **_custom_list_inputs(database_url, custom_lists, current_state))
    DEFAULT_DASHBOARD_PATH.write_text(render_dashboard_html(dashboard_data))

    # A discovery section or a per-film check failing is already caught and
    # carried-forward-on-failure by design (see above) — the run still
    # reports success either way, so without this, "one bad day" and "this
    # has been broken for a week" look identical unless someone happens to
    # go read stderr. Surfaced two ways: on the Actions run page directly
    # (visible even on a day with nothing else to report), and folded into
    # whatever email actually goes out today.
    if run_warnings:
        _write_step_summary(run_warnings)

    if text or run_warnings:
        print(text or "No new availability changes.")
        subject = "Letterboxd Watchlist — new availability" if text else "Letterboxd Watchlist — pipeline warnings"
        email_text = _prepend_warnings(text, run_warnings)
        html_body = render_report_html(report, config, global_subscriptions, revisitable) if text else None
        send_if_configured(subject, email_text, html_body=html_body)
    else:
        print("No new availability changes.")

    return 0


def _write_step_summary(warnings: list[str]) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = ["## ⚠️ Pipeline warnings this run", ""]
    lines += [f"- {w}" for w in warnings[:20]]
    if len(warnings) > 20:
        lines.append(f"- ...and {len(warnings) - 20} more (see the run's full log)")
    with open(summary_path, "a") as f:
        f.write("\n".join(lines) + "\n")


def _prepend_warnings(text: str, warnings: list[str]) -> str:
    if not warnings:
        return text
    lines = [f"⚠️ {len(warnings)} issue(s) this run:", ""]
    lines += [f"- {w}" for w in warnings[:20]]
    if len(warnings) > 20:
        lines.append(f"- ...and {len(warnings) - 20} more (see the run's full log)")
    block = "\n".join(lines)
    return f"{block}\n\n{text}" if text else block


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Cross-check a Letterboxd watchlist against JustWatch")
    parser.add_argument("--username", default=os.getenv("LETTERBOXD_USERNAME"),
                         help="Letterboxd username (or set LETTERBOXD_USERNAME in .env)")
    parser.add_argument("--sarah-username", default=os.getenv("SARAH_LETTERBOXD_USERNAME"),
                         help="Sarah's Letterboxd username, shown additively in her own dashboard tab "
                              "(or set SARAH_LETTERBOXD_USERNAME in .env). Optional — the feature no-ops "
                              "if unset.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"),
                         help="Postgres connection string (or set DATABASE_URL in .env)")
    parser.add_argument("--rank-services", action="store_true",
                         help="Print services you don't have ranked by watchlist coverage, using "
                              "already-fetched state (no network calls), then exit")
    parser.add_argument("--similar-to", metavar="TITLE",
                         help="Find films similar to TITLE (via TMDB), with Letterboxd rating and "
                              "live JustWatch availability, then exit")
    parser.add_argument("--year", type=int, help="Disambiguate --similar-to by release year")
    parser.add_argument("--count", type=int, default=8, help="Number of similar films to show")
    parser.add_argument("--dashboard", action="store_true",
                         help="Regenerate dashboard.html from already-fetched state (no network calls), then exit")
    parser.add_argument("--dashboard-path", type=Path, default=DEFAULT_DASHBOARD_PATH)
    parser.add_argument("--recommend-favorites", action="store_true",
                         help="Print services not in your favorites that would unlock films no current "
                              "favorite covers, using already-fetched state (no network calls), then exit")
    parser.add_argument("--favorites", type=Path, default=DEFAULT_FAVORITES_PATH)
    parser.add_argument("--email-audit", action="store_true",
                         help="Send a one-off email listing every watchlist film not on a favourited "
                              "service, using already-fetched state (no network calls), then exit")
    parser.add_argument("--email-audit-by-country", action="store_true",
                         help="Same as --email-audit but organized into sections by VPN country, using "
                              "already-fetched state (no network calls), then exit")
    parser.add_argument("--weekly-digest", action="store_true",
                         help="Send the weekly roundup email (films added to/leaving your streaming "
                              "services this week, main services grouped with posters, everything else "
                              "condensed), using already-fetched state (no network calls), then exit")
    parser.add_argument("--main-services", type=Path, default=DEFAULT_MAIN_SERVICES_PATH)
    parser.add_argument("--backfill-diary", action="store_true",
                         help="One-time full watch-history backfill into state.diary — must be run "
                              "locally, since Letterboxd blocks /username/films/ (diary included) from "
                              "GitHub Actions' IP range. Written straight to the database; the daily run "
                              "keeps it current by merging in your last few watches each day.")
    parser.add_argument("--backfill-diary-ratings", action="store_true",
                         help="One-time backfill of personal_rating/is_rewatch/watched_date into "
                              "state.diary from the dated diary pages, then personal_rating again from "
                              "your /films/ grid, which also covers films rated without ever being "
                              "logged (locally only, same IP block as --backfill-diary). The RSS feed "
                              "only covers your last ~50 entries; this covers everything older. Doesn't "
                              "include 'liked' — not reliably scrapable from the static diary page — "
                              "only --check-for-new-log captures that, going forward.")
    parser.add_argument("--backfill-language", action="store_true",
                         help="One-time TMDB-only backfill of original_language and tmdb_id for every "
                              "watchlist film missing either (normally fills in gradually via the "
                              "stale-checking rotation, see run()) — no JustWatch/Letterboxd calls, just "
                              "one TMDB search per film, which carries both, then exit.")
    parser.add_argument("--backfill-runtime", action="store_true",
                         help="One-time backfill of runtime_minutes for every watchlist film missing it "
                              "(normally fills in gradually via the stale-checking rotation, see run()) "
                              "— one Letterboxd film-page fetch per film (not the blocked "
                              "/username/films/ path, so safe from GitHub Actions too), no JustWatch/TMDB "
                              "calls, then exit.")
    parser.add_argument("--set-watch-together-status", nargs=2, metavar=("SLUG", "STATUS"),
                         help="Set one film's watch-with-Sarah review status to 'confirmed' or "
                              "'declined', then exit. No network calls — for manual/one-off use; the "
                              "dashboard's Review tab itself calls --set-watch-together-statuses-batch.")
    parser.add_argument("--set-watch-together-statuses-batch", metavar="JSON",
                         help="Set multiple films' watch-with-Sarah review status in one go — a JSON "
                              "array of {\"slug\": ..., \"status\": \"confirmed\"|\"declined\"} objects — "
                              "then exit. No network calls. This is what the dashboard's Review tab "
                              "actually calls: taps debounce-batch client-side (see dashboard.py) so a "
                              "review session costs one workflow run, not one per tap.")
    parser.add_argument("--migrate-json-to-db", type=Path, metavar="STATE_JSON",
                         help="One-time import of a legacy data/state.json file into the database "
                              "at --database-url, then exit")
    parser.add_argument("--refresh-custom-lists", action="store_true",
                         help="Fetch every Letterboxd list config/custom_lists.yaml sources from that's "
                              "missing or over a week old (no per-run cap) into the database, then exit — "
                              "for a newly added source to show up before the next daily run")
    parser.add_argument("--check-for-new-log", action="store_true",
                         help="Check the Letterboxd RSS feed for a log entry newer than the last check "
                              "(one cheap request, no watchlist/JustWatch calls). Prints 'new_log=true' "
                              "or 'new_log=false' to stdout for a GitHub Actions step to read via "
                              "$GITHUB_OUTPUT, then exits.")
    parser.add_argument("--scrape-raters", action="store_true",
                         help="Taste engine, stage 1 (locally only — Letterboxd blocks these pages from "
                              "datacenter IPs): find members who rate like you and scrape their public "
                              "ratings into the database, then exit. Resumable; stops at the first sign "
                              "of a block and then refuses to run again for 24h. See taste.py.")
    parser.add_argument("--screen-films", type=int, default=200,
                         help="--scrape-raters: how many of your most distinctive ratings to look up "
                              "same-rating members for (one request each, skipping any already done)")
    parser.add_argument("--max-raters", type=int, default=600,
                         help="--scrape-raters: most candidates to scrape this run (~12 requests each)")
    parser.add_argument("--request-delay", type=float, default=2.5,
                         help="--scrape-raters/--taste-recommend: minimum seconds between requests (randomised up to 1.8x, "
                              "plus a longer pause every 100)")
    parser.add_argument("--max-requests", type=int, default=10000,
                         help="--scrape-raters/--taste-recommend: hard cap on requests in one run")
    parser.add_argument("--taste-eval", action="store_true",
                         help="Taste engine: hold out some of your ratings, predict them from the rest, "
                              "and compare against simpler predictions (network-free apart from the "
                              "database; the corpus is aggregated server-side), then exit")
    parser.add_argument("--taste-recommend", action="store_true",
                         help="Taste engine: print your closest taste matches, the best-predicted films "
                              "you haven't seen (leaving out TV — a pick's film page is checked the first "
                              "time it's suggested), and your watchlist ranked by predicted rating, then exit")
    args = parser.parse_args()

    if not args.database_url:
        parser.error("--database-url is required (or set DATABASE_URL in .env)")

    if args.migrate_json_to_db:
        raw = json.loads(args.migrate_json_to_db.read_text())
        films = {}
        for slug, f in raw.get("films", {}).items():
            films[slug] = FilmState(
                slug=slug, title=f["title"], year=f["year"], entry_id=f["entry_id"],
                confidence=f["confidence"], last_checked=f["last_checked"],
                offers=[OfferRecord(**o) for o in f.get("offers", [])],
                rating=f.get("rating"), poster_url=f.get("poster_url"),
                director=f.get("director", []), starring=f.get("starring", []),
                synopsis=f.get("synopsis"),
            )
        state = StateDoc(
            schema_version=raw.get("schema_version", 1),
            last_run_at=raw.get("last_run_at"),
            films=films,
            recent_watches=raw.get("recent_watches", []),
            recommendation_sections=raw.get("recommendation_sections", []),
            discovery_films=raw.get("discovery_films", {}),
            recent_additions=raw.get("recent_additions", []),
            diary=raw.get("diary", {}),
        )
        save_state(args.database_url, state)
        print(f"Migrated {len(films)} films and {len(state.diary)} diary entries into the database.")
        sys.exit(0)

    if args.check_for_new_log:
        if not args.username:
            parser.error("--username is required (or set LETTERBOXD_USERNAME in .env)")
        # Runs hourly, and on almost every run nothing new has happened — a
        # full load_state() just to read this one value (every film's offer
        # data, the whole diary) on every single invocation was the actual
        # driver of Neon's free-tier data-transfer quota being exceeded.
        # The full read/write only happens on the rare run that finds
        # something new to record.
        last_seen_diary_guid = get_meta_value(args.database_url, "last_seen_diary_guid")
        new_entries = fetch_new_diary_entries(args.username, last_seen_diary_guid)
        if new_entries:
            state = load_state(args.database_url)
            # Newest first — entries[0] is the latest guid seen this check.
            state.last_seen_diary_guid = new_entries[0]["guid"]
            for entry in new_entries:
                diary_entry = state.diary.setdefault(entry["slug"], {
                    "title": entry["title"], "year": entry["year"], "rating": None,
                    "poster_url": None, "director": None, "starring": [], "synopsis": None,
                })
                diary_entry["personal_rating"] = entry["personal_rating"]
                diary_entry["liked"] = entry["liked"]
                diary_entry["is_rewatch"] = entry["is_rewatch"]
                diary_entry["watched_date"] = entry["watched_date"]
            save_state(args.database_url, state)
            print("new_log=true")
        else:
            print("new_log=false")
        sys.exit(0)

    if args.backfill_language:
        state = load_state(args.database_url)
        # Both come out of the same search, so a film missing either is
        # worth one call — and tmdb_id was added long after original_language,
        # so on the first run after that change almost every film needs it.
        missing = [f for f in state.films.values()
                   if f.original_language is None or f.tmdb_id is None]
        print(f"Backfilling TMDB facts for {len(missing)}/{len(state.films)} films...")
        updated = 0
        for i, film in enumerate(missing, start=1):
            tmdb_id, language = _fetch_tmdb_facts(film.title, film.year)
            # A film TMDB can't find returns (None, None); leaving what's
            # already there alone means a failed call never erases a value
            # an earlier one found.
            if tmdb_id is not None:
                film.tmdb_id = tmdb_id
            if language is not None:
                film.original_language = language
            if tmdb_id is not None or language is not None:
                updated += 1
            if i % 25 == 0:
                print(f"...checked {i}/{len(missing)}", file=sys.stderr)
            time.sleep(0.1)
        save_state(args.database_url, state)
        print(f"Backfilled {updated}/{len(missing)} films (the rest had no TMDB match), written to the database.")
        sys.exit(0)

    if args.backfill_runtime:
        state = load_state(args.database_url)
        missing = [f for f in state.films.values() if f.runtime_minutes is None]
        print(f"Backfilling runtime_minutes for {len(missing)}/{len(state.films)} films...")
        updated = 0
        for i, film in enumerate(missing, start=1):
            details = get_film_details_by_slug(film.slug)
            film.runtime_minutes = details["runtime_minutes"]
            if film.runtime_minutes is not None:
                updated += 1
            if i % 25 == 0:
                print(f"...checked {i}/{len(missing)}", file=sys.stderr)
            time.sleep(0.1)
        save_state(args.database_url, state)
        print(f"Backfilled {updated}/{len(missing)} films, written to the database.")
        sys.exit(0)

    if args.refresh_custom_lists:
        custom_lists = load_custom_lists(DEFAULT_CUSTOM_LISTS_PATH)
        # Everything missing or stale, with no per-run cap — the point is to
        # seed a newly added source (or a whole new batch of them) now
        # rather than waiting out the daily run's rotation.
        fetched = _refresh_custom_list_sources(args.database_url, custom_lists,
                                               lambda msg: print(f"warning: {msg}", file=sys.stderr))
        for path, count in fetched.items():
            print(f"{path}: {count} films")
        print(f"Fetched {len(fetched)} source(s); the rest were already fresh.")
        sys.exit(0)

    if args.set_watch_together_status:
        slug, status = args.set_watch_together_status
        if status not in ("confirmed", "declined"):
            parser.error(f"--set-watch-together-status STATUS must be 'confirmed' or 'declined', got {status!r}")
        set_watch_together_status(args.database_url, slug, status, datetime.now(timezone.utc).isoformat()[:10])
        print(f"Set {slug!r} to {status!r}.")
        sys.exit(0)

    if args.set_watch_together_statuses_batch:
        try:
            items = json.loads(args.set_watch_together_statuses_batch)
        except json.JSONDecodeError as exc:
            parser.error(f"--set-watch-together-statuses-batch must be valid JSON ({exc})")
        decided_at = datetime.now(timezone.utc).isoformat()[:10]
        decisions = []
        for item in items:
            slug, status = item.get("slug"), item.get("status")
            if not slug or status not in ("confirmed", "declined"):
                parser.error(f"invalid batch entry (need slug + confirmed/declined status): {item!r}")
            decisions.append((slug, status, decided_at))
        set_watch_together_statuses_batch(args.database_url, decisions)
        print(f"Set {len(decisions)} film(s) status.")
        sys.exit(0)

    if args.rank_services:
        config = load_config(args.config)
        state = load_state(args.database_url)
        print(render_ranking(rank_missing_services(state, config)))
        sys.exit(0)

    if args.dashboard:
        favorites = load_favorites(args.favorites)
        config = load_config(args.config)
        global_subscriptions = load_global_subscriptions(args.config)
        revisitable = load_revisitable_services(DEFAULT_REVISITABLE_PATH)
        dismissed = load_dismissed_recommendations(DEFAULT_DISMISSED_PATH)
        state = load_state(args.database_url)
        watch_together = load_watch_together(args.database_url)
        data = build_dashboard_data(state, favorites, config, global_subscriptions, revisitable, dismissed,
                                    watch_together=watch_together,
                                    **_custom_list_inputs(args.database_url,
                                                          load_custom_lists(DEFAULT_CUSTOM_LISTS_PATH), state))
        args.dashboard_path.write_text(render_dashboard_html(data))
        print(f"Wrote {args.dashboard_path}")
        sys.exit(0)

    if args.email_audit:
        config = load_config(args.config)
        global_subscriptions = load_global_subscriptions(args.config)
        revisitable = load_revisitable_services(DEFAULT_REVISITABLE_PATH)
        state = load_state(args.database_url)
        films = films_not_on_favorite(state, config, global_subscriptions)
        text = render_film_audit_text(films)
        html_body = render_film_audit_html(films, config, global_subscriptions, revisitable)
        sent = send_if_configured(f"Letterboxd Watchlist — {len(films)} films not on a service you have",
                                   text, html_body=html_body)
        print(text if sent else "Email not sent (RESEND_API_KEY/NOTIFY_EMAIL not configured):\n\n" + text)
        sys.exit(0)

    if args.email_audit_by_country:
        config = load_config(args.config)
        global_subscriptions = load_global_subscriptions(args.config)
        revisitable = load_revisitable_services(DEFAULT_REVISITABLE_PATH)
        state = load_state(args.database_url)
        countries = films_not_on_favorite_by_country(state, config, global_subscriptions, revisitable)
        text = render_country_audit_text(countries)
        html_body = render_country_audit_html(countries)
        total_films = sum(len(c["films"]) for c in countries)
        sent = send_if_configured(f"Letterboxd Watchlist — {total_films} films not on a service you have, by country",
                                   text, html_body=html_body)
        print(text if sent else "Email not sent (RESEND_API_KEY/NOTIFY_EMAIL not configured):\n\n" + text)
        sys.exit(0)

    if args.weekly_digest:
        config = load_config(args.config)
        global_subscriptions = load_global_subscriptions(args.config)
        revisitable = load_revisitable_services(DEFAULT_REVISITABLE_PATH)
        main_services = load_main_services(args.main_services)
        state = load_state(args.database_url)
        digest = compute_weekly_digest(state, config, global_subscriptions, revisitable, main_services)
        text = render_weekly_digest_text(digest)
        html_body = render_weekly_digest_html(digest)
        sent = send_if_configured(
            f"This week on your watchlist — {digest['total_added']} added, {digest['total_leaving']} leaving",
            text, html_body=html_body)
        print(text if sent else "Email not sent (RESEND_API_KEY/NOTIFY_EMAIL not configured):\n\n" + text)
        sys.exit(0)

    if args.backfill_diary:
        if not args.username:
            parser.error("--username is required (or set LETTERBOXD_USERNAME in .env)")
        state = load_state(args.database_url)
        watched_films = fetch_watched_films(args.username, full=True)
        added = 0
        for f in watched_films:
            if f.slug in state.diary:
                continue
            details = get_film_details_by_slug(f.slug)
            state.diary[f.slug] = {
                "title": f.title, "year": f.year, "rating": details["rating"],
                "poster_url": details["poster_url"],
                "director": ", ".join(details["director"]) if details["director"] else None,
                "starring": details["starring"], "synopsis": details["synopsis"],
                "genre": details["genre"], "original_language": _fetch_original_language(f.title, f.year),
            }
            added += 1
            if added % 25 == 0:
                print(f"...enriched {added} new watched films", file=sys.stderr)
            time.sleep(0.2)
        save_state(args.database_url, state)
        print(f"Backfilled {added} new watched films (diary total: {len(state.diary)}), written to the database.")
        sys.exit(0)

    if args.backfill_diary_ratings:
        if not args.username:
            parser.error("--username is required (or set LETTERBOXD_USERNAME in .env)")
        state = load_state(args.database_url)
        ratings_by_slug = fetch_diary_ratings(args.username)
        updated = 0
        for slug, fields in ratings_by_slug.items():
            if slug not in state.diary:
                continue
            state.diary[slug].update(fields)
            updated += 1
        # The diary pages only know films that were logged; the /films/
        # grid has a rating for every film ever rated, and it's each film's
        # current rating rather than one viewing's — so it wins where the
        # two disagree. Everything the taste engine does is measured
        # against these, which is why the gaps matter.
        try:
            grid_ratings = fetch_rated_films(args.username)
        except LetterboxdFetchError as exc:
            print(f"warning: /films/ grid ratings skipped ({exc})", file=sys.stderr)
            grid_ratings = {}
        grid_filled = sum(1 for slug in grid_ratings
                          if slug in state.diary and state.diary[slug].get("personal_rating") is None)
        grid_missing = sum(1 for slug in grid_ratings if slug not in state.diary)
        for slug, rating in grid_ratings.items():
            if slug in state.diary:
                state.diary[slug]["personal_rating"] = rating
        save_state(args.database_url, state)
        print(f"Backfilled ratings for {updated}/{len(ratings_by_slug)} diary entries "
              f"({len(ratings_by_slug) - updated} scraped but not already in state.diary, skipped). "
              f"The /films/ grid had {len(grid_ratings)} ratings: {grid_filled} filled a film with no "
              f"rating yet, {grid_missing} are films not in state.diary (run --backfill-diary first "
              f"to pick those up).")
        sys.exit(0)

    if args.scrape_raters:
        if not args.username:
            parser.error("--username is required (or set LETTERBOXD_USERNAME in .env)")
        diary, _ = load_taste_inputs(args.database_url)
        my_ratings = my_ratings_from_diary(diary)
        if len(my_ratings) < 50:
            print(f"error: only {len(my_ratings)} of your films have a rating stored — run "
                  f"--backfill-diary-ratings first", file=sys.stderr)
            sys.exit(1)
        outcome = scrape_raters(
            args.database_url, args.username, my_ratings, community_ratings_from_diary(diary),
            screen_films=args.screen_films, max_raters=args.max_raters,
            fetcher=PoliteFetcher(delay_seconds=args.request_delay, max_requests=args.max_requests),
        )
        sys.exit({"blocked": 2, "network": 1, "cooldown": 1}.get(outcome, 0))

    if args.taste_eval or args.taste_recommend:
        diary, watchlist = load_taste_inputs(args.database_url)
        my_ratings = my_ratings_from_diary(diary)
        try:
            with connect(args.database_url) as conn:
                if args.taste_eval:
                    report = evaluate(conn, my_ratings, community_ratings_from_diary(diary),
                                      {slug: entry["watched_date"] for slug, entry in diary.items()})
                    print(render_evaluation(report))
                if args.taste_recommend:
                    # Checks each pick's film page for TV the first time it's suggested
                    # (film pages, unlike /films/ grids, load fine from anywhere).
                    fetcher = PoliteFetcher(delay_seconds=args.request_delay, max_requests=args.max_requests)
                    print(render_recommendations(recommend(conn, my_ratings, set(diary), watchlist,
                                                           fetcher=fetcher)))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if args.recommend_favorites:
        favorites = load_favorites(args.favorites)
        state = load_state(args.database_url)
        print("New services worth adding as favourites (you don't have these anywhere yet):\n")
        print(render_favorite_recommendations(recommend_new_favorites(state, favorites)))
        print("\nServices you already favourite, but not in these countries (lower priority):\n")
        print(render_favorite_recommendations(recommend_extra_countries(state, favorites)))
        sys.exit(0)

    if args.similar_to:
        config = load_config(args.config)
        state = load_state(args.database_url)
        try:
            source, results = find_similar(args.similar_to, args.year, state=state, config=config, count=args.count)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(render_similar(source["title"], results))
        sys.exit(0)

    if not args.username:
        parser.error("--username is required (or set LETTERBOXD_USERNAME in .env)")

    try:
        exit_code = run(args.username, args.config, args.database_url, sarah_username=args.sarah_username)
    except LetterboxdFetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        exit_code = 1
    except Exception:
        traceback.print_exc()
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
