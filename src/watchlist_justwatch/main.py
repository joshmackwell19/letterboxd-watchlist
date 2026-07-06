import argparse
import os
import sys
import time
import traceback
from datetime import datetime, timezone
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
from .config import load_config, load_favorites, load_global_subscriptions, load_revisitable_services
from .dashboard import build_dashboard_data, compute_offer_snapshot, render_dashboard_html
from .diff import build_report
from .html_email import (
    render_country_audit_html,
    render_country_audit_text,
    render_film_audit_html,
    render_film_audit_text,
    render_report_html,
)
from .justwatch_client import resolve_and_fetch
from .letterboxd import (
    LetterboxdFetchError,
    fetch_recent_watches,
    fetch_watched_films,
    fetch_watchlist,
    get_film_details_by_slug,
)
from .notify import send_if_configured
from .report import render_report
from .similar import (
    discover_because_watched,
    discover_by_genre,
    discover_hidden_gems,
    discover_popular_now,
    discover_rewatch,
    discover_same_cast,
    discover_same_director,
    find_similar,
    render_similar,
)
from .state import StateDoc, get_cached_entry_id, load_state, save_state

DEFAULT_CONFIG_PATH = Path("config/services.yaml")
DEFAULT_FAVORITES_PATH = Path("config/favorites.yaml")
DEFAULT_REVISITABLE_PATH = Path("config/revisitable_services.yaml")
DEFAULT_STATE_PATH = Path("data/state.json")
DEFAULT_DASHBOARD_PATH = Path("dashboard.html")


