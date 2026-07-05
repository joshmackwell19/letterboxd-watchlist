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
    purpose (candidates is usually much longer than `limit`): some won't
    have a Letterboxd match, and some won't currently have any tracked
    offer anywhere, and silently skipping those is what lets a section
    reliably reach `limit` films instead of coming up short."""
    slugs: list[str] = []
    films: dict[str, dict] = {}

    for movie in candidates:
        if len(slugs) >= limit:
            break

        details = get_film_details_by_tmdb_id(movie["id"])
        if details is None or details["slug"] in exclude_slugs or details["slug"] in films:
            continue

        temp_film = WatchlistFilm(slug=details["slug"], title=movie.get("title") or "",
                                   year=tmdb_client.release_year(movie))
        film_state = resolve_and_fetch(temp_film, None, None, now_iso=now_iso)
        if not film_state.offers:
            continue
        all_offers = _all_offers_for_film(film_state, config, global_subscriptions, revisitable)
        if not all_offers:
            continue

        slug = details["slug"]
        films[slug] = {
            "slug": slug, "title": movie.get("title") or "", "year": tmdb_client.release_year(movie),
            "rating": details["rating"], "poster_url": details["poster_url"],
            "director": ", ".join(details["director"]) if details["director"] else None,
            "starring": details["starring"], "synopsis": details["synopsis"],
            "all_offers": all_offers,
        }
        slugs.append(slug)

    return slugs, films


def discover_because_watched(
    recent_watches: list[dict], now_iso: str, config: dict[str, CountryConfig], global_subscriptions: list[str],
    revisitable: set[str], exclude_slugs: set[str], *, limit: int = 4,
) -> tuple[str, list[str], dict[str, dict]]:
    candidates = _candidates_from_recent_watches(recent_watches, candidate_pool=30)
    slugs, films = _enrich_candidates(candidates, now_iso, config, global_subscriptions, revisitable,
                                       exclude_slugs=exclude_slugs, limit=limit)
    return "Because you've been watching", slugs, films


def discover_same_director(
    recent_watches: list[dict], now_iso: str, config: dict[str, CountryConfig], global_subscriptions: list[str],
    revisitable: set[str], exclude_slugs: set[str], *, limit: int = 4,
) -> tuple[str, list[str], dict[str, dict]]:
    directors: list[str] = []
    for watched in recent_watches:
        for d in watched.get("director") or []:
            if d not in directors:
                directors.append(d)
    if not directors:
        return "", [], {}

    candidates = _candidates_by_person(directors, "director", candidate_pool=30)
    slugs, films = _enrich_candidates(candidates, now_iso, config, global_subscriptions, revisitable,
                                       exclude_slugs=exclude_slugs, limit=limit)
    return "More from " + " & ".join(directors[:2]), slugs, films


def discover_same_cast(
    recent_watches: list[dict], now_iso: str, config: dict[str, CountryConfig], global_subscriptions: list[str],
    revisitable: set[str], exclude_slugs: set[str], *, limit: int = 4,
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

    candidates = _candidates_by_person(top_actors, "cast", candidate_pool=30)
    slugs, films = _enrich_candidates(candidates, now_iso, config, global_subscriptions, revisitable,
                                       exclude_slugs=exclude_slugs, limit=limit)
    return "More starring " + ", ".join(top_actors), slugs, films


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
