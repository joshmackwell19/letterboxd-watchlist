from datetime import datetime, timedelta, timezone

import pytest

from tests.letterboxd_pages import film_page, grid_page
from watchlist_justwatch.letterboxd import LetterboxdBlockedError
from watchlist_justwatch.taste import (
    FilmKindLookup,
    PoliteFetcher,
    RequestBudgetExhausted,
    TasteParams,
    choose_screening_films,
    films_only,
    kfold,
    letterboxd_offset,
    letterboxd_residuals,
    my_offset,
    my_residuals,
    recent_holdout,
    scrape_looks_active,
    scrape_profile,
    screen_hits,
    select_neighbours,
    spearman,
    stars_path,
    top_share_mean,
)


@pytest.mark.parametrize("rating,path", [(5.0, "5"), (4.5, "4.5"), (1.0, "1"), (0.5, ".5"), (2.5, "2.5")])
def test_stars_path_matches_letterboxd_urls(rating, path):
    assert stars_path(rating) == path


def test_screening_prefers_ratings_furthest_from_consensus_either_way():
    mine = {"loved-underrated": 5.0, "agreed": 4.0, "hated-classic": 1.5, "no-average": 5.0}
    community = {"loved-underrated": 3.1, "agreed": 3.9, "hated-classic": 4.4}
    chosen = choose_screening_films(mine, community, limit=4)
    assert [slug for slug, _ in chosen] == ["hated-classic", "loved-underrated", "no-average", "agreed"]
    assert chosen[0] == ("hated-classic", 1.5)


def test_screening_respects_limit():
    assert len(choose_screening_films({f"f{i}": 3.0 for i in range(10)}, {}, limit=3)) == 3


def test_screen_hits_skips_self_and_mismatched_ratings():
    members = [("Josh", 9), ("alice", 9), ("bob", 8), ("carol", 9)]
    assert screen_hits(members, 9, {"josh"}) == {"alice": 1, "carol": 1}


def test_select_neighbours_shrinks_small_overlaps():
    params = TasteParams(neighbours=2, lambda_sim=25)
    sims = [(1, "lucky", 8, 0.9), (2, "steady", 400, 0.6), (3, "middling", 100, 0.5)]
    chosen = select_neighbours(sims, params)
    assert [n.username for n in chosen] == ["steady", "middling"]
    assert chosen[0].weight == pytest.approx(0.6 * 400 / 425)


def test_my_offset_and_residuals_line_up_with_film_offsets():
    ratings = {"a": 4.0, "b": 3.0, "unknown": 5.0}
    film_info = {"a": (10, 0.5, 30, "A"), "b": (11, -0.5, 30, "B")}
    offset = my_offset(ratings, {"a": 0.5, "b": -0.5}, mu=3.0)
    # (0.5 + 0.5 + 2.0) / (3 + LAMBDA_RATER=10)
    assert offset == pytest.approx(3.0 / 13)
    ids, residuals = my_residuals(ratings, film_info, mu=3.0, offset=offset)
    assert ids == [10, 11]
    assert residuals == pytest.approx([0.5 - offset, 0.5 - offset])


def test_letterboxd_residuals_skip_films_without_an_average_or_outside_the_corpus():
    ratings = {"a": 4.0, "b": 3.0, "no-average": 5.0, "not-in-corpus": 2.0}
    community = {"a": 3.5, "b": 3.5, "not-in-corpus": 3.0}
    film_info = {"a": (10, 0.2, 5, "A"), "b": (11, -0.2, 5, "B"), "no-average": (12, 0.0, 5, "N")}
    offset = letterboxd_offset(ratings, community)
    # (0.5 - 0.5 - 1.0) / 3, unshrunk, and not-in-corpus still counts toward it
    assert offset == pytest.approx(-1 / 3)
    ids, residuals, means = letterboxd_residuals(ratings, film_info, community, offset)
    assert ids == [10, 11]
    assert residuals == pytest.approx([0.5 + 1 / 3, -0.5 + 1 / 3])
    assert means == [3.5, 3.5]


def test_letterboxd_offset_without_any_averages():
    assert letterboxd_offset({"a": 4.0}, {}) == 0.0


def test_params_label_names_the_baseline():
    assert TasteParams().label == "Taste twins (100 nbrs, damping 1)"
    assert TasteParams(baseline="letterboxd", neighbours=30, lambda_pred=0.5).label == \
        "Letterboxd + twins (30 nbrs, damping 0.5)"


