"""Which film a cinema listing is showing, when the title alone can't say.

A remake shares its title with the original, a re-release is listed with the
screening's year, and venues wrap titles in strand names, hosts and formats.
These cover matching on everything else a listing gives — year, director,
runtime, recency, other titles TMDB knows, and Clusterflick's own match for
the same screening — with every network call stubbed.
"""

from datetime import date, datetime

import pytest

from watchlist_justwatch.cinemas import (
    MATCHER_VERSION,
    attach_clusterflick_ids,
    listing_title_variants,
    match_watchlist_film,
    resolve_listing_to_letterboxd,
)
from watchlist_justwatch.dashboard import _cinema_listings
from watchlist_justwatch.models import FilmState
from watchlist_justwatch.state import StateDoc

TODAY = date(2026, 10, 1)

SENSE_1995 = {"id": 4584, "title": "Sense and Sensibility", "release_date": "1995-12-13"}
SENSE_2026 = {"id": 1100001, "title": "Sense and Sensibility", "release_date": "2026-09-18"}
FACTS = {
    4584: {"id": 4584, "title": "Sense and Sensibility", "year": 1995, "release_date": "1995-12-13",
           "runtime": 136, "directors": ["Ang Lee"], "titles": ["Sense and Sensibility"]},
    1100001: {"id": 1100001, "title": "Sense and Sensibility", "year": 2026, "release_date": "2026-09-18",
              "runtime": 132, "directors": ["Georgia Oakley"], "titles": ["Sense and Sensibility"]},
    807: {"id": 807, "title": "Se7en", "year": 1995, "release_date": "1995-09-22", "runtime": 127,
          "directors": ["David Fincher"], "titles": ["Se7en", "Seven"]},
    31414: {"id": 31414, "title": "The Devils", "year": 1971, "release_date": "1971-07-16",
            "runtime": 111, "directors": ["Ken Russell"], "titles": ["The Devils"]},
}


def _letterboxd(tmdb_id):
    return {"slug": f"film-{tmdb_id}", "rating": 3.9, "director": FACTS.get(tmdb_id, {}).get("directors", [])}


def _resolve(title, year=None, *, results, **listing):
    return resolve_listing_to_letterboxd(
        title, year, search_movies=lambda t, y: results(t, y), movie_facts=FACTS.get,
        film_details_by_tmdb_id=_letterboxd, today=TODAY, **listing)


def _both_sense(title, year):
    return [SENSE_1995, SENSE_2026] if "sense" in title.lower() else []


# --- remakes and re-releases ------------------------------------------------

def test_a_new_film_sharing_an_old_ones_title_is_the_new_one():
    # Vue (via CinemaGuide) gives no year or director, only a runtime: the
    # 2026 film's runtime and its being in cinemas now outweigh TMDB listing
    # 1995's first.
    match = _resolve("Sense and Sensibility", results=_both_sense, duration_minutes=132)
    assert match["tmdb_id"] == 1100001
    assert match["year"] == 2026


def test_the_director_settles_it_whatever_else_says():
    match = _resolve("Sense and Sensibility", results=_both_sense, director="Ang Lee", duration_minutes=132)
    assert match["tmdb_id"] == 4584


def test_a_listing_year_rules_out_films_made_after_it():
    match = _resolve("Sense and Sensibility", 1995, results=_both_sense)
    assert match["tmdb_id"] == 4584


def test_a_director_that_fits_no_candidate_means_no_match():
    assert _resolve("Sense and Sensibility", results=_both_sense, director="Someone Else") is None


def test_a_re_release_is_not_ruled_out_by_the_screenings_year():
    # A listing year after the film's is usually the re-release's own.
    match = _resolve("Se7en", 2026, results=lambda t, y: [] if y else [{"id": 807, "title": "Se7en",
                                                                         "release_date": "1995-09-22"}])
    assert match["tmdb_id"] == 807


# --- titles TMDB holds differently --------------------------------------------

def test_a_title_tmdb_only_lists_as_an_alternative_still_matches():
    match = _resolve("Seven", 1995, results=lambda t, y: [{"id": 807, "title": "Se7en",
                                                            "release_date": "1995-09-22"}])
    assert match["tmdb_id"] == 807


def test_a_strand_or_possessive_is_looked_past_when_the_facts_agree():
    def search(title, year):
        return [{"id": 31414, "title": "The Devils", "release_date": "1971-07-16"}] if title == "The Devils" else []

    match = _resolve("Ken Russell’s The Devils", 1971, results=search, director="Ken Russell")
    assert match["tmdb_id"] == 31414


