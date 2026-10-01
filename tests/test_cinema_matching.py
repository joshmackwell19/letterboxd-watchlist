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
    showing_match_key,
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
    # Vue's re-release label, with no separator to go on.
    ("Avengers: Endgame Encore", "Avengers: Endgame", None),
    ("Dune: Part Two - IMAX", "Dune: Part Two", None),
    # Stacked annotations unwind together.
    ("Alien (Theatrical Cut) (1979)", "Alien", 1979),
    # An undecorated title is left exactly as it is.
    ("Spider-Man: Brand New Day", "Spider-Man: Brand New Day", None),
])
def test_venue_annotations_are_stripped_and_a_real_year_kept(listing, expected_title, expected_year):
    assert clean_listing_title(listing) == (expected_title, expected_year)


@pytest.mark.parametrize("listing,expected", [
    # An ordinal in front of "Anniversary" — the bracketed form was caught
    # from the start, this one wasn't, and both are equally common.
    ("Amelie - 25th Anniversary", "Amelie"),
    ("The Transformers: The Movie: 40th Anniversary", "The Transformers: The Movie"),
    # Square brackets do the same job as round ones, usually for a language
    # note or the film's original title.
    ("Whisper of the Heart [SUBTITLED]", "Whisper of the Heart"),
    ("Cinema Paradiso [Nuovo Cinema Paradiso]", "Cinema Paradiso"),
    ("The Colour of Pomegranates [Sayat Nova]", "The Colour of Pomegranates"),
    # A strand the venue programmes under, in front of the film.
    ("Relaxed Screening: Ish", "Ish"),
    ("Kids' Club: Hoppers", "Hoppers"),
    ("Parent & Baby Screening: Lady", "Lady"),
    # An event bolted on behind it.
    ("Sense and Sensibility + Recorded Q&A with George Mackay", "Sense and Sensibility"),
    ("If.... + Short Film", "If...."),
    ("Better Class (Altas capacidades) + Q&A", "Better Class"),
    # Both ends at once.
    ("Preschool Pics: The Smeds and the Smoos + Music", "The Smeds and the Smoos"),
])
def test_strand_names_and_bolted_on_events_are_stripped(listing, expected):
    assert clean_listing_title(listing)[0] == expected


def test_a_colon_in_the_films_own_title_is_not_a_strand_name():
    # Only the strands venues actually programme are stripped — a blanket
    # "everything before the colon" would eat half the films showing.
    for title in ["Spider-Man: Brand New Day", "Dune: Part Two", "Star Wars: A New Hope"]:
        assert clean_listing_title(title)[0] == title


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
    """A TMDB search answering one film (or nothing) whatever it's asked."""
    return lambda title, year: [result] if result else []


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
        search_movies=_search(LA_LA_LAND_TMDB), film_details_by_tmdb_id=_details(LA_LA_LAND_LETTERBOXD))

    assert match["slug"] == "la-la-land"
    assert match["tmdb_id"] == 313369
    # The film's own year, not the screening's.
    assert match["year"] == 2016
    assert match["rating"] == 4.1
    assert match["director"] == "Damien Chazelle"


def test_a_listing_already_matched_to_tmdb_skips_the_search():
    # Clusterflick's BFI listings arrive matched: a title no search would
    # agree with ("25th Anniversary: ...") still resolves, by the id.
    match = resolve_listing_to_letterboxd(
        "IMAX exclusive previews: La La Land", 2016, tmdb_id=313369,
        search_movies=lambda *_: pytest.fail("searched"),
        film_details_by_tmdb_id=_details(LA_LA_LAND_LETTERBOXD))

    assert match["slug"] == "la-la-land"
    assert match["tmdb_id"] == 313369
    assert match["year"] == 2016


def test_a_listing_with_a_tmdb_id_is_keyed_by_it():
    showing = {"title": "Ganja and Hess + intro by the curator", "year": 1973, "tmdb_id": 42}
    assert showing_match_key(showing) == showing_match_key({**showing, "title": "Ganja and Hess"}) == "tmdb:42"
    assert showing_match_key({**showing, "tmdb_id": None}) == listing_match_key(showing["title"], 1973)


def test_a_year_qualified_miss_is_retried_without_the_year():
    # A repertory listing's year is the screening's often enough that giving
    # up on the first miss would lose most re-releases.
    calls = []

    def search(title, year):
        calls.append(year)
        return [LA_LA_LAND_TMDB] if year is None else []

    match = resolve_listing_to_letterboxd(
        "La La Land", 2026, search_movies=search, film_details_by_tmdb_id=_details(LA_LA_LAND_LETTERBOXD))

    assert calls == [2026, None]
    assert match["slug"] == "la-la-land"


