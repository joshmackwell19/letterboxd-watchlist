import json
import math
import re
import sys
import time
import xml.etree.ElementTree as ET

from curl_cffi import requests as curl_requests

from .models import WatchlistFilm

WATCHLIST_COUNT_RE = re.compile(r'js-watchlist-count">([\d,]+)')
ITEM_NAME_RE = re.compile(r'data-item-name="([^"]+)"')
ITEM_SLUG_RE = re.compile(r'data-item-slug="([^"]+)"')
TITLE_YEAR_RE = re.compile(r"^(?P<title>.+) \((?P<year>\d{4})\)$")
RATING_RE = re.compile(r'name="twitter:data2" content="([\d.]+) out of 5"')
JSON_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
RECENT_ACTIVITY_RE = re.compile(r'<section id="recent-activity".*?</section>', re.DOTALL)
DIARY_GUID_RE = re.compile(r"<guid[^>]*>([^<]+)</guid>")
DIARY_ROW_RE = re.compile(r'<tr class="diary-entry-row.*?</tr>\s*(?=<tr|</tbody>)', re.DOTALL)
DIARY_RATING_RE = re.compile(r'class="rateit-field diary-rating-\d+"[^>]*value="(\d+)"')
DIARY_REWATCH_RE = re.compile(r'js-td-rewatch icon-status-(on|off)')
DIARY_DATE_RE = re.compile(r"/diary/films/for/(\d{4})/(\d{2})/(\d{2})/")
_RSS_NS = {"letterboxd": "https://letterboxd.com"}
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


_EMPTY_FILM_DETAILS = {
    "rating": None, "rating_count": None, "poster_url": None, "director": [], "starring": [], "synopsis": None,
}


