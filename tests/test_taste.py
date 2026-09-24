import pytest

from tests.letterboxd_pages import grid_page
from watchlist_justwatch.taste import (
    PoliteFetcher,
    RequestBudgetExhausted,
    TasteParams,
    choose_screening_films,
    kfold,
    my_offset,
    my_residuals,
    recent_holdout,
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
