import json
import math
import re
import sys
import time

from curl_cffi import requests as curl_requests

from .models import WatchlistFilm

WATCHLIST_COUNT_RE = re.compile(r'js-watchlist-count">([\d,]+)')
ITEM_NAME_RE = re.compile(r'data-item-name="([^"]+)"')
ITEM_SLUG_RE = re.compile(r'data-item-slug="([^"]+)"')
TITLE_YEAR_RE = re.compile(r"^(?P<title>.+) \((?P<year>\d{4})\)$")
RATING_RE = re.compile(r'name="twitter:data2" content="([\d.]+) out of 5"')
JSON_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
RECENT_ACTIVITY_RE = re.compile(r'<section id="recent-activity".*?</section>', re.DOTALL)
MAX_STARRING = 5


class LetterboxdFetchError(Exception):
    pass


def _unescape(text: str) -> str:
    return text.replace("&#039;", "'").replace("&quot;", '"').replace("&amp;", "&")


def _fetch_url(session, url: str, *, max_retries: int, backoff_base_seconds: float,
                request_timeout_seconds: float, impersonate: str):
    last_error: str | None = None

    for attempt in range(max_retries + 1):
        try:
            response = session.get(url, impersonate=impersonate, timeout=request_timeout_seconds)
            if response.status_code == 200:
                return response
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:  # curl_cffi raises its own exception types
            last_error = str(exc)

        if attempt < max_retries:
            time.sleep(backoff_base_seconds * (2 ** attempt))

    raise LetterboxdFetchError(
        f"Letterboxd fetch failed for {url} ({last_error}). If this started suddenly, "
        f"Letterboxd's bot detection may have changed — try updating the curl_cffi "
        f"`impersonate` profile (e.g. to a newer chrome/safari version)."
    )


def _fetch_page(session, username: str, page_num: int, *, max_retries: int, backoff_base_seconds: float,
                 request_timeout_seconds: float, impersonate: str) -> str:
    url = f"https://letterboxd.com/{username}/watchlist/page/{page_num}/"
    response = _fetch_url(session, url, max_retries=max_retries, backoff_base_seconds=backoff_base_seconds,
                           request_timeout_seconds=request_timeout_seconds, impersonate=impersonate)
    return response.text


def get_rating_by_tmdb_id(
    tmdb_id: int,
    *,
    impersonate: str = "chrome124",
    max_retries: int = 3,
    backoff_base_seconds: float = 2.0,
    request_timeout_seconds: float = 15.0,
) -> float | None:
    """Letterboxd redirects /tmdb/{id}/ straight to the matching film page —
    no need to guess slugs from titles."""
    session = curl_requests.Session()
    try:
        response = _fetch_url(session, f"https://letterboxd.com/tmdb/{tmdb_id}/", max_retries=max_retries,
                               backoff_base_seconds=backoff_base_seconds,
                               request_timeout_seconds=request_timeout_seconds, impersonate=impersonate)
    except LetterboxdFetchError:
        return None

    match = RATING_RE.search(response.text)
    return float(match.group(1)) if match else None


