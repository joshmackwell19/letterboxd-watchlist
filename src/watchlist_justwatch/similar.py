import time
from collections import Counter
from dataclasses import dataclass

from . import tmdb_client
from .config import CountryConfig, canonical_display_name, classify_offer
from .dashboard import _all_offers_for_film
from .justwatch_client import fetch_offers, resolve_and_fetch, search_film
from .letterboxd import get_film_details_by_tmdb_id, get_rating_by_tmdb_id
from .models import WatchlistFilm
from .state import StateDoc


@dataclass
class SimilarFilmResult:
    title: str
    year: int | None
    letterboxd_rating: float | None
    on_watchlist: bool
    availability: dict[str, list[tuple[str, str]]]  # country -> [(clear_name, classification)]


def _match_watchlist_slug(state: StateDoc, title: str, year: int | None) -> str | None:
    title_lower = title.lower()
    for film in state.films.values():
        if film.title.lower() == title_lower and (year is None or film.year == year):
            return film.slug
    return None


def _on_watchlist(state: StateDoc, title: str, year: int | None) -> bool:
    return _match_watchlist_slug(state, title, year) is not None


def _rank_by_rating(movies: list[dict]) -> list[dict]:
    return sorted(movies, key=lambda m: -(m.get("vote_average") or 0))


# Documentary, TV Movie — covers most "making of"/behind-the-scenes content
# that would otherwise slip through a pure rating/vote-count filter.
_EXCLUDED_GENRE_IDS = {99, 10770}
_MIN_VOTE_AVERAGE = 6.0
_MIN_VOTE_COUNT = 150


def _passes_quality_filter(movie: dict) -> bool:
    if any(g in _EXCLUDED_GENRE_IDS for g in movie.get("genre_ids", [])):
        return False
    if (movie.get("vote_average") or 0) < _MIN_VOTE_AVERAGE:
        return False
    if (movie.get("vote_count") or 0) < _MIN_VOTE_COUNT:
        return False
    return True


def _candidates_from_recent_watches(recent_watches: list[dict], *, candidate_pool: int) -> list[dict]:
    """TMDB similar/recommended movies seeded from recent watches — not
    matched against the watchlist, since the whole point is surfacing films
    that aren't on it yet."""
    seen_ids: set[int] = set()
    candidates: list[dict] = []
    for watched in recent_watches:
        try:
            source = tmdb_client.search_movie(watched["title"], watched.get("year"))
            if source is None:
                continue
            movies = tmdb_client.similar_and_recommended(source["id"], limit=candidate_pool)
        except Exception:
            continue
        for movie in movies:
            if movie["id"] not in seen_ids:
                seen_ids.add(movie["id"])
                candidates.append(movie)
    return _rank_by_rating(candidates)


def _candidates_by_person(names: list[str], role: str, *, candidate_pool: int) -> list[dict]:
    """TMDB filmography for a director or actor name — role is "director"
    (crew credits with job == Director) or "cast" (acting credits)."""
    seen_ids: set[int] = set()
    candidates: list[dict] = []
    for name in names:
        try:
            person = tmdb_client.search_person(name)
            if person is None:
                continue
            credits = tmdb_client.person_movie_credits(person["id"])
        except Exception:
            continue
        movies = credits.get("crew", []) if role == "director" else credits.get("cast", [])
        if role == "director":
            movies = [m for m in movies if m.get("job") == "Director"]
        for movie in movies:
            if movie["id"] not in seen_ids:
                seen_ids.add(movie["id"])
                candidates.append(movie)
    return _rank_by_rating(candidates)[:candidate_pool]