def _film_details_from_json_ld(html: str) -> dict:
    data = _parse_film_json_ld(html)
    if data is None:
        return dict(_EMPTY_FILM_DETAILS)

    aggregate_rating = data.get("aggregateRating", {})
    rating = aggregate_rating.get("ratingValue")
    rating_count = aggregate_rating.get("ratingCount")
    return {
        "rating": float(rating) if rating is not None else None,
        # Members who've rated the film on Letterboxd — a direct Letterboxd
        # popularity/recognition signal (see discover_hidden_gems/
        # discover_by_genre), not just a proxy via TMDB's own vote count.
        "rating_count": int(rating_count) if rating_count is not None else None,
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


def fetch_watched_films(
    username: str,
    *,
    full: bool,
    max_pages: int = 3,
    max_full_pages: int = 60,
    impersonate: str = "chrome124",
    max_retries: int = 3,
    backoff_base_seconds: float = 2.0,
    request_timeout_seconds: float = 15.0,
    page_delay_seconds: float = 0.3,
) -> list[WatchlistFilm]:
    """Every film logged as watched (letterboxd.com/{username}/films/) — used
    to exclude already-seen films from discovery recommendations. This grid
    doesn't carry per-entry watch dates (only the dated /films/diary/ page
    does, and that one 403s from GitHub Actions' IP range specifically), so
    the result is a "have you seen this at all" set, not a timeline.

    Newly logged films always sort to the front of this grid, so a full
    backfill (previous state has no watched films yet) pages through the
    entire history once; every day after, only the first `max_pages` pages
    are re-checked, keeping this fast regardless of how large the history
    gets. Best-effort: any fetch failure just stops paging rather than
    breaking the whole daily run.
    """
    session = curl_requests.Session()
    all_films: dict[str, WatchlistFilm] = {}
    limit = max_full_pages if full else max_pages

    for page_num in range(1, limit + 1):
        try:
            response = _fetch_url(session, f"https://letterboxd.com/{username}/films/page/{page_num}/",
                                   max_retries=max_retries, backoff_base_seconds=backoff_base_seconds,
                                   request_timeout_seconds=request_timeout_seconds, impersonate=impersonate)
        except LetterboxdFetchError as exc:
            print(f"warning: watched-films fetch failed on page {page_num} ({exc})", file=sys.stderr)
            break

        films, _ = _parse_watchlist_page(response.text)
        if not films:
            break
        for f in films:
            all_films[f.slug] = f
        time.sleep(page_delay_seconds)

    return list(all_films.values())


def fetch_new_diary_entries(
    username: str,
    since_guid: str | None,
    *,
    impersonate: str = "chrome124",
    max_retries: int = 3,
    backoff_base_seconds: float = 2.0,
    request_timeout_seconds: float = 15.0,
) -> list[dict]:
    """Entries newer than since_guid (exclusive) from the user's Letterboxd
    RSS feed (/username/rss/, newest first, only 50 max — there's no
    pagination on this feed). Used both to detect "has anything changed"
    (see --check-for-new-log) and, since the feed already carries your own
    rating/like/rewatch per entry, to keep state.diary's personal-taste
    fields current without a second fetch. Returns [] on any fetch/parse
    failure so a transient hiccup just skips that check rather than raising.

    since_guid=None (first run, or the stored guid has scrolled off the
    50-entry window) returns every entry on the page."""
    session = curl_requests.Session()
    try:
        response = _fetch_url(session, f"https://letterboxd.com/{username}/rss/", max_retries=max_retries,
                               backoff_base_seconds=backoff_base_seconds,
                               request_timeout_seconds=request_timeout_seconds, impersonate=impersonate)
    except LetterboxdFetchError:
        return []

    try:
        root = ET.fromstring(response.text)
    except ET.ParseError:
        return []

    entries = []
    for item in root.findall(".//item"):
        guid = item.findtext("guid")
        if guid == since_guid:
            break
        link = item.findtext("link") or ""
        slug_match = re.search(r"/film/([^/]+)/", link)
        if guid is None or not slug_match:
            continue
        rating_text = item.findtext("letterboxd:memberRating", namespaces=_RSS_NS)
        year_text = item.findtext("letterboxd:filmYear", namespaces=_RSS_NS)
        entries.append({
            "guid": guid,
            "slug": slug_match.group(1),
            "title": item.findtext("letterboxd:filmTitle", namespaces=_RSS_NS),
            "year": int(year_text) if year_text else None,
            "watched_date": item.findtext("letterboxd:watchedDate", namespaces=_RSS_NS),
            "is_rewatch": item.findtext("letterboxd:rewatch", namespaces=_RSS_NS) == "Yes",
            "personal_rating": float(rating_text) if rating_text else None,
            "liked": item.findtext("letterboxd:memberLike", namespaces=_RSS_NS) == "Yes",
        })
    return entries


def fetch_diary_ratings(
    username: str,
    *,
    max_pages: int = 60,
    impersonate: str = "chrome124",
    max_retries: int = 3,
    backoff_base_seconds: float = 2.0,
    request_timeout_seconds: float = 15.0,
    page_delay_seconds: float = 0.3,
) -> dict[str, dict]:
    """One-time historical backfill of personal_rating/is_rewatch/watched_date
    (slug -> dict) from the dated diary pages — must run locally, same as
    fetch_watched_films, since Letterboxd blocks /username/films/ (diary
    included) from GitHub Actions' IP range. The RSS feed only covers the
    last ~50 entries; this covers everything older.

    "liked" isn't included — the diary page's like indicator is hydrated by
    a client-side API call this static fetch never makes, so it's just not
    reliably scrapable here. Only the RSS-fed path (fetch_new_diary_entries)
    can capture likes, going forward from whenever this app started polling.

    A rewatched film has multiple diary rows for the same slug; the first
    one encountered (newest first) wins, so this reflects your most recent
    viewing rather than being overwritten by an older one."""
    session = curl_requests.Session()
    result: dict[str, dict] = {}

    for page_num in range(1, max_pages + 1):
        try:
            response = _fetch_url(session, f"https://letterboxd.com/{username}/films/diary/page/{page_num}/",
                                   max_retries=max_retries, backoff_base_seconds=backoff_base_seconds,
                                   request_timeout_seconds=request_timeout_seconds, impersonate=impersonate)
        except LetterboxdFetchError as exc:
            print(f"warning: diary-ratings fetch failed on page {page_num} ({exc})", file=sys.stderr)
            break

        rows = DIARY_ROW_RE.findall(response.text)
        if not rows:
            break

        for row in rows:
            slug_match = ITEM_SLUG_RE.search(row)
            if not slug_match:
                continue
            slug = slug_match.group(1)
            if slug in result:
                continue

            entry: dict = {}
            rating_match = DIARY_RATING_RE.search(row)
            if rating_match:
                entry["personal_rating"] = int(rating_match.group(1)) / 2
            rewatch_match = DIARY_REWATCH_RE.search(row)
            if rewatch_match:
                entry["is_rewatch"] = rewatch_match.group(1) == "on"
            date_match = DIARY_DATE_RE.search(row)
            if date_match:
                entry["watched_date"] = "-".join(date_match.groups())
            if entry:
                result[slug] = entry

        time.sleep(page_delay_seconds)

    return result