def _parse_film_json_ld(html: str) -> dict | None:
    match = JSON_LD_RE.search(html)
    if not match:
        return None
    raw = match.group(1).replace("/* <![CDATA[ */", "").replace("/* ]]> */", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


_EMPTY_FILM_DETAILS = {"rating": None, "poster_url": None, "director": [], "starring": [], "synopsis": None}


def _film_details_from_json_ld(html: str) -> dict:
    data = _parse_film_json_ld(html)
    if data is None:
        return dict(_EMPTY_FILM_DETAILS)

    rating = data.get("aggregateRating", {}).get("ratingValue")
    return {
        "rating": float(rating) if rating is not None else None,
        "poster_url": data.get("image"),
        "director": [p["name"] for p in data.get("director", []) if p.get("name")],
        "starring": [p["name"] for p in data.get("actor", [])[:MAX_STARRING] if p.get("name")],
        "synopsis": data.get("description"),
    }


def get_film_details_by_slug(
    slug: str,
    *,
    session=None,
    impersonate: str = "chrome124",
    max_retries: int = 3,
    backoff_base_seconds: float = 2.0,
    request_timeout_seconds: float = 15.0,
) -> dict:
    """Fetch a film's rating, poster, director, top cast, and synopsis in one
    request via the JSON-LD schema.org block Letterboxd embeds on every film
    page — used for watchlist films where we already have the slug from the
    watchlist page, so no TMDB lookup is needed.

    Always returns a dict (possibly all-None/empty) rather than raising or
    returning None, so callers can merge it in unconditionally.
    """
    session = session or curl_requests.Session()
    try:
        response = _fetch_url(session, f"https://letterboxd.com/film/{slug}/", max_retries=max_retries,
                               backoff_base_seconds=backoff_base_seconds,
                               request_timeout_seconds=request_timeout_seconds, impersonate=impersonate)
    except LetterboxdFetchError:
        return dict(_EMPTY_FILM_DETAILS)

    return _film_details_from_json_ld(response.text)


def get_film_details_by_tmdb_id(
    tmdb_id: int,
    *,
    session=None,
    impersonate: str = "chrome124",
    max_retries: int = 3,
    backoff_base_seconds: float = 2.0,
    request_timeout_seconds: float = 15.0,
) -> dict | None:
    """Same rating/poster/director/starring/synopsis as get_film_details_by_slug,
    plus the resolved slug — for films discovered via TMDB correlation that
    aren't on the watchlist, where no Letterboxd slug is known yet.
    Letterboxd redirects /tmdb/{id}/ straight to the matching /film/{slug}/,
    so one request resolves both. Returns None if there's no Letterboxd
    match (rare) or the fetch fails, so callers can just skip that candidate.
    """
    session = session or curl_requests.Session()
    try:
        response = _fetch_url(session, f"https://letterboxd.com/tmdb/{tmdb_id}/", max_retries=max_retries,
                               backoff_base_seconds=backoff_base_seconds,
                               request_timeout_seconds=request_timeout_seconds, impersonate=impersonate)
    except LetterboxdFetchError:
        return None

    slug_match = re.search(r"/film/([^/]+)/", response.url)
    if not slug_match:
        return None

    return {"slug": slug_match.group(1), **_film_details_from_json_ld(response.text)}


def _parse_watchlist_page(html: str) -> tuple[list[WatchlistFilm], int | None]:
    count_match = WATCHLIST_COUNT_RE.search(html)
    total_count = int(count_match.group(1).replace(",", "")) if count_match else None

    names = ITEM_NAME_RE.findall(html)
    slugs = ITEM_SLUG_RE.findall(html)

    films: list[WatchlistFilm] = []
    for name, slug in zip(names, slugs):
        name = _unescape(name)
        m = TITLE_YEAR_RE.match(name)
        if m:
            films.append(WatchlistFilm(slug=slug, title=m.group("title"), year=int(m.group("year"))))
        else:
            films.append(WatchlistFilm(slug=slug, title=name, year=None))

    return films, total_count


def fetch_watchlist(
    username: str,
    *,
    impersonate: str = "chrome124",
    max_retries: int = 3,
    backoff_base_seconds: float = 2.0,
    request_timeout_seconds: float = 15.0,
    page_delay_seconds: float = 0.5,
) -> list[WatchlistFilm]:
    session = curl_requests.Session()

    page_1_html = _fetch_page(session, username, 1, max_retries=max_retries,
                               backoff_base_seconds=backoff_base_seconds,
                               request_timeout_seconds=request_timeout_seconds, impersonate=impersonate)
    films_page_1, total_count = _parse_watchlist_page(page_1_html)

    all_films: dict[str, WatchlistFilm] = {f.slug: f for f in films_page_1}

    if total_count and films_page_1:
        items_per_page = len(films_page_1)
        total_pages = math.ceil(total_count / items_per_page)

        for page_num in range(2, total_pages + 1):
            time.sleep(page_delay_seconds)
            html = _fetch_page(session, username, page_num, max_retries=max_retries,
                                backoff_base_seconds=backoff_base_seconds,
                                request_timeout_seconds=request_timeout_seconds, impersonate=impersonate)
            films, _ = _parse_watchlist_page(html)
            for f in films:
                all_films[f.slug] = f

    return list(all_films.values())


def fetch_recent_watches(
    username: str,
    *,
    limit: int = 4,
    impersonate: str = "chrome124",
    max_retries: int = 3,
    backoff_base_seconds: float = 2.0,
    request_timeout_seconds: float = 15.0,
) -> list[WatchlistFilm]:
    """Most recently watched films first, read from the "Recent activity"
    poster grid Letterboxd already embeds on the profile homepage. The
    dedicated diary page (/films/diary/) returns a 403 from GitHub Actions'
    IP range even though the profile and watchlist pages don't, so this
    reads the same last-few-watched data from a page that isn't blocked.
    Feeds the dashboard's "because you recently watched" recommendations,
    a nice-to-have — any fetch failure just yields no recent-watch data
    rather than breaking the whole daily run."""
    session = curl_requests.Session()
    try:
        response = _fetch_url(session, f"https://letterboxd.com/{username}/", max_retries=max_retries,
                               backoff_base_seconds=backoff_base_seconds,
                               request_timeout_seconds=request_timeout_seconds, impersonate=impersonate)
    except LetterboxdFetchError as exc:
        print(f"warning: profile fetch failed, skipping recent-watch recommendations ({exc})", file=sys.stderr)
        return []

    section_match = RECENT_ACTIVITY_RE.search(response.text)
    if not section_match:
        return []

    entries, _ = _parse_watchlist_page(section_match.group(0))  # same data-item-slug/name grid markup
    seen: set[str] = set()
    result: list[WatchlistFilm] = []
    for entry in entries:
        if entry.slug in seen:
            continue
        seen.add(entry.slug)
        result.append(entry)
        if len(result) >= limit:
            break
    return result