def test_kfold_partitions_deterministically():
    slugs = [f"f{i}" for i in range(23)]
    folds = kfold(slugs, 5, seed=0)
    assert set().union(*folds) == set(slugs)
    assert sum(len(f) for f in folds) == 23
    assert folds == kfold(list(reversed(slugs)), 5, seed=0)


def test_recent_holdout_takes_the_latest_dated_films():
    slugs = [f"f{i:02d}" for i in range(40)]
    dates = {s: f"2025-01-{i + 1:02d}" if i < 28 else None for i, s in enumerate(slugs)}
    held = recent_holdout(slugs, dates, fraction=0.15)
    assert held == {"f24", "f25", "f26", "f27"}


def test_recent_holdout_needs_enough_dates():
    assert recent_holdout(["a", "b"], {"a": "2025-01-01", "b": "2025-02-01"}) == set()


def test_spearman():
    assert spearman([(1, 10), (2, 20), (3, 30)]) == pytest.approx(1.0)
    assert spearman([(1, 30), (2, 20), (3, 10)]) == pytest.approx(-1.0)
    assert spearman([(3.5, 1), (3.5, 2), (3.5, 3)]) is None


def test_top_share_mean_scores_what_ranked_highest():
    pairs = [(4.9, 5.0), (4.8, 4.5)] + [(3.0, 2.0)] * 18
    assert top_share_mean(pairs, fraction=0.1) == pytest.approx(4.75)


def test_polite_fetcher_pauses_between_requests_and_caps_them():
    sleeps = []
    fetcher = PoliteFetcher(delay_seconds=2.0, max_requests=2, fetch=lambda url: url,
                            sleep=sleeps.append, rand=lambda: 0.5)
    assert fetcher.get("a") == "a"
    assert sleeps == []
    fetcher.get("b")
    assert sleeps == [pytest.approx(2.8)]
    with pytest.raises(RequestBudgetExhausted):
        fetcher.get("c")


def test_polite_fetcher_takes_a_longer_break_every_hundred():
    sleeps = []
    fetcher = PoliteFetcher(delay_seconds=1.0, max_requests=200, fetch=lambda url: url,
                            sleep=sleeps.append, rand=lambda: 0.0)
    for _ in range(101):
        fetcher.get("x")
    assert sleeps[99] == pytest.approx(31.0)
    assert sleeps[98] == pytest.approx(1.0)


def _fetcher(pages: dict[str, str | None]) -> PoliteFetcher:
    return PoliteFetcher(delay_seconds=0, max_requests=100, fetch=lambda url: pages[url], sleep=lambda s: None)


def test_scrape_profile_pages_until_the_last():
    base = "https://letterboxd.com/alice/films/page/"
    fetcher = _fetcher({
        f"{base}1/": grid_page([("a", "A", 8), ("b", "B", None)], next_href="/alice/films/page/2/"),
        f"{base}2/": grid_page([("c", "C", 3)]),
    })
    assert scrape_profile(fetcher, "alice") == [("a", "A", 8), ("c", "C", 3)]
    assert fetcher.requests == 2


def test_scrape_profile_respects_the_page_cap():
    base = "https://letterboxd.com/alice/films/page/"
    fetcher = _fetcher({f"{base}{i}/": grid_page([(f"f{i}", "F", 6)], next_href="more") for i in range(1, 5)})
    assert len(scrape_profile(fetcher, "alice", max_pages=3)) == 3


def test_scrape_profile_missing_member_and_non_rater():
    assert scrape_profile(_fetcher({"https://letterboxd.com/gone/films/page/1/": None}), "gone") is None
    silent = _fetcher({"https://letterboxd.com/quiet/films/page/1/": grid_page([("a", "A", None)], next_href="x")})
    assert scrape_profile(silent, "quiet") == []
    assert silent.requests == 1


def _pick(film_id: int, slug: str, kind: str | None = None) -> dict:
    return {"film_id": film_id, "slug": slug, "tmdb_kind": kind}


