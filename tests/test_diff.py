from watchlist_justwatch.config import CountryConfig
from watchlist_justwatch.diff import build_report, diff_film_offers
from watchlist_justwatch.models import FilmState, OfferRecord
from watchlist_justwatch.state import StateDoc

AU = CountryConfig(country="AU", subscriptions=["Stan"], free_tier=["Tubi TV"])


def _offer(package_clear_name: str, country: str = "AU", monetization_type: str = "FLATRATE") -> OfferRecord:
    # diff_key() is (country, package_technical_name, monetization_type) —
    # NOT package_clear_name — so the technical name has to actually vary
    # per service here, same as real JustWatch data, or two different
    # offers collide onto the same key.
    return OfferRecord(
        country=country, monetization_type=monetization_type,
        package_technical_name=package_clear_name.lower().replace(" ", "-"),
        package_clear_name=package_clear_name, package_id=1, url="https://example.com",
    )


def _film(slug: str, *, offers=None, confidence="exact") -> FilmState:
    return FilmState(
        slug=slug, title=slug, year=2020, entry_id="e1", confidence=confidence,
        last_checked="2026-09-08T00:00:00Z", offers=offers or [],
    )


def test_diff_film_offers_all_new_when_no_previous_film():
    current = _film("a", offers=[_offer("Stan")])
    assert diff_film_offers(None, current) == current.offers


def test_diff_film_offers_only_flags_genuinely_new_offers():
    shared = _offer("Stan")
    previous = _film("a", offers=[shared])
    new_offer = _offer("Tubi TV")
    current = _film("a", offers=[shared, new_offer])
    assert diff_film_offers(previous, current) == [new_offer]


def test_diff_film_offers_empty_when_nothing_changed():
    offers = [_offer("Stan")]
    previous = _film("a", offers=offers)
    current = _film("a", offers=list(offers))
    assert diff_film_offers(previous, current) == []


def test_build_report_first_run_classifies_but_skips_new_films():
    # No baseline at all — every film "looks new", which is already what the
    # have/free_tier/new_possible breakdown covers, so new_films stays empty
    # rather than duplicating the whole watchlist there too.
    previous_state = StateDoc()
    current_state = StateDoc(films={"a": _film("a", offers=[_offer("Stan")])})

    report = build_report(previous_state, current_state, {"AU": AU})

    assert report.new_films == []
    assert len(report.new_have) == 1
    assert report.new_have[0].film.slug == "a"


def test_build_report_flags_genuine_new_watchlist_addition():
    previous_state = StateDoc(films={"existing": _film("existing")})
    current_state = StateDoc(films={
        "existing": _film("existing"),
        "brand-new": _film("brand-new", offers=[_offer("Stan")]),
    })

    report = build_report(previous_state, current_state, {"AU": AU})

    assert [f.slug for f in report.new_films] == ["brand-new"]
    # A new addition's offers are covered by its own dedicated section, not
    # also repeated piecemeal in new_have/new_free_tier/new_possible.
    assert report.new_have == []


def test_build_report_flags_unmatched_confidence_for_new_and_existing_films():
    previous_state = StateDoc(films={"existing": _film("existing", confidence="exact")})
    current_state = StateDoc(films={
        "existing": _film("existing", confidence="unmatched"),
        "new-one": _film("new-one", confidence="low_confidence"),
    })

    report = build_report(previous_state, current_state, {"AU": AU})

    assert {f.slug for f in report.unmatched} == {"existing", "new-one"}


def test_build_report_classifies_new_offers_by_type():
    previous = _film("a", offers=[])
    current = _film("a", offers=[_offer("Stan"), _offer("Tubi TV"), _offer("Some Other Service")])
    previous_state = StateDoc(films={"a": previous})
    current_state = StateDoc(films={"a": current})

    report = build_report(previous_state, current_state, {"AU": AU})

    assert [e.offer.package_clear_name for e in report.new_have] == ["Stan"]
    assert [e.offer.package_clear_name for e in report.new_free_tier] == ["Tubi TV"]
    assert [e.offer.package_clear_name for e in report.new_possible] == ["Some Other Service"]


def test_build_report_ignores_offers_in_untracked_countries():
    previous = _film("a", offers=[])
    current = _film("a", offers=[_offer("Stan", country="ZZ")])
    previous_state = StateDoc(films={"a": previous})
    current_state = StateDoc(films={"a": current})

    report = build_report(previous_state, current_state, {"AU": AU})

    assert report.is_empty()
