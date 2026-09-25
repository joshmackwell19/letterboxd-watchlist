from datetime import datetime, timedelta, timezone

import pytest

from tests.letterboxd_pages import film_page, grid_page
from watchlist_justwatch.letterboxd import LetterboxdBlockedError
from watchlist_justwatch.taste import (
    FilmKindLookup,
    Recruitment,
    build_recruitment,
    PoliteFetcher,
    RequestBudgetExhausted,
    TasteParams,
    choose_screening_films,
    films_only,
    first_found_pages,
    followed_rater_ids,
    kfold,
    letterboxd_offset,
    letterboxd_residuals,
    my_offset,
    my_residuals,
    paired_bootstrap,
    recent_holdout,
    regressed_toward_mean,
    reconcile_recruits,
    scrape_looks_active,
    scrape_profile,
    screen_hits,
    select_neighbours,
    spearman,
    stars_path,
    stratified_folds,
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


def _at(minute: int, second: float = 0.0) -> str:
    return (datetime(2026, 9, 24, 11, 0, tzinfo=timezone.utc) + timedelta(minutes=minute, seconds=second)).isoformat()


# Josh's follows are seeded at 11:00; page a is screened at 11:01 (finding
# raters 2 and 3, inserted a moment before it's marked), page b at 11:02
# (finding 4). Rater 5 turned up after the last page and belongs to none.
TIMELINE = [("a", 1.0, 1, _at(1, 0.01), None), ("b", 4.5, 1, _at(2, 0.01), None)]
RATERS = [(1, "scraped", 3, _at(0), _at(3)), (2, "scraped", 2, _at(1), _at(3)),
          (3, "candidate", 1, _at(1), None), (4, "scraped", 1, _at(2), _at(3)),
          (5, "candidate", 1, _at(9), None)]


def test_followed_raters_are_the_ones_seeded_with_the_follows():
    # page a's own recruits (2, 3) were inserted before page a was marked,
    # but after following_seeded_at
    assert followed_rater_ids(RATERS, _at(0, 0.02)) == {1}
    assert followed_rater_ids(RATERS, None) == set()


def test_first_found_page_is_the_first_marked_at_or_after_discovery():
    assert first_found_pages(RATERS, TIMELINE, {1}) == {2: ("a", 1.0, 1), 3: ("a", 1.0, 1), 4: ("b", 4.5, 1)}


def test_reconcile_recruits_infers_the_pages_a_short_rater_could_have_been_on():
    pages = {("a", 1.0, 1): datetime.fromisoformat(TIMELINE[0][3]),
             ("b", 4.5, 1): datetime.fromisoformat(TIMELINE[1][3])}
    selected = [(1, 3, _at(0)), (2, 2, _at(1)), (4, 1, _at(2))]
    recorded = {1: {("a", 1.0, 1)}, 2: {("a", 1.0, 1)}, 4: {("b", 4.5, 1), ("a", 1.0, 1)}}
    # rater 2 gave page b's score to its film, but a was fetched before they
    # were discovered, so only b can be the page they're missing
    same_score = {2: {("a", 1.0, 1), ("b", 4.5, 1)}}
    status, inferred = reconcile_recruits(selected, {1}, recorded, same_score, pages)
    # 1: followed (2) + page a = 3 hits, exact. 2: two hits, one recorded.
    # 4: one hit, two recorded (a re-fetch saw them on a).
    assert status == {1: "exact", 2: "missing", 4: "extra"}
    assert inferred == [(("b", 4.5, 1), 2)]


def test_reconnecting_database_retries_on_a_fresh_connection_then_gives_up(monkeypatch):
    import psycopg
    from watchlist_justwatch import taste as taste_module

    opened = []

    class _Conn:
        def __init__(self):
            opened.append(self)

        def close(self):
            pass

    monkeypatch.setattr(taste_module.db, "connect", lambda url: _Conn())
    logged = []
    database = taste_module._ReconnectingDatabase("postgres://x", logged.append, attempts=3,
                                                 sleep=lambda seconds: None)
    calls = []

    def flaky(conn):
        calls.append(conn)
        if len(calls) == 1:
            raise psycopg.OperationalError("consuming input failed: could not receive data from server")
        return "ok"

    assert database(flaky) == "ok"
    assert calls == opened[:2] and len(opened) == 2
    assert "reconnecting" in logged[0]

    def dead(conn):
        raise psycopg.OperationalError("gone")

    with pytest.raises(psycopg.OperationalError):
        database(dead)
    assert len(opened) == 4



def _recruitment(**overrides) -> Recruitment:
    fields = dict(
        screened={"a", "b"}, unrecorded=set(),
        recruits={"a": {10, 11, 12}, "b": {12}},
        pages_of_slug={"a": {("a", 1.0, 1)}, "b": {("b", 4.5, 1)}},
        counted={10: {("a", 1.0, 1)}, 11: {("a", 1.0, 1), ("b", 4.5, 1), ("c", 2.0, 1), ("d", 5.0, 1)},
                 12: {("a", 1.0, 1), ("b", 4.5, 1)}},
        unsure=set(),
        # run 0 took raters down to 2 hits, id 20: 10 had 2 (1 page + nothing), 11 had 4, 12 had 2
        selection={10: (2, 0), 11: (4, 0), 12: (2, 0)},
        cutoffs={0: (2, 20)},
    )
    fields.update(overrides)
    return Recruitment(**fields)


def test_counterfactual_keeps_recruits_who_would_have_been_scraped_anyway():
    r = _recruitment()
    # without a's page: 10 falls to 1 hit (below the cut), 11 to 3 (still in),
    # 12 to 1 (out)
    assert r.exclusions("a", "counterfactual") == {10, 12}
    assert r.exclusions("a", "all") == {10, 11, 12}
    assert r.exclusions("a", "none") == set()
    # at the cut itself, the id tie-break decides: 2 hits and an id below 20 is in
    assert r.kept_without(11, {("a", 1.0, 1), ("b", 4.5, 1)})
    assert not r.kept_without(11, {("a", 1.0, 1), ("b", 4.5, 1), ("c", 2.0, 1)})


def test_counterfactual_never_keeps_a_rater_whose_count_when_queued_is_unknown():
    r = _recruitment(unsure={11}, selection={10: (2, 0), 11: (None, 0), 12: (2, 0)})
    assert r.exclusions("a", "counterfactual") == {10, 11, 12}


def test_counterfactual_treats_every_page_an_unsure_rater_may_have_been_on_as_lost():
    # 11's pages don't reconcile, but their count when queued is known (4):
    # losing a's page alone still leaves 3, above the cut; losing the three
    # pages they may have been on leaves 1, below it
    r = _recruitment(unsure={11})
    assert r.kept_without(11, {("a", 1.0, 1)})
    assert not r.kept_without(11, {("a", 1.0, 1), ("b", 4.5, 1), ("c", 2.0, 1)})


def test_time_split_drops_recruits_of_later_films_from_the_corpus():
    r = _recruitment()
    assert r.dropped_without({"a", "b"}, "counterfactual") == {10, 12}
    assert r.dropped_without({"a", "b"}, "all") == {10, 11, 12}
    assert r.dropped_without(set(), "counterfactual") == set()


def test_build_recruitment_replays_each_run_and_its_cutoff():
    timeline = [("a", 1.0, 1, _at(1), _at(9)), ("b", 4.5, 1, _at(5), _at(9))]
    hits = [("a", 1.0, 1, 2, "first-found"), ("a", 1.0, 1, 3, "first-found"), ("b", 4.5, 1, 3, "refetch"),
            ("b", 4.5, 1, 4, "first-found"), ("b", 4.5, 1, 2, "inferred")]
    raters = [(1, "scraped", 2, _at(0), _at(2)),          # followed, taken in run 1 (after page a)
              (2, "scraped", 1, _at(1, -0.1), _at(2)),    # page a, run 1
              (3, "scraped", 2, _at(1, -0.1), _at(6)),    # page a for certain, b only from a re-fetch; run 2
              (4, "candidate", 1, _at(5, -0.1), None)]
    r = build_recruitment(timeline, hits, raters, {1}, same_score=[])
    # 2's one hit is its certain page, so the inferred b is ignored; 3's
    # certain page covers one of two hits, so the re-fetched b counts too
    assert r.recruits == {"a": {2, 3}, "b": {3}}
    assert r.unsure == {3}
    # 3 is queued after every page, so its screen_hits is its count then
    assert r.selection == {1: (2, 1), 2: (1, 1), 3: (2, 2)}
    assert r.cutoffs == {1: (1, 2), 2: (2, 3)}
    assert r.counted[3] == {("a", 1.0, 1), ("b", 4.5, 1)}
    assert r.unrecorded == set()


def test_build_recruitment_counts_same_score_films_for_a_rater_short_of_certain_pages():
    timeline = [("a", 1.0, 1, _at(1), _at(9)), ("b", 4.5, 1, _at(5), _at(9)), ("c", 2.0, 1, _at(7), _at(9))]
    hits = [("a", 1.0, 1, 3, "first-found")]
    raters = [(3, "scraped", 2, _at(1, -0.1), _at(8))]
    # besides their certain page a, they rated b's film at b's score after
    # being found — so b is the page they may have been recruited through
    same_score = [("b", 4.5, 1, 3), ("a", 1.0, 1, 3)]
    r = build_recruitment(timeline, hits, raters, set(), same_score)
    assert r.recruits == {"a": {3}, "b": {3}}
    assert r.counted[3] == {("a", 1.0, 1), ("b", 4.5, 1)}


def test_build_recruitment_uses_screen_hits_for_an_unsure_rater_queued_after_every_page():
    timeline = [("a", 1.0, 1, _at(1), _at(9)), ("b", 4.5, 1, _at(5), _at(9))]
    # rater 3 has 2 hits but only page a was seen again (b turned over);
    # b is inferred from their rating
    hits = [("a", 1.0, 1, 3, "first-found"), ("b", 4.5, 1, 3, "inferred"),
            ("a", 1.0, 1, 5, "first-found"), ("b", 4.5, 1, 5, "inferred")]
    raters = [(3, "scraped", 2, _at(1, -0.1), _at(6)),   # queued after both pages: count known
              (5, "scraped", 2, _at(1, -0.1), _at(3))]   # queued between them: count unknown
    r = build_recruitment(timeline, hits, raters, set(), same_score=[])
    assert r.unsure == {3, 5}
    assert r.selection[3] == (2, 2) and r.counted[3] == {("a", 1.0, 1), ("b", 4.5, 1)}
    assert r.selection[5] == (None, 1)


def test_stratified_folds_share_out_the_screened_films():
    testable = [f"s{i}" for i in range(10)] + [f"r{i}" for i in range(20)]
    folds = stratified_folds(testable, {f"s{i}" for i in range(10)}, 5, seed=0)
    assert set().union(*folds) == set(testable)
    assert all(sum(1 for s in fold if s.startswith("s")) == 2 for fold in folds)


def test_regressed_toward_mean_fits_the_slope_through_josh_mean():
    train = {"a": 4.0, "b": 2.0, "c": 3.0, "no-average": 5.0}
    base = {"a": 5.0, "b": 1.0, "c": 3.5}
    mean, slope = regressed_toward_mean(train, base)
    assert mean == pytest.approx(3.5)
    # x = base - 3.5 = (1.5, -2.5, 0), y = rating - 3.5 = (0.5, -1.5, -0.5)
    assert slope == pytest.approx((0.75 + 3.75) / (2.25 + 6.25))


def test_paired_bootstrap_of_a_method_against_itself_is_zero():
    actual = {f"f{i}": 1.0 + (i % 9) * 0.5 for i in range(40)}
    preds = {s: a + 0.3 for s, a in actual.items()}
    result = paired_bootstrap(preds, preds, actual, {s: s[:2] for s in actual}, iterations=50)
    assert result["films"] == 40
    assert result["d_rmse"] == 0 and result["rmse_ci"] == (0, 0)
    better = paired_bootstrap(actual, preds, actual, {}, iterations=50)
    assert better["d_rmse"] == pytest.approx(-0.3)
    assert better["rmse_ci"][1] < 0
