from collections import defaultdict

from .availability import bucket_offers
from .brands import canonical_brand_name, is_junk_brand
from .config import CountryConfig, classify_offer, is_have_anywhere, normalize_service_name
from .countries import country_name
from .models import FilmState
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
            brand = canonical_brand_name(offer.package_clear_name)
            if is_junk_brand(brand):
                continue
            brands_here[brand].add(offer.country)

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
            brand = canonical_brand_name(offer.package_clear_name)
            if is_junk_brand(brand):
                continue
            brands_here[brand].add(offer.country)

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


def films_not_on_favorite(
    state: StateDoc, config: dict[str, CountryConfig], global_subscriptions: list[str]
) -> list[FilmState]:
    """Every watchlist film with no qualifying offer on a service you
    currently have (config/services.yaml — the primary "have" definition),
    in any country — sorted alphabetically."""

    def has_a_service(film: FilmState) -> bool:
        return any(
            is_have_anywhere(offer.package_clear_name, offer.country, config, global_subscriptions)
            for offer in film.offers
        )

    films = [film for film in state.films.values() if not has_a_service(film)]
    films.sort(key=lambda f: f.title.lower())
    return films


def films_not_on_favorite_by_country(
    state: StateDoc, config: dict[str, CountryConfig], global_subscriptions: list[str], revisitable: set[str]
) -> list[dict]:
    """The same film set as films_not_on_favorite, but organized by which
    country each film is available in (on some non-"have" service) — one
    section per country, films sorted by rating within each. A film with
    offers in several countries appears in each of those countries' sections.
    """
    films = films_not_on_favorite(state, config, global_subscriptions)

    by_country: dict[str, list[dict]] = defaultdict(list)
    for film in films:
        buckets = bucket_offers(film.offers, config, global_subscriptions, revisitable)
        # "have" is always empty here by construction of films_not_on_favorite.
        by_country_services: dict[str, list[dict]] = defaultdict(list)
        for classification in ("could_get_again", "free", "subscription"):
            for brand, country in buckets[classification]:
                by_country_services[country].append({"brand": brand, "classification": classification})

        for country, services in by_country_services.items():
            services.sort(key=lambda s: s["brand"])
            by_country[country].append({
                "title": film.title, "year": film.year, "slug": film.slug, "rating": film.rating,
                "services": services,
            })

    countries = []
    for code, country_films in by_country.items():
        country_films.sort(key=lambda f: (-(f["rating"] or 0), f["title"].lower()))
        countries.append({"code": code, "name": country_name(code), "films": country_films})
    countries.sort(key=lambda c: c["name"])
    return countries
