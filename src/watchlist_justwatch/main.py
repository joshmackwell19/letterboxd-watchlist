import argparse
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .analysis import (
    films_not_on_favorite,
    rank_missing_services,
    recommend_extra_countries,
    recommend_new_favorites,
    render_favorite_recommendations,
    render_ranking,
)
from .config import load_config, load_favorites, load_global_subscriptions, load_revisitable_services
from .dashboard import build_dashboard_data, render_dashboard_html
from .diff import build_report
from .html_email import render_film_audit_html, render_film_audit_text, render_report_html
from .justwatch_client import resolve_and_fetch
from .letterboxd import LetterboxdFetchError, fetch_watchlist, get_film_details_by_slug
from .notify import send_if_configured
from .report import render_report
from .similar import find_similar, render_similar
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

    report = build_report(previous_state, current_state, config)
    text = render_report(report, config, global_subscriptions, revisitable)

    if text:
        print(text)
        html_body = render_report_html(report, config, global_subscriptions, revisitable)
        send_if_configured("Letterboxd Watchlist — new availability", text, html_body=html_body)
    else:
        print("No new availability changes.")

    save_state(state_path, current_state)
    DEFAULT_DASHBOARD_PATH.write_text(render_dashboard_html(build_dashboard_data(current_state, favorites)))
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
    args = parser.parse_args()

    if args.rank_services:
        config = load_config(args.config)
        state = load_state(args.state)
        print(render_ranking(rank_missing_services(state, config)))
        sys.exit(0)

    if args.dashboard:
        favorites = load_favorites(args.favorites)
        state = load_state(args.state)
        data = build_dashboard_data(state, favorites)
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
