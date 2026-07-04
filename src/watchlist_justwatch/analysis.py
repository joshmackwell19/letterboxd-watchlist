from collections import defaultdict

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
