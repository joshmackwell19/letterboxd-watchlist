import time
from dataclasses import dataclass

from . import tmdb_client
from .config import CountryConfig, canonical_display_name, classify_offer
from .justwatch_client import fetch_offers, search_film
from .letterboxd import get_rating_by_tmdb_id
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


def recommend_from_recent_watches(
    recent_watches: list[dict], state: StateDoc, *, limit: int = 4, request_delay_seconds: float = 0.2,
) -> list[str]:
    """For each recently watched film, ask TMDB what's similar/recommended,
    then keep only the candidates that are actually on the watchlist —
    correlates "what you just watched" with "what to watch next" without
    fetching fresh availability for films that aren't already tracked.
    Best-effort: a TMDB hookup failure just skips that seed film rather than
    breaking the whole daily run, since this is a nice-to-have.
    """
    already_watched = {w["slug"] for w in recent_watches}
    seen_slugs: set[str] = set()
    recommendations: list[str] = []

    for watched in recent_watches:
        try:
            source = tmdb_client.search_movie(watched["title"], watched.get("year"))
            if source is None:
                continue
            candidates = tmdb_client.similar_and_recommended(source["id"], limit=30)
        except Exception:
            continue

        for movie in candidates:
            slug = _match_watchlist_slug(state, movie["title"], tmdb_client.release_year(movie))
            if slug and slug not in already_watched and slug not in seen_slugs:
                seen_slugs.add(slug)
                recommendations.append(slug)
                if len(recommendations) >= limit:
                    return recommendations
        time.sleep(request_delay_seconds)

    return recommendations


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