def test_a_looser_title_alone_is_not_enough():
    # Nothing but the stripped title agrees — "The Devils" could be any of
    # several films, so without a year, director or runtime it's left plain.
    def search(title, year):
        return [{"id": 31414, "title": "The Devils", "release_date": "1971-07-16"}] if title == "The Devils" else []

    assert _resolve("Ken Russell’s The Devils", results=search) is None


@pytest.mark.parametrize("listing,expected", [
    ("Funeral Parade presents \"The Long Day Closes\"", "The Long Day Closes"),
    ("SWEET BABY CHARLIE aka THE SADIST", "THE SADIST"),
    ("Thunder Road (1958) on 35mm", "Thunder Road"),
    ("MUBI FEST: BLUE HERON + STORYTELLING", "BLUE HERON"),
    ("(4DX Rewind) Shrek", "Shrek"),
    ("Rosemary's Baby Presented by the Cult Classic Collective", "Rosemary's Baby"),
    ("Finding My Voice (London Premiere) - LIFF", "Finding My Voice"),
    ("Throwback: Casino Royale (20th Anniversary", "Casino Royale"),
    ("Train to Busan Film Screening", "Train to Busan"),
])
def test_decorated_titles_offer_the_film_as_a_variant(listing, expected):
    variants, _ = listing_title_variants(listing)
    assert expected in variants


def test_the_strict_form_comes_first_and_keeps_a_real_colon():
    variants, _ = listing_title_variants("Dune: Part Three")
    assert variants[0] == "Dune: Part Three"


def test_a_year_freed_by_loosening_is_kept():
    assert listing_title_variants("Thunder Road (1958) on 35mm")[1] == 1958


# --- an id from upstream ------------------------------------------------------

def test_an_upstream_id_skips_the_search():
    match = _resolve("Sense and Sensibility + Recorded Q&A", tmdb_id=1100001,
                     results=lambda t, y: pytest.fail("searched"))
    assert match["tmdb_id"] == 1100001


def test_an_upstream_id_the_director_contradicts_is_not_trusted():
    match = _resolve("Sense and Sensibility", tmdb_id=1100001, director="Ang Lee", results=_both_sense)
    assert match["tmdb_id"] == 4584


def test_a_resolved_match_records_its_rules():
    assert _resolve("Sense and Sensibility", 1995, results=_both_sense)["matcher_version"] == MATCHER_VERSION


# --- the watchlist ------------------------------------------------------------

def _film(slug, title, year, director=(), runtime=None):
    return FilmState(slug=slug, title=title, year=year, entry_id=None, confidence="exact", last_checked="",
                     director=list(director), runtime_minutes=runtime)


def test_a_remake_is_not_the_watchlist_film_it_shares_a_title_with():
    films = {"sense-and-sensibility": _film("sense-and-sensibility", "Sense and Sensibility", 1995,
                                            ["Ang Lee"], 136)}
    assert match_watchlist_film("Sense and Sensibility", 2026, films, director="Georgia Oakley") is None
    assert match_watchlist_film("Sense and Sensibility", None, films, director="Ang Lee") == "sense-and-sensibility"


def test_a_decorated_title_matches_the_watchlist_when_the_facts_agree():
    films = {"the-devils": _film("the-devils", "The Devils", 1971, ["Ken Russell"], 111)}
    assert match_watchlist_film("Ken Russell’s The Devils", 1971, films, director="Ken Russell") == "the-devils"
    assert match_watchlist_film("Ken Russell’s The Devils", None, films) is None


def test_the_dashboard_trusts_the_resolved_film_over_a_title_match():
    state = StateDoc(films={"sense-and-sensibility": _film("sense-and-sensibility", "Sense and Sensibility",
                                                           1995, ["Ang Lee"], 136)})
    state.cinema_showtimes = [{"cinema": "Vue West End", "title": "Sense and Sensibility", "year": None,
                               "showtime": "2026-10-03T14:05:00", "duration_minutes": 132, "director": None,
                               "synopsis": None, "poster_url": None, "booking_url": None,
                               "tmdb_id": 1100001}]
    state.cinema_matches = {"tmdb:1100001": {"slug": "sense-and-sensibility-2026", "tmdb_id": 1100001,
                                             "title": "Sense and Sensibility", "year": 2026}}

    row = _cinema_listings(state, now=datetime(2026, 10, 1))[0]

    assert row["matched_slug"] is None
    assert row["letterboxd_slug"] == "sense-and-sensibility-2026"
    assert row["year"] == 2026


# --- Clusterflick's match for the same screening ------------------------------

