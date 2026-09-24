"""The taste engine against a real Postgres — the SQL is where it's most
likely to break, and nothing else exercises it. Skipped unless
TASTE_TEST_DATABASE_URL points at a *local* throwaway database: every test
here empties the taste tables first, so it refuses anything that isn't
localhost rather than risk emptying the real corpus on Neon."""

import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from tests.letterboxd_pages import CHALLENGE_PAGE, following_page, grid_page, members_page
from watchlist_justwatch import db, taste
from watchlist_justwatch.letterboxd import LetterboxdBlockedError

URL = os.environ.get("TASTE_TEST_DATABASE_URL")


def _is_local(url: str) -> bool:
    host = conninfo_to_dict(url).get("host") or ""
    return host == "" or host.startswith("/") or host in {"localhost", "127.0.0.1", "::1"}


pytestmark = pytest.mark.skipif(not URL or not _is_local(URL),
                                reason="needs TASTE_TEST_DATABASE_URL pointing at a local database")

LB = "https://letterboxd.com"
NOW = datetime(2026, 9, 24, 22, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    connection = db.connect(URL)
    connection.execute("TRUNCATE raters, rater_films, rater_ratings, rater_screened, taste_meta "
                       "RESTART IDENTITY CASCADE")
    yield connection
    connection.close()


class _Pages:
    """A fake Letterboxd: url -> html, None (404), or an exception to raise."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.requested: list[str] = []

    def __call__(self, url: str):
        self.requested.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return page


def _fetcher(pages: _Pages) -> taste.PoliteFetcher:
    return taste.PoliteFetcher(delay_seconds=0, max_requests=1000, fetch=pages, sleep=lambda s: None)


def _world(block_on: str | None = None) -> _Pages:
    pages = {
        f"{LB}/josh/following/page/1/": following_page([("friend", 400)]),
        f"{LB}/film/cult-film/members/rated/5/": members_page([("josh", 10), ("stranger", 10)]),
        f"{LB}/film/overrated/members/rated/1.5/": members_page([("stranger", 3), ("josh", 3)]),
        f"{LB}/friend/films/page/1/": grid_page([("cult-film", "Cult Film (1999)", 10), ("other", "Other", 6)]),
        f"{LB}/stranger/films/page/1/": grid_page([("cult-film", "Cult Film (1999)", 9)],
                                                  next_href="/stranger/films/page/2/"),
        f"{LB}/stranger/films/page/2/": grid_page([("overrated", "Overrated (2010)", 2)]),
    }
    if block_on:
        pages[block_on] = LetterboxdBlockedError("HTTP 403")
    return _Pages(pages)


def _scrape(pages: _Pages, *, now=NOW, log=None) -> str:
    return taste.scrape_raters(
        URL, "josh", {"cult-film": 5.0, "overrated": 1.5, "agreed": 4.0},
        {"cult-film": 3.2, "overrated": 4.3, "agreed": 4.0},
        screen_films=2, max_raters=10, fetcher=_fetcher(pages), log=log or (lambda msg: None), now=lambda: now,
    )


def test_full_session_seeds_screens_scrapes_and_refreshes(conn):
    assert _scrape(_world()) == "done"
    raters = {u: (status, hits, count) for u, status, hits, count in
              conn.execute("SELECT username, status, screen_hits, rating_count FROM raters").fetchall()}
    # stranger was on both screening lists, friend came from following —
    # and josh, on every list of his own, never became a candidate.
    assert raters == {"stranger": ("scraped", 2, 2), "friend": ("scraped", taste.FOLLOWING_HITS, 2)}
    assert conn.execute("SELECT count(*) FROM rater_ratings").fetchone()[0] == 4
    assert db.taste_meta_get(conn, "mu") == pytest.approx((10 + 6 + 9 + 2) / 4 / 2)


def test_a_block_stops_the_run_keeps_progress_and_starts_the_cooldown(conn):
    pages = _world(block_on=f"{LB}/stranger/films/page/2/")
    assert _scrape(pages) == "blocked"
    # friend (tied on hits, found first) finished before stranger's second
    # page was refused; nothing was requested after the refusal, and the
    # half-scraped stranger saved nothing.
    assert pages.requested[-1] == f"{LB}/stranger/films/page/2/"
    statuses = dict(conn.execute("SELECT username, status FROM raters").fetchall())
    assert statuses == {"friend": "scraped", "stranger": "candidate"}
    assert conn.execute("SELECT count(*) FROM rater_ratings").fetchone() == (2,)
    assert db.taste_meta_get(conn, "blocked_at") == NOW.isoformat()

    retry = _world()
    assert _scrape(retry, now=NOW + timedelta(hours=23)) == "cooldown"
    assert retry.requested == []
    assert _scrape(retry, now=NOW + timedelta(hours=25)) == "done"
    assert retry.requested == [f"{LB}/stranger/films/page/1/", f"{LB}/stranger/films/page/2/"]


def test_challenge_page_counts_as_a_block(conn):
    pages = _world()
    pages.pages[f"{LB}/film/cult-film/members/rated/5/"] = CHALLENGE_PAGE
    # the fake returns raw html, so route it through the real block check
    from watchlist_justwatch.letterboxd import fetch_page_strict

    class _Session:
        def get(self, url, **kwargs):
            html = pages(url)
            return type("R", (), {"status_code": 200 if html is not None else 404, "text": html or ""})()

    fetcher = taste.PoliteFetcher(delay_seconds=0, max_requests=100, sleep=lambda s: None,
                                  fetch=lambda url: fetch_page_strict(_Session(), url, sleep=lambda s: None))
    outcome = taste.scrape_raters(URL, "josh", {"cult-film": 5.0}, {"cult-film": 3.2}, screen_films=1,
                                  max_raters=10, fetcher=fetcher, log=lambda msg: None, now=lambda: NOW)
    assert outcome == "blocked"


def _save(conn, username: str, ratings: dict[str, int]) -> None:
    db.add_rater_candidates(conn, {username: 1}, NOW.isoformat())
    (rater_id,) = conn.execute("SELECT id FROM raters WHERE username = %s", (username,)).fetchone()
    db.save_rater_ratings(conn, rater_id, [(slug, slug.title(), r) for slug, r in ratings.items()],
                          NOW.isoformat())


def test_recommend_finds_the_twin_and_follows_their_taste(conn):
    films = [f"f{i}" for i in range(30)]
    josh = {f: (1.0 + (i % 9) * 0.5) for i, f in enumerate(films)}
    _save(conn, "twin", {**{f: round(r * 2) for f, r in josh.items()}, "twin-loves": 10, "twin-hates": 2})
    _save(conn, "opposite", {**{f: 12 - round(r * 2) for f, r in josh.items()}, "twin-hates": 10,
                             "twin-loves": 2})
    for n in range(4):
        _save(conn, f"filler{n}", {f: 6 + (i + n) % 3 for i, f in enumerate(films)} | {"twin-loves": 6,
                                                                                         "twin-hates": 6})
    db.refresh_rater_baselines(conn, lambda_film=taste.LAMBDA_FILM, lambda_rater=taste.LAMBDA_RATER,
                               now_iso=NOW.isoformat())

    params = taste.TasteParams(min_overlap=10, min_support=1)
    result = taste.recommend(conn, josh, set(josh), {"twin-loves", "twin-hates"}, params=params)
    assert result["neighbours"][0].username == "twin"
    assert "opposite" not in {n.username for n in result["neighbours"]}
    # Both films have the same corpus average; only the twin tells them apart.
    ranked = {row["slug"]: row["predicted"] for row in result["watchlist"]}
    assert ranked["twin-loves"] > ranked["twin-hates"]
    # Everything else is already seen or on the watchlist.
    assert result["picks"] == []


def test_scoring_refreshes_baselines_a_dead_scrape_left_stale(conn):
    _save(conn, "alice", {"a": 10, "b": 2})
    db.refresh_rater_baselines(conn, lambda_film=0, lambda_rater=0, now_iso=NOW.isoformat())
    # a later scrape saved bob but died before refreshing
    db.add_rater_candidates(conn, {"bob": 1}, NOW.isoformat())
    (bob,) = conn.execute("SELECT id FROM raters WHERE username = 'bob'").fetchone()
    db.save_rater_ratings(conn, bob, [("a", "A", 10), ("b", "B", 10)], (NOW + timedelta(hours=1)).isoformat())
    assert db.taste_meta_get(conn, "mu") == pytest.approx(3.0)
    assert taste._ensure_mu(conn) == pytest.approx(4.0)
    assert db.taste_meta_get(conn, "baselines_at") >= (NOW + timedelta(hours=1)).isoformat()


def test_rescrape_replaces_a_raters_ratings(conn):
    _save(conn, "alice", {"a": 8, "b": 6})
    _save(conn, "alice", {"a": 4})
    assert conn.execute("SELECT count(*), max(rating) FROM rater_ratings").fetchone() == (1, 4)


def test_connect_is_autocommit(conn):
    assert conn.autocommit is True
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            conn.execute("INSERT INTO raters (username, discovered_at) VALUES ('x', 'now')")
            conn.execute("INSERT INTO raters (username, discovered_at) VALUES ('x', 'now')")
    assert conn.execute("SELECT count(*) FROM raters").fetchone() == (0,)