def test_event_cinema_does_not_get_matched_to_whatever_tmdb_returns():
    # TMDB answers *something* for almost any string. A concert broadcast
    # has no Letterboxd film, and inventing one would put a wrong poster,
    # rating and link on the card — worse than leaving it plain.
    match = resolve_listing_to_letterboxd(
        "André Rieu's 2026 Summer Concert: Viva Maastricht!", 2026,
        search_movies=_search({"id": 1, "title": "Summer Concert", "release_date": "1999-01-01"}),
        film_details_by_tmdb_id=_details(LA_LA_LAND_LETTERBOXD))

    assert match is None


def test_accents_do_not_block_a_match():
    # Venues type "Amelie"; TMDB holds "Amélie". Without folding, the check
    # that stops a concert film matching a real one rejects this too.
    match = resolve_listing_to_letterboxd(
        "Amelie - 25th Anniversary", None,
        search_movies=_search({"id": 194, "title": "Amélie", "release_date": "2001-04-25"}),
        film_details_by_tmdb_id=_details({**LA_LA_LAND_LETTERBOXD, "slug": "amelie"}))

    assert match["slug"] == "amelie"


def test_the_original_title_counts_as_agreement():
    # TMDB localizes titles; a foreign-language film listed under its
    # original name still matches.
    match = resolve_listing_to_letterboxd(
        "La Haine", None,
        search_movies=_search({"id": 406, "title": "Hate", "original_title": "La Haine",
                              "release_date": "1995-05-31"}),
        film_details_by_tmdb_id=_details({**LA_LA_LAND_LETTERBOXD, "slug": "la-haine"}))

    assert match["slug"] == "la-haine"


def test_no_tmdb_match_and_no_letterboxd_page_both_mean_unresolved():
    assert resolve_listing_to_letterboxd(
        "Bing & Friends: Birthday Celebration", 2024,
        search_movies=_search(None), film_details_by_tmdb_id=_details(LA_LA_LAND_LETTERBOXD)) is None

    assert resolve_listing_to_letterboxd(
        "La La Land", None,
        search_movies=_search(LA_LA_LAND_TMDB), film_details_by_tmdb_id=_details(None)) is None


# --- the cache key ------------------------------------------------------

def test_the_same_film_at_two_venues_shares_one_key():
    # Resolving costs two network calls, so the same film showing at three
    # cinemas must not pay for three of them.
    assert listing_match_key("La La Land (10th Anniversary)", None) == listing_match_key("la la land", None)
    assert listing_match_key("The Hunger Games (2012)", None) == listing_match_key("Hunger Games", 2012)


def test_two_different_films_sharing_a_title_do_not_share_a_key():
    assert listing_match_key("Godzilla (1954)", None) != listing_match_key("Godzilla (2014)", None)


# --- how many listings a run resolves -------------------------------------

def test_the_daily_cap_binds_unless_lifted(monkeypatch):
    from watchlist_justwatch import main
    showings = [{"title": f"Film {i}", "year": None, "cinema": "PCC"} for i in range(5)]
    monkeypatch.setattr(main, "_tmdb_search_movies", lambda title, year: [])
    monkeypatch.setattr(main, "_tmdb_movie_facts", lambda tmdb_id: None)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    assert len(main._resolved_cinema_matches(showings, {}, warn=print, budget=2)) == 2
    assert len(main._resolved_cinema_matches(showings, {}, warn=print, budget=None)) == 5


def test_a_failed_lookup_is_not_remembered_as_not_a_film(monkeypatch):
    # Letterboxd 403ing the lookup says nothing about the listing — caching
    # that as "no film" hid real films (Harry Potter, Mulholland Dr.) for a
    # month. It's left out, to be asked again next run.
    from watchlist_justwatch import main
    from watchlist_justwatch.letterboxd import LetterboxdFetchError

    def refused(tmdb_id, raise_on_error=False):
        assert raise_on_error
        raise LetterboxdFetchError("HTTP 403", status_code=403)

    monkeypatch.setattr(main, "_tmdb_search_movies", lambda title, year: [LA_LA_LAND_TMDB])
    monkeypatch.setattr(main, "_tmdb_movie_facts", lambda tmdb_id: None)
    monkeypatch.setattr(main, "get_film_details_by_tmdb_id", refused)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    warnings: list[str] = []
    showings = [{"title": f"La La Land {'!' * i}", "year": None, "cinema": "PCC"} for i in range(8)]

    out = main._resolved_cinema_matches(showings, {}, warn=warnings.append, budget=None)

    assert out == {}
    # Not eight lots of retries: past a handful in a row it's a block.
    assert len(warnings) == main.CINEMA_RESOLVE_FAILURES_BEFORE_STOP + 1
    assert "leaving the rest" in warnings[-1]


