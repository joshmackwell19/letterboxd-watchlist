from datetime import date, timedelta

from watchlist_justwatch.dashboard import (
    _build_home_sections,
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
