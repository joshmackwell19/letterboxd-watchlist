import html
import re
import unicodedata
import time
from dataclasses import dataclass
from functools import lru_cache
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .models import FilmState

CINEMA_PRINCE_CHARLES = "Prince Charles Cinema"
CINEMA_BARBICAN = "Barbican"
CINEMA_BFI_SOUTHBANK = "BFI Southbank"
CINEMA_BFI_IMAX = "BFI IMAX"
CINEMA_GATE = "The Gate Notting Hill"
CINEMA_VUE_FULHAM = "Vue Fulham Broadway"
CINEMA_VUE_SHEPHERDS_BUSH = "Vue Shepherd's Bush"
CINEMA_VUE_WEST_END = "Vue West End"
CINEMA_VUE_PICCADILLY = "Vue Piccadilly"
CINEMA_RIVERSIDE = "Riverside Studios"
# Every venue, in the order the dashboard's venue filter lists them.
CINEMA_VENUES = (
    CINEMA_PRINCE_CHARLES, CINEMA_BARBICAN, CINEMA_BFI_SOUTHBANK, CINEMA_BFI_IMAX, CINEMA_GATE,
    CINEMA_VUE_FULHAM, CINEMA_VUE_SHEPHERDS_BUSH, CINEMA_VUE_WEST_END, CINEMA_VUE_PICCADILLY,
    CINEMA_RIVERSIDE,
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


class CinemaFetchError(Exception):
    pass


# Every venue is in London and every stored showtime is a naive London
# wall-clock ISO string — so "has this showing passed" has to be asked in
# London time too, not the runner's (GitHub Actions runs in UTC, which is
# an hour behind all summer).
LONDON = ZoneInfo("Europe/London")


def london_now() -> datetime:
    return datetime.now(LONDON).replace(tzinfo=None)


def drop_past_showings(showings: list[dict], now: datetime | None = None) -> list[dict]:
    """Only the showings that haven't started yet. Applied when a run
    stores the listing (which matters most for a venue whose fetch failed
    and was carried forward — otherwise its last good listing would be
    carried forward, and shown, indefinitely) and again when the
    dashboard is built."""
    now_iso = (now or london_now()).isoformat()
    return [s for s in showings if s["showtime"] >= now_iso]


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
# Two weeks, for the dashboard's "Next week" filter.
BARBICAN_DAYS_AHEAD = 14
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


# ---------- Vue ----------
# Official, versioned JSON API — no HTML parsing at all.
#
# myvue.com now sits behind a Cloudflare managed challenge that every
# datacenter IP gets (GitHub Actions included), whichever browser
# curl_cffi impersonates — the HTML page and the API alike answer 403
# "Just a moment...". So the direct API is still tried first (it works
# from a residential connection, and carries the most detail), and when
# it's refused the same programme comes from CinemaGuide instead.
#
# Every site comes through the same two routes, so a site is only its two
# ids and adding one is a line in VUE_SITES: Vue's cinema id is the one in
# the site's own booking links (/book-tickets/summary/<id>/...), and
# CinemaGuide's slug is its venue name slugified.


@dataclass(frozen=True)
class VueSite:
    name: str
    vue_id: str
    cinemaguide_slug: str


VUE_SITES = (
    VueSite(CINEMA_VUE_FULHAM, "10046", "vue-london-fulham-broadway"),
    VueSite(CINEMA_VUE_SHEPHERDS_BUSH, "10072", "vue-london-westfield-shepherds-bush"),
    VueSite(CINEMA_VUE_WEST_END, "10030", "vue-london-west-end-leicester-square"),
    VueSite(CINEMA_VUE_PICCADILLY, "10080", "vue-london-piccadilly-circus"),
)

# Any Vue page sets the session cookie the API wants, for every site — so
# one page visit opens the session for all of them.
VUE_WHATS_ON_URL = "https://www.myvue.com/cinema/fulham-broadway/whats-on"
VUE_API_URL = "https://www.myvue.com/api/microservice/showings/cinemas/{cinema_id}/films"
# Two weeks, so the dashboard's "Next week" filter isn't empty for Vue
# alone when run() runs somewhere the direct route works.
VUE_DAYS_AHEAD = 14

# cinemaguide.co.uk's own backend (a public Firebase function its SPA
# calls): Vue's whole forward programme in one request, for as many sites
# as it's asked about, with Vue's own booking links, times in UTC.
# Unofficial — if it ever changes shape the parse comes back empty and the
# fetch fails soft like any other venue.
CINEMAGUIDE_SCREENINGS_URL = "https://europe-west2-cinema-viewer1.cloudfunctions.net/api/getScreenings"


class VueProgramme:
    """One run's Vue listings, fetched once for every site and handed out a
    site at a time — so run() keeps each site's failure (and yesterday's
    carried-forward listing) to itself, as with any other venue, while the
    network sees one Vue session and at most one CinemaGuide request rather
    than one of each per site.

    The direct route is given up for the whole run the moment the page visit
    that opens its session is refused: that's the Cloudflare challenge, and
    it answers every site alike."""

    def __init__(self, sites: tuple[VueSite, ...] = VUE_SITES, *, now: datetime | None = None):
        self._sites = tuple(sites)
        self._now = now
        self._session = None
        self._direct_error: str | None = None
        self._cinemaguide: dict[str, list[dict]] | None = None
        self._cinemaguide_error: str | None = None

    def fetch(self, site: VueSite) -> list[dict]:
        # The direct route failing is expected on every Actions run, so it
        # isn't worth a warning of its own — only both routes failing is.
        try:
            return self._fetch_direct(site)
        except Exception as direct_exc:
            try:
                return self._fetch_cinemaguide(site)
            except CinemaFetchError as fallback_exc:
                raise CinemaFetchError(f"{direct_exc}; fallback: {fallback_exc}") from fallback_exc

    def _fetch_direct(self, site: VueSite) -> list[dict]:
        if self._direct_error is not None:
            raise CinemaFetchError(self._direct_error)
        if self._session is None:
            try:
                self._session = _open_vue_session()
            except Exception as exc:
                self._direct_error = str(exc)
                raise
        return _fetch_vue_direct(self._session, site, now=self._now)

    def _fetch_cinemaguide(self, site: VueSite) -> list[dict]:
        if self._cinemaguide is None and self._cinemaguide_error is None:
            try:
                self._cinemaguide = _fetch_vue_cinemaguide(self._sites)
            except CinemaFetchError as exc:
                self._cinemaguide_error = str(exc)
        if self._cinemaguide_error is not None:
            raise CinemaFetchError(self._cinemaguide_error)
        showings = self._cinemaguide.get(site.name)
        if not showings:
            # An empty programme for a multiplex is a broken response, not a
            # quiet week — fail so yesterday's is kept.
            raise CinemaFetchError(f"CinemaGuide returned no showings for {site.name}")
        return showings


def _open_vue_session():
    # The showings API is gated behind an anonymous-session JWT that only
    # Vue's own HTML page sets as a cookie — a cold request straight to
    # the API 401s. One throwaway page visit first (same curl_cffi
    # session, so the cookie carries over) unlocks the real API calls.
    session = curl_requests.Session()
    page = session.get(VUE_WHATS_ON_URL, impersonate="chrome124", timeout=15)
    if not page.ok:
        # A 403 here is the Cloudflare challenge; every API call after it
        # would fail the same way, so don't make any.
        raise CinemaFetchError(f"Vue what's-on page request failed (HTTP {page.status_code})")
    return session


def _fetch_vue_direct(session, site: VueSite, *, days_ahead: int = VUE_DAYS_AHEAD,
                      now: datetime | None = None) -> list[dict]:
    now = now or london_now()
    showings: list[dict] = []
    for offset in range(days_ahead):
        day = now.date() + timedelta(days=offset)
        response = session.get(VUE_API_URL.format(cinema_id=site.vue_id), params={
            "showingDate": f"{day.isoformat()}T00:00:00",
            "minEmbargoLevel": 3, "includesSession": "true", "includeSessionAttributes": "true",
        }, impersonate="chrome124", timeout=15)
        if not response.ok:
            raise CinemaFetchError(f"Vue showings request failed for {site.name} on {day.isoformat()} "
                                   f"(HTTP {response.status_code})")
        showings.extend(_parse_vue(response.json(), site.name))
    return showings


def _parse_vue(data: dict, cinema: str) -> list[dict]:
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
                    "cinema": cinema, "title": title, "year": year,
                    "showtime": start_time, "duration_minutes": film.get("runningTime"),
                    "director": film.get("director") or None,
                    "synopsis": film.get("synopsisShort") or None,
                    "poster_url": film.get("posterImageSrc"), "booking_url": booking_url,
                })
    return showings


