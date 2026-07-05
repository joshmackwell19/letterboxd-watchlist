import os

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
    # both get a fair shot rather than one list dominating the cap.
    for a, b in zip(recommended, similar):
        for movie in (a, b):
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
