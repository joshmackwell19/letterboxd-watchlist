from watchlist_justwatch.config import CountryConfig
from watchlist_justwatch.countries import ALL_JUSTWATCH_COUNTRIES
from watchlist_justwatch.dashboard import _classify, _search_taxonomy
from watchlist_justwatch.models import FilmState, OfferRecord
from watchlist_justwatch.state import StateDoc

GLOBAL_SUBSCRIPTIONS = ["Netflix", "MUBI"]
REVISITABLE = {"Now TV"}

CONFIG = {
    "GB": CountryConfig(country="GB", subscriptions=["BBC iPlayer", *GLOBAL_SUBSCRIPTIONS], free_tier=["ITVX"]),
    "AU": CountryConfig(country="AU", subscriptions=["Stan", *GLOBAL_SUBSCRIPTIONS], free_tier=[]),
}


def _offer(clear_name: str, country: str, monetization_type: str = "FLATRATE") -> OfferRecord:
    return OfferRecord(
        country=country, monetization_type=monetization_type, package_technical_name=clear_name.lower(),
        package_clear_name=clear_name, package_id=1, url=None,
    )


def _state(*offers: OfferRecord) -> StateDoc:
    film = FilmState(
        slug="a-film", title="A Film", year=2020, entry_id="e1", confidence="exact",
        last_checked="2026-09-08T00:00:00Z", offers=list(offers),
    )
    return StateDoc(films={"a-film": film})


def _taxonomy(*offers: OfferRecord) -> dict:
    return _search_taxonomy(_state(*offers), CONFIG, GLOBAL_SUBSCRIPTIONS, REVISITABLE)


def test_only_clear_names_that_canonicalize_differently_are_stored():
    taxonomy = _taxonomy(_offer("Netflix Standard with Ads", "GB"), _offer("Netflix", "GB"))

    # The ad tier needs a lookup; the plain name is what the page's own
    # fallback already produces, so storing it would bloat every page load
    # to say nothing.
    assert taxonomy["brand_by_clear_name"]["Netflix Standard with Ads"] == "Netflix"
    assert "Netflix" not in taxonomy["brand_by_clear_name"]
    assert not any(clear_name == brand for clear_name, brand in taxonomy["brand_by_clear_name"].items())


def test_global_subscription_is_have_everywhere_not_per_country():
    taxonomy = _taxonomy(_offer("Netflix", "GB"), _offer("BBC iPlayer", "GB"))

    assert "Netflix" in taxonomy["have_brands_global"]
    # Already covered globally, so repeating it under each country would just
    # be weight — the page checks the global set first.
    assert "Netflix" not in taxonomy["have_brands_by_country"]["GB"]
    assert "BBC iPlayer" in taxonomy["have_brands_by_country"]["GB"]


def test_country_subscription_does_not_leak_into_other_countries():
    taxonomy = _taxonomy(_offer("Stan", "AU"), _offer("BBC iPlayer", "GB"))

    assert "Stan" in taxonomy["have_brands_by_country"]["AU"]
    assert "Stan" not in taxonomy["have_brands_by_country"]["GB"]
    assert "BBC iPlayer" in taxonomy["have_brands_by_country"]["GB"]
    assert "BBC iPlayer" not in taxonomy["have_brands_by_country"]["AU"]


def test_free_tier_service_counts_as_have_in_its_country():
    taxonomy = _taxonomy(_offer("ITVX", "GB", "ADS"))

    assert "ITVX" in taxonomy["have_brands_by_country"]["GB"]
    assert "ITVX" not in taxonomy["have_brands_by_country"]["AU"]


def test_revisitable_and_junk_brands_are_published():
    taxonomy = _taxonomy(_offer("Netflix", "GB"))

    assert taxonomy["revisitable_brands"] == ["Now TV"]
    # Lowercased, the way is_junk_brand compares.
    assert "justwatch tv" in taxonomy["junk_brands"]


def test_country_list_is_the_one_the_worker_is_meant_to_use():
    taxonomy = _taxonomy(_offer("Netflix", "GB"))

    assert taxonomy["justwatch_countries"] == sorted(ALL_JUSTWATCH_COUNTRIES)


# --- The one that actually matters -------------------------------------
#
# The page classifies a searched film's offers from the table above instead
# of from brands.py/config.py, so the table plus that rule has to reach the
# same verdict _classify does for a watchlist film. This is the Python twin
# of the JS the page runs — if _classify's precedence changes, or a lookup
# stops being emitted, this fails here rather than quietly misbadging a
# searched film in the browser.

def _classify_from_taxonomy(taxonomy: dict, clear_name: str, country: str, monetization_types: set[str]) -> str | None:
    brand = taxonomy["brand_by_clear_name"].get(clear_name, clear_name)
    if brand.lower() in set(taxonomy["junk_brands"]):
        return None  # dropped, same as group_offers_by_brand_and_country does
    if brand in set(taxonomy["have_brands_global"]):
        return "have"
    if brand in set(taxonomy["have_brands_by_country"].get(country, [])):
        return "have"
    if brand in set(taxonomy["revisitable_brands"]):
        return "could_get_again"
    if "FLATRATE" in monetization_types:
        return "subscription"
    return "free"


