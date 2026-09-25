import psycopg
from psycopg.types.json import Jsonb

from .models import FilmState, OfferRecord
from .state import SCHEMA_VERSION, StateDoc

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS films (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    year INTEGER,
    entry_id TEXT,
    confidence TEXT NOT NULL,
    last_checked TEXT NOT NULL,
    offers JSONB NOT NULL DEFAULT '[]',
    rating DOUBLE PRECISION,
    poster_url TEXT,
    director JSONB NOT NULL DEFAULT '[]',
    starring JSONB NOT NULL DEFAULT '[]',
    synopsis TEXT
);
CREATE TABLE IF NOT EXISTS diary (
    slug TEXT PRIMARY KEY,
    data JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_films (
    slug TEXT PRIMARY KEY,
    data JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS recommendation_sections (
    key TEXT PRIMARY KEY,
    header TEXT NOT NULL,
    slugs JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS josh_watchlist (
    slug TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS sarah_watchlist (
    slug TEXT PRIMARY KEY
);
-- Superseded: Sarah's watchlist films are now full `films` rows (real
-- JustWatch offers, same enrichment pipeline as Josh's own watchlist —
-- see main.py's combined_films), so a separate lightweight copy with no
-- offer data is no longer needed.
DROP TABLE IF EXISTS sarah_extra_films;
-- Written incrementally from two separate call sites (the daily run seeding
-- new "pending" rows, and the standalone --set-watch-together-status flag)
-- rather than replaced wholesale each run like the tables above — see
-- seed_pending_watch_together/set_watch_together_status.
CREATE TABLE IF NOT EXISTS watch_together (
    slug TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    added_at TEXT NOT NULL,
    decided_at TEXT
);
ALTER TABLE films ADD COLUMN IF NOT EXISTS genre JSONB NOT NULL DEFAULT '[]';
ALTER TABLE films ADD COLUMN IF NOT EXISTS original_language TEXT;
ALTER TABLE films ADD COLUMN IF NOT EXISTS runtime_minutes INTEGER;
ALTER TABLE films ADD COLUMN IF NOT EXISTS tmdb_id INTEGER;
-- Raw scraped showtimes for the cinemas in cinemas.py — no stored
-- watchlist match (see dashboard.py, which matches fresh at build time
-- from state.films, same "recompute rather than let a derived field go
-- stale" principle as everything else the dashboard builds).
-- Resolved Letterboxd films for cinema listings the watchlist can't
-- identify. Keyed by cinemas.listing_match_key (normalized title + year),
-- not by venue or showtime: the same film at three cinemas resolves once.
-- A row with a NULL slug is a listing that has no Letterboxd film —
-- remembered so the daily run doesn't pay two network calls rediscovering
-- that a Bing birthday screening still isn't a film.
CREATE TABLE IF NOT EXISTS cinema_film_matches (
    listing_key TEXT PRIMARY KEY,
    slug TEXT,
    data JSONB,
    resolved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cinema_showtimes (
    id SERIAL PRIMARY KEY,
    cinema TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    showtime TEXT NOT NULL,
    duration_minutes INTEGER,
    director TEXT,
    synopsis TEXT,
    poster_url TEXT,
    booking_url TEXT
);
-- Cached members of each Letterboxd list a custom list sources from (see
-- custom_lists.py). Written per-source by _refresh_custom_list_sources,
-- deliberately NOT part of save_state's full replace (same as
-- watch_together) so the standalone --refresh-custom-lists flag can
-- update it without a full pipeline run, and a failed fetch simply
-- leaves the previous copy in place.
CREATE TABLE IF NOT EXISTS custom_list_sources (
    source TEXT PRIMARY KEY,
    slugs JSONB NOT NULL,
    fetched_at TEXT NOT NULL
);
-- The taste engine's corpus (see taste.py): other Letterboxd members'
-- public ratings, scraped locally by --scrape-raters. Written
-- incrementally, one member at a time, and never part of save_state's
-- full replace. Big by this database's standards (~1.5M rows at full
-- size), so it's only ever aggregated server-side — nothing reads it out
-- whole; see rater_similarities/rater_predictions.
--   raters.status: candidate (found, not yet scraped) / scraped /
--   missing (404) / unrated (rates nothing, not worth the pages).
--   bias columns: refresh_rater_baselines' shrunk offsets, in stars.
--   rater_ratings.rating: half-stars 1-10, to keep the row narrow.
CREATE TABLE IF NOT EXISTS raters (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'candidate',
    screen_hits INTEGER NOT NULL DEFAULT 0,
    rating_count INTEGER,
    bias REAL NOT NULL DEFAULT 0,
    discovered_at TEXT NOT NULL,
    scraped_at TEXT
);
CREATE TABLE IF NOT EXISTS rater_films (
    id SERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT,
    rating_count INTEGER NOT NULL DEFAULT 0,
    bias REAL NOT NULL DEFAULT 0
);
-- "movie" / "tv" from the film page's TMDB link, "gone" for a page that
-- 404s, NULL until checked — see taste.recommend, which checks lazily and
-- only for the films it's about to suggest.
ALTER TABLE rater_films ADD COLUMN IF NOT EXISTS tmdb_kind TEXT;
CREATE TABLE IF NOT EXISTS rater_ratings (
    rater_id INTEGER NOT NULL REFERENCES raters(id) ON DELETE CASCADE,
    film_id INTEGER NOT NULL REFERENCES rater_films(id),
    rating SMALLINT NOT NULL,
    PRIMARY KEY (rater_id, film_id)
);
CREATE INDEX IF NOT EXISTS rater_ratings_film_idx ON rater_ratings (film_id);
-- Which /film/<slug>/members/rated/<stars>/ pages have already been
-- screened for candidates, so an interrupted run resumes where it stopped.
CREATE TABLE IF NOT EXISTS rater_screened (
    slug TEXT NOT NULL,
    stars REAL NOT NULL,
    page INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (slug, stars, page)
);
-- Small taste-engine values (global mean rating, when Letterboxd last
-- blocked a scrape) — its own table because save_state replaces `meta`
-- wholesale on every daily run.
CREATE TABLE IF NOT EXISTS taste_meta (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL
);
"""


def _ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)


def _offer_to_dict(offer: OfferRecord) -> dict:
    return {
        "country": offer.country,
        "monetization_type": offer.monetization_type,
        "package_technical_name": offer.package_technical_name,
        "package_clear_name": offer.package_clear_name,
        "package_id": offer.package_id,
        "url": offer.url,
        "available_to": offer.available_to,
    }


def _offer_from_dict(data: dict) -> OfferRecord:
    return OfferRecord(
        country=data["country"],
        monetization_type=data["monetization_type"],
        package_technical_name=data["package_technical_name"],
        package_clear_name=data["package_clear_name"],
        package_id=data["package_id"],
        url=data["url"],
        available_to=data.get("available_to"),
    )


def get_meta_value(database_url: str, key: str):
    """A single meta value without pulling the rest of state — the
    15-min-turned-hourly --check-for-new-log run only ever needs
    last_seen_diary_guid, and a full load_state() (every film's offer data,
    the whole diary) on every single invocation was the actual driver of
    Neon's free-tier data-transfer quota being exceeded, not run frequency
    alone."""
    with psycopg.connect(database_url) as conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
        return row[0] if row else None


def load_state(database_url: str) -> StateDoc:
    with psycopg.connect(database_url) as conn:
        _ensure_schema(conn)

        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())

        films: dict[str, FilmState] = {}
        for row in conn.execute(
            "SELECT slug, title, year, entry_id, confidence, last_checked, offers, "
            "rating, poster_url, director, starring, synopsis, genre, original_language, "
            "runtime_minutes, tmdb_id FROM films"
        ).fetchall():
            (slug, title, year, entry_id, confidence, last_checked, offers,
             rating, poster_url, director, starring, synopsis, genre, original_language,
             runtime_minutes, tmdb_id) = row
            films[slug] = FilmState(
                slug=slug, title=title, year=year, entry_id=entry_id, confidence=confidence,
                last_checked=last_checked, offers=[_offer_from_dict(o) for o in offers],
                rating=rating, poster_url=poster_url, director=director, starring=starring,
                synopsis=synopsis, genre=genre, original_language=original_language,
                runtime_minutes=runtime_minutes, tmdb_id=tmdb_id,
            )

        diary = dict(conn.execute("SELECT slug, data FROM diary").fetchall())
        discovery_films = dict(conn.execute("SELECT slug, data FROM discovery_films").fetchall())
        recommendation_sections = [
            {"key": key, "header": header, "slugs": slugs}
            for key, header, slugs in conn.execute(
                "SELECT key, header, slugs FROM recommendation_sections"
            ).fetchall()
        ]
        josh_watchlist = {row[0] for row in conn.execute("SELECT slug FROM josh_watchlist").fetchall()}
        sarah_watchlist = {row[0] for row in conn.execute("SELECT slug FROM sarah_watchlist").fetchall()}
        cinema_matches = {
            key: data
            for key, data in conn.execute(
                "SELECT listing_key, data FROM cinema_film_matches"
            ).fetchall()
        }
        cinema_showtimes = [
            {"cinema": cinema, "title": title, "year": year, "showtime": showtime,
             "duration_minutes": duration_minutes, "director": director, "synopsis": synopsis,
             "poster_url": poster_url, "booking_url": booking_url}
            for (cinema, title, year, showtime, duration_minutes, director, synopsis, poster_url,
                 booking_url) in conn.execute(
                "SELECT cinema, title, year, showtime, duration_minutes, director, synopsis, "
                "poster_url, booking_url FROM cinema_showtimes"
            ).fetchall()
        ]

    return StateDoc(
        schema_version=meta.get("schema_version", SCHEMA_VERSION),
        last_run_at=meta.get("last_run_at"),
        last_justwatch_check_date=meta.get("last_justwatch_check_date"),
        last_seen_diary_guid=meta.get("last_seen_diary_guid"),
        films=films,
        recent_watches=meta.get("recent_watches", []),
        recommendation_sections=recommendation_sections,
        discovery_films=discovery_films,
        recent_additions=meta.get("recent_additions", []),
        diary=diary,
        josh_watchlist=josh_watchlist,
        sarah_watchlist=sarah_watchlist,
        cinema_showtimes=cinema_showtimes,
        cinema_matches=cinema_matches,
        for_you=meta.get("for_you"),
    )


def save_state(database_url: str, state: StateDoc) -> None:
    # The full StateDoc is always a complete snapshot already (see main.py,
    # where every collection is rebuilt from a copy of the previous run's
    # state plus this run's changes) — so replacing each table wholesale
    # each run is correct, not just an approximation, and is far simpler
    # than diffing rows to upsert/delete individually for ~2000 rows total.
    with psycopg.connect(database_url) as conn:
        _ensure_schema(conn)

        conn.execute("DELETE FROM films")
        conn.execute("DELETE FROM diary")
        conn.execute("DELETE FROM discovery_films")
        conn.execute("DELETE FROM recommendation_sections")
        conn.execute("DELETE FROM josh_watchlist")
        conn.execute("DELETE FROM sarah_watchlist")
        conn.execute("DELETE FROM cinema_showtimes")
        conn.execute("DELETE FROM cinema_film_matches")
        conn.execute("DELETE FROM meta")
        # watch_together is deliberately NOT wiped here — it's written
        # incrementally by seed_pending_watch_together/set_watch_together_status,
        # not rebuilt wholesale each run like everything else in this function.

        if state.films:
            conn.cursor().executemany(
                "INSERT INTO films (slug, title, year, entry_id, confidence, last_checked, "
                "offers, rating, poster_url, director, starring, synopsis, genre, original_language, "
                "runtime_minutes, tmdb_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (f.slug, f.title, f.year, f.entry_id, f.confidence, f.last_checked,
                     Jsonb([_offer_to_dict(o) for o in f.offers]), f.rating, f.poster_url,
                     Jsonb(f.director), Jsonb(f.starring), f.synopsis, Jsonb(f.genre), f.original_language,
                     f.runtime_minutes, f.tmdb_id)
                    for f in state.films.values()
                ],
            )

        if state.diary:
            conn.cursor().executemany(
                "INSERT INTO diary (slug, data) VALUES (%s, %s)",
                [(slug, Jsonb(data)) for slug, data in state.diary.items()],
            )

        if state.discovery_films:
            conn.cursor().executemany(
                "INSERT INTO discovery_films (slug, data) VALUES (%s, %s)",
                [(slug, Jsonb(data)) for slug, data in state.discovery_films.items()],
            )

        if state.recommendation_sections:
            conn.cursor().executemany(
                "INSERT INTO recommendation_sections (key, header, slugs) VALUES (%s, %s, %s)",
                [(s["key"], s["header"], Jsonb(s["slugs"])) for s in state.recommendation_sections],
            )

        if state.josh_watchlist:
            conn.cursor().executemany(
                "INSERT INTO josh_watchlist (slug) VALUES (%s)",
                [(slug,) for slug in state.josh_watchlist],
            )

        if state.sarah_watchlist:
            conn.cursor().executemany(
                "INSERT INTO sarah_watchlist (slug) VALUES (%s)",
                [(slug,) for slug in state.sarah_watchlist],
            )

        if state.cinema_showtimes:
            conn.cursor().executemany(
                "INSERT INTO cinema_showtimes (cinema, title, year, showtime, duration_minutes, "
                "director, synopsis, poster_url, booking_url) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (s["cinema"], s["title"], s["year"], s["showtime"], s["duration_minutes"],
                     s["director"], s["synopsis"], s["poster_url"], s["booking_url"])
                    for s in state.cinema_showtimes
                ],
            )

        if state.cinema_matches:
            conn.cursor().executemany(
                "INSERT INTO cinema_film_matches (listing_key, slug, data, resolved_at) "
                "VALUES (%s, %s, %s, %s)",
                [
                    (key, (data or {}).get("slug"), Jsonb(data),
                     (data or {}).get("resolved_at") or state.last_run_at or "")
                    for key, data in state.cinema_matches.items()
                ],
            )

        conn.cursor().executemany(
            "INSERT INTO meta (key, value) VALUES (%s, %s)",
            [
                ("schema_version", Jsonb(state.schema_version)),
                ("last_run_at", Jsonb(state.last_run_at)),
                ("last_justwatch_check_date", Jsonb(state.last_justwatch_check_date)),
                ("last_seen_diary_guid", Jsonb(state.last_seen_diary_guid)),
                ("recent_watches", Jsonb(state.recent_watches)),
                ("recent_additions", Jsonb(state.recent_additions)),
                ("for_you", Jsonb(state.for_you)),
            ],
        )


def load_watch_together(database_url: str) -> dict[str, dict]:
    """slug -> {status, added_at, decided_at}. Unlike load_state, this reads
    a table that's written incrementally (see seed_pending_watch_together/
    set_watch_together_status below), not replaced wholesale each run."""
    with psycopg.connect(database_url) as conn:
        _ensure_schema(conn)
        rows = conn.execute("SELECT slug, status, added_at, decided_at FROM watch_together").fetchall()
    return {
        slug: {"status": status, "added_at": added_at, "decided_at": decided_at}
        for slug, status, added_at, decided_at in rows
    }


def seed_pending_watch_together(database_url: str, slugs: set[str], added_at: str) -> None:
    """Adds a 'pending' row for each slug that doesn't already have one —
    called once per daily run with every watchlist slug missing an entry, so
    it covers both the one-time backfill of the existing watchlist (every
    slug is "missing" the first time this runs) and future additions (only
    the new slug is) with the same code path. A no-op for slugs that already
    have a row, so it's safe to call with the same slug on repeat runs."""
    if not slugs:
        return
    with psycopg.connect(database_url) as conn:
        _ensure_schema(conn)
        conn.cursor().executemany(
            "INSERT INTO watch_together (slug, status, added_at) VALUES (%s, 'pending', %s) "
            "ON CONFLICT (slug) DO NOTHING",
            [(slug, added_at) for slug in slugs],
        )


def set_watch_together_status(database_url: str, slug: str, status: str, decided_at: str) -> None:
    with psycopg.connect(database_url) as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE watch_together SET status = %s, decided_at = %s WHERE slug = %s",
            (status, decided_at, slug),
        )


def set_watch_together_statuses_batch(database_url: str, decisions: list[tuple[str, str, str]]) -> None:
    """decisions: (slug, status, decided_at) triples. One connection/
    transaction for the whole batch instead of one per decision — the
    Review tab now debounce-batches taps client-side specifically so a
    session of many decisions costs one workflow run (and one DB
    round-trip) instead of one each."""
    if not decisions:
        return
    with psycopg.connect(database_url) as conn:
        _ensure_schema(conn)
        conn.cursor().executemany(
            "UPDATE watch_together SET status = %s, decided_at = %s WHERE slug = %s",
            [(status, decided_at, slug) for slug, status, decided_at in decisions],
        )


def custom_list_source_fetch_times(database_url: str) -> dict[str, str]:
    """source -> fetched_at, without pulling any slugs — all the refresh
    step needs to decide what's stale."""
    with psycopg.connect(database_url) as conn:
        _ensure_schema(conn)
        return dict(conn.execute("SELECT source, fetched_at FROM custom_list_sources").fetchall())


def load_custom_list_memberships(database_url: str, relevant_slugs: set[str]) -> dict[str, set[str]]:
    """source -> the subset of its slugs that are in `relevant_slugs` (the
    watchlist plus the diary — the only films the dashboard can say
    anything about). Intersected server-side: festival sources run to
    thousands of films each, and shipping every one of them on every
    dashboard regen is exactly the kind of read that has blown Neon's
    free-tier transfer quota before (see get_meta_value)."""
    with psycopg.connect(database_url) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT s.source, COALESCE(array_agg(e) FILTER (WHERE e IS NOT NULL), '{}') "
            "FROM custom_list_sources s "
            "LEFT JOIN LATERAL jsonb_array_elements_text(s.slugs) e ON e = ANY(%s) "
            "GROUP BY s.source",
            (list(relevant_slugs),),
        ).fetchall()
    return {source: set(slugs) for source, slugs in rows}


def custom_list_source_totals(database_url: str, groups: dict[str, tuple[list[str], list[str], list[str]]]
                              ) -> dict[str, int]:
    """key -> distinct film count across (sources + include - exclude), per
    group — the "of N" in a list's "M of N seen". Counted server-side for
    the same transfer-quota reason as load_custom_list_memberships. A group
    whose sources aren't all cached yet is left out (total unknown)."""
    if not groups:
        return {}
    totals: dict[str, int] = {}
    with psycopg.connect(database_url) as conn:
        _ensure_schema(conn)
        cached = {row[0] for row in conn.execute("SELECT source FROM custom_list_sources").fetchall()}
        for key, (sources, include, exclude) in groups.items():
            if not sources or not set(sources) <= cached:
                continue
            (count,) = conn.execute(
                "SELECT count(*) FROM ("
                "  SELECT jsonb_array_elements_text(slugs) AS e FROM custom_list_sources WHERE source = ANY(%s)"
                "  UNION SELECT unnest(%s::text[])"
                ") u WHERE NOT (e = ANY(%s))",
                (sources, include, exclude),
            ).fetchone()
            totals[key] = count
    return totals


def save_custom_list_source(database_url: str, source: str, slugs: list[str], fetched_at: str) -> None:
    with psycopg.connect(database_url) as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO custom_list_sources (source, slugs, fetched_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (source) DO UPDATE SET slugs = EXCLUDED.slugs, fetched_at = EXCLUDED.fetched_at",
            (source, Jsonb(slugs), fetched_at),
        )


# --- Taste engine (see taste.py) ------------------------------------------
#
# Unlike everything above, these take an open connection: an evaluation
# run issues dozens of queries back to back, and a connection (plus the
# schema DDL) per query would dominate it. connect() gives one with the
# schema applied, in autocommit mode — so a scrape interrupted overnight
# keeps every rater it finished, and the multi-statement writes below
# opt into a transaction explicitly.


def connect(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, autocommit=True)
    _ensure_schema(conn)
    return conn


def load_taste_inputs(database_url: str) -> tuple[dict[str, dict], set[str]]:
    """(slug -> {personal_rating, rating, watched_date} for every diary
    film, Josh's watchlist slugs) — just the fields the taste engine reads,
    rather than load_state's every offer of every film."""
    with psycopg.connect(database_url) as conn:
        _ensure_schema(conn)
        diary = {
            slug: {"personal_rating": personal, "rating": community, "watched_date": watched}
            for slug, personal, community, watched in conn.execute(
                "SELECT slug, (data->>'personal_rating')::real, (data->>'rating')::real, "
                "data->>'watched_date' FROM diary"
            ).fetchall()
        }
        watchlist = {row[0] for row in conn.execute("SELECT slug FROM josh_watchlist").fetchall()}
    return diary, watchlist


def taste_meta_get(conn: psycopg.Connection, key: str):
    row = conn.execute("SELECT value FROM taste_meta WHERE key = %s", (key,)).fetchone()
    return row[0] if row else None


def taste_meta_set(conn: psycopg.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO taste_meta (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, Jsonb(value)),
    )


def add_rater_candidates(conn: psycopg.Connection, hits: dict[str, int], now_iso: str) -> None:
    """Adds each username as a candidate, or bumps screen_hits for one
    already known — however far along it is, so a scraped rater's count
    still says how many screening lists they turned up on."""
    if not hits:
        return
    conn.cursor().executemany(
        "INSERT INTO raters (username, screen_hits, discovered_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (username) DO UPDATE SET screen_hits = raters.screen_hits + EXCLUDED.screen_hits",
        [(username, count, now_iso) for username, count in hits.items()],
    )


def screened_pages(conn: psycopg.Connection) -> set[tuple[str, float, int]]:
    return {(slug, stars, page) for slug, stars, page in
            conn.execute("SELECT slug, stars, page FROM rater_screened").fetchall()}


def mark_screened(conn: psycopg.Connection, slug: str, stars: float, page: int, now_iso: str) -> None:
    conn.execute(
        "INSERT INTO rater_screened (slug, stars, page, fetched_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING",
        (slug, stars, page, now_iso),
    )


def next_raters_to_scrape(conn: psycopg.Connection, limit: int, exclude: set[str]) -> list[tuple[int, str]]:
    """The most promising unscraped candidates: most screening-list
    appearances first, oldest discovery breaking ties."""
    return conn.execute(
        "SELECT id, username FROM raters WHERE status = 'candidate' AND NOT (lower(username) = ANY(%s)) "
        "ORDER BY screen_hits DESC, id LIMIT %s",
        ([u.lower() for u in exclude], limit),
    ).fetchall()


def save_rater_ratings(conn: psycopg.Connection, rater_id: int, rated: list[tuple[str, str | None, int]],
                       now_iso: str) -> None:
    """Replaces one rater's ratings wholesale (a re-scrape should drop films
    they've since un-rated) in a single transaction. The slug -> film id
    mapping happens server-side, so nothing is read back."""
    by_slug = {slug: (name, half_stars) for slug, name, half_stars in rated}
    slugs = list(by_slug)
    with conn.transaction():
        conn.execute(
            "INSERT INTO rater_films (slug, name) SELECT * FROM unnest(%s::text[], %s::text[]) "
            "ON CONFLICT (slug) DO NOTHING",
            (slugs, [by_slug[s][0] for s in slugs]),
        )
        conn.execute("DELETE FROM rater_ratings WHERE rater_id = %s", (rater_id,))
        conn.execute(
            "INSERT INTO rater_ratings (rater_id, film_id, rating) "
            "SELECT %s, f.id, x.rating FROM unnest(%s::text[], %s::smallint[]) AS x(slug, rating) "
            "JOIN rater_films f ON f.slug = x.slug",
            (rater_id, slugs, [by_slug[s][1] for s in slugs]),
        )
        conn.execute(
            "UPDATE raters SET status = 'scraped', rating_count = %s, scraped_at = %s WHERE id = %s",
            (len(slugs), now_iso, rater_id),
        )


def set_rater_status(conn: psycopg.Connection, rater_id: int, status: str, now_iso: str) -> None:
    conn.execute("UPDATE raters SET status = %s, scraped_at = %s WHERE id = %s", (status, now_iso, rater_id))


def refresh_rater_baselines(conn: psycopg.Connection, *, lambda_film: float, lambda_rater: float,
                            now_iso: str) -> float | None:
    """Recomputes the corpus's global mean (stored as taste_meta 'mu'), each
    film's offset from it, then each rater's offset from mean-plus-film —
    the standard shrunk baseline estimates, so a film three people rated
    doesn't get a confident offset. Films first, so a rater who only
    watches acclaimed films isn't mistaken for a generous one. Entirely
    server-side. Records when (taste_meta 'baselines_at'), so a scoring
    run can tell they're older than the newest scrape. Returns mu (None
    for an empty corpus)."""
    (mu,) = conn.execute("SELECT avg(rating) / 2.0 FROM rater_ratings").fetchone()
    if mu is None:
        return None
    mu = float(mu)
    with conn.transaction():
        conn.execute(
            "UPDATE rater_films f SET bias = s.b, rating_count = s.n FROM ("
            "  SELECT film_id, sum(rating / 2.0 - %(mu)s) / (count(*) + %(lam)s) AS b, count(*) AS n"
            "  FROM rater_ratings GROUP BY film_id"
            ") s WHERE f.id = s.film_id",
            {"mu": mu, "lam": lambda_film},
        )
        conn.execute(
            "UPDATE raters u SET bias = s.b, rating_count = s.n FROM ("
            "  SELECT r.rater_id, sum(r.rating / 2.0 - %(mu)s - f.bias) / (count(*) + %(lam)s) AS b,"
            "         count(*) AS n"
            "  FROM rater_ratings r JOIN rater_films f ON f.id = r.film_id GROUP BY r.rater_id"
            ") s WHERE u.id = s.rater_id",
            {"mu": mu, "lam": lambda_rater},
        )
        conn.cursor().executemany(
            "INSERT INTO taste_meta (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            [("mu", Jsonb(mu)), ("baselines_at", Jsonb(now_iso))],
        )
    return mu


def latest_scrape_activity(conn: psycopg.Connection) -> str | None:
    """When --scrape-raters last finished anything (a member, whatever
    their status, or a screening page) — a heartbeat, so another command
    about to request Letterboxd pages can tell a scrape is running."""
    return conn.execute(
        "SELECT greatest((SELECT max(scraped_at) FROM raters), (SELECT max(fetched_at) FROM rater_screened))"
    ).fetchone()[0]


def latest_rater_scrape(conn: psycopg.Connection) -> str | None:
    return conn.execute("SELECT max(scraped_at) FROM raters WHERE status = 'scraped'").fetchone()[0]


def rater_corpus_summary(conn: psycopg.Connection) -> dict:
    by_status = dict(conn.execute("SELECT status, count(*) FROM raters GROUP BY status").fetchall())
    (ratings,) = conn.execute("SELECT count(*) FROM rater_ratings").fetchone()
    (films,) = conn.execute("SELECT count(*) FROM rater_films").fetchone()
    (screened,) = conn.execute("SELECT count(*) FROM rater_screened").fetchone()
    return {"raters": by_status, "ratings": ratings, "films": films, "screened_pages": screened}


def rater_film_lookup(conn: psycopg.Connection, slugs: list[str]) -> dict[str, tuple[int, float, int, str | None]]:
    """slug -> (film id, film bias, corpus rating count, name) for whichever
    of `slugs` the corpus has."""
    return {
        slug: (film_id, float(bias), rating_count, name)
        for film_id, slug, bias, rating_count, name in conn.execute(
            "SELECT id, slug, bias, rating_count, name FROM rater_films WHERE slug = ANY(%s)", (slugs,)
        ).fetchall()
    }


def rater_similarities(conn: psycopg.Connection, film_ids: list[int], residuals: list[float], *,
                       mu: float, min_overlap: int,
                       film_means: list[float] | None = None) -> list[tuple[int, str, int, float]]:
    """(rater id, username, films shared, Pearson correlation) for every
    rater who shares at least `min_overlap` rated films with the given
    residuals and correlates positively with them. The correlation is of
    residuals on both sides — each rating minus the global mean, the
    rater's own offset and the film's offset — so agreeing that a
    universally loved film is great counts for nothing, and agreeing that
    it's overrated counts for a lot. `film_means` (aligned with `film_ids`)
    centres the rater's side on each film's Letterboxd average instead of
    the corpus's own estimate of it, to match residuals measured the same
    way. One aggregate over the corpus; only the per-rater result comes
    back."""
    rows = conn.execute(
        "SELECT * FROM ("
        "  SELECT r.rater_id, u.username, count(*) AS overlap,"
        "         corr(me.z, r.rating / 2.0 - coalesce(me.m, %(mu)s + f.bias) - u.bias) AS pearson"
        "  FROM unnest(%(ids)s::int[], %(z)s::real[], %(m)s::real[]) AS me(film_id, z, m)"
        "  JOIN rater_ratings r ON r.film_id = me.film_id"
        "  JOIN raters u ON u.id = r.rater_id"
        "  JOIN rater_films f ON f.id = r.film_id"
        "  GROUP BY r.rater_id, u.username"
        "  HAVING count(*) >= %(min_overlap)s"
        ") s WHERE pearson > 0",
        {"ids": film_ids, "z": residuals, "m": film_means if film_means is not None else [None] * len(film_ids),
         "mu": mu, "min_overlap": min_overlap},
    ).fetchall()
    return [(rater_id, username, overlap, float(pearson)) for rater_id, username, overlap, pearson in rows]


def rater_predictions(conn: psycopg.Connection, neighbour_ids: list[int], weights: list[float], *,
                      mu: float, lambda_pred: float, min_support: int,
                      target_film_ids: list[int] | None = None, exclude_film_ids: list[int] | None = None,
                      limit: int | None = None, film_means: dict[int, float] | None = None,
                      lambda_rater: float = 0.0) -> list[dict]:
    """Per film the neighbours rated: the film's offset, and the
    weight-averaged residual of the neighbours who rated it — shrunk toward
    zero by `lambda_pred` in the denominator, so a film two neighbours
    loved moves the prediction less than one twenty did. Restricted to
    `target_film_ids` when given, never `exclude_film_ids`, best first.

    `film_means` (film id -> Letterboxd average) re-centres the residuals
    of the films it covers on that average, and each neighbour's own
    offset on how they rate those films against it (shrunk by
    `lambda_rater`, like refresh_rater_baselines) — so that the result can
    go on top of the Letterboxd average. Left on the corpus's own
    estimate, a residual would still carry whatever of the film's quality
    its heavily shrunk corpus offset missed, and count it twice."""
    means = film_means or {}
    rows = conn.execute(
        "WITH lb AS (SELECT * FROM unnest(%(lb_ids)s::int[], %(lb_means)s::real[]) AS lb(film_id, m)),"
        "     nb AS (SELECT * FROM unnest(%(ids)s::int[], %(w)s::real[]) AS nb(rater_id, w)),"
        "     nb_lb AS ("
        "       SELECT r.rater_id, sum(r.rating / 2.0 - lb.m) / (count(*) + %(lam_rater)s) AS b"
        "       FROM nb JOIN rater_ratings r ON r.rater_id = nb.rater_id JOIN lb ON lb.film_id = r.film_id"
        "       GROUP BY r.rater_id"
        "     )"
        "SELECT * FROM ("
        "  SELECT f.id, f.slug, f.name, f.bias, f.tmdb_kind, count(*) AS support,"
        "         sum(nb.w * (r.rating / 2.0 - CASE WHEN lb.m IS NULL THEN %(mu)s + u.bias + f.bias"
        "                                          ELSE lb.m + coalesce(nb_lb.b, 0) END))"
        "           / (sum(nb.w) + %(lam)s) AS nb_offset,"
        "         avg(r.rating) / 2.0 AS neighbour_mean"
        "  FROM nb"
        "  JOIN rater_ratings r ON r.rater_id = nb.rater_id"
        "  JOIN raters u ON u.id = nb.rater_id"
        "  JOIN rater_films f ON f.id = r.film_id"
        "  LEFT JOIN lb ON lb.film_id = f.id"
        "  LEFT JOIN nb_lb ON nb_lb.rater_id = nb.rater_id"
        "  WHERE (%(targets)s::int[] IS NULL OR f.id = ANY(%(targets)s::int[]))"
        "    AND NOT (f.id = ANY(%(exclude)s::int[]))"
        "  GROUP BY f.id"
        "  HAVING count(*) >= %(min_support)s"
        ") s ORDER BY bias + nb_offset DESC LIMIT %(limit)s",
        {"ids": neighbour_ids, "w": weights, "mu": mu, "lam": lambda_pred, "min_support": min_support,
         "targets": target_film_ids, "exclude": exclude_film_ids or [], "limit": limit,
         "lb_ids": list(means), "lb_means": list(means.values()), "lam_rater": lambda_rater},
    ).fetchall()
    return [
        {"film_id": film_id, "slug": slug, "name": name, "bias": float(bias), "tmdb_kind": tmdb_kind,
         "support": support, "nb_offset": float(nb_offset), "neighbour_mean": float(neighbour_mean)}
        for film_id, slug, name, bias, tmdb_kind, support, nb_offset, neighbour_mean in rows
    ]


def set_rater_film_kinds(conn: psycopg.Connection, kinds: dict[int, str]) -> None:
    """Records what taste.recommend found out (film id -> "movie"/"tv"/
    "gone"), so each film's page is only ever checked once."""
    if kinds:
        conn.execute(
            "UPDATE rater_films f SET tmdb_kind = k.kind "
            "FROM unnest(%s::int[], %s::text[]) AS k(id, kind) WHERE f.id = k.id",
            (list(kinds), list(kinds.values())),
        )


def taste_because(conn: psycopg.Connection, neighbour_ids: list[int], target_film_ids: list[int],
                  loved_film_ids: list[int], *, top: int = 3, min_shared: int = 2) -> dict[int, list[tuple[int, int]]]:
    """The "because you loved" line: for each target film, which of Josh's
    own favourites (`loved_film_ids`) the neighbours who loved the target
    (4.5+) love unusually often — the share of the target's fans who loved
    it, minus the share of all the neighbours who did, so The Dark Knight
    (loved by everyone) never explains anything. target id -> up to `top`
    (favourite id, fans in common), best first. One aggregate; only the
    winners come back."""
    rows = conn.execute(
        "WITH nb AS (SELECT unnest(%(nb)s::int[]) AS rater_id),"
        "     fans AS (SELECT r.film_id AS t, r.rater_id FROM rater_ratings r JOIN nb USING (rater_id)"
        "              WHERE r.film_id = ANY(%(targets)s::int[]) AND r.rating >= 9),"
        "     fan_counts AS (SELECT t, count(*) AS n FROM fans GROUP BY t),"
        "     lovers AS (SELECT r.film_id AS l, r.rater_id FROM rater_ratings r JOIN nb USING (rater_id)"
        "                WHERE r.film_id = ANY(%(loved)s::int[]) AND r.rating >= 9),"
        "     base AS (SELECT l, count(*)::float / %(n_nb)s AS p FROM lovers GROUP BY l),"
        "     shared AS (SELECT f.t, lv.l, count(*) AS n FROM fans f JOIN lovers lv USING (rater_id)"
        "                WHERE lv.l <> f.t GROUP BY f.t, lv.l HAVING count(*) >= %(min_shared)s),"
        "     ranked AS (SELECT s.t, s.l, s.n, s.n::float / fc.n - b.p AS lift,"
        "                       row_number() OVER (PARTITION BY s.t ORDER BY s.n::float / fc.n - b.p DESC, s.l) AS rk"
        "                FROM shared s JOIN fan_counts fc USING (t) JOIN base b USING (l))"
        "SELECT t, l, n FROM ranked WHERE rk <= %(top)s AND lift > 0 ORDER BY t, rk",
        {"nb": neighbour_ids, "targets": target_film_ids, "loved": loved_film_ids,
         "n_nb": max(len(neighbour_ids), 1), "min_shared": min_shared, "top": top},
    ).fetchall()
    result: dict[int, list[tuple[int, int]]] = {}
    for target, loved, shared in rows:
        result.setdefault(target, []).append((loved, shared))
    return result


def taste_fans_also_loved(conn: psycopg.Connection, source_film_ids: list[int], candidate_film_ids: list[int], *,
                          top: int = 8, min_fans: int = 5, min_shared: int = 3) -> dict[int, list[int]]:
    """"If you like this, see…": for each source film, the candidates its
    fans (every scraped member who rated it 4.5+) love unusually often —
    share of its fans who loved the candidate, minus the share of all
    scraped members who did, so a crowd-pleaser doesn't follow every film
    around. Films with fewer than `min_fans` fans get nothing rather than
    a list built on two people. source id -> up to `top` candidate ids,
    best first."""
    rows = conn.execute(
        "WITH fans AS (SELECT film_id AS s, rater_id FROM rater_ratings"
        "              WHERE film_id = ANY(%(sources)s::int[]) AND rating >= 9),"
        "     fan_counts AS (SELECT s, count(*) AS n FROM fans GROUP BY s HAVING count(*) >= %(min_fans)s),"
        "     lovers AS (SELECT film_id AS c, rater_id FROM rater_ratings"
        "                WHERE film_id = ANY(%(cands)s::int[]) AND rating >= 9),"
        "     base AS (SELECT c, count(*)::float / greatest((SELECT count(*) FROM raters WHERE status = 'scraped'), 1)"
        "                AS p FROM lovers GROUP BY c),"
        "     shared AS (SELECT f.s, lv.c, count(*) AS n FROM fans f JOIN fan_counts USING (s)"
        "                JOIN lovers lv USING (rater_id) WHERE lv.c <> f.s"
        "                GROUP BY f.s, lv.c HAVING count(*) >= %(min_shared)s),"
        "     ranked AS (SELECT sh.s, sh.c, sh.n::float / fc.n - b.p AS lift,"
        "                       row_number() OVER (PARTITION BY sh.s ORDER BY sh.n::float / fc.n - b.p DESC, sh.c) AS rk"
        "                FROM shared sh JOIN fan_counts fc USING (s) JOIN base b USING (c))"
        "SELECT s, c FROM ranked WHERE rk <= %(top)s AND lift > 0 ORDER BY s, rk",
        {"sources": source_film_ids, "cands": candidate_film_ids, "min_fans": min_fans,
         "min_shared": min_shared, "top": top},
    ).fetchall()
    result: dict[int, list[int]] = {}
    for source, candidate in rows:
        result.setdefault(source, []).append(candidate)
    return result


def neighbour_fans(conn: psycopg.Connection, neighbour_ids: list[int], film_ids: list[int]) -> dict[int, int]:
    """film id -> how many of the neighbours rated it 4.5+ (the "10 of 13
    matches loved it" count; rater_predictions has the 13)."""
    return dict(conn.execute(
        "SELECT film_id, count(*) FROM rater_ratings "
        "WHERE rater_id = ANY(%s::int[]) AND film_id = ANY(%s::int[]) AND rating >= 9 GROUP BY film_id",
        (neighbour_ids, film_ids),
    ).fetchall())
