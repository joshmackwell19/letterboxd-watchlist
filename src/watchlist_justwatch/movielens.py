"""MovieLens as an alternative taste-engine corpus.

Letterboxd's own members have to be scraped a page at a time from a home
connection, and a block stops everything (see taste.py). MovieLens is the
GroupLens research dataset (ml-32m: ~32M ratings from ~200k members, half
stars, 0.5-5 — the same scale), downloaded once, with every film carrying
a TMDB id. So Josh's rated films can be matched to it by TMDB id and the
same engine run against its members instead.

The dataset is far too big for Neon's free tier, so it goes into a *local*
Postgres with the same rater_* schema (import_corpus refuses any other
host), and --taste-eval / --taste-recommend point at it with
--corpus-database-url. Josh's own ratings still come from the main
database. Only members who rated at least `min_overlap` of Josh's films
are imported — nobody else could qualify as a neighbour (TasteParams'
min_overlap) — but everything they rated, so their offsets mean the same
as a scraped member's.

Licence: GroupLens allows non-commercial use with a citation, and no
redistribution — the data stays in the gitignored data/movielens/.
F. Maxwell Harper and Joseph A. Konstan. 2015. The MovieLens Datasets:
History and Context. ACM TiiS 5, 4: 19:1-19:19.
"""

from __future__ import annotations

import csv
import json
import os
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import db
from .taste import LAMBDA_FILM, LAMBDA_RATER

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}
# A TMDB match whose release year is further than this from Letterboxd's
# is taken to be a different film with the same title.
YEAR_TOLERANCE = 1
SLUG_PREFIX = "ml:"


def is_local(database_url: str) -> bool:
    return (urlparse(database_url).hostname or "") in LOCAL_HOSTS


def match_tmdb_ids(entries: dict[str, dict], search, cache: dict[str, int | None], *, log=print
                   ) -> dict[str, int | None]:
    """slug -> TMDB id for each diary entry ({title, year}), by TMDB's best
    match for title + year, or None where there's no match within
    YEAR_TOLERANCE. `search(title, year)` is tmdb_client.search_movie;
    `cache` holds earlier answers (updated in place) so a re-run only
    searches what's new."""
    for i, (slug, entry) in enumerate(sorted(entries.items()), start=1):
        if slug in cache or not entry.get("title"):
            continue
        year = int(entry["year"]) if entry.get("year") else None
        match = search(entry["title"], year) or (search(entry["title"], None) if year else None)
        found = (match or {}).get("release_date", "")[:4]
        ok = match is not None and (year is None or (found.isdigit() and abs(int(found) - year) <= YEAR_TOLERANCE))
        cache[slug] = match["id"] if ok else None
        if i % 100 == 0:
            log(f"...matched {i}/{len(entries)}")
    return {slug: cache.get(slug) for slug in entries}


def load_cache(path: str) -> dict[str, int | None]:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_cache(path: str, cache: dict[str, int | None]) -> None:
    with open(path, "w") as f:
        json.dump(cache, f, indent=0, sort_keys=True)


def movie_slugs(links_path: str, slug_by_tmdb: dict[int, str]) -> dict[int, str]:
    """MovieLens movieId -> Josh's Letterboxd slug, for his films the
    dataset has (links.csv: movieId, imdbId, tmdbId). A few films appear
    twice under one TMDB id; the first entry gets the slug, the rest stay
    MovieLens-only films."""
    found: dict[int, str] = {}
    taken: set[str] = set()
    with open(links_path, newline="") as f:
        for row in csv.DictReader(f):
            slug = slug_by_tmdb.get(int(row["tmdbId"])) if row["tmdbId"] else None
            if slug and slug not in taken:
                found[int(row["movieId"])] = slug
                taken.add(slug)
    return found


def unambiguous(matched: dict[str, int | None]) -> dict[int, str]:
    """TMDB id -> slug, leaving out any id two of Josh's films both matched
    — at least one of them is wrong, and there's no telling which."""
    claims = Counter(tmdb_id for tmdb_id in matched.values() if tmdb_id)
    return {tmdb_id: slug for slug, tmdb_id in matched.items() if tmdb_id and claims[tmdb_id] == 1}