def _enrich_candidates(
    candidates: list[dict], now_iso: str, config: dict[str, CountryConfig], global_subscriptions: list[str],
    revisitable: set[str], *, exclude_slugs: set[str], limit: int,
) -> tuple[list[str], dict[str, dict]]:
    """Resolves TMDB candidates to real Letterboxd + JustWatch data, stopping
    once `limit` films have real tracked availability. Over-fetches on
    purpose (candidates is usually much longer than `limit`): some fail the
    quality filter, some won't have a Letterboxd match, some have no
    Letterboxd rating at all, and some won't currently have any tracked
    offer anywhere — silently skipping those is what lets a section reach
    `limit` films without ever padding with a "making of" featurette or an
    unrated obscurity just to hit a number."""
    resolved: list[dict] = []
    seen_slugs: set[str] = set()

    for movie in candidates:
        if len(resolved) >= limit * 2:  # enough headroom for the availability check below to still hit `limit`
            break
        if not _passes_quality_filter(movie):
            continue

        details = get_film_details_by_tmdb_id(movie["id"])
        if details is None or details["rating"] is None:
            continue
        if details["slug"] in exclude_slugs or details["slug"] in seen_slugs:
            continue
        seen_slugs.add(details["slug"])

        resolved.append({
            "slug": details["slug"], "title": movie.get("title") or "", "year": tmdb_client.release_year(movie),
            "rating": details["rating"], "poster_url": details["poster_url"],
            "director": ", ".join(details["director"]) if details["director"] else None,
            "starring": details["starring"], "synopsis": details["synopsis"],
        })

    return _enrich_resolved_candidates(resolved, now_iso, config, global_subscriptions, revisitable,
                                        exclude_slugs=exclude_slugs, limit=limit)


def _enrich_resolved_candidates(
    candidates: list[dict], now_iso: str, config: dict[str, CountryConfig], global_subscriptions: list[str],
    revisitable: set[str], *, exclude_slugs: set[str], limit: int,
) -> tuple[list[str], dict[str, dict]]:
    """Like _enrich_candidates, but for candidates that already have full
    Letterboxd details resolved (slug/title/year/rating/poster_url/director/
    starring/synopsis) — e.g. from the watched-films cache — so only the
    JustWatch availability check remains."""
    slugs: list[str] = []
    films: dict[str, dict] = {}

    for candidate in candidates:
        if len(slugs) >= limit:
            break
        slug = candidate["slug"]
        if slug in exclude_slugs or slug in films:
            continue

        temp_film = WatchlistFilm(slug=slug, title=candidate["title"], year=candidate.get("year"))
        film_state = resolve_and_fetch(temp_film, None, None, now_iso=now_iso)
        if not film_state.offers:
            continue
        all_offers = _all_offers_for_film(film_state, config, global_subscriptions, revisitable)
        if not all_offers:
            continue

        films[slug] = {**candidate, "all_offers": all_offers}
        slugs.append(slug)

    return slugs, films


def discover_because_watched(
    recent_watches: list[dict], now_iso: str, config: dict[str, CountryConfig], global_subscriptions: list[str],
    revisitable: set[str], exclude_slugs: set[str], *, limit: int = 10,
) -> tuple[str, list[str], dict[str, dict]]:
    candidates = _candidates_from_recent_watches(recent_watches, candidate_pool=40)
    slugs, films = _enrich_candidates(candidates, now_iso, config, global_subscriptions, revisitable,
                                       exclude_slugs=exclude_slugs, limit=limit)
    return "Recommended from your recent watches", slugs, films


def discover_same_director(
    recent_watches: list[dict], now_iso: str, config: dict[str, CountryConfig], global_subscriptions: list[str],
    revisitable: set[str], exclude_slugs: set[str], *, limit: int = 10,
) -> tuple[str, list[str], dict[str, dict]]:
    directors: list[str] = []
    for watched in recent_watches:
        for d in watched.get("director") or []:
            if d not in directors:
                directors.append(d)
    if not directors:
        return "", [], {}

    candidates = _candidates_by_person(directors, "director", candidate_pool=40)
    slugs, films = _enrich_candidates(candidates, now_iso, config, global_subscriptions, revisitable,
                                       exclude_slugs=exclude_slugs, limit=limit)
    return "More from " + " & ".join(directors[:2]), slugs, films