def test_films_only_skips_tv_and_stops_asking_once_there_are_enough():
    rows = [_pick(1, "a"), _pick(2, "show"), _pick(3, "b"), _pick(4, "deleted"), _pick(5, "c"), _pick(6, "d")]
    kinds = {"a": "movie", "show": "tv", "b": None, "deleted": "gone", "c": "movie", "d": "movie"}
    asked = []

    def kind_of(row):
        asked.append(row["slug"])
        return kinds[row["slug"]]

    kept, skipped = films_only(rows, 3, kind_of)
    # an unchecked film is kept rather than guessed at; one Letterboxd has
    # dropped is left out without counting as TV
    assert [row["slug"] for row in kept] == ["a", "b", "c"]
    assert skipped == 1
    assert asked == ["a", "show", "b", "deleted", "c"]


def test_scrape_looks_active_within_the_window_only():
    now = datetime(2026, 9, 24, 22, 0, tzinfo=timezone.utc)
    assert scrape_looks_active((now - timedelta(minutes=3)).isoformat(), now)
    assert not scrape_looks_active((now - timedelta(minutes=30)).isoformat(), now)
    assert not scrape_looks_active(None, now)


LB_FILM = "https://letterboxd.com/film"


def _lookup(pages: dict, **kwargs) -> FilmKindLookup:
    def fetch(url):
        page = pages[url]
        if isinstance(page, Exception):
            raise page
        return page
    kwargs.setdefault("log", lambda msg: None)
    return FilmKindLookup(PoliteFetcher(delay_seconds=0, max_requests=100, fetch=fetch, sleep=lambda s: None),
                          **kwargs)


def test_film_kind_lookup_uses_what_is_stored_and_saves_each_answer_as_it_goes():
    saved = []
    lookup = _lookup({f"{LB_FILM}/loki/": film_page("tv"), f"{LB_FILM}/heat/": film_page("movie"),
                      f"{LB_FILM}/deleted/": None}, save=lambda film_id, kind: saved.append((film_id, kind)))
    assert lookup(_pick(1, "stored", "movie")) == "movie"
    assert lookup(_pick(2, "loki")) == "tv"
    assert saved == [(2, "tv")]
    assert lookup(_pick(2, "loki")) == "tv"
    assert lookup(_pick(3, "heat")) == "movie"
    assert lookup(_pick(4, "deleted")) == "gone"
    assert saved == [(2, "tv"), (3, "movie"), (4, "gone")]
    assert lookup.found == dict(saved)
    assert lookup.fetcher.requests == 3


def test_film_kind_lookup_treats_a_page_without_a_tmdb_link_as_its_own_failure():
    saved, logged = [], []
    lookup = _lookup({f"{LB_FILM}/odd/": film_page(None), f"{LB_FILM}/heat/": film_page("movie")},
                     save=lambda film_id, kind: saved.append(kind), log=logged.append)
    assert lookup(_pick(1, "odd")) is None
    assert lookup(_pick(2, "heat")) is None
    assert lookup.stopped == "failed"
    assert saved == [] and lookup.found == {}
    assert lookup.fetcher.requests == 1
    assert "markup" in logged[0]


def test_film_kind_lookup_records_a_block_the_moment_it_happens():
    blocks, logged = [], []
    lookup = _lookup({f"{LB_FILM}/a/": LetterboxdBlockedError("HTTP 429"), f"{LB_FILM}/b/": film_page("tv")},
                     on_block=lambda: blocks.append(1), log=logged.append)
    assert lookup(_pick(1, "a")) is None
    assert blocks == [1]
    assert lookup(_pick(2, "b")) is None
    assert lookup.stopped == "blocked"
    assert lookup.fetcher.requests == 1
    assert lookup.found == {}
    assert "refusing" in logged[0]


def test_film_kind_lookup_stops_when_another_run_has_been_blocked():
    clear = iter([True, False])
    lookup = _lookup({f"{LB_FILM}/a/": film_page("movie"), f"{LB_FILM}/b/": film_page("tv")},
                     still_clear=lambda: next(clear))
    assert lookup(_pick(1, "a")) == "movie"
    assert lookup(_pick(2, "b")) is None
    assert lookup.stopped == "cooldown"
    assert lookup.fetcher.requests == 1


def test_film_kind_lookup_without_a_fetcher_checks_nothing():
    lookup = FilmKindLookup(None, lambda msg: None)
    assert lookup(_pick(1, "a")) is None
    assert lookup(_pick(2, "b", "tv")) == "tv"
