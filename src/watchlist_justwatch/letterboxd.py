import json
import math
import re
import sys
import time

import defusedxml.ElementTree as ET
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
DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?$")


class LetterboxdFetchError(Exception):
    pass


class LetterboxdBlockedError(LetterboxdFetchError):
    """Letterboxd (or Cloudflare in front of it) refused the request outright
    — a 403/429/503 or a "Just a moment..." challenge page. Unlike a network
    hiccup this is never worth retrying: carrying on is how a temporary
    challenge becomes a longer block."""


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
    except LetterboxdFetchError as exc:
        print(f"warning: rating fetch failed for tmdb_id={tmdb_id} ({exc})", file=sys.stderr)
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
    "genre": [], "runtime_minutes": None,
}


def _parse_duration_minutes(duration: str | None) -> int | None:
    """schema.org's ISO-8601 duration format, e.g. "PT2H13M" -> 133. Only
    the H/M components appear in practice (films aren't measured in days),
    and either can be absent (a sub-hour short is just "PT45M")."""
    if not duration:
        return None
    match = DURATION_RE.match(duration)
    if not match:
        return None
    hours, minutes = match.groups()
    total = int(hours or 0) * 60 + int(minutes or 0)
    return total or None


def _film_details_from_json_ld(html: str) -> dict:
    data = _parse_film_json_ld(html)
    if data is None:
        return dict(_EMPTY_FILM_DETAILS)

    aggregate_rating = data.get("aggregateRating", {})
    rating = aggregate_rating.get("ratingValue")
    rating_count = aggregate_rating.get("ratingCount")
    # schema.org allows "genre" to be either a single string or a list —
    # Letterboxd emits a list for every film we've seen, but normalize
    # defensively rather than trust that never changes.
    genre = data.get("genre", [])
    if isinstance(genre, str):
        genre = [genre]
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
        "genre": genre,
        "runtime_minutes": _parse_duration_minutes(data.get("duration")),
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


MAX_LIST_PAGES = 50


def fetch_list_slugs(
    list_path: str,
    *,
    impersonate: str = "chrome124",
    max_retries: int = 3,
    backoff_base_seconds: float = 2.0,
    request_timeout_seconds: float = 15.0,
    page_delay_seconds: float = 0.5,
) -> list[str]:
    """Every film slug on a Letterboxd list (`user/list/slug`), in list
    order. Unlike the watchlist there's no total-count element to size the
    pagination from, so this walks pages until one comes back empty —
    capped at MAX_LIST_PAGES so a markup change can't loop forever."""
    session = curl_requests.Session()
    slugs: dict[str, None] = {}
    for page_num in range(1, MAX_LIST_PAGES + 1):
        if page_num > 1:
            time.sleep(page_delay_seconds)
        response = _fetch_url(session, f"https://letterboxd.com/{list_path}/page/{page_num}/",
                               max_retries=max_retries, backoff_base_seconds=backoff_base_seconds,
                               request_timeout_seconds=request_timeout_seconds, impersonate=impersonate)
        page_slugs = ITEM_SLUG_RE.findall(response.text)
        if not page_slugs:
            break
        for slug in page_slugs:
            slugs[slug] = None
    return list(slugs)


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


# --- Other members' public ratings (the taste engine, see taste.py) --------
#
# Three page types, all verified against live markup: a member's /films/ grid
# (72 films a page, each poster carrying its `rated-N` half-star class in the
# static HTML), a film's /members/rated/<stars>/ table (25 members a page who
# gave that film exactly that rating), and a member's /following/ table. All
# three sit under the paths Letterboxd blocks from datacenter IP ranges, so
# like the diary backfills they only work from a home connection.

GRID_ITEM_SPLIT_RE = re.compile(r'<li class="griditem[^"]*"')
GRID_RATING_RE = re.compile(r'<span class="rating[^"]*\brated-(\d+)"')
ITEM_NAME_ATTR_RE = re.compile(r'data-item-name="([^"]*)"')
PERSON_ROW_SPLIT_RE = re.compile(r'<td class="col-member table-person">')
PERSON_LINK_RE = re.compile(r'<a href="/([^/"]+)/" class="name"')
MEMBER_RATING_RE = re.compile(r'<td class="col-rating[^"]*">\s*<span class="rating[^"]*\brated-(\d+)"')
WATCHED_COUNT_RE = re.compile(r'class="has-icon icon-16 icon-watched" href="/[^/"]+/films/">([\d,]+)</a>')
NEXT_PAGE_RE = re.compile(r'<a class="next" href="')
BLOCKED_STATUSES = {403, 429, 503}


def _has_next_page(html: str) -> bool:
    return NEXT_PAGE_RE.search(html) is not None


