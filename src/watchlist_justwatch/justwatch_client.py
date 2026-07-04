import time

from simplejustwatchapi.justwatch import offers_for_countries, search

from .countries import ALL_JUSTWATCH_COUNTRIES, QUALIFYING_MONETIZATION_TYPES
from .models import FilmState, MatchResult, OfferRecord, WatchlistFilm

CACHEABLE_CONFIDENCE = {"exact", "year_tolerant"}


def search_film(title: str, year: int | None, *, country: str = "US", count: int = 5) -> MatchResult:
    results = search(title, country=country, language="en", count=count)

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

    top = results[0]
    return MatchResult(slug="", entry_id=top.entry_id, matched_title=top.title,
                        matched_year=top.release_year, confidence="low_confidence")


def fetch_offers(entry_id: str, *, countries: frozenset[str] = ALL_JUSTWATCH_COUNTRIES) -> list[OfferRecord]:
    offers_by_country = offers_for_countries(entry_id, set(countries), language="en")

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
    request_delay_seconds: float = 0.3,
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