def test_the_same_screening_borrows_clusterflicks_id():
    showings = [
        {"cinema": "Vue West End", "title": "Sense and Sensibility", "showtime": "2026-10-03T14:05:00"},
        {"cinema": "Vue West End", "title": "Pressure", "showtime": "2026-10-03T14:05:00"},
        {"cinema": "Vue West End", "title": "Verity", "showtime": "2026-10-03T16:00:00"},
    ]
    screenings = {"Vue West End": {"2026-10-03T14:05": [
        ("Pressure", 555), ("Sense and Sensibility + Recorded Q&A", 1100001)]}}

    assert attach_clusterflick_ids(showings, screenings) == 2
    assert [s.get("tmdb_id") for s in showings] == [1100001, 555, None]


def test_a_screening_whose_title_disagrees_is_left_alone():
    showings = [{"cinema": "Vue West End", "title": "Verity", "showtime": "2026-10-03T14:05:00"}]
    screenings = {"Vue West End": {"2026-10-03T14:05": [("Pressure", 555)]}}
    assert attach_clusterflick_ids(showings, screenings) == 0
    assert "tmdb_id" not in showings[0]


# --- re-checking old matches --------------------------------------------------

def test_an_old_match_is_rechecked_without_asking_letterboxd_again(monkeypatch):
    from watchlist_justwatch import main

    old = {"slug": "sense-and-sensibility", "tmdb_id": 4584, "director": "Ang Lee",
           "resolved_at": "2026-09-30"}       # no matcher_version: made under the old rules
    showings = [{"cinema": "Vue West End", "title": "Sense and Sensibility", "year": None,
                 "showtime": "2026-10-03T14:05:00", "duration_minutes": 132, "director": "Ang Lee"}]
    key = main.showing_match_key(showings[0])
    monkeypatch.setattr(main, "_tmdb_search_movies", lambda t, y: [SENSE_1995, SENSE_2026])
    monkeypatch.setattr(main, "_tmdb_movie_facts", FACTS.get)
    monkeypatch.setattr(main, "get_film_details_by_tmdb_id", lambda *a, **k: pytest.fail("asked Letterboxd"))

    out = main._resolved_cinema_matches(showings, {key: old}, warn=print, budget=None)

    assert out[key]["slug"] == "sense-and-sensibility"
    assert out[key]["matcher_version"] == MATCHER_VERSION


def test_an_old_match_survives_a_failed_recheck(monkeypatch):
    from watchlist_justwatch import main

    old = {"slug": "sense-and-sensibility", "tmdb_id": 4584, "resolved_at": "2026-09-30"}
    showings = [{"cinema": "Vue West End", "title": "Sense and Sensibility", "year": None,
                 "showtime": "2026-10-03T14:05:00"}]
    key = main.showing_match_key(showings[0])

    def down(title, year):
        raise RuntimeError("TMDB unreachable")

    monkeypatch.setattr(main, "_tmdb_search_movies", down)
    out = main._resolved_cinema_matches(showings, {key: old}, warn=lambda m: None, budget=None)
    assert out[key] is old


# --- the same director, written differently ------------------------------------

@pytest.mark.parametrize("listing,tmdb", [
    ("Andrzej Zulawski", "Andrzej Żuławski"),      # ł isn't an accent, so folding alone missed it
    ("Chan-wook Park", "Park Chan-wook"),          # family name first, or last
    ("Siu-pong Wong", "Wong Siu-pong"),
    ("Amma Assante", "Amma Asante"),               # the venue's typo
])
def test_one_director_written_two_ways_still_agrees(listing, tmdb):
    from watchlist_justwatch.cinemas import _director_names, _directors_agree
    assert _directors_agree(_director_names(listing), _director_names([tmdb]))


def test_two_directors_sharing_a_first_name_do_not_agree():
    from watchlist_justwatch.cinemas import _director_names, _directors_agree
    assert not _directors_agree(_director_names("David Lynch"), _director_names(["David Fincher"]))


def test_an_upstream_id_survives_a_differently_written_director():
    facts = {**FACTS, 21484: {"id": 21484, "title": "Possession", "year": 1981, "release_date": "1981-05-27",
                              "runtime": 124, "directors": ["Andrzej Żuławski"], "titles": ["Possession"]}}
    match = resolve_listing_to_letterboxd(
        "Possession", 1981, tmdb_id=21484, director="Andrzej Zulawski", duration_minutes=124,
        search_movies=lambda t, y: pytest.fail("searched"), movie_facts=facts.get,
        film_details_by_tmdb_id=_letterboxd, today=TODAY)
    assert match["tmdb_id"] == 21484