def _is_challenge_page(html: str) -> bool:
    return "<title>Just a moment...</title>" in html[:4000]


def parse_rated_films_page(html: str) -> tuple[list[tuple[str, str | None, int]], bool]:
    """(slug, display name, half-stars 1-10) for every *rated* film on one
    page of a member's /films/ grid — watched-but-unrated posters are skipped
    — plus whether there's a next page."""
    rated: list[tuple[str, str | None, int]] = []
    for item in GRID_ITEM_SPLIT_RE.split(html)[1:]:
        slug_match = ITEM_SLUG_RE.search(item)
        rating_match = GRID_RATING_RE.search(item)
        if not slug_match or not rating_match:
            continue
        half_stars = int(rating_match.group(1))
        if not 1 <= half_stars <= 10:
            continue
        name_match = ITEM_NAME_ATTR_RE.search(item)
        rated.append((slug_match.group(1), _unescape(name_match.group(1)) if name_match else None, half_stars))
    return rated, _has_next_page(html)


def parse_member_ratings_page(html: str) -> tuple[list[tuple[str, int]], bool]:
    """(username, half-stars) per row of a film's /members/rated/<stars>/
    table, plus whether there's a next page."""
    members: list[tuple[str, int]] = []
    for row in PERSON_ROW_SPLIT_RE.split(html)[1:]:
        user_match = PERSON_LINK_RE.search(row)
        rating_match = MEMBER_RATING_RE.search(row)
        if user_match and rating_match:
            members.append((user_match.group(1), int(rating_match.group(1))))
    return members, _has_next_page(html)


def parse_following_page(html: str) -> tuple[list[tuple[str, int | None]], bool]:
    """(username, films watched) per row of a member's /following/ table,
    plus whether there's a next page."""
    people: list[tuple[str, int | None]] = []
    for row in PERSON_ROW_SPLIT_RE.split(html)[1:]:
        user_match = PERSON_LINK_RE.search(row)
        if not user_match:
            continue
        watched_match = WATCHED_COUNT_RE.search(row)
        people.append((user_match.group(1), int(watched_match.group(1).replace(",", "")) if watched_match else None))
    return people, _has_next_page(html)


def fetch_page_strict(
    session,
    url: str,
    *,
    impersonate: str = "chrome124",
    request_timeout_seconds: float = 20.0,
    network_retries: int = 2,
    network_backoff_seconds: float = 30.0,
    sleep=time.sleep,
) -> str | None:
    """One page, for long-running scrapes where getting blocked is the thing
    to avoid above all. A block (see LetterboxdBlockedError) raises
    immediately with no retry — _fetch_url's retry-with-backoff is the right
    call for a daily run's handful of requests, and exactly the wrong one
    for thousands of them. A 404 (renamed/deleted member) returns None.
    Only connection failures and other 5xx get a couple of slow retries,
    and still raise if they persist (the Mac went to sleep, the Wi-Fi
    dropped), so an unattended run stops rather than spinning."""
    last_error = ""
    for attempt in range(network_retries + 1):
        try:
            response = session.get(url, impersonate=impersonate, timeout=request_timeout_seconds)
        except Exception as exc:  # curl_cffi raises its own exception types
            last_error = str(exc)
        else:
            if response.status_code in BLOCKED_STATUSES or _is_challenge_page(response.text):
                raise LetterboxdBlockedError(f"HTTP {response.status_code} from {url}")
            if response.status_code == 404:
                return None
            if response.status_code == 200:
                return response.text
            last_error = f"HTTP {response.status_code}"
        if attempt < network_retries:
            sleep(network_backoff_seconds * (attempt + 1))
    raise LetterboxdFetchError(f"{url} failed after {network_retries + 1} attempts ({last_error})")


def fetch_rated_films(
    username: str,
    *,
    max_pages: int = 60,
    page_delay_seconds: float = 1.0,
    impersonate: str = "chrome124",
) -> dict[str, float]:
    """slug -> rating (0.5-5) for every film the user has rated, from their
    /films/ grid. Unlike the diary pages (fetch_diary_ratings), the grid
    covers films rated without ever being logged, and shows each film's
    current rating rather than one viewing's. Local only, same IP block as
    the other /username/films/ backfills; raises LetterboxdBlockedError on
    the first sign of a block rather than retrying through it."""
    session = curl_requests.Session()
    ratings: dict[str, float] = {}
    for page_num in range(1, max_pages + 1):
        html = fetch_page_strict(session, f"https://letterboxd.com/{username}/films/page/{page_num}/",
                                 impersonate=impersonate)
        if html is None:
            break
        rated, has_next = parse_rated_films_page(html)
        for slug, _, half_stars in rated:
            ratings[slug] = half_stars / 2
        if not has_next:
            break
        time.sleep(page_delay_seconds)
    return ratings
