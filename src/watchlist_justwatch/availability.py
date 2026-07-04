from .brands import group_offers_by_brand_and_country
from .config import CountryConfig, is_have_anywhere


def bucket_offers(
    offers,
    config: dict[str, CountryConfig],
    global_subscriptions: list[str],
    revisitable: set[str],
) -> dict[str, list[tuple[str, str]]]:
    """Classify every (brand, country) an offer list covers into one of four
    buckets, most-actionable first: on a service you actually have (the
    PRIMARY category — your real current subscriptions, config/services.yaml),
    could get again (friends/family/resubscribe), free/ad-supported, or needs
    a subscription you don't have. Shared by the text and HTML report
    renderers so the categorization logic lives in exactly one place.
    """
    buckets: dict[str, list[tuple[str, str]]] = {
        "have": [], "could_get_again": [], "free": [], "subscription": [],
    }

    by_brand_country = group_offers_by_brand_and_country(offers)
    for brand, by_country in by_brand_country.items():
        for country, monetization_types in by_country.items():
            if is_have_anywhere(brand, country, config, global_subscriptions):
                buckets["have"].append((brand, country))
            elif brand in revisitable:
                buckets["could_get_again"].append((brand, country))
            elif "FLATRATE" in monetization_types:
                buckets["subscription"].append((brand, country))
            else:
                buckets["free"].append((brand, country))

    return buckets