def run(username: str, config_path: Path, state_path: Path, *, progress: bool = True) -> int:
    config = load_config(config_path)
    global_subscriptions = load_global_subscriptions(config_path)
    favorites = load_favorites(DEFAULT_FAVORITES_PATH)
    revisitable = load_revisitable_services(DEFAULT_REVISITABLE_PATH)
    previous_state = load_state(state_path)

    films = fetch_watchlist(username)
    now_iso = datetime.now(timezone.utc).isoformat()

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
            }

    # Correlated across all of TMDB, not just the watchlist, so these can
    # surface films worth discovering rather than only re-surfacing what's
    # already tracked. Done early (like the recent-watches fetch above),
    # before the per-film loop's ~600+ requests pile up.
    recommendation_sections: list[dict] = []
    discovery_films: dict[str, dict] = {}
    if recent_watches:
        already_seen = set(current_state_diary.keys())
        exclude_slugs = {w["slug"] for w in recent_watches} | already_seen

        # Discoverers are called one at a time (not pre-built into a tuple)
        # so exclude_slugs.update() below actually takes effect between
        # them — otherwise every section would be computed against the same
        # starting exclusion set and could independently pick the same film.
        discoverers = [
            ("because_you_watched", lambda ex: discover_because_watched(
                recent_watches, now_iso, config, global_subscriptions, revisitable, ex)),
            ("same_director", lambda ex: discover_same_director(
                recent_watches, now_iso, config, global_subscriptions, revisitable, ex)),
            ("same_cast", lambda ex: discover_same_cast(
                recent_watches, now_iso, config, global_subscriptions, revisitable, ex)),
            ("hidden_gems", lambda ex: discover_hidden_gems(
                now_iso, config, global_subscriptions, revisitable, ex)),
            ("popular_now", lambda ex: discover_popular_now(
                now_iso, config, global_subscriptions, revisitable, ex)),
            ("by_genre", lambda ex: discover_by_genre(
                recent_watches, now_iso, config, global_subscriptions, revisitable, ex)),
        ]
        for key, discoverer in discoverers:
            # Each section is a nice-to-have on top of the core watchlist
            # refresh below, not essential to it — an API hiccup (TMDB rate
            # limit, JustWatch timeout) in one section shouldn't cost the
            # whole daily run, so it's skipped rather than left to crash out.
            try:
                header, slugs, films_map = discoverer(exclude_slugs)
            except Exception as exc:
                print(f"warning: discovery section {key!r} failed, skipping it ({exc})", file=sys.stderr)
                continue
            if slugs:
                recommendation_sections.append({"key": key, "header": header, "slugs": slugs})
                discovery_films.update(films_map)
                exclude_slugs.update(slugs)

        # Rewatch pulls FROM the diary on purpose, so it can't receive the
        # full diary as an exclusion set like the sections above — only
        # whatever they already claimed, so nothing shows twice on the page.
        rewatch_exclude = exclude_slugs - already_seen
        try:
            header, slugs, films_map = discover_rewatch(
                current_state_diary, recent_watches, now_iso, config, global_subscriptions, revisitable,
                rewatch_exclude)
        except Exception as exc:
            print(f"warning: discovery section 'rewatch' failed, skipping it ({exc})", file=sys.stderr)
            slugs = []
        if slugs:
            recommendation_sections.append({"key": "rewatch", "header": header, "slugs": slugs})
            discovery_films.update(films_map)

    current_state = StateDoc(last_run_at=now_iso)
    for i, film in enumerate(films, start=1):
        if progress and i % 25 == 0:
            print(f"...processed {i}/{len(films)} films", file=sys.stderr)

        cached_entry_id, cached_confidence = get_cached_entry_id(previous_state, film.slug)
        film_state = resolve_and_fetch(film, cached_entry_id, cached_confidence, now_iso=now_iso)

        previous_film = previous_state.films.get(film.slug)
        if previous_film is not None and previous_film.poster_url is not None:
            film_state.rating = previous_film.rating
            film_state.poster_url = previous_film.poster_url
            film_state.director = previous_film.director
            film_state.starring = previous_film.starring
            film_state.synopsis = previous_film.synopsis
        else:
            details = get_film_details_by_slug(film.slug)
            film_state.rating = details["rating"]
            film_state.poster_url = details["poster_url"]
            film_state.director = details["director"]
            film_state.starring = details["starring"]
            film_state.synopsis = details["synopsis"]

        current_state.films[film.slug] = film_state

    current_state.recent_watches = recent_watches
    current_state.recommendation_sections = recommendation_sections
    current_state.discovery_films = discovery_films
    current_state.diary = current_state_diary

    # A single day's diff is usually too small to fill a "recently added"
    # section on its own, so newly-detected have/free offers accumulate into
    # a capped rolling log instead of a one-day snapshot. Skipped on a true
    # first run, where every offer would otherwise look "new".
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
        current_state.recent_additions = (new_additions + previous_state.recent_additions)[:200]

    report = build_report(previous_state, current_state, config)
    text = render_report(report, config, global_subscriptions, revisitable)

    if text:
        print(text)
        html_body = render_report_html(report, config, global_subscriptions, revisitable)
        send_if_configured("Letterboxd Watchlist — new availability", text, html_body=html_body)
    else:
        print("No new availability changes.")

    save_state(state_path, current_state)
    dashboard_data = build_dashboard_data(current_state, favorites, config, global_subscriptions, revisitable)
    DEFAULT_DASHBOARD_PATH.write_text(render_dashboard_html(dashboard_data))
    return 0


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Cross-check a Letterboxd watchlist against JustWatch")
    parser.add_argument("--username", default=os.getenv("LETTERBOXD_USERNAME"),
                         help="Letterboxd username (or set LETTERBOXD_USERNAME in .env)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
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
    parser.add_argument("--backfill-diary", action="store_true",
                         help="One-time full watch-history backfill into state.diary — must be run "
                              "locally, since Letterboxd blocks /username/films/ (diary included) from "
                              "GitHub Actions' IP range. Commit the updated state file afterwards; the "
                              "daily run keeps it current by merging in your last few watches each day.")
    args = parser.parse_args()

    if args.rank_services:
        config = load_config(args.config)
        state = load_state(args.state)
        print(render_ranking(rank_missing_services(state, config)))
        sys.exit(0)

    if args.dashboard:
        favorites = load_favorites(args.favorites)
        config = load_config(args.config)
        global_subscriptions = load_global_subscriptions(args.config)
        revisitable = load_revisitable_services(DEFAULT_REVISITABLE_PATH)
        state = load_state(args.state)
        data = build_dashboard_data(state, favorites, config, global_subscriptions, revisitable)
        args.dashboard_path.write_text(render_dashboard_html(data))
        print(f"Wrote {args.dashboard_path}")
        sys.exit(0)

    if args.email_audit:
        config = load_config(args.config)
        global_subscriptions = load_global_subscriptions(args.config)
        revisitable = load_revisitable_services(DEFAULT_REVISITABLE_PATH)
        state = load_state(args.state)
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
        state = load_state(args.state)
        countries = films_not_on_favorite_by_country(state, config, global_subscriptions, revisitable)
        text = render_country_audit_text(countries)
        html_body = render_country_audit_html(countries)
        total_films = sum(len(c["films"]) for c in countries)
        sent = send_if_configured(f"Letterboxd Watchlist — {total_films} films not on a service you have, by country",
                                   text, html_body=html_body)
        print(text if sent else "Email not sent (RESEND_API_KEY/NOTIFY_EMAIL not configured):\n\n" + text)
        sys.exit(0)

    if args.backfill_diary:
        if not args.username:
            parser.error("--username is required (or set LETTERBOXD_USERNAME in .env)")
        state = load_state(args.state)
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
            }
            added += 1
            if added % 25 == 0:
                print(f"...enriched {added} new watched films", file=sys.stderr)
            time.sleep(0.2)
        save_state(args.state, state)
        print(f"Backfilled {added} new watched films (diary total: {len(state.diary)}).")
        print(f"Commit and push {args.state} to finish — the daily run will keep it current from there.")
        sys.exit(0)

    if args.recommend_favorites:
        favorites = load_favorites(args.favorites)
        state = load_state(args.state)
        print("New services worth adding as favourites (you don't have these anywhere yet):\n")
        print(render_favorite_recommendations(recommend_new_favorites(state, favorites)))
        print("\nServices you already favourite, but not in these countries (lower priority):\n")
        print(render_favorite_recommendations(recommend_extra_countries(state, favorites)))
        sys.exit(0)

    if args.similar_to:
        config = load_config(args.config)
        state = load_state(args.state)
        try:
            source, results = find_similar(args.similar_to, args.year, state=state, config=config, count=args.count)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(render_similar(source["title"], results))
        sys.exit(0)

    if not args.username:
        parser.error("--username is required (or set LETTERBOXD_USERNAME in .env)")

    args.state.parent.mkdir(parents=True, exist_ok=True)

    try:
        exit_code = run(args.username, args.config, args.state)
    except LetterboxdFetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        exit_code = 1
    except Exception:
        traceback.print_exc()
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
