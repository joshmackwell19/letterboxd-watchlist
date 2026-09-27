from watchlist_justwatch.db import _compact_showtimes, _expand_showtimes


def _showing(cinema, title, showtime, poster="https://example.com/p.jpg", synopsis="A film."):
    return {"cinema": cinema, "title": title, "year": None, "showtime": showtime, "duration_minutes": 100,
            "director": None, "synopsis": synopsis, "poster_url": poster, "booking_url": f"https://book/{showtime}"}


def test_film_details_are_stored_once_per_venue_and_film_and_restored_on_load():
    showings = [
        _showing("Vue West End", "Pressure", "2026-10-02T18:00:00"),
        _showing("Vue West End", "Pressure", "2026-10-02T21:00:00"),
        _showing("Vue Piccadilly", "Pressure", "2026-10-02T19:00:00", poster="https://example.com/q.jpg"),
        _showing("Vue West End", "Digger", "2026-10-02T20:00:00", poster=None, synopsis=None),
    ]
    stored = _compact_showtimes(showings)
    assert [s["poster_url"] for s in stored] == [
        "https://example.com/p.jpg", None, "https://example.com/q.jpg", None]
    # Everything that differs per showing is untouched.
    assert [s["booking_url"] for s in stored] == [s["booking_url"] for s in showings]
    # Loaded in any order, every showing gets its film's details back.
    assert sorted(_expand_showtimes(list(reversed(stored))), key=lambda s: s["booking_url"]) == \
        sorted(showings, key=lambda s: s["booking_url"])
