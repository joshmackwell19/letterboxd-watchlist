from datetime import date

from watchlist_justwatch.ranking import (
    full_order, place_it_notifications, plan_roster_sync, ranking_csv, ranking_year,
)


def _entry(watched, rating=4.0, **extra):
    return {"title": "Film", "year": 2001, "poster_url": "p.jpg", "personal_rating": rating,
            "watched_date": watched, "is_rewatch": False, **extra}


def _row(watched, stars=4.0, **extra):
    return {"title": "Film", "film_year": 2001, "poster_url": "p.jpg", "stars": stars,
            "watched_date": watched, "is_rewatch": False, **extra}


def test_january_still_ranks_the_year_before():
    assert ranking_year(date(2026, 11, 10)) == 2026
    assert ranking_year(date(2027, 1, 20)) == 2026
    assert ranking_year(date(2027, 2, 1)) == 2027


def test_first_sync_seeds_the_year_quietly():
    diary = {"a": _entry("2026-03-01"), "old": _entry("2025-12-31"), "undated": _entry(None)}
    plan = plan_roster_sync(diary, {}, 2026)
    assert [row["slug"] for row in plan.inserts] == ["a"]
    assert plan.inserts[0]["queue_first"] is False
    assert plan.inserts[0]["notified"] is True
    assert not plan.reopened


def test_a_new_log_is_queued_first_and_notified():
    diary = {"a": _entry("2026-03-01"), "b": _entry("2026-11-09", rating=None)}
    plan = plan_roster_sync(diary, {"a": _row("2026-03-01")}, 2026)
    assert [row["slug"] for row in plan.inserts] == ["b"]
    assert plan.inserts[0]["queue_first"] is True
    assert plan.inserts[0]["notified"] is False
    assert plan.inserts[0]["stars"] is None
    assert not plan.updates


def test_a_rewatch_reopens_the_film_with_its_new_rating():
    diary = {"a": _entry("2026-09-18", rating=4.5, is_rewatch=True)}
    plan = plan_roster_sync(diary, {"a": _row("2026-01-17", stars=3.5)}, 2026)
    assert plan.reopened == ["a"]
    assert plan.updates == [{"slug": "a", "stars": 4.5, "watched_date": "2026-09-18",
                             "is_rewatch": True, "queue_first": True, "notified": False}]


def test_a_rerating_alone_changes_the_stars_without_reopening():
    plan = plan_roster_sync({"a": _entry("2026-03-01", rating=5.0)}, {"a": _row("2026-03-01")}, 2026)
    assert plan.updates == [{"slug": "a", "stars": 5.0}]
    assert not plan.reopened


def test_a_rewatch_next_year_leaves_this_years_entry_alone():
    plan = plan_roster_sync({"a": _entry("2027-01-05", rating=2.0, is_rewatch=True)},
                            {"a": _row("2026-03-01")}, 2026)
    assert not plan


def test_a_film_that_left_the_diary_stays_on_the_roster():
    plan = plan_roster_sync({}, {"a": _row("2026-03-01")}, 2026)
    assert not plan


def test_a_poster_fetched_later_is_copied_across():
    plan = plan_roster_sync({"a": _entry("2026-03-01", poster_url="new.jpg")},
                            {"a": _row("2026-03-01", poster_url=None)}, 2026)
    assert plan.updates == [{"slug": "a", "poster_url": "new.jpg"}]


ROSTER = [
    {"slug": "a", "title": "A", "film_year": 2001, "stars": 5.0, "watched_date": "2026-01-01"},
    {"slug": "b", "title": "B, the Sequel", "film_year": 2002, "stars": 4.0, "watched_date": "2026-01-02"},
    {"slug": "c", "title": "C", "film_year": None, "stars": 3.0, "watched_date": "2026-01-03"},
    {"slug": "wait", "title": "W", "film_year": 2004, "stars": 4.5, "watched_date": "2026-01-04"},
    {"slug": "unrated", "title": "U", "film_year": 2005, "stars": None, "watched_date": "2026-01-05"},
]


def test_full_order_slots_waiting_films_at_the_bottom_of_their_stars():
    assert full_order(ROSTER, ["a", "b", "c", "gone"]) == ["a", "wait", "b", "unrated", "c"]


def test_csv_is_in_list_order_with_each_films_url():
    assert ranking_csv(ROSTER[:3], ["b", "a", "c"]).splitlines()[:3] == [
        "Title,Year,LetterboxdURI",
        '"B, the Sequel",2002,https://letterboxd.com/film/b/',
        "A,2001,https://letterboxd.com/film/a/",
    ]


def test_several_new_films_go_out_as_one_notification():
    films = [{"slug": f"f{i}", "title": f"Film {i}", "is_rewatch": False} for i in range(4)]
    notifications = place_it_notifications(films, 2026, "https://example.test/")
    assert len(notifications) == 1
    assert notifications[0]["url"] == "https://example.test/?view=rank"
    assert len(place_it_notifications(films[:2], 2026, "https://example.test/")) == 2