def discover_same_cast(
    recent_watches: list[dict], now_iso: str, config: dict[str, CountryConfig], global_subscriptions: list[str],
    revisitable: set[str], exclude_slugs: set[str], *, limit: int = 10,
) -> tuple[str, list[str], dict[str, dict]]:
    counts: Counter = Counter()
    order: list[str] = []
    for watched in recent_watches:
        for actor in watched.get("starring") or []:
            counts[actor] += 1
            if actor not in order:
                order.append(actor)
    top_actors = sorted(order, key=lambda a: (-counts[a], order.index(a)))[:3]
    if not top_actors:
        return "", [], {}

    candidates = _candidates_by_person(top_actors, "cast", candidate_pool=40)
    slugs, films = _enrich_candidates(candidates, now_iso, config, global_subscriptions, revisitable,
                                       exclude_slugs=exclude_slugs, limit=limit)
    return "More starring " + ", ".join(top_actors), slugs, films


def discover_rewatch(
    diary: dict[str, dict], recent_watches: list[dict], now_iso: str, config: dict[str, CountryConfig],
    global_subscriptions: list[str], revisitable: set[str], exclude_slugs: set[str], *, limit: int = 10,
) -> tuple[str, list[str], dict[str, dict]]:
    """Highly-rated films you've already logged, minus your last few
    watches — not true watch-date recency (see fetch_watched_films), just
    "not one of the ones you just watched"."""
    just_watched = {w["slug"] for w in recent_watches}
    candidates = [
        {**info, "slug": slug} for slug, info in diary.items()
        if slug not in just_watched and slug not in exclude_slugs
        and info.get("rating") is not None and info["rating"] >= 3.5
    ]
    candidates.sort(key=lambda c: -c["rating"])
    slugs, films = _enrich_resolved_candidates(candidates[:limit * 2], now_iso, config, global_subscriptions,
                                                revisitable, exclude_slugs=exclude_slugs, limit=limit)
    return "Worth a rewatch", slugs, films


def discover_hidden_gems(
    now_iso: str, config: dict[str, CountryConfig], global_subscriptions: list[str], revisitable: set[str],
    exclude_slugs: set[str], *, limit: int = 10,
) -> tuple[str, list[str], dict[str, dict]]:
    """Well-rated but not blockbuster-popular — a narrower vote-count band
    than the other sections, biased toward less mainstream picks."""
    candidates = tmdb_client.discover_movies(
        sort_by="vote_average.desc", vote_count_gte=300, vote_count_lte=3000, vote_average_gte=7.2, pages=3,
    )
    slugs, films = _enrich_candidates(candidates, now_iso, config, global_subscriptions, revisitable,
                                       exclude_slugs=exclude_slugs, limit=limit)
    return "Hidden gems on your services", slugs, films


def discover_popular_now(
    now_iso: str, config: dict[str, CountryConfig], global_subscriptions: list[str], revisitable: set[str],
    exclude_slugs: set[str], *, limit: int = 10,
) -> tuple[str, list[str], dict[str, dict]]:
    candidates = tmdb_client.trending_movies("week") + tmdb_client.trending_movies("day")
    slugs, films = _enrich_candidates(candidates, now_iso, config, global_subscriptions, revisitable,
                                       exclude_slugs=exclude_slugs, limit=limit)
    return "Popular right now", slugs, films


