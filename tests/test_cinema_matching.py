"""Turning a cinema listing into a film.

Cinema sites decorate titles in ways no film database does, and only about
a tenth of what's on is ever on the watchlist — so both halves of this
(cleaning the title, and resolving what's left against TMDB/Letterboxd)
decide whether a listing gets a poster, a rating and somewhere to click,
or shows as a bare line of text. The network calls are injected, so what's
covered here is the matching judgement rather than either service.
"""

import pytest

from watchlist_justwatch.cinemas import (
    clean_listing_title,
    listing_match_key,
    match_watchlist_film,
    resolve_listing_to_letterboxd,
)
from watchlist_justwatch.models import FilmState


def _film(slug: str, title: str, year: int | None) -> FilmState:
    return FilmState(slug=slug, title=title, year=year, entry_id=None, confidence="exact",
                     last_checked="2026-09-23T00:00:00Z")


# --- what the venue added, and the film underneath it -------------------

@pytest.mark.parametrize("listing,expected_title,expected_year", [
    # Re-release years are the single most common annotation, and the one
    # that carries information worth keeping.
    ("The Hunger Games (2012)", "The Hunger Games", 2012),
    ("Godzilla (1954)", "Godzilla", 1954),
    # Anniversaries, cuts and presentation formats carry none.
    ("La La Land (10th Anniversary)", "La La Land", None),
    ("Star Trek IV: The Voyage Home (40th Anniversary)", "Star Trek IV: The Voyage Home", None),
    ("Alien (Theatrical Cut)", "Alien", None),
    ("The Cabinet of Dr. Caligari (Live Score)", "The Cabinet of Dr. Caligari", None),
    ("Blade Runner - The Final Cut", "Blade Runner", None),
    ("Aliens - 70mm", "Aliens", None),
    ("Dune: Part Two - IMAX", "Dune: Part Two", None),
    # Stacked annotations unwind together.
    ("Alien (Theatrical Cut) (1979)", "Alien", 1979),
    # An undecorated title is left exactly as it is.
    ("Spider-Man: Brand New Day", "Spider-Man: Brand New Day", None),
])
def test_venue_annotations_are_stripped_and_a_real_year_kept(listing, expected_title, expected_year):
    assert clean_listing_title(listing) == (expected_title, expected_year)


def test_brackets_that_are_part_of_the_title_survive():
    # Stripping these would leave a title no film has, which is worse than
    # leaving an annotation on.
    assert clean_listing_title("(500) Days of Summer")[0] == "(500) Days of Summer"
    assert clean_listing_title("Am I OK?")[0] == "Am I OK?"


def test_a_wholly_bracketed_title_is_not_stripped_to_nothing():
    assert clean_listing_title("(Anniversary)")[0] == "(Anniversary)"


# --- matching against the watchlist -------------------------------------

def test_a_re_release_now_matches_the_watchlist_film():
    films = {"la-la-land": _film("la-la-land", "La La Land", 2016)}
    # The listing's own year is the screening's, not the film's — matching on
    # it is what used to make every repertory listing miss.
    assert match_watchlist_film("La La Land (10th Anniversary)", 2026, films) == "la-la-land"


def test_a_year_in_the_title_beats_the_listings_own_year():
    films = {
        "hunger-games": _film("hunger-games", "The Hunger Games", 2012),
        "hunger-games-2026": _film("hunger-games-2026", "The Hunger Games", 2026),
    }
    assert match_watchlist_film("The Hunger Games (2012)", 2026, films) == "hunger-games"


def test_a_listing_matching_nothing_tracked_stays_unmatched():
    assert match_watchlist_film("Spider-Man: Brand New Day", 2026, {}) is None


# --- resolving everything else ------------------------------------------

def _search(result):
    return lambda title, year: result


def _details(result):
    return lambda tmdb_id: result


LA_LA_LAND_TMDB = {"id": 313369, "title": "La La Land", "release_date": "2016-12-09"}
LA_LA_LAND_LETTERBOXD = {
    "slug": "la-la-land", "rating": 4.1, "poster_url": "https://a.ltrbxd.com/p.jpg",
    "director": ["Damien Chazelle"], "starring": ["Ryan Gosling"], "synopsis": "Jazz.",
    "genre": ["Drama"], "runtime_minutes": 128,
}


def test_a_listing_resolves_to_its_letterboxd_film():
    match = resolve_listing_to_letterboxd(
        "La La Land (10th Anniversary)", 2026,
        search_movie=_search(LA_LA_LAND_TMDB), film_details_by_tmdb_id=_details(LA_LA_LAND_LETTERBOXD))

    assert match["slug"] == "la-la-land"
    assert match["tmdb_id"] == 313369
    # The film's own year, not the screening's.
    assert match["year"] == 2016
    assert match["rating"] == 4.1
    assert match["director"] == "Damien Chazelle"


def test_a_year_qualified_miss_is_retried_without_the_year():
    # A repertory listing's year is the screening's often enough that giving
    # up on the first miss would lose most re-releases.
    calls = []

    def search(title, year):
        calls.append(year)
        return LA_LA_LAND_TMDB if year is None else None

    match = resolve_listing_to_letterboxd(
        "La La Land", 2026, search_movie=search, film_details_by_tmdb_id=_details(LA_LA_LAND_LETTERBOXD))

    assert calls == [2026, None]
    assert match["slug"] == "la-la-land"


def test_event_cinema_does_not_get_matched_to_whatever_tmdb_returns():
    # TMDB answers *something* for almost any string. A concert broadcast
    # has no Letterboxd film, and inventing one would put a wrong poster,
    # rating and link on the card — worse than leaving it plain.
    match = resolve_listing_to_letterboxd(
        "André Rieu's 2026 Summer Concert: Viva Maastricht!", 2026,
        search_movie=_search({"id": 1, "title": "Summer Concert", "release_date": "1999-01-01"}),
        film_details_by_tmdb_id=_details(LA_LA_LAND_LETTERBOXD))

    assert match is None


def test_the_original_title_counts_as_agreement():
    # TMDB localizes titles; a foreign-language film listed under its
    # original name still matches.
    match = resolve_listing_to_letterboxd(
        "La Haine", None,
        search_movie=_search({"id": 406, "title": "Hate", "original_title": "La Haine",
                              "release_date": "1995-05-31"}),
        film_details_by_tmdb_id=_details({**LA_LA_LAND_LETTERBOXD, "slug": "la-haine"}))

    assert match["slug"] == "la-haine"


def test_no_tmdb_match_and_no_letterboxd_page_both_mean_unresolved():
    assert resolve_listing_to_letterboxd(
        "Bing & Friends: Birthday Celebration", 2024,
        search_movie=_search(None), film_details_by_tmdb_id=_details(LA_LA_LAND_LETTERBOXD)) is None

    assert resolve_listing_to_letterboxd(
        "La La Land", None,
        search_movie=_search(LA_LA_LAND_TMDB), film_details_by_tmdb_id=_details(None)) is None


# --- the cache key ------------------------------------------------------

def test_the_same_film_at_two_venues_shares_one_key():
    # Resolving costs two network calls, so the same film showing at three
    # cinemas must not pay for three of them.
    assert listing_match_key("La La Land (10th Anniversary)", None) == listing_match_key("la la land", None)
    assert listing_match_key("The Hunger Games (2012)", None) == listing_match_key("Hunger Games", 2012)


def test_two_different_films_sharing_a_title_do_not_share_a_key():
    assert listing_match_key("Godzilla (1954)", None) != listing_match_key("Godzilla (2014)", None)