def _fetch_vue_cinemaguide(sites: tuple[VueSite, ...] = VUE_SITES) -> dict[str, list[dict]]:
    try:
        response = requests.post(CINEMAGUIDE_SCREENINGS_URL, headers=_HEADERS, timeout=30, json={
            "venues": [site.cinemaguide_slug for site in sites], "page": 0, "initial_view": False,
        })
    except requests.RequestException as exc:
        raise CinemaFetchError(f"CinemaGuide request for Vue failed ({exc})") from exc
    if not response.ok:
        raise CinemaFetchError(f"CinemaGuide request for Vue failed (HTTP {response.status_code})")
    by_cinema: dict[str, list[dict]] = {}
    for showing in _parse_cinemaguide(response.json(), sites):
        by_cinema.setdefault(showing["cinema"], []).append(showing)
    if not by_cinema:
        raise CinemaFetchError("CinemaGuide returned no Vue showings")
    return by_cinema


def _cinemaguide_venue_slug(venue_name: str) -> str:
    # "Vue London - Westfield (Shepherd's Bush)" -> "vue-london-westfield-shepherds-bush"
    name = venue_name.lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-")


def _parse_cinemaguide(data: dict, sites: tuple[VueSite, ...] = VUE_SITES) -> list[dict]:
    cinema_by_slug = {site.cinemaguide_slug: site.name for site in sites}
    meta_by_key = data.get("film_meta_data_map") or {}
    films = (data.get("all_screenings_on_all_dates") or {}).get("film_data") or []
    showings: list[dict] = []
    for film in films:
        meta = meta_by_key.get(film.get("title")) or {}
        title = meta.get("display_film_title")
        if not title:
            continue
        # 0 is what CinemaGuide says when it doesn't know the runtime.
        duration_minutes = meta.get("length_in_minutes") or None
        for day in film.get("screenings_data") or []:
            for screening in day.get("screenings") or []:
                # One response covers every site asked about; each screening
                # says which it's at. A venue it wasn't asked about is dropped
                # rather than misfiled.
                cinema = cinema_by_slug.get(_cinemaguide_venue_slug(screening.get("venue_name") or ""))
                if cinema is None:
                    continue
                showtime = _utc_iso_to_london(screening.get("time"))
                if showtime is None:
                    continue
                booking_url = screening.get("link") or None
                if booking_url:
                    booking_url = booking_url.replace("myvue.com//", "myvue.com/", 1)
                showings.append({
                    "cinema": cinema, "title": title, "year": None,
                    "showtime": showtime, "duration_minutes": duration_minutes,
                    "director": None, "synopsis": meta.get("description") or None,
                    "poster_url": meta.get("image_link") or None, "booking_url": booking_url,
                })
    return showings


