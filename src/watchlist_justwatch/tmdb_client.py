import os
from itertools import zip_longest

import requests

TMDB_BASE_URL = "https://api.themoviedb.org/3"


class TMDBError(Exception):
    pass


def _api_key() -> str:
    key = os.getenv("TMDB_API_KEY")
    if not key:
        raise TMDBError("TMDB_API_KEY is not set (add it to .env)")
    return key


def _get(path: str, **params) -> dict:
    params["api_key"] = _api_key()
    response = requests.get(f"{TMDB_BASE_URL}{path}", params=params, timeout=15)
    if not response.ok:
        raise TMDBError(f"TMDB request to {path} failed: HTTP {response.status_code}")
    return response.json()


def search_movie(title: str, year: int | None = None) -> dict | None:
    params = {"query": title}
    if year is not None:
        params["year"] = year

    results = _get("/search/movie", **params).get("results", [])
    if not results:
        return None
    return results[0]


def similar_and_recommended(tmdb_id: int, *, limit: int = 15) -> list[dict]:
    similar = _get(f"/movie/{tmdb_id}/similar").get("results", [])
    recommended = _get(f"/movie/{tmdb_id}/recommendations").get("results", [])

    seen: set[int] = set()
    merged: list[dict] = []
    # Interleave so recommendations (behavior-based) and similar (content-based)
    # both get a fair shot rather than one list dominating the cap. zip_longest
    # (not zip) so a short list — these two endpoints often return very
    # different counts — doesn't silently cap the whole merge to its length
    # once the shorter list runs out, the longer one keeps contributing.
    for a, b in zip_longest(recommended, similar):
        for movie in (a, b):
            if movie is None:
                continue
            if movie["id"] not in seen:
                seen.add(movie["id"])
                merged.append(movie)
            if len(merged) >= limit:
                return merged
    return merged


def release_year(movie: dict) -> int | None:
    date = movie.get("release_date") or ""
    return int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None


def search_person(name: str) -> dict | None:
    results = _get("/search/person", query=name).get("results", [])
    return results[0] if results else None


def person_movie_credits(person_id: int) -> dict:
    """{"cast": [...], "crew": [...]} — crew entries include a "job" field
    ("Director", "Writer", etc.) to filter down to directing credits."""
    return _get(f"/person/{person_id}/movie_credits")


def discover_movies(
    *, sort_by: str, vote_count_gte: int = 0, vote_count_lte: int | None = None,
    vote_average_gte: float | None = None, with_genres: str | None = None, pages: int = 1,
) -> list[dict]:
    results: list[dict] = []
    for page in range(1, pages + 1):
        params: dict = {"sort_by": sort_by, "vote_count.gte": vote_count_gte, "page": page}
        if vote_count_lte is not None:
            params["vote_count.lte"] = vote_count_lte
        if vote_average_gte is not None:
            params["vote_average.gte"] = vote_average_gte
        if with_genres:
            params["with_genres"] = with_genres
        results.extend(_get("/discover/movie", **params).get("results", []))
    return results


def trending_movies(window: str = "week") -> list[dict]:
    return _get(f"/trending/movie/{window}").get("results", [])


GENRE_NAMES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance", 878: "Science Fiction",
    10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
}
