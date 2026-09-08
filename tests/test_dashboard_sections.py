from datetime import date, datetime, timedelta

from watchlist_justwatch.dashboard import (
    _build_home_sections,
    _cinema_section,
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


# ---------- _cinema_section ----------

def _showing(title: str, year: int, showtime: str, cinema: str = "Prince Charles Cinema") -> dict:
    return {"cinema": cinema, "title": title, "year": year, "showtime": showtime,
            "duration_minutes": 100, "director": None, "synopsis": None,
            "poster_url": None, "booking_url": None}


def test_cinema_section_only_includes_matched_upcoming_showings():
    state = StateDoc(films={"taxi-driver": _film("taxi-driver", title="Taxi Driver", year=1976)})
    state.cinema_showtimes = [
        _showing("Taxi Driver", 1976, "2026-09-09T18:00:00"),
        _showing("Some Unmatched Film", 2020, "2026-09-09T20:00:00"),
    ]
    now = datetime(2026, 9, 8, 12, 0)

    section = _cinema_section(state, exclude=set(), now=now)

    assert [f["slug"] for f in section["films"]] == ["taxi-driver"]
    assert "Prince Charles Cinema" in section["films"][0]["cinema_note"]


def test_cinema_section_excludes_showings_already_in_the_past():
    state = StateDoc(films={"taxi-driver": _film("taxi-driver", title="Taxi Driver", year=1976)})
    state.cinema_showtimes = [_showing("Taxi Driver", 1976, "2026-09-07T18:00:00")]
    now = datetime(2026, 9, 8, 12, 0)

    section = _cinema_section(state, exclude=set(), now=now)

    assert section["films"] == []


def test_cinema_section_picks_soonest_showing_per_film():
    state = StateDoc(films={"taxi-driver": _film("taxi-driver", title="Taxi Driver", year=1976)})
    state.cinema_showtimes = [
        _showing("Taxi Driver", 1976, "2026-09-12T18:00:00"),
        _showing("Taxi Driver", 1976, "2026-09-09T15:00:00"),
    ]
    now = datetime(2026, 9, 8, 12, 0)

    section = _cinema_section(state, exclude=set(), now=now)

    assert len(section["films"]) == 1
    assert "9 Sep" in section["films"][0]["cinema_note"]


def test_cinema_section_respects_exclude_set():
    state = StateDoc(films={"taxi-driver": _film("taxi-driver", title="Taxi Driver", year=1976)})
    state.cinema_showtimes = [_showing("Taxi Driver", 1976, "2026-09-09T18:00:00")]
    now = datetime(2026, 9, 8, 12, 0)

    section = _cinema_section(state, exclude={"taxi-driver"}, now=now)

    assert section["films"] == []


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
                                     dismissed_recommendations=set(), watch_together={})

    person_sections = [s for s in sections if s["key"].startswith(("director:", "cast:"))]
    assert len(person_sections) == 4
    # All 3 director sections show before any cast section fills the
    # remaining slot — same "directors first" order main.py generates them in.
    assert [s["key"] for s in person_sections] == ["director:Person 0", "director:Person 1",
                                                     "director:Person 2", "cast:Person 0"]


# ---------- _build_home_sections cross-section exclusion ----------

def test_build_home_sections_does_not_repeat_a_film_across_sections():
    # A film qualifying for both leaving_soon and recently_added should only
    # appear in the higher-priority section (leaving_soon leads).
    state = StateDoc(films={"a": _film("a")})
    state.recent_additions = [
        {"slug": "a", "brand": "Netflix", "country": "AU", "classification": "have", "added_at": "2026-09-08"},
    ]
    offers = {"a": [_offer("Netflix", "AU", "have", _in_days(3))]}

    sections = _build_home_sections(state, offers, films_by_slug={}, dismissed_recommendations=set(),
                                     watch_together={})

    by_key = {s["key"]: [f["slug"] for f in s["films"]] for s in sections}
    assert by_key.get("leaving_soon") == ["a"]
    assert "recently_added" not in by_key


def test_build_home_sections_omits_empty_sections_entirely():
    state = StateDoc(films={})
    sections = _build_home_sections(state, films_all_offers={}, films_by_slug={},
                                     dismissed_recommendations=set(), watch_together={})
    assert sections == []
