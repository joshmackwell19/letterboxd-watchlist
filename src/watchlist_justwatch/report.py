from datetime import date

from .brands import group_offers_by_brand_and_country
from .countries import country_name
from .diff import Report, ReportEntry

# JustWatch often lists multiple near-duplicate packages for the same real
# subscription (e.g. "Amazon Prime Video" and "Amazon Prime Video with Ads").
# FLATRATE is the "plain" tier and preferred as the representative offer;
# shorter clear_name is used as a tiebreaker (more likely to be the canonical
# name rather than an ads/channel variant).
_MONETIZATION_PRIORITY = {"FLATRATE": 0, "FREE": 1, "ADS": 2}


def _dedupe_by_film_country(entries: list[ReportEntry]) -> list[ReportEntry]:
    best: dict[tuple[str, str], ReportEntry] = {}
    for entry in entries:
        key = (entry.film.slug, entry.offer.country)
        current_best = best.get(key)
        if current_best is None:
            best[key] = entry
            continue
        rank = (_MONETIZATION_PRIORITY.get(entry.offer.monetization_type, 9), len(entry.offer.package_clear_name))
        current_rank = (_MONETIZATION_PRIORITY.get(current_best.offer.monetization_type, 9),
                        len(current_best.offer.package_clear_name))
        if rank < current_rank:
            best[key] = entry
    return list(best.values())


def _line(entry: ReportEntry) -> str:
    year = f" ({entry.film.year})" if entry.film.year else ""
    return f"  • {entry.film.title}{year} — {entry.offer.package_clear_name} ({entry.offer.country})"


def _country_service_lines(entries: list[tuple[str, str]]) -> list[str]:
    """entries: list of (brand, country) -> lines grouped by country, full
    country names, sorted alphabetically by country."""
    by_country: dict[str, list[str]] = {}
    for brand, country in entries:
        by_country.setdefault(country_name(country), []).append(brand)

    return [
        f"      {country}: {', '.join(sorted(services))}"
        for country, services in sorted(by_country.items())
    ]


def _new_film_lines(film, favorites: set[tuple[str, str]]) -> list[str]:
    year = f" ({film.year})" if film.year else ""
    rating = f" — Letterboxd {film.rating:.2f}★" if film.rating is not None else ""
    lines = [f"  {film.title}{year}{rating}", f"    https://letterboxd.com/film/{film.slug}/"]

    by_brand_country = group_offers_by_brand_and_country(film.offers)
    if not by_brand_country:
        lines.append("    Not currently streaming (subscription/free) in any tracked country.")
        return lines

    already_have: list[tuple[str, str]] = []
    free_no_sub: list[tuple[str, str]] = []
    needs_subscription: list[tuple[str, str]] = []

    for brand, by_country in by_brand_country.items():
        for country, monetization_types in by_country.items():
            if (brand, country) in favorites:
                already_have.append((brand, country))
            elif "FLATRATE" in monetization_types:
                needs_subscription.append((brand, country))
            else:
                free_no_sub.append((brand, country))

    if already_have:
        lines.append("    ✅ Already available on a service you have:")
        lines.extend(_country_service_lines(already_have))
    if free_no_sub:
        lines.append("    \U0001F193 Available without a subscription (free/ad-supported):")
        lines.extend(_country_service_lines(free_no_sub))
    if needs_subscription:
        lines.append("    \U0001F4B3 Available on other subscription services:")
        lines.extend(_country_service_lines(needs_subscription))

    return lines


def render_report(report: Report, favorites: set[tuple[str, str]]) -> str | None:
    if report.is_empty():
        return None

    lines = [f"Letterboxd Watchlist — JustWatch Update ({date.today().isoformat()})", ""]

    if report.new_films:
        lines.append("\U0001F3AC New to your watchlist — full availability, all countries")
        for film in report.new_films:
            lines.extend(_new_film_lines(film, favorites))
            lines.append("")

    if report.new_have:
        lines.append("✅ Available on a service you have")
        lines.extend(_line(e) for e in _dedupe_by_film_country(report.new_have))
        lines.append("")

    if report.new_free_tier:
        lines.append("\U0001F193 Available via free-tier app")
        lines.extend(_line(e) for e in _dedupe_by_film_country(report.new_free_tier))
        lines.append("")

    if report.new_possible:
        lines.append("\U0001F195 Available elsewhere (you don't have this service)")
        lines.extend(_line(e) for e in _dedupe_by_film_country(report.new_possible))
        lines.append("")

    if report.unmatched:
        lines.append("---")
        lines.append("⚠️ Could not confidently match on JustWatch (review manually):")
        for film in report.unmatched:
            year = f" ({film.year})" if film.year else ""
            reason = "no search results" if film.confidence == "unmatched" else f"low-confidence match"
            lines.append(f"  • {film.title}{year} — {reason}")

    return "\n".join(lines).rstrip()
