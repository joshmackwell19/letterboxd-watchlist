import pytest

from tests.letterboxd_pages import CHALLENGE_PAGE, film_page, following_page, grid_page, members_page
from watchlist_justwatch.letterboxd import (
    LetterboxdBlockedError,
    LetterboxdFetchError,
    fetch_page_strict,
    parse_following_page,
    parse_member_ratings_page,
    parse_rated_films_page,
    parse_tmdb_kind,
    parse_tmdb_link,
)


def test_grid_page_keeps_rated_films_only():
    html = grid_page([
        ("heat-1995", "Heat (1995)", 9),
        ("cats-2019", "Cats (2019)", None),
        ("in-the-mood-for-love", "In the Mood for Love (2000)", 10),
    ], next_href="/someone/films/page/2/")
    rated, has_next = parse_rated_films_page(html)
    assert rated == [("heat-1995", "Heat (1995)", 9), ("in-the-mood-for-love", "In the Mood for Love (2000)", 10)]
    assert has_next is True


def test_grid_page_unescapes_names_and_reads_last_page():
    rated, has_next = parse_rated_films_page(grid_page([("amelie", "Am&#039;lie &amp; co (2001)", 7)]))
    assert rated == [("amelie", "Am'lie & co (2001)", 7)]
    assert has_next is False


def test_members_page():
    members, has_next = parse_member_ratings_page(
        members_page([("alice", 9), ("bob_2", 9)], next_href="/film/x/members/rated/4.5/page/2/"))
    assert members == [("alice", 9), ("bob_2", 9)]
    assert has_next is True


def test_following_page_reads_watched_counts():
    people, has_next = parse_following_page(following_page([("alice", 1469), ("bob", 12)]))
    assert people == [("alice", 1469), ("bob", 12)]
    assert has_next is False


class _Response:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _Session:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.parametrize("status", [403, 429, 503])
def test_strict_fetch_stops_on_a_block_without_retrying(status):
    session = _Session(_Response(status), _Response(200, "fine"))
    with pytest.raises(LetterboxdBlockedError):
        fetch_page_strict(session, "https://letterboxd.com/x/films/", sleep=lambda s: None)
    assert session.calls == 1


def test_strict_fetch_treats_a_challenge_page_as_a_block_even_on_200():
    with pytest.raises(LetterboxdBlockedError):
        fetch_page_strict(_Session(_Response(200, CHALLENGE_PAGE)), "u", sleep=lambda s: None)


@pytest.mark.parametrize("kind", ["movie", "tv"])
def test_tmdb_kind_comes_from_the_tmdb_button_not_the_body_attribute(kind):
    assert parse_tmdb_kind(film_page(kind)) == kind


def test_tmdb_link_carries_the_id_too():
    assert parse_tmdb_link(film_page("tv")) == ("tv", 84958)
    assert parse_tmdb_link(film_page(None)) == (None, None)


def test_tmdb_kind_without_a_tmdb_button():
    assert parse_tmdb_kind(film_page(None)) is None


def test_strict_fetch_404_is_none():
    assert fetch_page_strict(_Session(_Response(404)), "u", sleep=lambda s: None) is None


def test_strict_fetch_retries_network_errors_then_succeeds():
    sleeps = []
    session = _Session(ConnectionError("reset"), _Response(502), _Response(200, "ok"))
    assert fetch_page_strict(session, "u", sleep=sleeps.append) == "ok"
    assert session.calls == 3
    assert len(sleeps) == 2


def test_strict_fetch_gives_up_on_persistent_network_errors():
    session = _Session(ConnectionError("a"), ConnectionError("b"), ConnectionError("c"))
    with pytest.raises(LetterboxdFetchError) as info:
        fetch_page_strict(session, "u", sleep=lambda s: None)
    assert not isinstance(info.value, LetterboxdBlockedError)