def test_and_and_ampersand_agree():
    match = resolve_listing_to_letterboxd(
        "The Hunger Games: The Ballad of Songbirds and Snakes", None,
        search_movies=_search({"id": 695721, "title": "The Hunger Games: The Ballad of Songbirds & Snakes",
                              "release_date": "2023-11-15"}),
        film_details_by_tmdb_id=_details({**LA_LA_LAND_LETTERBOXD, "slug": "the-hunger-games-the-ballad"}))
    assert match is not None


# --- the negative cache must not outlive the rules that produced it ------

def test_a_cleaner_change_invalidates_the_listings_it_failed():
    # A listing that resolved to nothing is cached as "not a film" for a
    # month. Without a version on that verdict, fixing the cleaner wouldn't
    # reach the listings it was written for until the month was up — the
    # fix would be inert exactly where it mattered.
    from watchlist_justwatch import main
    from watchlist_justwatch.cinemas import MATCHER_VERSION

    showings = [{"title": "Amelie - 25th Anniversary", "year": None, "cinema": "PCC"}]
    key = listing_match_key("Amelie - 25th Anniversary", None)
    asked: list[str] = []

    def search(title, year):
        asked.append(title)
        return [{"id": 194, "title": "Amélie", "release_date": "2001-04-25"}]

    original_search, original_facts = main._tmdb_search_movies, main._tmdb_movie_facts
    original_details = main.get_film_details_by_tmdb_id
    main._tmdb_search_movies = search
    main._tmdb_movie_facts = lambda tmdb_id: None
    main.get_film_details_by_tmdb_id = lambda tmdb_id, **_: {
        "slug": "amelie", "rating": 4.2, "poster_url": "p", "director": ["Jean-Pierre Jeunet"],
        "starring": [], "synopsis": "s", "genre": [], "runtime_minutes": 122,
    }
    try:
        stale = {key: {"slug": None, "resolved_at": "2099-01-01", "matcher_version": MATCHER_VERSION - 1}}
        out = main._resolved_cinema_matches(showings, stale, warn=lambda m: None)
        assert asked, "a negative from older rules should be retried"
        assert out[key]["slug"] == "amelie"

        asked.clear()
        current = {key: {"slug": None, "resolved_at": "2099-01-01", "matcher_version": MATCHER_VERSION}}
        main._resolved_cinema_matches(showings, current, warn=lambda m: None)
        assert not asked, "a negative from the current rules should stand"

        asked.clear()
        main._resolved_cinema_matches(showings, {key: out[key]}, warn=lambda m: None)
        assert not asked, "a film already resolved should never be re-asked"
    finally:
        main._tmdb_search_movies, main._tmdb_movie_facts = original_search, original_facts
        main.get_film_details_by_tmdb_id = original_details


# --- a 404 is an answer; anything else is a failure ---------------------

class _Response:
    def __init__(self, status_code, url="https://letterboxd.com/tmdb/1/"):
        self.status_code, self.url, self.text = status_code, url, ""


class _Session:
    def __init__(self, status_code):
        self.status_code, self.calls = status_code, 0

    def get(self, url, **_):
        self.calls += 1
        return _Response(self.status_code)


def test_no_letterboxd_film_is_none_but_a_refusal_raises():
    from watchlist_justwatch.letterboxd import LetterboxdFetchError, get_film_details_by_tmdb_id

    missing = _Session(404)
    assert get_film_details_by_tmdb_id(1, session=missing, raise_on_error=True, backoff_base_seconds=0) is None
    assert missing.calls == 1, "a 404 isn't retried"

    with pytest.raises(LetterboxdFetchError) as exc:
        get_film_details_by_tmdb_id(1, session=_Session(403), raise_on_error=True, backoff_base_seconds=0)
    assert exc.value.status_code == 403
    # Callers that don't remember a None keep the old behaviour.
    assert get_film_details_by_tmdb_id(1, session=_Session(403), backoff_base_seconds=0) is None
