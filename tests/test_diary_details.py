from watchlist_justwatch.main import _diary_entry_needs_details, _with_film_details

DETAILS = {
    "rating": 3.9, "rating_count": 12000, "poster_url": "https://a.ltrbxd.com/poster.jpg",
    "director": ["Céline Sciamma"], "starring": ["Noémie Merlant", "Adèle Haenel"],
    "synopsis": "On an isolated island in Brittany...", "genre": ["Drama", "Romance"], "runtime_minutes": 122,
}
EMPTY_DETAILS = {
    "rating": None, "rating_count": None, "poster_url": None, "director": [], "starring": [],
    "synopsis": None, "genre": [], "runtime_minutes": None,
}


def _no_language_call(title, year):
    raise AssertionError("fetch_language shouldn't be called when the entry already has one")


def _hourly_entry():
    # Exactly what --check-for-new-log writes for a film it reaches first.
    return {
        "title": "Portrait of a Lady on Fire", "year": 2019, "rating": None,
        "poster_url": None, "director": None, "starring": [], "synopsis": None,
        "personal_rating": 4.5, "liked": True, "is_rewatch": False, "watched_date": "2026-09-20",
    }


def test_hourly_check_entry_needs_details():
    assert _diary_entry_needs_details(_hourly_entry())


def test_missing_entry_needs_details():
    assert _diary_entry_needs_details(None)


def test_enriched_entry_does_not_need_details():
    entry = _with_film_details(_hourly_entry(), "Portrait of a Lady on Fire", 2019, DETAILS, lambda t, y: "fr")
    assert not _diary_entry_needs_details(entry)


def test_entry_with_poster_but_no_average_still_needs_details():
    assert _diary_entry_needs_details({"rating": None, "poster_url": "https://a.ltrbxd.com/poster.jpg"})


def test_merge_fills_film_page_fields_into_hourly_entry():
    entry = _with_film_details(_hourly_entry(), "Portrait of a Lady on Fire", 2019, DETAILS, lambda t, y: "fr")
    assert entry["rating"] == 3.9
    assert entry["poster_url"] == "https://a.ltrbxd.com/poster.jpg"
    assert entry["director"] == "Céline Sciamma"
    assert entry["starring"] == ["Noémie Merlant", "Adèle Haenel"]
    assert entry["synopsis"] == "On an isolated island in Brittany..."
    assert entry["genre"] == ["Drama", "Romance"]
    assert entry["original_language"] == "fr"


def test_merge_keeps_what_the_hourly_check_recorded():
    entry = _with_film_details(_hourly_entry(), "Portrait of a Lady on Fire", 2019, DETAILS, lambda t, y: "fr")
    assert entry["personal_rating"] == 4.5
    assert entry["liked"] is True
    assert entry["is_rewatch"] is False
    assert entry["watched_date"] == "2026-09-20"
    assert entry["title"] == "Portrait of a Lady on Fire"
    assert entry["year"] == 2019


def test_merge_does_not_mutate_the_original_entry():
    original = _hourly_entry()
    _with_film_details(original, "Portrait of a Lady on Fire", 2019, DETAILS, lambda t, y: "fr")
    assert original == _hourly_entry()


def test_failed_fetch_never_erases_existing_values():
    # get_film_details_by_slug returns all-empty rather than raising.
    existing = {**_hourly_entry(), "poster_url": "https://a.ltrbxd.com/poster.jpg", "director": "Céline Sciamma",
                "starring": ["Noémie Merlant"], "genre": ["Drama"], "original_language": "fr"}
    entry = _with_film_details(existing, "Portrait of a Lady on Fire", 2019, EMPTY_DETAILS, _no_language_call)
    assert entry == existing


def test_failed_fetch_still_gives_a_new_entry_every_field():
    entry = _with_film_details(None, "Portrait of a Lady on Fire", 2019, EMPTY_DETAILS, lambda t, y: None)
    assert entry == {
        "title": "Portrait of a Lady on Fire", "year": 2019, "rating": None, "poster_url": None,
        "director": None, "starring": [], "synopsis": None, "genre": [], "original_language": None,
    }


def test_new_entry_takes_title_and_year_and_asks_for_language():
    calls = []

    def fetch_language(title, year):
        calls.append((title, year))
        return "fr"

    entry = _with_film_details(None, "Portrait of a Lady on Fire", 2019, DETAILS, fetch_language)
    assert calls == [("Portrait of a Lady on Fire", 2019)]
    assert entry["title"] == "Portrait of a Lady on Fire"
    assert entry["year"] == 2019
    assert entry["original_language"] == "fr"
    assert "personal_rating" not in entry


def test_known_language_skips_the_tmdb_search():
    existing = {**_hourly_entry(), "original_language": "fr"}
    entry = _with_film_details(existing, "Portrait of a Lady on Fire", 2019, DETAILS, _no_language_call)
    assert entry["original_language"] == "fr"


def test_runtime_and_rating_count_are_not_copied_into_the_diary():
    # The diary never stored them; adding them here would grow every row.
    entry = _with_film_details(None, "Portrait of a Lady on Fire", 2019, DETAILS, lambda t, y: "fr")
    assert "runtime_minutes" not in entry
    assert "rating_count" not in entry
