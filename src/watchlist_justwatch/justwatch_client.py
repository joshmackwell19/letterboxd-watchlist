import time

from simplejustwatchapi.exceptions import JustWatchHttpError
from simplejustwatchapi.justwatch import offers_for_countries, search

from .countries import ALL_JUSTWATCH_COUNTRIES, QUALIFYING_MONETIZATION_TYPES
from .models import FilmState, MatchResult, OfferRecord, WatchlistFilm

CACHEABLE_CONFIDENCE = {"exact", "year_tolerant"}


def _with_retry(fn, *, max_retries: int = 5, backoff_base_seconds: float = 2.0):
    # simplejustwatchapi wraps every underlying httpx failure — rate limits,
    # but also plain timeouts and connection resets, which are far more
    # common — into this same JustWatchHttpError class, so retrying only on
    # "429" left the more frequent transient failures with zero retries
    # (unlike letterboxd.py's _fetch_url, which retries any exception).
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except JustWatchHttpError:
            if attempt == max_retries:
                raise
            time.sleep(backoff_base_seconds * (2 ** attempt))


def search_film(title: str, year: int | None, *, country: str = "US", count: int = 20) -> MatchResult:
    # A too-narrow count silently breaks common titles: "Obsession" (1976,
    # Brian De Palma) fell out of the top 5 the moment JustWatch indexed an
    # unrelated newer title with the same name, so the year-match loops below
    # never saw it and fell all the way back to an arbitrary top result.
    # 20 gives real headroom for title collisions without meaningfully
    # slower requests (JustWatch's response time is dominated by round-trip,
    # not result count).
    results = _with_retry(lambda: search(title, country=country, language="en", count=count))

    if not results:
        return MatchResult(slug="", entry_id=None, matched_title=None, matched_year=None, confidence="unmatched")

    if year is not None:
        for r in results:
            if r.release_year == year:
                return MatchResult(slug="", entry_id=r.entry_id, matched_title=r.title,
                                    matched_year=r.release_year, confidence="exact")
        for r in results:
            if r.release_year is not None and abs(r.release_year - year) <= 1:
                return MatchResult(slug="", entry_id=r.entry_id, matched_title=r.title,
                                    matched_year=r.release_year, confidence="year_tolerant")

        # Still nothing close — prefer whichever result's year is nearest
        # the one we're looking for, rather than trusting JustWatch's own
        # result ordering (which favours new/popular titles, not the
        # specific year of an ambiguous, commonly-reused title like this).
        with_year = [r for r in results if r.release_year is not None]
        if with_year:
            closest = min(with_year, key=lambda r: abs(r.release_year - year))
            return MatchResult(slug="", entry_id=closest.entry_id, matched_title=closest.title,
                                matched_year=closest.release_year, confidence="low_confidence")

    top = results[0]
    return MatchResult(slug="", entry_id=top.entry_id, matched_title=top.title,
                        matched_year=top.release_year, confidence="low_confidence")


def fetch_offers(entry_id: str, *, countries: frozenset[str] = ALL_JUSTWATCH_COUNTRIES) -> list[OfferRecord]:
    offers_by_country = _with_retry(lambda: offers_for_countries(entry_id, set(countries), language="en"))

    seen: set[tuple[str, str, str]] = set()
    records: list[OfferRecord] = []
    for country, offers in offers_by_country.items():
        for offer in offers:
            if offer.monetization_type not in QUALIFYING_MONETIZATION_TYPES:
                continue
            key = (country, offer.package.technical_name, offer.monetization_type)
            if key in seen:
                continue
            seen.add(key)
            records.append(OfferRecord(
                country=country,
                monetization_type=offer.monetization_type,
                package_technical_name=offer.package.technical_name,
                package_clear_name=offer.package.name,
                package_id=offer.package.package_id,
                url=offer.url,
                available_to=offer.available_to,
            ))
    return records


def resolve_film(film: WatchlistFilm, cached_entry_id: str | None, cached_confidence: str | None) -> MatchResult:
    if cached_entry_id is not None and cached_confidence in CACHEABLE_CONFIDENCE:
        return MatchResult(slug=film.slug, entry_id=cached_entry_id, matched_title=film.title,
                            matched_year=film.year, confidence=cached_confidence)

    result = search_film(film.title, film.year)
    return MatchResult(slug=film.slug, entry_id=result.entry_id, matched_title=result.matched_title,
                        matched_year=result.matched_year, confidence=result.confidence)


def resolve_and_fetch(
    film: WatchlistFilm,
    cached_entry_id: str | None,
    cached_confidence: str | None,
    *,
    now_iso: str,
    request_delay_seconds: float = 0.6,
) -> FilmState:
    match = resolve_film(film, cached_entry_id, cached_confidence)

    offers: list[OfferRecord] = []
    if match.entry_id is not None:
        offers = fetch_offers(match.entry_id)

    time.sleep(request_delay_seconds)

    return FilmState(
        slug=film.slug,
        title=film.title,
        year=film.year,
        entry_id=match.entry_id,
        confidence=match.confidence,
        last_checked=now_iso,
        offers=offers,
    )