def test_taxonomy_lookup_agrees_with_classify_across_every_case():
    clear_names = [
        "Netflix",                      # global subscription
        "Netflix Standard with Ads",    # ad tier of one
        "MUBI Amazon Channel",          # channel bundle of one
        "BBC iPlayer",                  # GB-only subscription
        "ITVX",                         # GB free tier
        "Stan",                         # AU-only subscription
        "Now TV",                       # revisitable
        "Disney Plus",                  # subscribed to by nobody here
        "Some Regional Service",        # never seen anywhere
    ]
    # Every name above is in the corpus except the last, which is the
    # not-in-the-corpus fallback this is meant to prove harmless.
    taxonomy = _taxonomy(*[_offer(name, "GB") for name in clear_names[:-1]])

    countries = ["GB", "AU", "US", "DE"]  # configured, and not
    monetizations = [{"FLATRATE"}, {"ADS"}, {"FREE"}, {"FLATRATE", "ADS"}]

    for clear_name in clear_names:
        for country in countries:
            for monetization_types in monetizations:
                from watchlist_justwatch.brands import canonical_brand_name, is_junk_brand

                brand = canonical_brand_name(clear_name)
                expected = None if is_junk_brand(brand) else _classify(
                    brand, country, monetization_types, CONFIG, GLOBAL_SUBSCRIPTIONS, REVISITABLE
                )
                actual = _classify_from_taxonomy(taxonomy, clear_name, country, monetization_types)

                assert actual == expected, (
                    f"{clear_name!r} in {country} with {sorted(monetization_types)}: "
                    f"taxonomy said {actual!r}, _classify said {expected!r}"
                )


def test_subscription_with_nothing_on_it_today_is_still_have():
    # The corpus is every service some watchlist film currently streams on,
    # which is not the same as every service Josh pays for — a subscription
    # with nothing watchlisted on it would drop out of the have lists, and a
    # searched film streaming there would wrongly read as one more thing to
    # pay for. The config seeds the universe so that can't happen.
    taxonomy = _taxonomy(_offer("Some Unrelated Service", "GB"))

    assert "Netflix" in taxonomy["have_brands_global"]
    assert "BBC iPlayer" in taxonomy["have_brands_by_country"]["GB"]
    assert "Stan" in taxonomy["have_brands_by_country"]["AU"]
    assert _classify_from_taxonomy(taxonomy, "BBC iPlayer", "GB", {"FLATRATE"}) == "have"


def test_variants_of_a_service_you_have_resolve_without_the_corpus():
    # The corpus supplies the variant names a service turns up under, so a
    # variant it hasn't happened to see would read as a service of its own —
    # i.e. "subscribe to this" for something already paid for, which is the
    # expensive way to be wrong. The qualifiers are known, so the variants of
    # a configured service don't have to be waited for.
    taxonomy = _taxonomy(_offer("Some Unrelated Service", "GB"))

    assert taxonomy["brand_by_clear_name"]["Netflix Standard with Ads"] == "Netflix"
    assert taxonomy["brand_by_clear_name"]["MUBI Amazon Channel"] == "MUBI"
    assert taxonomy["brand_by_clear_name"]["BBC iPlayer Premium"] == "BBC iPlayer"
    assert _classify_from_taxonomy(taxonomy, "Netflix Standard with Ads", "US", {"FLATRATE"}) == "have"
    assert _classify_from_taxonomy(taxonomy, "MUBI Amazon Channel", "DE", {"FLATRATE"}) == "have"


# --- Discovery films are classified on every build, not once and stored ---

def test_stored_discovery_classification_is_recomputed_against_todays_config():
    # similar.py classifies a discovery film's offers when it finds it and
    # stores the verdict, so a Settings change (or a fix to the matching
    # rules) never reached those cards — recommendations went on badging a
    # service as one Josh had long after nothing else did.
    from watchlist_justwatch.dashboard import _reclassified_discovery_films

    stored = {"some-film": {"slug": "some-film", "title": "Some Film", "all_offers": [
        # Stale: "have" was right under the old rules, wrong under today's.
        {"brand": "YouTube TV", "country": "US", "classification": "have",
         "available_to": None, "url": None, "monetization_types": ["FLATRATE"]},
        {"brand": "Netflix", "country": "US", "classification": "subscription",
         "available_to": None, "url": None, "monetization_types": ["FLATRATE"]},
    ]}}

    fresh = _reclassified_discovery_films(stored, CONFIG, GLOBAL_SUBSCRIPTIONS, REVISITABLE)
    verdicts = {o["brand"]: o["classification"] for o in fresh["some-film"]["all_offers"]}

    assert verdicts["YouTube TV"] == "subscription"   # no longer a service you have
    assert verdicts["Netflix"] == "have"              # and this one now is
    # Everything else about the entry survives untouched.
    assert fresh["some-film"]["title"] == "Some Film"


def test_entries_stored_without_monetization_types_are_still_re_judged():
    # Whether a service is one you have needs only its brand and country, so
    # an old entry can still be corrected on that — which is the rung that
    # was wrong. Only free-vs-subscription needs the types it doesn't carry.
    from watchlist_justwatch.dashboard import _reclassified_discovery_films

    stored = {"old-film": {"all_offers": [
        # Wrongly "have" under the old rules, and no types to recompute from.
        {"brand": "YouTube TV", "country": "US", "classification": "have",
         "available_to": None, "url": None},
        # Genuinely a service in the config: still "have", from brand alone.
        {"brand": "Netflix", "country": "US", "classification": "subscription",
         "available_to": None, "url": None},
        # Not a service Josh has either way — the stored free/subscription
        # answer is the only thing that can tell those two apart, so it stands.
        {"brand": "Some Other Service", "country": "US", "classification": "free",
         "available_to": None, "url": None},
    ]}}

    fresh = _reclassified_discovery_films(stored, CONFIG, GLOBAL_SUBSCRIPTIONS, REVISITABLE)
    verdicts = {o["brand"]: o["classification"] for o in fresh["old-film"]["all_offers"]}

    assert verdicts["YouTube TV"] == "subscription"   # no longer claimed as yours
    assert verdicts["Netflix"] == "have"
    assert verdicts["Some Other Service"] == "free"
