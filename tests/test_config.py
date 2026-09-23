from watchlist_justwatch.brands import canonical_brand_name
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


def test_service_matches_works_whichever_name_is_given_first():
    # config.yaml often has a short name ("Netflix") where JustWatch has a
    # longer variant ("Netflix Standard with Ads"); which of the two is the
    # config side and which the package side shouldn't change the answer.
    assert service_matches("Netflix", "Netflix Standard with Ads")
    assert service_matches("Netflix Standard with Ads", "Netflix")


def test_service_matches_rejects_a_name_that_merely_ends_with_a_service():
    # All of these were real: JustWatch carries a Polish service called
    # "Player" and a Canadian one called "TSN Standard", and squashing names
    # to a single string made them substrings of "BBC iPlayer" and "Stan" —
    # so films on them were badged as services Josh subscribes to.
    assert not service_matches("BBC iPlayer", "Player")
    assert not service_matches("Stan", "TSN Standard")
    assert not service_matches("Stan", "Netflix Standard with Ads")
    assert not service_matches("YouTube", "NFL GamePass on YouTube")


def test_service_matches_rejects_separate_products_sharing_a_prefix():
    # Extra trailing words usually mean a variant of the same service, but
    # not for these — YouTube TV and YouTube Sports are their own paid
    # products, and having YouTube doesn't get you either.
    assert not service_matches("YouTube", "YouTube TV")
    assert not service_matches("YouTube", "YouTube Sports")
    # Naming one in config still matches that exact service, so someone who
    # does subscribe can just list it.
    assert service_matches("YouTube TV", "YouTube TV")
    # Not "YouTube Premium" though: that's a tier, stripped by brands.py
    # before matching is ever reached — see the canonicalization test below.


def test_service_matches_keeps_real_variants_of_the_same_service():
    # The shape every genuine variant takes: the service's name, then extra
    # words for the tier or the bundle it's sold through.
    assert service_matches("MUBI", "MUBI Amazon Channel")
    assert service_matches("Amazon Prime Video", "Amazon Prime Video with Ads")
    assert service_matches("ITVX", "ITVX Premium")
    assert service_matches("HBO Max", "HBO Max Amazon Channel")


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


def test_tier_qualifiers_collapse_but_separate_products_do_not():
    # Classification sees canonical brand names, not raw package names (see
    # _all_offers_for_film), and the two rules interact: brands.py strips
    # tier qualifiers, so "YouTube Premium" is already "YouTube" by the time
    # matching happens, while "YouTube TV" survives as its own name and is
    # held apart here. Getting this pair backwards is easy — the audit that
    # motivated _STANDALONE_SERVICES compared raw names and reported a
    # change to YouTube Premium that the pipeline never made.
    config = {"GB": CountryConfig(country="GB", subscriptions=["YouTube"], free_tier=[])}

    assert canonical_brand_name("YouTube Premium") == "YouTube"
    assert is_have_anywhere(canonical_brand_name("YouTube Premium"), "GB", config, ["YouTube"])

    for separate in ["YouTube TV", "YouTube Sports"]:
        assert canonical_brand_name(separate) == separate
        assert not is_have_anywhere(canonical_brand_name(separate), "GB", config, ["YouTube"])


# --- Tiers and profiles of a service you already pay for -----------------

def test_a_tier_or_profile_folds_into_the_service_it_belongs_to():
    from watchlist_justwatch.brands import canonical_brand_name

    # Both reach you on a subscription you already have, so leaving them
    # separate made a film on them read as one more thing to pay for.
    assert canonical_brand_name("Netflix Kids") == "Netflix"
    assert canonical_brand_name("Channel 4 Plus") == "Channel 4"


def test_a_name_that_merely_starts_with_another_brand_is_left_alone():
    from watchlist_justwatch.brands import canonical_brand_name

    # The tempting generalisation — fold anything prefixed by another brand's
    # name — is wrong more often than right. Each of these is a separate
    # product with a separate bill.
    for name in ["YouTube TV", "AMC Plus", "MGM Plus", "Now TV Cinema",
                 "Sony Pictures Core", "Sky Go"]:
        assert canonical_brand_name(name) == name


def test_folding_a_variant_does_not_disturb_the_suffix_rules():
    from watchlist_justwatch.brands import canonical_brand_name

    assert canonical_brand_name("Netflix Standard with Ads") == "Netflix"
    assert canonical_brand_name("MUBI Amazon Channel") == "MUBI"
    assert canonical_brand_name("Disney Plus") == "Disney Plus"
