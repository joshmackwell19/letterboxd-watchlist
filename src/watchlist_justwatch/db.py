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
CREATE TABLE IF NOT EXISTS sarah_watchlist (
    slug TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS sarah_extra_films (
    slug TEXT PRIMARY KEY,
    data JSONB NOT NULL
);
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
            "rating, poster_url, director, starring, synopsis, genre FROM films"
        ).fetchall():
            (slug, title, year, entry_id, confidence, last_checked, offers,
             rating, poster_url, director, starring, synopsis, genre) = row
            films[slug] = FilmState(
                slug=slug, title=title, year=year, entry_id=entry_id, confidence=confidence,
                last_checked=last_checked, offers=[_offer_from_dict(o) for o in offers],
                rating=rating, poster_url=poster_url, director=director, starring=starring,
                synopsis=synopsis, genre=genre,
            )

        diary = dict(conn.execute("SELECT slug, data FROM diary").fetchall())
        discovery_films = dict(conn.execute("SELECT slug, data FROM discovery_films").fetchall())
        recommendation_sections = [
            {"key": key, "header": header, "slugs": slugs}
            for key, header, slugs in conn.execute(
                "SELECT key, header, slugs FROM recommendation_sections"
            ).fetchall()
        ]
        sarah_watchlist = {row[0] for row in conn.execute("SELECT slug FROM sarah_watchlist").fetchall()}
        sarah_extra_films = dict(conn.execute("SELECT slug, data FROM sarah_extra_films").fetchall())

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
        sarah_watchlist=sarah_watchlist,
        sarah_extra_films=sarah_extra_films,
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
        conn.execute("DELETE FROM sarah_watchlist")
        conn.execute("DELETE FROM sarah_extra_films")
        conn.execute("DELETE FROM meta")
        # watch_together is deliberately NOT wiped here — it's written
        # incrementally by seed_pending_watch_together/set_watch_together_status,
        # not rebuilt wholesale each run like everything else in this function.

        if state.films:
            conn.cursor().executemany(
                "INSERT INTO films (slug, title, year, entry_id, confidence, last_checked, "
                "offers, rating, poster_url, director, starring, synopsis, genre) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (f.slug, f.title, f.year, f.entry_id, f.confidence, f.last_checked,
                     Jsonb([_offer_to_dict(o) for o in f.offers]), f.rating, f.poster_url,
                     Jsonb(f.director), Jsonb(f.starring), f.synopsis, Jsonb(f.genre))
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

        if state.sarah_watchlist:
            conn.cursor().executemany(
                "INSERT INTO sarah_watchlist (slug) VALUES (%s)",
                [(slug,) for slug in state.sarah_watchlist],
            )

        if state.sarah_extra_films:
            conn.cursor().executemany(
                "INSERT INTO sarah_extra_films (slug, data) VALUES (%s, %s)",
                [(slug, Jsonb(data)) for slug, data in state.sarah_extra_films.items()],
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
