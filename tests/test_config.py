from watchlist_justwatch.config import (
    CountryConfig,
    classify_offer,
    is_have_anywhere,
    normalize_service_name,
    service_matches,
)
from watchlist_justwatch.models import OfferRecord


def _offer(package_clear_name: str, country: str = "AU") -> OfferRecord:
    return OfferRecord(
        country=country, monetization_type="FLATRATE", package_technical_name="x",
        package_clear_name=package_clear_name, package_id=1, url="https://example.com",
    )


def test_normalize_service_name_strips_punctuation_and_case():
    assert normalize_service_name("Amazon Prime Video") == "amazonprimevideo"
    assert normalize_service_name("Disney+") == "disneyplus"
    assert normalize_service_name("HBO Max") == "hbomax"


def test_service_matches_is_substring_both_directions():
    # config.yaml often has a short name ("Netflix") that should match
    # JustWatch's longer variant ("Netflix Standard with Ads") either way.
    assert service_matches("Netflix", "Netflix Standard with Ads")
    assert service_matches("Netflix Standard with Ads", "Netflix")


def test_service_matches_short_names_require_exact_match():
    # Guards against a hypothetical short config entry (e.g. "TV") producing
    # false-positive substring matches against unrelated long service names.
    assert not service_matches("TV", "Some TV Streaming Service")
    assert service_matches("TV", "TV")


def test_service_matches_is_case_and_punctuation_insensitive():
    assert service_matches("disney plus", "Disney+")


def test_classify_offer_have_takes_priority_over_free_tier():
    config = CountryConfig(country="AU", subscriptions=["Stan"], free_tier=["Stan"])
    assert classify_offer(_offer("Stan"), config) == "have"


def test_classify_offer_free_tier_when_not_a_subscription():
    config = CountryConfig(country="AU", subscriptions=["Stan"], free_tier=["Tubi TV"])
    assert classify_offer(_offer("Tubi TV"), config) == "free_tier"


def test_classify_offer_new_possible_when_unrecognized():
    config = CountryConfig(country="AU", subscriptions=["Stan"], free_tier=[])
    assert classify_offer(_offer("Some Other Service"), config) == "new_possible"


def test_is_have_anywhere_matches_global_subscription_regardless_of_country():
    config = {"AU": CountryConfig(country="AU", subscriptions=[], free_tier=[])}
    assert is_have_anywhere("Amazon Prime Video", "AU", config, ["Amazon Prime Video"])
    # Global subscriptions are VPN-portable — count even in a country this
    # app has no CountryConfig entry for at all.
    assert is_have_anywhere("Amazon Prime Video", "FR", config, ["Amazon Prime Video"])


def test_is_have_anywhere_matches_country_specific_subscription():
    config = {"AU": CountryConfig(country="AU", subscriptions=["Stan"], free_tier=[])}
    assert is_have_anywhere("Stan", "AU", config, [])
    # Same service name, but not tracked in this country's config at all.
    assert not is_have_anywhere("Stan", "GB", config, [])


def test_is_have_anywhere_false_for_untracked_country_and_service():
    assert not is_have_anywhere("Stan", "ZZ", {}, [])
