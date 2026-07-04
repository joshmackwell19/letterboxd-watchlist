from collections import defaultdict

from .brands import canonical_brand_name
from .config import CountryConfig, classify_offer, normalize_service_name
from .state import StateDoc


def rank_missing_services(state: StateDoc, config: dict[str, CountryConfig]) -> dict[str, list[tuple[str, int]]]:
    """For each configured country, rank services the user doesn't have by how
    many distinct watchlist films are available on them. Merges JustWatch
    package variants (e.g. "Amazon Prime Video" / "... with Ads") into one
    canonical service so a film isn't double-counted across variants.
    """
    per_country: dict[str, dict[str, tuple[str, set[str]]]] = defaultdict(dict)

    for slug, film in state.films.items():
        for offer in film.offers:
            country_config = config.get(offer.country)
            if country_config is None or classify_offer(offer, country_config) != "new_possible":
                continue

            norm = normalize_service_name(offer.package_clear_name)
            bucket = per_country[offer.country]
            display, slugs = bucket.get(norm, (offer.package_clear_name, set()))
            if len(offer.package_clear_name) < len(display):
                display = offer.package_clear_name
            slugs.add(slug)
            bucket[norm] = (display, slugs)

    return {
        country: sorted(((display, len(slugs)) for display, slugs in bucket.values()), key=lambda x: -x[1])
        for country, bucket in per_country.items()
    }


def render_ranking(ranking: dict[str, list[tuple[str, int]]], *, top_n: int = 10) -> str:
    lines = ["Services you don't have, ranked by watchlist coverage", ""]
    for country in sorted(ranking):
        lines.append(f"{country}:")
        for name, count in ranking[country][:top_n]:
            lines.append(f"  {count:>3}  {name}")
        lines.append("")
    return "\n".join(lines).rstrip()


def recommend_new_favorites(state: StateDoc, favorites: set[tuple[str, str]], *, min_films: int = 2) -> list[dict]:
    """Brands you have NO existing favourite for, anywhere, that would unlock
    at least `min_films` watchlist films no current favourite covers. Grouped
    by brand rather than (brand, country) — "should I add this service" is a
    per-brand decision; candidate countries are listed underneath so you know
    where to point the VPN. Deliberately excludes brands you already favourite
    in some countries (e.g. Amazon Prime Video) — extending an existing
    service to more countries is a different, lower-stakes decision than
    adding a brand-new one; see recommend_extra_countries for that.
    """
    favorited_brands = {brand for brand, _country in favorites}
    covered_by_favorites: set[str] = set()
    candidate_films: dict[str, set[str]] = defaultdict(set)
    candidate_countries: dict[str, set[str]] = defaultdict(set)

    for slug, film in state.films.items():
        brands_here: dict[str, set[str]] = defaultdict(set)
        for offer in film.offers:
            brands_here[canonical_brand_name(offer.package_clear_name)].add(offer.country)

        if any((brand, country) in favorites for brand, countries in brands_here.items() for country in countries):
            covered_by_favorites.add(slug)

        for brand, countries in brands_here.items():
            if brand not in favorited_brands:
                candidate_films[brand].add(slug)
                candidate_countries[brand] |= countries

    recommendations = []
    for brand, slugs in candidate_films.items():
        unique_slugs = slugs - covered_by_favorites
        if len(unique_slugs) >= min_films:
            titles = sorted(state.films[s].title for s in unique_slugs)
            recommendations.append({
                "brand": brand,
                "countries": sorted(candidate_countries[brand]),
                "titles": titles,
            })

    recommendations.sort(key=lambda r: -len(r["titles"]))
    return recommendations


def recommend_extra_countries(state: StateDoc, favorites: set[tuple[str, str]], *, min_films: int = 2) -> list[dict]:
    """For brands you ALREADY favourite somewhere, additional countries that
    would unlock films your current favourited countries for that brand
    don't cover. A lower-stakes companion to recommend_new_favorites: you
    already have the service, this just flags under-used regional catalogs.
    """
    favorited_brands = {brand for brand, _country in favorites}
    covered_by_favorites: set[str] = set()
    candidate_films: dict[str, set[str]] = defaultdict(set)
    candidate_countries: dict[str, set[str]] = defaultdict(set)

    for slug, film in state.films.items():
        brands_here: dict[str, set[str]] = defaultdict(set)
        for offer in film.offers:
            brands_here[canonical_brand_name(offer.package_clear_name)].add(offer.country)

        if any((brand, country) in favorites for brand, countries in brands_here.items() for country in countries):
            covered_by_favorites.add(slug)

        for brand, countries in brands_here.items():
            if brand in favorited_brands:
                missing = {c for c in countries if (brand, c) not in favorites}
                if missing:
                    candidate_films[brand].add(slug)
                    candidate_countries[brand] |= missing

    recommendations = []
    for brand, slugs in candidate_films.items():
        unique_slugs = slugs - covered_by_favorites
        if len(unique_slugs) >= min_films:
            titles = sorted(state.films[s].title for s in unique_slugs)
            recommendations.append({
                "brand": brand,
                "countries": sorted(candidate_countries[brand]),
                "titles": titles,
            })

    recommendations.sort(key=lambda r: -len(r["titles"]))
    return recommendations


def render_favorite_recommendations(recommendations: list[dict], *, max_titles: int = 5, max_countries: int = 8) -> str:
    if not recommendations:
        return "None found."

    lines = []
    for rec in recommendations:
        countries = rec["countries"]
        country_str = ", ".join(countries[:max_countries])
        if len(countries) > max_countries:
            country_str += f" (+{len(countries) - max_countries} more)"

        titles = rec["titles"]
        title_str = ", ".join(titles[:max_titles])
        if len(titles) > max_titles:
            title_str += f" (+{len(titles) - max_titles} more)"

        lines.append(f"  {len(titles):>3}  {rec['brand']} — available in: {country_str}")
        lines.append(f"       unlocks: {title_str}")
    return "\n".join(lines)