def qualifying_users(ratings_path: str, josh_movies: set[int], min_overlap: int) -> set[int]:
    """The members who rated at least `min_overlap` of Josh's films."""
    counts: Counter[int] = Counter()
    with open(ratings_path, newline="") as f:
        reader = csv.reader(f)
        next(reader)
        for user, movie, _rating, _ts in reader:
            if int(movie) in josh_movies:
                counts[int(user)] += 1
    return {user for user, n in counts.items() if n >= min_overlap}


def import_corpus(database_url: str, ml_dir: str, slug_by_tmdb: dict[int, str], *, min_overlap: int,
                  log=print) -> dict:
    """Replaces the rater_* tables at `database_url` (local only) with the
    MovieLens members who rated at least `min_overlap` of Josh's films,
    then computes their baselines. Members are `ml:<userId>`; films are
    Josh's slug where he rated them, else `ml:<movieId>`, so the engine's
    slug lookups work unchanged."""
    if not is_local(database_url):
        raise ValueError("the MovieLens corpus only goes into a local database — "
                         "--corpus-database-url must point at localhost")
    links = os.path.join(ml_dir, "links.csv")
    ratings = os.path.join(ml_dir, "ratings.csv")
    movies = os.path.join(ml_dir, "movies.csv")
    josh_movies = movie_slugs(links, slug_by_tmdb)
    log(f"{len(josh_movies)} of Josh's {len(slug_by_tmdb)} matched films are in MovieLens")
    users = qualifying_users(ratings, set(josh_movies), min_overlap)
    log(f"{len(users)} members rated at least {min_overlap} of them")

    now = datetime.now(timezone.utc).isoformat()
    names: dict[int, str] = {}
    with open(movies, newline="") as f:
        for row in csv.DictReader(f):
            names[int(row["movieId"])] = row["title"]

    with db.connect(database_url) as conn:
        with conn.transaction():
            conn.execute("TRUNCATE rater_screen_hits, rater_screened, rater_ratings, rater_films, raters CASCADE")
            conn.execute("DELETE FROM taste_meta")
            conn.execute("ALTER TABLE rater_ratings DROP CONSTRAINT IF EXISTS rater_ratings_pkey")
            conn.execute("DROP INDEX IF EXISTS rater_ratings_film_idx")
            with conn.cursor().copy("COPY raters (id, username, status, discovered_at, scraped_at) FROM STDIN") as cp:
                for user in sorted(users):
                    cp.write_row((user, f"{SLUG_PREFIX}{user}", "scraped", now, now))
            with conn.cursor().copy("COPY rater_films (id, slug, name) FROM STDIN") as cp:
                for movie, name in names.items():
                    cp.write_row((movie, josh_movies.get(movie, f"{SLUG_PREFIX}{movie}"), name))
            count = 0
            with conn.cursor().copy("COPY rater_ratings (rater_id, film_id, rating) FROM STDIN") as cp, \
                    open(ratings, newline="") as f:
                reader = csv.reader(f)
                next(reader)
                for user, movie, rating, _ts in reader:
                    if int(user) in users:
                        cp.write_row((user, movie, round(float(rating) * 2)))
                        count += 1
            log(f"loaded {count} ratings; indexing")
            conn.execute("ALTER TABLE rater_ratings ADD PRIMARY KEY (rater_id, film_id)")
            conn.execute("CREATE INDEX rater_ratings_film_idx ON rater_ratings (film_id)")
            db.taste_meta_set(conn, "corpus", "movielens")
        conn.execute("ANALYZE")
        mu = db.refresh_rater_baselines(conn, lambda_film=LAMBDA_FILM, lambda_rater=LAMBDA_RATER, now_iso=now)
    return {"matched_in_movielens": len(josh_movies), "members": len(users), "ratings": count, "mu": mu}