def discover_by_genre(
    recent_watches: list[dict], now_iso: str, config: dict[str, CountryConfig], global_subscriptions: list[str],
    revisitable: set[str], exclude_slugs: set[str], *, limit: int = 10,
) -> tuple[str, list[str], dict[str, dict]]:
    """Most-common genre across recent watches, then TMDB's best-rated in
    that genre — a different axis from the director/cast/similar sections
    above (mood/theme rather than a specific person or "similar to X")."""
    genre_counts: Counter = Counter()
    for watched in recent_watches:
        try:
            source = tmdb_client.search_movie(watched["title"], watched.get("year"))
        except Exception:
            continue
        if source is None:
            continue
        for genre_id in source.get("genre_ids", []):
            genre_counts[genre_id] += 1
    if not genre_counts:
        return "", [], {}

    top_genre_id = genre_counts.most_common(1)[0][0]
    candidates = tmdb_client.discover_movies(
        sort_by="vote_average.desc", vote_count_gte=200, vote_average_gte=6.5,
        with_genres=str(top_genre_id), pages=3,
    )
    slugs, films = _enrich_candidates(candidates, now_iso, config, global_subscriptions, revisitable,
                                       exclude_slugs=exclude_slugs, limit=limit)
    genre_name = tmdb_client.GENRE_NAMES.get(top_genre_id, "this genre")
    return "More " + genre_name, slugs, films


def find_similar(
    title: str,
    year: int | None,
    *,
    state: StateDoc,
    config: dict[str, CountryConfig],
    count: int = 8,
    request_delay_seconds: float = 0.3,
) -> tuple[dict, list[SimilarFilmResult]]:
    source = tmdb_client.search_movie(title, year)
    if source is None:
        raise ValueError(f"Couldn't find {title!r} on TMDB")

    candidates = tmdb_client.similar_and_recommended(source["id"], limit=count * 2)

    results: list[SimilarFilmResult] = []
    for movie in candidates:
        if len(results) >= count:
            break

        candidate_title = movie["title"]
        candidate_year = tmdb_client.release_year(movie)

        rating = get_rating_by_tmdb_id(movie["id"])
        on_watchlist = _on_watchlist(state, candidate_title, candidate_year)

        match = search_film(candidate_title, candidate_year)
        availability: dict[str, list[tuple[str, str]]] = {}
        if match.entry_id is not None:
            offers = fetch_offers(match.entry_id, countries=frozenset(config.keys()))
            # Dedupe JustWatch package variants of the same real service (e.g.
            # "Amazon Prime Video" / "... with Ads") down to one canonical entry:
            # your config's own name for have/free_tier, else the raw JustWatch name.
            seen: dict[tuple[str, str, str], None] = {}
            for offer in offers:
                country_config = config[offer.country]
                classification = classify_offer(offer, country_config)
                display_name = canonical_display_name(offer, country_config)
                seen[(offer.country, classification, display_name)] = None
                availability.setdefault(offer.country, [])
            for country, classification, display_name in seen:
                availability[country].append((display_name, classification))

        results.append(SimilarFilmResult(
            title=candidate_title, year=candidate_year, letterboxd_rating=rating,
            on_watchlist=on_watchlist, availability=availability,
        ))
        time.sleep(request_delay_seconds)

    return source, results


def render_similar(source_title: str, results: list[SimilarFilmResult]) -> str:
    lines = [f"Films similar to {source_title}:", ""]

    for r in results:
        year = f" ({r.year})" if r.year else ""
        rating = f"{r.letterboxd_rating:.2f}★" if r.letterboxd_rating is not None else "no rating"
        watchlist_tag = " [on your watchlist]" if r.on_watchlist else ""
        lines.append(f"• {r.title}{year} — {rating}{watchlist_tag}")

        if not r.availability:
            lines.append("    not currently streaming (subscription/free) anywhere tracked")
            continue

        for country in sorted(r.availability):
            have = [name for name, cls in r.availability[country] if cls == "have"]
            free = [name for name, cls in r.availability[country] if cls == "free_tier"]
            other = [name for name, cls in r.availability[country] if cls == "new_possible"]
            if have:
                lines.append(f"    ✅ {country}: {', '.join(have)}")
            if free:
                lines.append(f"    \U0001F193 {country}: {', '.join(free)}")
            if other:
                shown = other[:4]
                suffix = f" (+{len(other) - 4} more)" if len(other) > 4 else ""
                lines.append(f"    \U0001F195 {country}: {', '.join(shown)}{suffix}")
        lines.append("")

    return "\n".join(lines).rstrip()
