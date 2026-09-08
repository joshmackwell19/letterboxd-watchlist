import html
import re
import time
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .models import FilmState

CINEMA_PRINCE_CHARLES = "Prince Charles Cinema"
CINEMA_BARBICAN = "Barbican"
CINEMA_VUE_FULHAM = "Vue Fulham Broadway"
CINEMA_RIVERSIDE = "Riverside Studios"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


class CinemaFetchError(Exception):
    pass


def _get(url: str, *, params: dict | None = None, max_retries: int = 3,
          backoff_base_seconds: float = 2.0) -> requests.Response:
    last_error: str | None = None
    for attempt in range(max_retries + 1):
        try:
            response = requests.get(url, headers=_HEADERS, params=params, timeout=15)
            if response.ok:
                return response
            last_error = f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            last_error = str(exc)
        if attempt < max_retries:
            time.sleep(backoff_base_seconds * (2 ** attempt))
    raise CinemaFetchError(f"request to {url} failed after {max_retries + 1} attempts ({last_error})")


def _get_html(url: str, *, params: dict | None = None) -> str:
    return _get(url, params=params).text


def _get_json(url: str, *, params: dict | None = None):
    return _get(url, params=params).json()


# ---------- Prince Charles Cinema ----------
# Plain server-rendered listing — every film's full future date list is
# already embedded in one page load (a JACRO cinema-booking plugin), no
# per-day requests needed.

PCC_WHATS_ON_URL = "https://princecharlescinema.com/whats-on/"
_ORDINAL_RE = re.compile(r"(\d+)(?:st|nd|rd|th)", re.IGNORECASE)
_MINS_RE = re.compile(r"(\d+)\s*mins?", re.IGNORECASE)


