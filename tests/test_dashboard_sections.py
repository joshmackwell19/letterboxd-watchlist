from datetime import date, datetime, timedelta

from watchlist_justwatch.dashboard import (
    _build_home_sections,
    _cinema_listings,
    _leaving_soon_section,
    _quick_watch_section,
    _recently_added_section,
    _watch_together_section,
)
from watchlist_justwatch.models import FilmState
from watchlist_justwatch.state import StateDoc


def _film(slug: str, **kwargs) -> FilmState:
    defaults = dict(
        slug=slug, title=slug.title(), year=2020, entry_id="e1", confidence="exact",
        last_checked="2026-09-08T00:00:00Z",
    )
    defaults.update(kwargs)
    return FilmState(**defaults)


def _offer(brand: str, country: str, classification: str, available_to: str | None = None) -> dict:
    return {"brand": brand, "country": country, "classification": classification,
            "available_to": available_to, "url": None}


def _in_days(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


# ---------- _leaving_soon_section ----------

def test_leaving_soon_includes_a_have_offer_expiring_within_the_window():
    state = StateDoc(films={"a": _film("a")})
    offers = {"a": [_offer("Netflix", "AU", "have", _in_days(5))]}

    section = _leaving_soon_section(state, offers, exclude=set())

    assert [f["slug"] for f in section["films"]] == ["a"]
    assert "5 days" in section["films"][0]["leaving_note"]


def test_leaving_soon_excludes_offers_outside_the_window():
    state = StateDoc(films={"a": _film("a")})
    offers = {"a": [_offer("Netflix", "AU", "have", _in_days(40))]}

    section = _leaving_soon_section(state, offers, exclude=set())

    assert section["films"] == []


def test_leaving_soon_ignores_could_get_again_even_if_expiring():
    # Losing a could_get_again offer isn't "about to lose access" — you
    # don't currently have it via that route regardless.
    state = StateDoc(films={"a": _film("a")})
    offers = {"a": [_offer("HBO Max", "AU", "could_get_again", _in_days(2))]}

    section = _leaving_soon_section(state, offers, exclude=set())

    assert section["films"] == []


def test_leaving_soon_respects_exclude_set():
    state = StateDoc(films={"a": _film("a")})
    offers = {"a": [_offer("Netflix", "AU", "have", _in_days(1))]}

    section = _leaving_soon_section(state, offers, exclude={"a"})

    assert section["films"] == []


def test_leaving_soon_sorts_soonest_first():
    state = StateDoc(films={"a": _film("a"), "b": _film("b")})
    offers = {
        "a": [_offer("Netflix", "AU", "have", _in_days(10))],
        "b": [_offer("Netflix", "AU", "have", _in_days(2))],
    }

    section = _leaving_soon_section(state, offers, exclude=set())

    assert [f["slug"] for f in section["films"]] == ["b", "a"]


# ---------- _recently_added_section ----------

def test_recently_added_respects_newest_first_order_and_skips_excluded():
    state = StateDoc(films={"a": _film("a"), "b": _film("b")})
    state.recent_additions = [
        {"slug": "b", "brand": "Stan", "country": "AU", "classification": "have", "added_at": "2026-09-08"},
        {"slug": "a", "brand": "Netflix", "country": "AU", "classification": "have", "added_at": "2026-09-07"},
    ]

    section = _recently_added_section(state, exclude=set())

    assert [f["slug"] for f in section["films"]] == ["b", "a"]
    assert section["films"][0]["added_service"] == "Stan (Australia)"


def test_recently_added_skips_slugs_no_longer_on_the_watchlist():
    state = StateDoc(films={"a": _film("a")})
    state.recent_additions = [
        {"slug": "dropped", "brand": "Stan", "country": "AU", "classification": "have", "added_at": "2026-09-08"},
        {"slug": "a", "brand": "Netflix", "country": "AU", "classification": "have", "added_at": "2026-09-07"},
    ]

    section = _recently_added_section(state, exclude=set())

    assert [f["slug"] for f in section["films"]] == ["a"]


def test_recently_added_skips_free_tier_additions():
    # The log records both "a service you have picked this up" and "a free
    # ad-supported service did" — only the first is what this section says.
    state = StateDoc(films={"a": _film("a"), "b": _film("b")})
    state.recent_additions = [
        {"slug": "b", "brand": "ITVX", "country": "GB", "classification": "free", "added_at": "2026-09-08"},
        {"slug": "a", "brand": "Netflix", "country": "AU", "classification": "have", "added_at": "2026-09-07"},
    ]

    section = _recently_added_section(state, exclude=set())

    assert [f["slug"] for f in section["films"]] == ["a"]


# ---------- _watch_together_section ----------

def test_watch_together_only_includes_confirmed():
    state = StateDoc(films={"a": _film("a"), "b": _film("b")})
    watch_together = {
        "a": {"status": "confirmed", "added_at": "2026-09-01", "decided_at": "2026-09-05"},
        "b": {"status": "declined", "added_at": "2026-09-01", "decided_at": "2026-09-05"},
    }

    section = _watch_together_section(state, watch_together, exclude=set())

    assert [f["slug"] for f in section["films"]] == ["a"]


def test_watch_together_sorts_most_recently_decided_first():
    state = StateDoc(films={"a": _film("a"), "b": _film("b")})
    watch_together = {
        "a": {"status": "confirmed", "added_at": "2026-09-01", "decided_at": "2026-09-01"},
        "b": {"status": "confirmed", "added_at": "2026-09-01", "decided_at": "2026-09-05"},
    }

    section = _watch_together_section(state, watch_together, exclude=set())

    assert [f["slug"] for f in section["films"]] == ["b", "a"]


def test_watch_together_skips_slugs_no_longer_on_the_watchlist():
    state = StateDoc(films={"a": _film("a")})
    watch_together = {
        "gone": {"status": "confirmed", "added_at": "2026-09-01", "decided_at": "2026-09-05"},
    }

    section = _watch_together_section(state, watch_together, exclude=set())

    assert section["films"] == []


# ---------- _quick_watch_section ----------

def test_quick_watch_excludes_films_outside_the_90_minute_window():
    state = StateDoc(films={
        "short": _film("short", runtime_minutes=95, rating=4.0),
        "too_short": _film("too_short", runtime_minutes=70, rating=4.0),
        "too_long": _film("too_long", runtime_minutes=140, rating=4.0),
        "unknown": _film("unknown", runtime_minutes=None, rating=4.0),
    })

    section = _quick_watch_section(state, films_all_offers={}, exclude=set())

    assert [f["slug"] for f in section["films"]] == ["short"]


def test_quick_watch_prefers_watchable_now_then_falls_back_to_rating():
    state = StateDoc(films={
        "watchable": _film("watchable", runtime_minutes=90, rating=3.0),
        "unwatchable_higher_rated": _film("unwatchable_higher_rated", runtime_minutes=90, rating=4.5),
    })
    offers = {"watchable": [_offer("Netflix", "AU", "have")]}

    section = _quick_watch_section(state, offers, exclude=set())

    assert [f["slug"] for f in section["films"]] == ["watchable", "unwatchable_higher_rated"]


def test_quick_watch_respects_exclude_set():
    state = StateDoc(films={"a": _film("a", runtime_minutes=90, rating=4.0)})

    section = _quick_watch_section(state, films_all_offers={}, exclude={"a"})

    assert section["films"] == []


def _showing(title: str, year: int, showtime: str, cinema: str = "Prince Charles Cinema") -> dict:
    return {"cinema": cinema, "title": title, "year": year, "showtime": showtime,
            "duration_minutes": 100, "director": None, "synopsis": None,
            "poster_url": None, "booking_url": None}


# ---------- _cinema_listings ----------

def test_cinema_listings_merges_a_matched_film_across_cinemas_into_one_row():
    # The same watchlist film showing at two different cinemas should be
    # ONE row with both cinemas' showtimes attached, not two cards.
    state = StateDoc(films={
        "taxi-driver": _film("taxi-driver", title="Taxi Driver", year=1976, rating=4.2,
                              genre=["Crime", "Drama"], runtime_minutes=113,
                              director=["Martin Scorsese"], synopsis="A cabbie's descent."),
    })
    state.cinema_showtimes = [
        _showing("Taxi Driver", 1976, "2026-09-09T18:00:00", cinema="Prince Charles Cinema"),
        _showing("TAXI DRIVER!", 1976, "2026-09-10T20:00:00", cinema="Barbican"),
    ]

    rows = _cinema_listings(state)

    assert len(rows) == 1
    row = rows[0]
    assert row["matched_slug"] == "taxi-driver"
    assert {s["cinema"] for s in row["showtimes"]} == {"Prince Charles Cinema", "Barbican"}
    # Matched rows use the watchlist's own richer metadata, not the venue's.
    assert row["rating"] == 4.2
    assert row["genre"] == ["Crime", "Drama"]
    assert row["director"] == "Martin Scorsese"
    assert row["synopsis"] == "A cabbie's descent."


def test_cinema_listings_keeps_unmatched_same_title_films_separate_per_cinema():
    # No reliable cross-cinema identity for an unmatched title — merging
    # by title alone risks conflating two different films that share a
    # name, so these stay one row per (cinema, title) as before.
    state = StateDoc(films={})
    state.cinema_showtimes = [
        _showing("Mystery Film", None, "2026-09-09T18:00:00", cinema="Prince Charles Cinema"),
        _showing("Mystery Film", None, "2026-09-10T20:00:00", cinema="Riverside Studios"),
    ]

    rows = _cinema_listings(state)

    assert len(rows) == 2
    assert all(row["matched_slug"] is None for row in rows)


def test_cinema_listings_sorts_by_soonest_showtime():
    state = StateDoc(films={})
    state.cinema_showtimes = [
        _showing("Later Film", None, "2026-09-12T18:00:00"),
        _showing("Sooner Film", None, "2026-09-09T18:00:00"),
    ]

    rows = _cinema_listings(state)

    assert [r["title"] for r in rows] == ["Sooner Film", "Later Film"]


# ---------- _build_home_sections director/cast section cap ----------

def _discovery_entry(slug: str) -> dict:
    return {"slug": slug, "title": slug.title(), "year": 2020, "rating": 4.0, "poster_url": None,
            "director": "Someone", "genre": ["Drama"]}


def test_build_home_sections_caps_person_sections_across_director_and_cast():
    # A run with several multi-cast recent watches can genuinely generate a
    # dozen+ director/cast sections (see main.py) — Home should only ever
    # show a handful, not a wall of near-identical "More starring X" rows.
    state = StateDoc(films={})
    state.recommendation_sections = [
        {"key": f"director:Person {i}", "header": f"More from Person {i}", "slugs": [f"d{i}"]}
        for i in range(3)
    ] + [
        {"key": f"cast:Person {i}", "header": f"More starring Person {i}", "slugs": [f"c{i}"]}
        for i in range(5)
    ]
    films_by_slug = {f"d{i}": _discovery_entry(f"d{i}") for i in range(3)}
    films_by_slug |= {f"c{i}": _discovery_entry(f"c{i}") for i in range(5)}

    sections = _build_home_sections(state, films_all_offers={}, films_by_slug=films_by_slug,
                                     discovery_films={}, dismissed_recommendations=set(), watch_together={})

    person_sections = [s for s in sections if s["key"].startswith(("director:", "cast:"))]
    assert len(person_sections) == 4
    # All 3 director sections show before any cast section fills the
    # remaining slot — same "directors first" order main.py generates them in.
    assert [s["key"] for s in person_sections] == ["director:Person 0", "director:Person 1",
                                                     "director:Person 2", "cast:Person 0"]


# ---------- _build_home_sections cross-section exclusion ----------

def test_build_home_sections_does_not_repeat_a_film_across_sections():
    # A film qualifying for both recently_added and leaving_soon should only
    # appear in the higher-priority section (recently_added leads Home).
    state = StateDoc(films={"a": _film("a")})
    state.recent_additions = [
        {"slug": "a", "brand": "Netflix", "country": "AU", "classification": "have", "added_at": "2026-09-08"},
    ]
    offers = {"a": [_offer("Netflix", "AU", "have", _in_days(3))]}

    sections = _build_home_sections(state, offers, films_by_slug={}, discovery_films={},
                                     dismissed_recommendations=set(), watch_together={})

    by_key = {s["key"]: [f["slug"] for f in s["films"]] for s in sections}
    assert by_key.get("recently_added") == ["a"]
    assert "leaving_soon" not in by_key


def test_build_home_sections_leads_with_new_arrivals_and_ends_with_quick_watch():
    state = StateDoc(films={"a": _film("a"), "b": _film("b"), "c": _film("c", runtime_minutes=85)})
    state.recent_additions = [
        {"slug": "a", "brand": "Netflix", "country": "AU", "classification": "have", "added_at": "2026-09-08"},
    ]
    offers = {
        "a": [_offer("Netflix", "AU", "have", None)],
        "b": [_offer("Stan", "AU", "have", _in_days(3))],
        "c": [_offer("Netflix", "AU", "have", None)],
    }

    keys = [s["key"] for s in _build_home_sections(state, offers, films_by_slug={}, discovery_films={},
                                                    dismissed_recommendations=set(), watch_together={})]

    assert keys[0] == "recently_added"
    assert keys[1] == "leaving_soon"
    assert keys[-1] == "quick_watch"
    # The cinema section moved out to its own tab entirely.
    assert "cinema" not in keys


def test_build_home_sections_omits_empty_sections_entirely():
    state = StateDoc(films={})
    sections = _build_home_sections(state, films_all_offers={}, films_by_slug={}, discovery_films={},
                                     dismissed_recommendations=set(), watch_together={})
    assert sections == []


# --- Services tab: one card per service, and what it's uniquely worth -----

def _svc_offer(country, brand, classification="subscription"):
    return {"brand": brand, "country": country, "classification": classification,
            "available_to": None, "url": None}


def _service_state(films: dict[str, list[dict]]):
    from watchlist_justwatch.models import FilmState
    from watchlist_justwatch.state import StateDoc
    state = StateDoc(films={
        slug: FilmState(slug=slug, title=slug.replace("-", " ").title(), year=2000, entry_id=None,
                        confidence="exact", last_checked="2026-09-23T00:00:00Z")
        for slug in films
    })
    return state, films


def test_a_service_is_one_row_however_many_countries_it_is_in():
    from watchlist_justwatch.dashboard import _service_rows

    state, offers = _service_state({
        "a-film": [_svc_offer("GB", "Netflix", "have"), _svc_offer("US", "Netflix", "have"),
                   _svc_offer("DE", "Netflix", "have")],
    })
    rows = _service_rows(state, offers)

    assert [r["brand"] for r in rows] == ["Netflix"]
    # The film is counted once, not once per country.
    assert rows[0]["film_count"] == 1
    assert rows[0]["country_count"] == 3
    assert [c["code"] for c in rows[0]["countries"]] == ["DE", "GB", "US"]


def test_countries_carry_their_own_film_lists_for_the_pills():
    from watchlist_justwatch.dashboard import _service_rows

    state, offers = _service_state({
        "one": [_svc_offer("GB", "Netflix", "have")],
        "two": [_svc_offer("GB", "Netflix", "have"), _svc_offer("US", "Netflix", "have")],
    })
    rows = _service_rows(state, offers)

    assert rows[0]["slugs_by_country"] == {"GB": ["one", "two"], "US": ["two"]}
    # Busiest country first, so the pills lead with the useful ones.
    assert [(c["code"], c["film_count"]) for c in rows[0]["countries"]] == [("GB", 2), ("US", 1)]


def test_unique_means_on_no_other_service_you_have():
    from watchlist_justwatch.dashboard import _service_rows

    state, offers = _service_state({
        # Only on Netflix, of the things you have — cancelling Netflix loses it.
        "netflix-only": [_svc_offer("GB", "Netflix", "have")],
        # On both, so neither service is the reason you can watch it.
        "on-both": [_svc_offer("GB", "Netflix", "have"), _svc_offer("GB", "Disney Plus", "have")],
        # On Netflix and on a service you don't have: still unique to Netflix,
        # because the other one isn't something you could watch it on today.
        "netflix-and-a-paid-one": [_svc_offer("GB", "Netflix", "have"), _svc_offer("GB", "Hayu", "subscription")],
    })
    rows = {r["brand"]: r for r in _service_rows(state, offers)}

    assert rows["Netflix"]["unique_slugs"] == ["netflix-and-a-paid-one", "netflix-only"]
    assert rows["Disney Plus"]["unique_slugs"] == []


def test_uniqueness_is_judged_across_countries_not_within_one():
    from watchlist_justwatch.dashboard import _service_rows

    state, offers = _service_state({
        "here-and-there": [_svc_offer("GB", "Netflix", "have"), _svc_offer("US", "Disney Plus", "have")],
    })
    rows = {r["brand"]: r for r in _service_rows(state, offers)}

    # Reachable on Disney Plus in another market, so Netflix is not the only
    # way to see it — even though it is the only way to see it in GB.
    assert rows["Netflix"]["unique_slugs"] == []
    assert rows["Disney Plus"]["unique_slugs"] == []


def test_for_a_service_you_do_not_have_unique_reads_as_what_it_would_add():
    from watchlist_justwatch.dashboard import _service_rows

    state, offers = _service_state({
        "nowhere-else": [_svc_offer("GB", "Hayu", "subscription")],
        "already-covered": [_svc_offer("GB", "Hayu", "subscription"), _svc_offer("GB", "Netflix", "have")],
    })
    rows = {r["brand"]: r for r in _service_rows(state, offers)}

    assert rows["Hayu"]["unique_slugs"] == ["nowhere-else"]


def test_a_free_broadcaster_counts_as_a_service_you_have():
    from watchlist_justwatch.dashboard import _service_rows

    state, offers = _service_state({
        # On iPlayer as well, so it isn't a reason to keep paying for Netflix.
        "on-iplayer-too": [_svc_offer("GB", "Netflix", "have"), _svc_offer("GB", "BBC iPlayer", "have")],
    })
    rows = {r["brand"]: r for r in _service_rows(state, offers)}

    assert rows["Netflix"]["unique_slugs"] == []