def _utc_iso_to_london(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(LONDON).replace(tzinfo=None).isoformat()


# ---------- BFI Southbank, BFI IMAX, The Gate (via Clusterflick) ----------
# whatson.bfi.org.uk, where both venues' programmes live, answers every
# datacenter IP with the same Cloudflare challenge as Vue, and CinemaGuide
# doesn't carry the IMAX at all. Clusterflick (clusterflick.com), an
# open-source London listings aggregator, publishes each venue's programme
# as a JSON file in a daily GitHub release under CC BY 4.0, which asks for
# the credit line the Cinemas tab carries. Its `themoviedb`/`themoviedbs`
# objects are TMDB's metadata and excluded from that grant, so all that's
# kept of them is the id — as a join key, which the licence suggests, and
# which spares resolve_listing_to_letterboxd a title search.
#
# Clusterflick covers 400+ London venues in this one schema, so any of them
# is a line in CLUSTERFLICK_VENUES (its id is the venue's file name in the
# release). The Gate is a Picturehouse; picturehouses.com isn't blocked, but
# one more parser to maintain would buy nothing over this one.

CLUSTERFLICK_URL = "https://github.com/clusterflick/data-transformed/releases/latest/download/{venue_id}"
CLUSTERFLICK_VENUES = {
    CINEMA_BFI_SOUTHBANK: "bfi.org.uk-southbank",
    CINEMA_BFI_IMAX: "bfi.org.uk-imax",
    CINEMA_GATE: "picturehouses.com-the-gate",
}
# Talks, workshops and quizzes share the programme but aren't screenings.
_CLUSTERFLICK_SKIPPED_CATEGORIES = {"talk", "workshop", "quiz"}


def fetch_clusterflick(cinema: str) -> list[dict]:
    showings = _parse_clusterflick(
        _get_json(CLUSTERFLICK_URL.format(venue_id=CLUSTERFLICK_VENUES[cinema])), cinema)
    if not showings:
        # A whole programme with nothing on is a broken release, not a
        # quiet month — fail so yesterday's is kept.
        raise CinemaFetchError(f"Clusterflick returned no showings for {cinema}")
    return showings


def _parse_clusterflick(data: list[dict], cinema: str) -> list[dict]:
    showings: list[dict] = []
    for event in data:
        title = event.get("title")
        if not title or event.get("category") in _CLUSTERFLICK_SKIPPED_CATEGORIES:
            continue
        overview = event.get("overview") or {}
        year = str(overview.get("year") or "")
        duration_ms = overview.get("duration")
        # A double bill lists several films under `themoviedbs`, and has no
        # one id to resolve to.
        tmdb_id = (event.get("themoviedb") or {}).get("id")
        for performance in event.get("performances") or []:
            timestamp = performance.get("time")
            if not isinstance(timestamp, (int, float)):
                continue
            showtime = datetime.fromtimestamp(timestamp / 1000, LONDON).replace(tzinfo=None, microsecond=0)
            showings.append({
                "cinema": cinema, "title": title, "year": int(year) if year.isdigit() else None,
                "showtime": showtime.isoformat(),
                "duration_minutes": round(duration_ms / 60000) if duration_ms else None,
                "director": ", ".join(overview.get("directors") or []) or None,
                "synopsis": None, "poster_url": None,
                "booking_url": performance.get("bookingUrl") or event.get("url"),
                "tmdb_id": tmdb_id,
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
                    # In London time, not the runner's: Actions runs in UTC,
                    # which put every showing an hour early all summer.
                    showtime = datetime.fromtimestamp(int(timestamp), LONDON).replace(tzinfo=None)
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

# What a cinema adds to a title that the film itself doesn't have. Repertory
# programming is mostly re-releases, so these are the norm rather than the
# exception: "La La Land (10th Anniversary)", "Alien (Theatrical Cut)",
# "The Hunger Games (2012)". Left in, each one is a title no film has.
_TRAILING_PAREN_RE = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]\s*$")
_YEAR_IN_PARENS_RE = re.compile(r"^(19|20)\d{2}$")
# Suffixes venues append without brackets, after a dash or colon. The
# ordinal is optional and separate because "- 25th Anniversary" and
# "(10th Anniversary)" are both common and only the bracketed form was
# being caught.
_FORMAT_SUFFIX_RE = re.compile(
    r"\s*[-–—:]\s*(?:in\s+)?"
    r"(?:imax(?:\s+70mm)?|70mm|35mm|4k(?:\s+restoration)?|remastered|"
    r"(?:the\s+)?final\s+cut|director['’]?s\s+cut|extended\s+cut|sing[- ]?along|the\s+imax\s+experience|"
    r"live\s+score|q\s*&\s*a|double\s+bill|re[- ]?release|"
    r"(?:\d+(?:st|nd|rd|th)\s+)?anniversary(?:\s+(?:screening|edition|re[- ]?release))?|"
    r"subtitled|restored(?:\s+(?:and|&)\s+uncut)?|uncut|the\s+musical\s+experience)\s*$",
    re.IGNORECASE,
)
# What a venue bolts onto a screening that isn't part of the film: a strand
# name in front ("Kids' Club: Hoppers") or an event behind it ("Sense and
# Sensibility + Recorded Q&A"). Both are listed as separate programmes by
# the same cinema showing the same film plainly, so leaving them on splits
# one film into two rows and matches neither.
_PROGRAMME_PREFIX_RE = re.compile(
    r"^(?:relaxed\s+screening|kids'?\s+club|family\s+film\s+club|"
    r"parent\s*(?:&|and)\s*baby(?:\s+screening)?|preschool\s+pics|"
    r"toddler\s+time|senior\s+screening|autism[- ]friendly(?:\s+screening)?|"
    r"members'?\s+screening|classic\s+matinee|midnight\s+movies?)\s*:\s*",
    re.IGNORECASE,
)
# "+ Q&A", "+ Short Film", "+ Recorded Q&A with George Mackay". A literal
# " + " almost never appears in a film's own title, and where the strip is
# wrong the title-agreement check in resolve_listing_to_letterboxd rejects
# the match rather than accepting a wrong one.
_APPENDED_EVENT_RE = re.compile(r"\s+\+\s+.*$")
# Vue's re-releases: "Avengers: Endgame Encore". Only ever trailing, and
# never the whole title — there are films called Encore.
_ENCORE_SUFFIX_RE = re.compile(r"(?<=\S)\s+encore$", re.IGNORECASE)
# A bracketed qualifier that is part of the film's real title, not the
# venue's annotation — stripping these would match the wrong film, or none.
_KEEP_PAREN_RE = re.compile(r"^(19|20)\d{2}\s+film$|^tv$|^uk$|^us$", re.IGNORECASE)


def clean_listing_title(title: str) -> tuple[str, int | None]:
    """A cinema listing's title with the venue's own annotations removed,
    plus any release year those annotations gave away.

    Cinema sites decorate titles in ways no film database does — the year
    of a re-release, which anniversary it is, which cut is being shown.
    Matching on the raw string means a repertory programme (which is most
    of what the Prince Charles shows) almost never matches anything.
    """
    cleaned = _PROGRAMME_PREFIX_RE.sub("", title.strip()).strip()
    stripped_event = _APPENDED_EVENT_RE.sub("", cleaned).strip()
    if stripped_event:
        cleaned = stripped_event
    year: int | None = None

    # Repeatedly, because "Alien (Theatrical Cut) (1979)" happens.
    while True:
        match = _TRAILING_PAREN_RE.search(cleaned)
        if not match:
            break
        inner = match.group(1).strip()
        if _KEEP_PAREN_RE.match(inner):
            break
        if _YEAR_IN_PARENS_RE.match(inner):
            # The one annotation that's worth keeping — as a year, not a title.
            year = year or int(inner)
        stripped = cleaned[: match.start()].strip()
        if not stripped:
            break   # the whole title was bracketed; leave it alone
        cleaned = stripped

    while True:
        stripped = _ENCORE_SUFFIX_RE.sub("", _FORMAT_SUFFIX_RE.sub("", cleaned)).strip()
        if stripped == cleaned or not stripped:
            break
        cleaned = stripped

    return cleaned, year


@lru_cache(maxsize=20000)
def _fold(text: str) -> str:
    # Accents folded, because a venue types "Amelie" where TMDB holds
    # "Amélie" — and the title-agreement check that keeps a concert film
    # from matching a real one would otherwise reject the right answer too.
    decomposed = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # "Songbirds and Snakes" at the cinema, "Songbirds & Snakes" on TMDB.
    folded = folded.replace("&", " and ")
    # "Sixty-Year Mission" and "Sixty Year Mission" are the same title.
    folded = _DASH_RE.sub(" ", folded)
    return re.sub(r"\s+", " ", _PUNCTUATION_RE.sub("", folded.lower())).strip()


_DASH_RE = re.compile(r"[-‐‑‒–—]")


@lru_cache(maxsize=20000)
def _normalize_title(title: str) -> str:
    return _LEADING_ARTICLE_RE.sub("", _fold(title)).strip()


# ---------- Telling films apart ----------
# A title alone isn't an identity. "Sense and Sensibility" is Ang Lee's
# 1995 film and a 2026 one; "Halloween" is four films. So a listing is
# matched on everything it says about itself — its title and the forms that
# title takes under the venue's decoration, plus whichever of year, director
# and runtime its source gives — against everything TMDB says about each
# candidate.

# Loosening a title, for when the strict clean finds no film. Venues decorate
# far more inventively than clean_listing_title can safely undo ("MUBI FEST:
# MINOTAUR", "Funeral Parade presents ...", "Ken Russell's The Devils",
# "SWEET BABY CHARLIE aka THE SADIST", "Thunder Road (1958) on 35mm"), and
# each of these can also eat part of a real title — "Dune: Part Three",
# "Schindler's List" — which is why only the strict form is accepted on title
# alone, and every looser one needs something else to agree (_corroborate).
_LOOSE_SUFFIX_RES = (
    re.compile(r"\s+(?:presented|introduced|hosted)\s+(?:by|in)\s+.+$", re.IGNORECASE),
    re.compile(r"\s+on\s+(?:8|16|35|70)\s*mm$", re.IGNORECASE),
    re.compile(r"\s+(?:plus|with)\s+.+$", re.IGNORECASE),
    re.compile(r"\s*[-–—]\s*[^-–—]*\b(?:premiere|festival|liff|the\s+play|screening)\b[^-–—]*$",
               re.IGNORECASE),
    re.compile(r"\s*\([^)]*$"),     # an unclosed bracket: "Casino Royale (20th Anniversary"
    re.compile(r"\s+(?:film\s+)?screening$|\s+book\s+launch$", re.IGNORECASE),
)
_PRESENTS_PREFIX_RE = re.compile(r"^.+?\s+presents?\s*(?::|\.{2,}|…|-|–)?\s+", re.IGNORECASE)
_LEADING_BRACKET_RE = re.compile(r"^\s*[(\[][^)\]]*[)\]]\s*")     # "(4DX Rewind) Shrek"
# "Ken Russell's The Devils", "Alain Gomis' DAO" — up to three capitalised
# words, then a possessive.
_POSSESSIVE_RE = re.compile(r"^(?:[A-Z][\w.\-]*\s+){0,2}[A-Z][\w.\-]*['’]s?\s+")
_SEPARATOR_RE = re.compile(r"\s*:\s*(?=\S)|\s+[-–—]\s+")
_AKA_RE = re.compile(r"\s+a\.?k\.?a\.?\s+", re.IGNORECASE)
MAX_TITLE_VARIANTS = 8


@lru_cache(maxsize=5000)
def listing_title_variants(title: str) -> tuple[tuple[str, ...], int | None]:
    """The titles a listing might be showing, strictest first: the
    clean_listing_title form, then ever looser ones with a strand name,
    host, format or alternative title taken off. Plus any year found on
    the way ("Thunder Road (1958) on 35mm" only gives up its year once the
    "on 35mm" is gone)."""
    base, year = clean_listing_title(title)
    variants = [base]
    queue = [base]

    def consider(text: str) -> None:
        nonlocal year
        cleaned, found_year = clean_listing_title(text)
        cleaned = cleaned.strip(" \"'“”‘’.,")
        year = year or found_year
        if (cleaned and _normalize_title(cleaned) and cleaned not in variants
                and len(variants) < MAX_TITLE_VARIANTS):
            variants.append(cleaned)
            queue.append(cleaned)

    while queue and len(variants) < MAX_TITLE_VARIANTS:
        form = queue.pop(0)
        for suffix in _LOOSE_SUFFIX_RES:
            consider(suffix.sub("", form))
        consider(_LEADING_BRACKET_RE.sub("", form))
        consider(_PRESENTS_PREFIX_RE.sub("", form))
        for part in _AKA_RE.split(form):
            consider(part)
        for separator in _SEPARATOR_RE.finditer(form):
            consider(form[separator.end():])
        consider(_POSSESSIVE_RE.sub("", form, count=1))
    return tuple(variants), year


def _director_surnames(directors) -> frozenset[str]:
    """"Alejandro G. Iñárritu, Someone Else" or a list of names -> the
    folded surnames, the part of a name venues and TMDB agree on."""
    if not directors:
        return frozenset()
    names = directors if isinstance(directors, (list, tuple)) else re.split(r",|/|\s&\s|\sand\s", directors)
    return frozenset(_fold(name).split()[-1] for name in names if _fold(name))


# How far ahead a listing with no year of its own still reads as a new
# release — the films a multiplex shows without saying when they're from.
RECENT_RELEASE_DAYS = 400


def _corroborate(*, listing_year: int | None, listing_directors: frozenset[str],
                 listing_runtime: int | None, film_year: int | None, film_directors: frozenset[str],
                 film_runtime: int | None, film_release: str | None = None,
                 today: date | None = None) -> tuple[bool, float]:
    """Whether a candidate film can be what the listing is showing, and how
    much the listing's own facts back it up. A contradiction rules it out:
    a different director, a film newer than the listing's year, a film
    longer than the slot (or a short in a feature's slot). Agreement adds
    support. A film older than the listing's year is neither — that year is
    often the re-release's, not the film's."""
    support = 0.0
    if listing_directors and film_directors:
        if not listing_directors & film_directors:
            return False, 0.0
        support += 2
    if listing_year and film_year:
        if film_year > listing_year + 1:
            return False, 0.0
        if abs(film_year - listing_year) <= 1:
            support += 1.5
    if listing_runtime and film_runtime:
        if listing_runtime < film_runtime - 20 or listing_runtime > film_runtime + 60:
            return False, 0.0
        if abs(listing_runtime - film_runtime) <= 5:
            support += 1
    if listing_year is None and film_release and today is not None:
        try:
            released = date.fromisoformat(film_release[:10])
        except ValueError:
            released = None
        if released and -120 <= (today - released).days <= RECENT_RELEASE_DAYS:
            support += 1
    return True, support


def _titles_agree(variant: str, titles) -> bool:
    target = _normalize_title(variant)
    return any(t and _normalize_title(t) == target for t in titles)


def _release_year(movie: dict) -> int | None:
    stamp = str(movie.get("release_date") or "")[:4]
    return int(stamp) if stamp.isdigit() else None


# Bumped whenever the matching rules above change: every cached match —
# positive or "not a film" — made under older rules is re-checked, since a
# fix to them would otherwise never reach the listings it was written for.
MATCHER_VERSION = 4


def listing_match_key(title: str, year: int | None) -> str:
    """Cache key for one cinema listing's identity, so the same film showing
    at three venues (and again tomorrow) is resolved once, not every run."""
    cleaned, title_year = clean_listing_title(title)
    return f"{_normalize_title(cleaned)}|{title_year or year or ''}"


def showing_match_key(showing: dict) -> str:
    """listing_match_key for a stored showing — except that one already
    matched to TMDB (Clusterflick's matching, see attach_clusterflick_ids)
    is keyed by that id, which is a firmer identity than any title, and
    resolves without a search."""
    if showing.get("tmdb_id"):
        return f"tmdb:{showing['tmdb_id']}"
    return listing_match_key(showing["title"], showing["year"])


def _best_candidate(variant: str, *, strict: bool, year: int | None, directors: frozenset[str],
                    runtime: int | None, search_movies, facts, today: date) -> dict | None:
    results = search_movies(variant, year) or []
    agreeing = [m for m in results if _titles_agree(variant, (m.get("title"), m.get("original_title")))]
    # A repertory listing's year is often the screening's, not the film's, so
    # a year-qualified miss is retried without it rather than given up on.
    if not agreeing and year is not None:
        results = search_movies(variant, None) or []
        agreeing = [m for m in results if _titles_agree(variant, (m.get("title"), m.get("original_title")))]
    # The title the venue uses may be one TMDB only lists as an alternative:
    # "Mulholland Dr." for Mulholland Drive, "Seven" for Se7en.
    if not agreeing and strict:
        for movie in results[:3]:
            known = facts(movie["id"])
            if known and _titles_agree(variant, known["titles"]):
                agreeing.append(movie)

    best: tuple[float, dict] | None = None
    for rank, movie in enumerate(agreeing[:4]):
        known = facts(movie["id"]) or {}
        film_year = known.get("year") or _release_year(movie)
        consistent, support = _corroborate(
            listing_year=year, listing_directors=directors, listing_runtime=runtime,
            film_year=film_year, film_directors=_director_surnames(known.get("directors")),
            film_runtime=known.get("runtime"),
            film_release=known.get("release_date") or movie.get("release_date"), today=today)
        if not consistent or (not strict and support < 1):
            continue
        # TMDB's own order is the tie-break: its first answer is usually the
        # better-known film.
        score = support - 0.1 * rank
        if best is None or score > best[0]:
            best = (score, {"id": movie["id"], "title": known.get("title") or movie.get("title") or variant,
                            "year": film_year})
    return best[1] if best else None


def resolve_listing_to_letterboxd(
    title: str, year: int | None, *, search_movies, film_details_by_tmdb_id, movie_facts=None,
    tmdb_id: int | None = None, director: str | None = None, duration_minutes: int | None = None,
    today: date | None = None,
) -> dict | None:
    """The Letterboxd film a cinema listing is showing, or None.

    match_watchlist_film can only ever answer for films already tracked,
    which is a small fraction of what's on — the rest showed as bare titles
    with no rating, no poster of ours and nowhere to click through to. This
    resolves any listing the same way discovery does: TMDB for the id,
    then Letterboxd's /tmdb/<id>/ redirect for the slug and details.

    TMDB is asked about each of listing_title_variants in turn, and every
    candidate whose title (or original or alternative title) agrees is
    weighed against the listing's year, director and runtime
    (_corroborate) — so a 2026 Sense and Sensibility doesn't land on 1995's
    just because TMDB lists that first. A listing that arrives with its TMDB
    id (see showing_match_key) skips the search, unless its director says
    the id is wrong.

    The network calls are injected so the matching logic around them —
    which is where this can go wrong — is testable without any service.
    Returns None for the listings that genuinely aren't films: an André Rieu
    concert or a Bing birthday screening has no Letterboxd entry, and
    guessing one would be worse than leaving the listing plain.
    """
    today = today or london_now().date()
    variants, title_year = listing_title_variants(title)
    # A year in the title ("The Hunger Games (2012)") beats the listing's own.
    year = title_year or year
    directors = _director_surnames(director)
    known_facts: dict[int, dict | None] = {}

    def facts(movie_id: int) -> dict | None:
        if movie_facts is None:
            return None
        if movie_id not in known_facts:
            known_facts[movie_id] = movie_facts(movie_id)
        return known_facts[movie_id]

    chosen = None
    if tmdb_id is not None:
        known = facts(tmdb_id) or {}
        film_directors = _director_surnames(known.get("directors"))
        if not (directors and film_directors and not directors & film_directors):
            chosen = {"id": tmdb_id, "title": known.get("title") or variants[0],
                      "year": known.get("year") or year}
    if chosen is None:
        for index, variant in enumerate(variants):
            chosen = _best_candidate(variant, strict=index == 0, year=year, directors=directors,
                                     runtime=duration_minutes, search_movies=search_movies,
                                     facts=facts, today=today)
            if chosen:
                break
    if chosen is None:
        return None

    details = film_details_by_tmdb_id(chosen["id"])
    if details is None or not details.get("slug"):
        return None

    return {
        "slug": details["slug"],
        "tmdb_id": chosen["id"],
        "title": chosen["title"],
        "year": chosen["year"],
        "rating": details.get("rating"),
        "poster_url": details.get("poster_url"),
        "director": ", ".join(details["director"]) if details.get("director") else None,
        "starring": details.get("starring") or [],
        "synopsis": details.get("synopsis"),
        "genre": details.get("genre") or [],
        "runtime_minutes": details.get("runtime_minutes"),
        "matcher_version": MATCHER_VERSION,
    }


def match_watchlist_film(title: str, year: int | None, films: dict[str, FilmState], *,
                         director: str | None = None, duration_minutes: int | None = None) -> str | None:
    """Matches a cinema listing against the watchlist by normalized title —
    cinema sites vary in punctuation/article-stripping and rarely give a
    reliable year at all, so this can't be the exact-slug lookup Letterboxd
    matching gets to use. Held to the same evidence as
    resolve_listing_to_letterboxd: a watchlist film the listing's year,
    director or runtime contradicts isn't a match (a 2026 remake isn't the
    1995 film on the list), and a looser form of the title only counts with
    one of them agreeing."""
    variants, title_year = listing_title_variants(title)
    year = title_year or year
    directors = _director_surnames(director)
    by_title: dict[str, list[tuple[str, FilmState]]] = {}
    for slug, film in films.items():
        by_title.setdefault(_normalize_title(film.title), []).append((slug, film))

    for index, variant in enumerate(variants):
        best: tuple[float, str] | None = None
        for slug, film in by_title.get(_normalize_title(variant), []):
            consistent, support = _corroborate(
                listing_year=year, listing_directors=directors, listing_runtime=duration_minutes,
                film_year=film.year, film_directors=_director_surnames(film.director),
                film_runtime=film.runtime_minutes)
            if not consistent or (index > 0 and support < 1):
                continue
            if best is None or support > best[0]:
                best = (support, slug)
        if best:
            return best[1]
    return None


# ---------- Clusterflick's matching, for the venues scraped here ----------
# Clusterflick (see the BFI section) matches every screening at 400+ London
# venues to its TMDB film — its own pipeline, with its own title cleaning —
# including the venues this module scrapes itself. Its answer for the same
# screening (same venue, same minute) is an independent second opinion, and
# where it has one, it's what the listing is keyed and resolved by. Its
# matching is part of what its licence covers, so the Cinemas tab's credit
# line names it for every venue.
CLUSTERFLICK_REFERENCE = {
    CINEMA_PRINCE_CHARLES: "princecharlescinema.com",
    CINEMA_BARBICAN: "barbican.org.uk",
    CINEMA_RIVERSIDE: "riversidestudios.co.uk",
    CINEMA_VUE_FULHAM: "myvue.com-fulham-broadway",
    CINEMA_VUE_SHEPHERDS_BUSH: "myvue.com-westfield",
    CINEMA_VUE_WEST_END: "myvue.com-leicester-square",
    CINEMA_VUE_PICCADILLY: "myvue.com-piccadilly",
}


def fetch_clusterflick_screenings(cinema: str) -> dict[str, list[tuple[str, int | None]]]:
    """Start minute ("2026-10-03T17:40") -> [(title, TMDB id)] of every
    screening Clusterflick has for one venue."""
    venue_id = CLUSTERFLICK_VENUES.get(cinema) or CLUSTERFLICK_REFERENCE[cinema]
    return _clusterflick_screenings(_get_json(CLUSTERFLICK_URL.format(venue_id=venue_id)))


def _clusterflick_screenings(data: list[dict]) -> dict[str, list[tuple[str, int | None]]]:
    screenings: dict[str, list[tuple[str, int | None]]] = {}
    for event in data:
        tmdb_id = (event.get("themoviedb") or {}).get("id")
        for performance in event.get("performances") or []:
            timestamp = performance.get("time")
            if not isinstance(timestamp, (int, float)):
                continue
            minute = datetime.fromtimestamp(timestamp / 1000, LONDON).strftime("%Y-%m-%dT%H:%M")
            screenings.setdefault(minute, []).append((event.get("title") or "", tmdb_id))
    return screenings


def attach_clusterflick_ids(showings: list[dict],
                            screenings_by_cinema: dict[str, dict[str, list[tuple[str, int | None]]]]) -> int:
    """Gives each showing without a TMDB id the one Clusterflick matched the
    same screening to. Same venue and minute isn't quite enough at a
    multiplex, where three films can start at 18:00 — so the titles have to
    agree too (any form of either, see listing_title_variants). Returns how
    many showings it identified."""
    attached = 0
    for showing in showings:
        if showing.get("tmdb_id"):
            continue
        at_minute = screenings_by_cinema.get(showing["cinema"], {}).get(showing["showtime"][:16]) or []
        ours = {_normalize_title(v) for v in listing_title_variants(showing["title"])[0]}
        same = {tmdb_id for title, tmdb_id in at_minute
                if tmdb_id and ours & {_normalize_title(v) for v in listing_title_variants(title)[0]}}
        if len(same) == 1:
            showing["tmdb_id"] = same.pop()
            attached += 1
    return attached