def fetch_prince_charles(*, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    return _parse_prince_charles(_get_html(PCC_WHATS_ON_URL), now)


def _parse_prince_charles(html: str, now: datetime) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    showings: list[dict] = []

    for event in soup.select(".jacro-event"):
        title_el = event.select_one(".liveeventtitle")
        perf_list = event.select_one(".performance-list-items")
        if title_el is None or perf_list is None:
            continue
        title = title_el.get_text(strip=True)

        running_time_spans = [s.get_text(strip=True) for s in event.select(".running-time span")]
        year = next((int(s) for s in running_time_spans if s.isdigit() and len(s) == 4), None)
        duration_minutes = None
        for s in running_time_spans:
            m = _MINS_RE.match(s)
            if m:
                duration_minutes = int(m.group(1))
                break

        director = None
        for span in event.select(".film-info span"):
            text = span.get_text(strip=True)
            if text.lower().startswith("directed by"):
                director = text[len("Directed by"):].strip()
                break

        synopsis_el = event.select_one(".jacro-formatted-text")
        synopsis = synopsis_el.get_text(strip=True) if synopsis_el else None
        poster_el = event.select_one(".film_img img")
        poster_url = poster_el.get("src") if poster_el else None

        # .heading date divs and <li> showtime items are siblings inside
        # the same <ul>, not nested — walk direct children in document
        # order, tracking whichever heading was seen most recently.
        current_date_str: str | None = None
        for child in perf_list.find_all(recursive=False):
            if "heading" in (child.get("class") or []):
                current_date_str = child.get_text(strip=True)
                continue
            if child.name != "li" or current_date_str is None:
                continue
            link_el = child.select_one("a.film_book_button")
            time_el = child.select_one(".time")
            if link_el is None or time_el is None:
                continue
            showtime = _parse_pcc_datetime(current_date_str, time_el.get_text(strip=True), now)
            if showtime is None:
                continue
            showings.append({
                "cinema": CINEMA_PRINCE_CHARLES, "title": title, "year": year,
                "showtime": showtime.isoformat(), "duration_minutes": duration_minutes,
                "director": director, "synopsis": synopsis, "poster_url": poster_url,
                "booking_url": link_el.get("href"),
            })

    return showings


def _parse_pcc_datetime(date_str: str, time_str: str, now: datetime) -> datetime | None:
    # e.g. "Tuesday 8th September" — no year given, so infer whichever of
    # this year/next year keeps the date from landing in the past (beyond
    # a week's slack, to tolerate the odd same-week listing quirk).
    parts = date_str.split()
    if len(parts) < 3:
        return None
    day_match = _ORDINAL_RE.match(parts[1])
    if not day_match:
        return None
    day = int(day_match.group(1))
    try:
        month = datetime.strptime(parts[2], "%B").month
    except ValueError:
        return None

    year = now.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    if candidate < (now.date() - timedelta(days=7)):
        try:
            candidate = date(year + 1, month, day)
        except ValueError:
            return None

    try:
        time_of_day = datetime.strptime(time_str.strip().lower().replace(" ", ""), "%I:%M%p").time()
    except ValueError:
        return None
    return datetime.combine(candidate, time_of_day)


# ---------- Barbican (cinema programme) ----------
# Server-rendered, but only ever shows the day passed via ?day= — no
# single request covers a date range, so one request per day.

BARBICAN_URL = "https://www.barbican.org.uk/whats-on/cinema"
BARBICAN_DAYS_AHEAD = 7
_HOUR_RE = re.compile(r"(\d+)\s*hr", re.IGNORECASE)
_HM_MIN_RE = re.compile(r"(\d+)\s*mins?", re.IGNORECASE)


def fetch_barbican(*, days_ahead: int = BARBICAN_DAYS_AHEAD, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    showings: list[dict] = []
    for offset in range(days_ahead):
        day = now.date() + timedelta(days=offset)
        html = _get_html(BARBICAN_URL, params={"day": day.isoformat()})
        showings.extend(_parse_barbican(html, day))
    return showings


def _parse_barbican(html: str, day: date) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    showings: list[dict] = []

    for card in soup.select(".cinema-listing-card"):
        title_el = card.select_one(".cinema-listing-card__title a")
        if title_el is None:
            continue
        title = title_el.get_text(strip=True)

        # No reliable director field on this card — the <strong> tag in
        # the blurb is sometimes a director's name, sometimes just a
        # bolded phrase mid-sentence (e.g. a cast member's name), so
        # splitting it out risks both a wrong "director" and a mangled
        # synopsis. Keep the full blurb intact instead; matching against
        # the watchlist relies on title, not director, anyway.
        director = None
        synopsis = None
        p = card.select_one(".cinema-listing-card__content p")
        if p is not None:
            synopsis = p.get_text(" ", strip=True)

        duration_minutes = None
        tag_el = card.select_one(".cinema-listing-card__tag")
        if tag_el is not None:
            duration_minutes = _parse_hr_min_duration(tag_el.get_text(strip=True))

        poster_el = card.select_one(".cinema-listing-card__media img")
        poster_url = poster_el.get("src") if poster_el else None
        if poster_url and poster_url.startswith("/"):
            poster_url = "https://www.barbican.org.uk" + poster_url

        for instance in card.select(".cinema-instance-list__instance"):
            link = instance.select_one("a[href]")
            if link is None:
                continue
            spans = link.find_all("span")
            time_text = spans[-1].get_text(strip=True) if spans else None
            if not time_text:
                continue
            showtime = _parse_barbican_time(day, time_text)
            if showtime is None:
                continue
            showings.append({
                "cinema": CINEMA_BARBICAN, "title": title, "year": None,
                "showtime": showtime.isoformat(), "duration_minutes": duration_minutes,
                "director": director, "synopsis": synopsis, "poster_url": poster_url,
                "booking_url": link.get("href"),
            })

    return showings


def _parse_hr_min_duration(text: str) -> int | None:
    hour_match = _HOUR_RE.search(text)
    min_match = _HM_MIN_RE.search(text)
    if not hour_match and not min_match:
        return None
    hours = int(hour_match.group(1)) if hour_match else 0
    minutes = int(min_match.group(1)) if min_match else 0
    return hours * 60 + minutes


def _parse_barbican_time(day: date, text: str) -> datetime | None:
    normalized = text.strip().lower().replace(".", ":").replace(" ", "")
    try:
        time_of_day = datetime.strptime(normalized, "%I:%M%p").time()
    except ValueError:
        return None
    return datetime.combine(day, time_of_day)


# ---------- Vue Fulham Broadway ----------
# Official, versioned JSON API — no HTML parsing at all. cinemaId 10046
# verified against myvue.com/cinema/fulham-broadway.

VUE_CINEMA_SLUG = "fulham-broadway"
VUE_CINEMA_ID = "10046"
VUE_WHATS_ON_URL = "https://www.myvue.com/cinema/{slug}/whats-on"
VUE_API_URL = "https://www.myvue.com/api/microservice/showings/cinemas/{cinema_id}/films"
VUE_DAYS_AHEAD = 7


def fetch_vue(*, cinema_slug: str = VUE_CINEMA_SLUG, cinema_id: str = VUE_CINEMA_ID,
              days_ahead: int = VUE_DAYS_AHEAD, now: datetime | None = None) -> list[dict]:
    # The showings API is gated behind an anonymous-session JWT that only
    # Vue's own HTML page sets as a cookie — a cold request straight to
    # the API 401s. One throwaway page visit first (same curl_cffi
    # session, so the cookie carries over) unlocks the real API calls.
    now = now or datetime.now()
    session = curl_requests.Session()
    session.get(VUE_WHATS_ON_URL.format(slug=cinema_slug), impersonate="chrome124", timeout=15)

    showings: list[dict] = []
    for offset in range(days_ahead):
        day = now.date() + timedelta(days=offset)
        response = session.get(VUE_API_URL.format(cinema_id=cinema_id), params={
            "showingDate": f"{day.isoformat()}T00:00:00",
            "minEmbargoLevel": 3, "includesSession": "true", "includeSessionAttributes": "true",
        }, impersonate="chrome124", timeout=15)
        if not response.ok:
            raise CinemaFetchError(f"Vue showings request failed for {day.isoformat()} (HTTP {response.status_code})")
        showings.extend(_parse_vue(response.json()))
    return showings


def _parse_vue(data: dict) -> list[dict]:
    showings: list[dict] = []
    for film in data.get("result", []):
        title = film.get("filmTitle")
        if not title:
            continue
        year = None
        release_date = film.get("releaseDate") or ""
        if len(release_date) >= 4 and release_date[:4].isdigit():
            year = int(release_date[:4])

        for group in film.get("showingGroups", []):
            for session in group.get("sessions", []):
                start_time = session.get("startTime")
                if not start_time:
                    continue
                booking_url = session.get("bookingUrl")
                if booking_url and booking_url.startswith("/"):
                    booking_url = "https://www.myvue.com" + booking_url
                showings.append({
                    "cinema": CINEMA_VUE_FULHAM, "title": title, "year": year,
                    "showtime": start_time, "duration_minutes": film.get("runningTime"),
                    "director": film.get("director") or None,
                    "synopsis": film.get("synopsisShort") or None,
                    "poster_url": film.get("posterImageSrc"), "booking_url": booking_url,
                })
    return showings


# ---------- Riverside Studios ----------
# Their listing page only ever fetches its cards via a JS-driven,
# encrypted filter token (not something derivable without running their
# frontend JS) — captured for "no filter" (every event, every category),
# filtered to Cinema ourselves below rather than trying to capture a
# cinema-specific token. If this ever starts returning nothing/erroring:
# open riversidestudios.co.uk/whats-on/ in a real browser, open devtools'
# network tab, reload, find the /ajax/filter_stream/<token>/ request, and
# swap the token below for the new one.
RIVERSIDE_FILTER_TOKEN = "ZWhHVEdwSDNuekJLUWI1OXVDQ0Fvdz09"
RIVERSIDE_URL = f"https://riversidestudios.co.uk/ajax/filter_stream/{RIVERSIDE_FILTER_TOKEN}/"
_RIVERSIDE_DURATION_RE = re.compile(r"(\d+)")
_RIVERSIDE_DIRECTOR_RE = re.compile(r"Director:(.*?)(?:Cast:|Company:|$)")


def fetch_riverside() -> list[dict]:
    return _parse_riverside(_get_json(RIVERSIDE_URL, params={"offset": 0, "limit": 500}))


def _parse_riverside(data: list[dict]) -> list[dict]:
    showings: list[dict] = []
    for item in data:
        if item.get("slot_tag") != "Cinema":
            continue
        title = item.get("title") or item.get("name")
        if not title:
            continue

        duration_minutes = None
        duration_match = _RIVERSIDE_DURATION_RE.match(item.get("duration") or "")
        if duration_match:
            duration_minutes = int(duration_match.group(1))

        director = None
        director_match = _RIVERSIDE_DIRECTOR_RE.search(item.get("search_text") or "")
        if director_match:
            director = director_match.group(1).strip() or None

        synopsis = html.unescape(item["text"]) if item.get("text") else None
        poster_url = item.get("image_url")
        booking_url = item.get("url")

        for performances in (item.get("performances") or {}).values():
            for perf in performances:
                timestamp = perf.get("timestamp")
                if not timestamp:
                    continue
                try:
                    showtime = datetime.fromtimestamp(int(timestamp))
                except (ValueError, OSError, OverflowError):
                    continue
                showings.append({
                    "cinema": CINEMA_RIVERSIDE, "title": title, "year": None,
                    "showtime": showtime.isoformat(), "duration_minutes": duration_minutes,
                    "director": director, "synopsis": synopsis, "poster_url": poster_url,
                    "booking_url": booking_url,
                })

    return showings


# ---------- Watchlist matching ----------

_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


def _normalize_title(title: str) -> str:
    normalized = _PUNCTUATION_RE.sub("", title.lower())
    normalized = _LEADING_ARTICLE_RE.sub("", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def match_watchlist_film(title: str, year: int | None, films: dict[str, FilmState]) -> str | None:
    """Matches a cinema listing's title (+ optional year) against the
    watchlist by normalized title — cinema sites vary in punctuation/
    article-stripping and rarely give a reliable year at all, so this
    can't be the exact-slug lookup Letterboxd matching gets to use.
    Year-tolerant (±1) when both sides have one, same spirit as
    Letterboxd's own year-tolerant confidence tier."""
    target = _normalize_title(title)
    if not target:
        return None

    candidates = [(slug, film) for slug, film in films.items() if _normalize_title(film.title) == target]
    if not candidates:
        return None
    if year is None or len(candidates) == 1:
        return candidates[0][0]

    for slug, film in candidates:
        if film.year is not None and abs(film.year - year) <= 1:
            return slug
    return candidates[0][0]
