"""The taste engine: film recommendations from the ratings of Letterboxd
members whose taste tracks Josh's (user-user collaborative filtering).

Three stages, each resumable and each behind its own CLI flag (main.py):

1. Collect (--scrape-raters, local only — Letterboxd blocks these pages
   from datacenter IPs). First *screen* for candidates: for the films where
   Josh's rating is furthest from the Letterboxd consensus, fetch the
   members who gave exactly the same rating (/film/<slug>/members/rated/
   <stars>/, 25 people a request) and count how many of those lists each
   turns up on — plus the people Josh follows. Then *scrape* the full rated
   history of the most promising candidates (/<user>/films/, 72 films a
   request). Screening keeps the expensive part pointed at people likely to
   matter: agreeing that Parasite deserves 5★ says nothing, millions did;
   agreeing on a film Josh rated a star and a half above consensus says a
   lot.

2. Baselines (db.refresh_rater_baselines, after every scrape session): the
   corpus's mean rating, and each film's and each rater's shrunk offset
   from it.

3. Score (--taste-eval, --taste-recommend): correlate Josh's residuals
   (rating minus mean, minus his own offset, minus the film's) with each
   rater's over the films they share, shrink that by how few films it is,
   keep the strongest as neighbours, and predict an unseen film as its
   baseline plus the neighbours' weighted residuals on it.

The heavy lifting happens inside Postgres (db.rater_similarities,
db.rater_predictions) so the corpus never crosses the wire. This module
holds the decisions around it — which films to screen, how to weight,
whether any of it beats simpler predictions — kept free of I/O where it
can be, so they're testable.
"""

import random
import time
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import psycopg
from curl_cffi import requests as curl_requests

from . import db
from .letterboxd import (
    LetterboxdBlockedError,
    LetterboxdFetchError,
    fetch_page_strict,
    parse_following_page,
    parse_member_ratings_page,
    parse_rated_films_page,
    parse_tmdb_kind,
)

# Shrinkage, in "ratings' worth" of pull toward zero: a film with 10 corpus
# ratings gets half its raw offset, one with 100 nearly all of it.
LAMBDA_FILM = 10.0
LAMBDA_RATER = 10.0
# Stand-in for the consensus when a diary film has no stored Letterboxd
# average, so it can still be ranked for screening (at half weight — see
# choose_screening_films).
FALLBACK_CONSENSUS = 3.3
# How many screening lists a followed account counts as, so the people
# Josh chose to follow are scraped ahead of one-list strangers.
FOLLOWING_HITS = 2
# A rater's most recent 30 pages (2,160 films) is plenty to correlate on,
# and stops a 10,000-film account eating an hour of an overnight run.
MAX_PROFILE_PAGES = 30
# After a block, --scrape-raters refuses to start again for this long.
BLOCK_COOLDOWN = timedelta(hours=24)
# Picks outside the watchlist need more neighbours behind them than
# anything else — they're the ones nothing else vouches for.
MIN_SUPPORT_PICKS = 5
# Picks are chosen from this many times as many of the best-predicted
# films, since some of them will turn out to be TV.
PICK_POOL = 3
# A scrape that saved something this recently is taken to still be
# running, and --taste-recommend leaves Letterboxd alone rather than
# double the rate the scrape's delays were chosen for.
SCRAPE_ACTIVE_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True)
class TasteParams:
    min_overlap: int = 20      # shared rated films before a rater's correlation counts at all
    lambda_sim: float = 25.0   # correlation * overlap / (overlap + lambda_sim)
    neighbours: int = 100      # how many of the most similar raters predict
    lambda_pred: float = 1.0   # damping toward the baseline when few neighbours rated a film
    min_support: int = 3       # neighbours who rated a film before it gets a prediction at all
    # What a film's expected rating is measured from: "corpus" (the scraped
    # members' own shrunk average) or "letterboxd" (its Letterboxd average,
    # where one is stored — every member's vote rather than a few dozen).
    baseline: str = "corpus"

    @property
    def label(self) -> str:
        name = "Letterboxd + twins" if self.baseline == "letterboxd" else "Taste twins"
        return f"{name} ({self.neighbours} nbrs, damping {self.lambda_pred:g})"


DEFAULT_PARAMS = TasteParams()
EVAL_GRID = [replace(DEFAULT_PARAMS, neighbours=n, lambda_pred=lp)
             for n in (30, 100, 300) for lp in (0.5, 1.0, 2.0)]
# No 300: it can't differ from 100 until more than 100 raters correlate.
LETTERBOXD_GRID = [replace(DEFAULT_PARAMS, baseline="letterboxd", neighbours=n, lambda_pred=lp)
                   for n in (30, 100) for lp in (0.5, 1.0, 2.0)]
# The corpus-centred taste layer added straight onto the Letterboxd
# average, kept in the evaluation to show what re-centring is worth.
SIMPLE_SWAP_LABEL = "Letterboxd + corpus twins (simple swap)"


@dataclass(frozen=True)
class Neighbour:
    rater_id: int
    username: str
    overlap: int
    pearson: float
    weight: float


# --- Pure logic -------------------------------------------------------------


def my_ratings_from_diary(diary: dict[str, dict]) -> dict[str, float]:
    return {slug: float(entry["personal_rating"]) for slug, entry in diary.items()
            if entry.get("personal_rating")}


def community_ratings_from_diary(diary: dict[str, dict]) -> dict[str, float]:
    return {slug: float(entry["rating"]) for slug, entry in diary.items() if entry.get("rating")}


def stars_path(rating: float) -> str:
    """The /members/rated/<stars>/ path segment Letterboxd uses: 5 -> "5",
    4.5 -> "4.5", 0.5 -> ".5" (checked against the live site)."""
    text = f"{rating:g}"
    return text[1:] if text.startswith("0.") else text


def choose_screening_films(my_ratings: dict[str, float], community: dict[str, float],
                           limit: int) -> list[tuple[str, float]]:
    """(slug, Josh's rating) for the `limit` films where his rating says
    most about his taste — furthest from the Letterboxd average, in either
    direction. A film with no stored average is ranked by how extreme the
    rating is, at half weight, since "extreme" is only a guess at "unusual"."""
    def distinctiveness(slug: str) -> float:
        rating = my_ratings[slug]
        consensus = community.get(slug)
        if consensus is None:
            return abs(rating - FALLBACK_CONSENSUS) / 2
        return abs(rating - consensus)

    ranked = sorted(my_ratings, key=lambda slug: (-distinctiveness(slug), slug))
    return [(slug, my_ratings[slug]) for slug in ranked[:limit]]


def my_offset(ratings: dict[str, float], film_bias: dict[str, float], mu: float) -> float:
    """Josh's own shrunk offset, computed the same way as every rater's
    (db.refresh_rater_baselines) so the two sets of residuals line up."""
    if not ratings:
        return 0.0
    return sum(r - mu - film_bias.get(slug, 0.0) for slug, r in ratings.items()) / (len(ratings) + LAMBDA_RATER)


def my_residuals(ratings: dict[str, float], film_info: dict[str, tuple], mu: float,
                 offset: float) -> tuple[list[int], list[float]]:
    """(film ids, residuals) for the rated films the corpus knows."""
    ids: list[int] = []
    residuals: list[float] = []
    for slug, rating in ratings.items():
        info = film_info.get(slug)
        if info is not None:
            ids.append(info[0])
            residuals.append(rating - mu - offset - info[1])
    return ids, residuals


def letterboxd_offset(ratings: dict[str, float], community: dict[str, float]) -> float:
    """How far above (or below) the Letterboxd average Josh rates, on the
    films that have one. Unshrunk: it's measured over hundreds of films."""
    diffs = [r - community[slug] for slug, r in ratings.items() if slug in community]
    return sum(diffs) / len(diffs) if diffs else 0.0


def letterboxd_residuals(ratings: dict[str, float], film_info: dict[str, tuple], community: dict[str, float],
                         offset: float) -> tuple[list[int], list[float], list[float]]:
    """(film ids, residuals, Letterboxd averages) for the rated films the
    corpus knows that have a Letterboxd average — my_residuals measured
    from that average instead of the corpus's estimate of it."""
    ids: list[int] = []
    residuals: list[float] = []
    means: list[float] = []
    for slug, rating in ratings.items():
        info = film_info.get(slug)
        if info is not None and slug in community:
            ids.append(info[0])
            residuals.append(rating - community[slug] - offset)
            means.append(community[slug])
    return ids, residuals, means


def select_neighbours(similarities: list[tuple[int, str, int, float]], params: TasteParams) -> list[Neighbour]:
    """The `params.neighbours` raters with the highest overlap-shrunk
    correlation: 0.9 over 8 shared films is mostly luck, 0.6 over 400 isn't."""
    ranked = [
        Neighbour(rater_id, username, overlap, pearson, pearson * overlap / (overlap + params.lambda_sim))
        for rater_id, username, overlap, pearson in similarities
    ]
    ranked.sort(key=lambda n: (-n.weight, n.username))
    return ranked[:params.neighbours]


def clamp_rating(value: float) -> float:
    return min(5.0, max(0.5, value))


def kfold(slugs: list[str], k: int, seed: int) -> list[set[str]]:
    shuffled = sorted(slugs)
    random.Random(seed).shuffle(shuffled)
    return [set(shuffled[i::k]) for i in range(k)]


def recent_holdout(slugs: list[str], watched_dates: dict[str, str | None], fraction: float = 0.15,
                   minimum: int = 20) -> set[str]:
    """The most recently watched `fraction` of `slugs` — the "learn from
    everything before, predict what came after" test that mirrors real use.
    Empty when too few films carry a watched date to make it meaningful."""
    dated = sorted((watched_dates[s], s) for s in slugs if watched_dates.get(s))
    if len(dated) < minimum:
        return set()
    count = max(1, int(len(dated) * fraction))
    return {slug for _, slug in dated[-count:]}


def rmse(pairs: list[tuple[float, float]]) -> float:
    return (sum((p - a) ** 2 for p, a in pairs) / len(pairs)) ** 0.5


def mae(pairs: list[tuple[float, float]]) -> float:
    return sum(abs(p - a) for p, a in pairs) / len(pairs)


def _average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for idx in order[i:j + 1]:
            ranks[idx] = (i + j) / 2
        i = j + 1
    return ranks


def spearman(pairs: list[tuple[float, float]]) -> float | None:
    """Rank correlation of predicted vs actual (ties share an average rank);
    None when either side is constant."""
    if len(pairs) < 2:
        return None
    xs = _average_ranks([p for p, _ in pairs])
    ys = _average_ranks([a for _, a in pairs])
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


def top_share_mean(pairs: list[tuple[float, float]], fraction: float = 0.1) -> float:
    """What Josh actually rated, on average, the films a method ranked in
    its top `fraction` — closest to "are its top picks any good?"."""
    count = max(1, int(len(pairs) * fraction))
    top = sorted(pairs, key=lambda pair: -pair[0])[:count]
    return sum(a for _, a in top) / count


# --- Collecting the corpus (--scrape-raters) ---------------------------------


class RequestBudgetExhausted(Exception):
    pass


class PoliteFetcher:
    """Every corpus request goes through here: a randomised pause before
    each one (delay to 1.8x delay), a longer one every 100, a hard cap per
    run, and fetch_page_strict's stop-on-first-block."""

    def __init__(self, *, delay_seconds: float, max_requests: int, fetch=None, sleep=time.sleep,
                 rand=random.random):
        self.delay_seconds = delay_seconds
        self.max_requests = max_requests
        self.requests = 0
        self._sleep = sleep
        self._rand = rand
        if fetch is None:
            session = curl_requests.Session()
            fetch = lambda url: fetch_page_strict(session, url)  # noqa: E731
        self._fetch = fetch

    def get(self, url: str) -> str | None:
        if self.requests >= self.max_requests:
            raise RequestBudgetExhausted()
        if self.requests:
            pause = self.delay_seconds * (1 + 0.8 * self._rand())
            if self.requests % 100 == 0:
                pause += 30 + 30 * self._rand()
            self._sleep(pause)
        self.requests += 1
        return self._fetch(url)


def screen_hits(members: list[tuple[str, int]], half_stars: int, exclude: set[str]) -> dict[str, int]:
    """One hit per member of a /members/rated/ page who gave exactly
    `half_stars` (the page should hold nothing else — checked anyway) and
    isn't in `exclude` (Josh himself turns up on every one of his own lists)."""
    excluded = {u.lower() for u in exclude}
    return dict(Counter(u for u, rating in members if rating == half_stars and u.lower() not in excluded))


def scrape_profile(fetcher: PoliteFetcher, username: str, *, max_pages: int = MAX_PROFILE_PAGES
                   ) -> list[tuple[str, str | None, int]] | None:
    """Every rated film on a member's /films/ grid, newest first, up to
    `max_pages`. None for a member who no longer exists; [] for one whose
    first page rates nothing — someone who logs without rating isn't worth
    paging through."""
    rated: list[tuple[str, str | None, int]] = []
    for page in range(1, max_pages + 1):
        html = fetcher.get(f"https://letterboxd.com/{username}/films/page/{page}/")
        if html is None:
            return None if page == 1 else rated
        page_rated, has_next = parse_rated_films_page(html)
        if page == 1 and not page_rated:
            return []
        rated.extend(page_rated)
        if not has_next:
            break
    return rated


def _cooldown_remaining(blocked_at: str | None, now: datetime) -> timedelta | None:
    if not blocked_at:
        return None
    remaining = datetime.fromisoformat(blocked_at) + BLOCK_COOLDOWN - now
    return remaining if remaining > timedelta(0) else None


def scrape_raters(database_url: str, username: str, my_ratings: dict[str, float], community: dict[str, float], *,
                  screen_films: int, max_raters: int, fetcher: PoliteFetcher, log=print,
                  now=lambda: datetime.now(timezone.utc)) -> str:
    """One collection session: seed from who Josh follows (once ever),
    screen up to `screen_films` films not already screened, then scrape up
    to `max_raters` of the best candidates. Stops at the first block and
    records it, so the next run waits out BLOCK_COOLDOWN. Every screened
    page and every rater is committed as it finishes, so stopping at any
    point — block, budget, Ctrl-C, a sleeping Mac — loses at most the one
    in flight. Returns why it stopped: done / cooldown / busy / blocked /
    budget / network / interrupted."""
    conn = db.connect(database_url)
    scraped_this_run = 0
    outcome = "done"
    try:
        remaining = _cooldown_remaining(db.taste_meta_get(conn, "blocked_at"), now())
        if remaining is not None:
            hours = remaining.total_seconds() / 3600
            log(f"Letterboxd blocked the last run — not starting for another {hours:.1f}h.")
            return "cooldown"
        if scrape_looks_active(db.latest_scrape_activity(conn), now()):
            log(f"--scrape-raters or --record-screen-hits saved something in the last "
                f"{SCRAPE_ACTIVE_WINDOW.total_seconds() / 60:.0f} minutes — not starting alongside it. If that was "
                f"a run that has since stopped, try again once that long has passed.")
            return "busy"

        if not db.taste_meta_get(conn, "following_seeded_at"):
            page = 1
            followed: dict[str, int] = {}
            while True:
                html = fetcher.get(f"https://letterboxd.com/{username}/following/page/{page}/")
                people, has_next = parse_following_page(html) if html else ([], False)
                followed.update({u: FOLLOWING_HITS for u, _ in people if u.lower() != username.lower()})
                if not has_next:
                    break
                page += 1
            db.add_rater_candidates(conn, followed, now().isoformat())
            db.taste_meta_set(conn, "following_seeded_at", now().isoformat())
            log(f"Seeded {len(followed)} candidates from the accounts you follow.")

        done = db.screened_pages(conn)
        todo = [(slug, stars) for slug, stars in choose_screening_films(my_ratings, community, screen_films)
                if (slug, stars, 1) not in done]
        for i, (slug, stars) in enumerate(todo, start=1):
            html = fetcher.get(f"https://letterboxd.com/film/{slug}/members/rated/{stars_path(stars)}/")
            members, _ = parse_member_ratings_page(html) if html else ([], False)
            hits = screen_hits(members, round(stars * 2), {username})
            screened_at = now().isoformat()
            # One transaction, so an interruption can't leave hits counted
            # for a page that isn't marked (and so gets screened, and
            # counted, again) or marked without its members recorded.
            with conn.transaction():
                db.add_rater_candidates(conn, hits, screened_at)
                db.mark_screened(conn, slug, stars, 1, screened_at, member_count=len(members))
                db.record_screen_hits(conn, slug, stars, 1,
                                      list(db.rater_ids_by_username(conn, list(hits)).values()), "screening")
            if i % 20 == 0 or i == len(todo):
                log(f"Screened {i}/{len(todo)} films ({fetcher.requests} requests so far).")

        queue = db.next_raters_to_scrape(conn, max_raters, {username})
        for i, (rater_id, rater) in enumerate(queue, start=1):
            rated = scrape_profile(fetcher, rater)
            if rated is None:
                db.set_rater_status(conn, rater_id, "missing", now().isoformat())
                note = "no longer exists"
            elif not rated:
                db.set_rater_status(conn, rater_id, "unrated", now().isoformat())
                note = "rates nothing recent, skipped"
            else:
                db.save_rater_ratings(conn, rater_id, rated, now().isoformat())
                scraped_this_run += 1
                note = f"{len(rated)} ratings"
            log(f"[{i}/{len(queue)}] {rater}: {note} ({fetcher.requests} requests so far)")
    except LetterboxdBlockedError as exc:
        db.taste_meta_set(conn, "blocked_at", now().isoformat())
        log(f"STOPPED — Letterboxd is refusing requests ({exc}). Everything up to here is saved; "
            f"the next run won't start for {BLOCK_COOLDOWN.total_seconds() / 3600:.0f}h.")
        outcome = "blocked"
    except RequestBudgetExhausted:
        log(f"Stopped at the {fetcher.max_requests}-request cap for one run (raise it with --max-requests).")
        outcome = "budget"
    except LetterboxdFetchError as exc:
        log(f"Stopped — requests keep failing ({exc}). Network down, or the Mac went to sleep? "
            f"Everything up to here is saved.")
        outcome = "network"
    except psycopg.OperationalError as exc:
        log(f"Stopped — lost the database connection ({exc}). Everything up to the last finished "
            f"rater is saved.")
        outcome = "network"
    except KeyboardInterrupt:
        log("Interrupted — everything up to the last finished rater is saved.")
        outcome = "interrupted"
    finally:
        try:
            if scraped_this_run:
                log("Refreshing baselines...")
                db.refresh_rater_baselines(conn, lambda_film=LAMBDA_FILM, lambda_rater=LAMBDA_RATER,
                                           now_iso=now().isoformat())
            summary = db.rater_corpus_summary(conn)
            statuses = ", ".join(f"{count} {status}" for status, count in sorted(summary["raters"].items()))
            log(f"Corpus: {summary['ratings']:,} ratings of {summary['films']:,} films from raters: "
                f"{statuses or 'none'}. {fetcher.requests} requests this run.")
        except psycopg.Error as exc:
            log(f"Couldn't finish up in the database ({exc}) — the next --taste-eval or "
                f"--taste-recommend refreshes the baselines itself.")
        finally:
            conn.close()
    return outcome


# --- Who each screening page recruited (--record-screen-hits) ---------------


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def followed_rater_ids(raters: list[tuple], following_seeded_at: str | None) -> set[int]:
    """The accounts Josh follows: seeded all at once, just before taste_meta
    'following_seeded_at' was written — not "before the first page", since a
    page's own recruits are inserted a moment before it's marked. `raters`
    rows are db.rater_recruitment's."""
    if following_seeded_at is None:
        return set()
    seeded = _ts(following_seeded_at)
    return {rater_id for rater_id, _, _, discovered_at, _ in raters if _ts(discovered_at) <= seeded}


def first_found_pages(raters: list[tuple], timeline: list[tuple], followed: set[int]
                      ) -> dict[int, tuple[str, float, int]]:
    """rater id -> the screening page that first found them. Screening
    inserts a page's new members with one timestamp just before marking the
    page screened, so a rater's first page is the first one marked at or
    after their discovery — exact, with no request. `timeline` is
    db.screening_timeline's, oldest first."""
    times = [_ts(row[3]) for row in timeline]
    found: dict[int, tuple[str, float, int]] = {}
    for rater_id, _, _, discovered_at, _ in raters:
        if rater_id in followed:
            continue
        i = bisect_left(times, _ts(discovered_at))
        if i < len(timeline):
            found[rater_id] = tuple(timeline[i][:3])
    return found


def reconcile_recruits(selected: list[tuple[int, int, str]], followed: set[int],
                       recorded: dict[int, set[tuple]], same_score: dict[int, set[tuple]],
                       page_times: dict[tuple, datetime]) -> tuple[dict[int, str], list[tuple[tuple, int]]]:
    """Checks the recorded pages of each scraped rater (id, screen_hits,
    discovered_at) against their screening count — one hit a page, plus
    FOLLOWING_HITS for someone Josh follows. "exact": they account for
    every hit. "extra": more are recorded (a re-fetch saw them on a page
    they weren't on first time; leaving them out of that film too only
    costs the engine). "missing": fewer, so a page they were on wasn't
    seen again — for those, every page fetched after their discovery that
    they rated at its score and aren't recorded on is returned as an
    inferred hit, erring toward leaving them out rather than letting a
    recruit's rating through."""
    status: dict[int, str] = {}
    inferred: list[tuple[tuple, int]] = []
    for rater_id, hits, discovered_at in selected:
        expected = hits - (FOLLOWING_HITS if rater_id in followed else 0)
        have = recorded.get(rater_id, set())
        if len(have) == expected:
            status[rater_id] = "exact"
        elif len(have) > expected:
            status[rater_id] = "extra"
        else:
            status[rater_id] = "missing"
            inferred += [(page, rater_id) for page in sorted(same_score.get(rater_id, set()) - have)
                         if page_times[page] >= _ts(discovered_at)]
    return status, inferred


class _ReconnectingDatabase:
    """Runs db calls through a connection that's replaced when it dies, up
    to `attempts` times in a row — so a long local run doesn't lose its
    place to one dropped connection. Only for statements that are safe to
    repeat: an insert that ignores conflicts, an update to fixed values."""

    def __init__(self, database_url: str, log, attempts: int = 4, sleep=time.sleep):
        self._url = database_url
        self._log = log
        self._attempts = attempts
        self._sleep = sleep
        self.conn = db.connect(database_url)

    def __call__(self, work):
        for attempt in range(1, self._attempts + 1):
            try:
                if self.conn is None:
                    self.conn = db.connect(self._url)
                return work(self.conn)
            except psycopg.OperationalError as exc:
                if attempt == self._attempts:
                    raise
                self._log(f"Lost the database connection ({str(exc).splitlines()[0]}); reconnecting...")
                try:
                    if self.conn is not None:
                        self.conn.close()
                except psycopg.Error:
                    pass
                self.conn = None
                self._sleep(5 * attempt)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()


def record_screen_hits(database_url: str, username: str, *, fetcher: PoliteFetcher, log=print,
                       now=lambda: datetime.now(timezone.utc)) -> str:
    """Records who each screening page recruited, for pages screened before
    scrape_raters recorded it itself — what --taste-eval needs to test a
    screened film without the raters it recruited. The page that first
    found each rater comes from stored timestamps, exactly; the rest by
    fetching each page once more (only page 1, as screening did), keeping
    members already known before the page was first fetched. A page's
    re-fetch can miss someone who has since dropped off it, which is the
    direction that leaks, so it finishes with reconcile_recruits and
    records inferred hits for anyone left short. Same politeness as the
    scrape — cooldown, the scrape heartbeat, stop at the first block — and
    resumable: a page is marked only once it's done. Database writes are
    retried on a fresh connection if one drops; a page request never is.
    Returns why it stopped: done / cooldown / busy / blocked / failed /
    budget / network / interrupted."""
    database = _ReconnectingDatabase(database_url, log)
    outcome = "done"
    try:
        remaining = _cooldown_remaining(database(lambda c: db.taste_meta_get(c, "blocked_at")), now())
        if remaining is not None:
            log(f"Letterboxd blocked a recent run — not starting for another {remaining.total_seconds() / 3600:.1f}h.")
            return "cooldown"
        timeline = database(db.screening_timeline)
        todo = [row for row in timeline if row[4] is None]
        if todo and scrape_looks_active(database(db.latest_scrape_activity), now()):
            log(f"--scrape-raters or --record-screen-hits saved something in the last "
                f"{SCRAPE_ACTIVE_WINDOW.total_seconds() / 60:.0f} minutes — not fetching alongside it. If that was "
                f"a run that has since stopped, try again once that long has passed.")
            return "busy"
        raters = database(db.rater_recruitment)
        followed = followed_rater_ids(raters, database(lambda c: db.taste_meta_get(c, "following_seeded_at")))
        discovered = {rater_id: _ts(discovered_at) for rater_id, _, _, discovered_at, _ in raters}

        if todo:
            first = defaultdict(list)
            todo_pages = {tuple(row[:3]) for row in todo}
            for rater_id, page in first_found_pages(raters, timeline, followed).items():
                if page in todo_pages:
                    first[page].append(rater_id)
            rows = [(*page, rater_id) for page, rater_ids in first.items() for rater_id in rater_ids]
            database(lambda c: db.record_screen_hits_many(c, rows, "first-found"))
            log(f"Recorded the page that first found each of {len(rows)} members (from discovery "
                f"timestamps, no requests); re-fetching {len(todo)} pages for the rest.")
            stability: list[float] = []
            unknown = 0
            try:
                for i, (slug, stars, page, fetched_at, _) in enumerate(todo, start=1):
                    html = fetcher.get(f"https://letterboxd.com/film/{slug}/members/rated/{stars_path(stars)}/")
                    members, _ = parse_member_ratings_page(html) if html else ([], False)
                    if html is not None and not members:
                        log(f"STOPPED at /film/{slug}/members/rated/{stars_path(stars)}/: no members the parser "
                            f"recognises (has Letterboxd's markup changed?). Nothing recorded for it.")
                        outcome = "failed"
                        break
                    names = list(screen_hits(members, round(stars * 2), {username}))
                    known = database(lambda c: db.rater_ids_by_username(c, names))
                    unknown += len(names) - len(known)
                    # Only someone already known when the page was first
                    # fetched can have been counted on it then.
                    eligible = [rid for rid in known.values()
                                if rid in discovered and discovered[rid] <= _ts(fetched_at)]

                    def save(c, slug=slug, stars=stars, page=page, eligible=eligible, count=len(members)):
                        with c.transaction():
                            db.record_screen_hits(c, slug, stars, page, eligible, "refetch")
                            db.set_screen_page_refetched(c, slug, stars, page, now().isoformat(), count)
                    database(save)
                    group = set(first.get((slug, stars, page), []))
                    if group:
                        stability.append(len(group & set(known.values())) / len(group))
                    if i % 20 == 0 or i == len(todo):
                        log(f"Re-fetched {i}/{len(todo)} pages ({fetcher.requests} requests so far).")
            finally:
                if stability:
                    log(f"Page stability: {sum(stability) / len(stability):.0%} of the members each page first "
                        f"found were on it again ({sum(1 for x in stability if x < 1)} of {len(stability)} pages "
                        f"had lost someone); {unknown} members now on the pages were never candidates.")

        if outcome == "done":
            database(lambda c: _reconcile_and_infer(c, raters, followed, log))
    except LetterboxdBlockedError as exc:
        log(f"STOPPED — Letterboxd is refusing requests ({exc}). Pages done so far are saved; "
            f"the next run won't start for {BLOCK_COOLDOWN.total_seconds() / 3600:.0f}h.")
        outcome = "blocked"
        try:
            database(lambda c: db.taste_meta_set(c, "blocked_at", now().isoformat()))
        except psycopg.Error as write_exc:
            log(f"...but the block couldn't be recorded in the database ({write_exc}). Don't run --scrape-raters "
                f"or --record-screen-hits for {BLOCK_COOLDOWN.total_seconds() / 3600:.0f}h.")
    except RequestBudgetExhausted:
        log(f"Stopped at the {fetcher.max_requests}-request cap (raise it with --max-requests).")
        outcome = "budget"
    except LetterboxdFetchError as exc:
        log(f"Stopped — requests keep failing ({exc}). Pages done so far are saved; run it again.")
        outcome = "network"
    except psycopg.OperationalError as exc:
        log(f"Stopped — lost the database connection and couldn't get it back ({exc}). Pages done so far "
            f"are saved; run it again.")
        outcome = "network"
    except KeyboardInterrupt:
        log("Interrupted — pages done so far are saved.")
        outcome = "interrupted"
    finally:
        try:
            database.close()
        except psycopg.Error:
            pass
    return outcome


def _reconcile_and_infer(conn, raters: list[tuple], followed: set[int], log) -> None:
    timeline = db.screening_timeline(conn)
    if any(row[4] is None for row in timeline):
        log("Some pages aren't recorded yet, so recruits can't be reconciled — run it again to finish.")
        return
    page_times = {tuple(row[:3]): _ts(row[3]) for row in timeline}
    recorded: dict[int, set[tuple]] = defaultdict(set)
    for slug, stars, page, rater_id, source in db.recorded_screen_hits(conn):
        if source in CERTAIN_SOURCES:
            recorded[rater_id].add((slug, stars, page))
    same_score: dict[int, set[tuple]] = defaultdict(set)
    for slug, stars, page, rater_id in db.same_score_ratings(conn):
        same_score[rater_id].add((slug, stars, page))
    selected = [(rater_id, hits, discovered_at) for rater_id, status, hits, discovered_at, _ in raters
                if status == "scraped"]
    status, inferred = reconcile_recruits(selected, followed, recorded, same_score, page_times)
    db.record_screen_hits_many(conn, [(*page, rater_id) for page, rater_id in inferred], "inferred")
    counts = Counter(status.values())
    log(f"Reconciled against screening counts: {counts['exact']} scraped raters' pages are known for certain; "
        f"for the other {counts['missing'] + counts['extra']}, every screened film they rated at its score after "
        f"being found counts as one they may have been recruited through ({len(inferred)} page hits recorded as "
        f"inferred).")


# --- Scoring -----------------------------------------------------------------


def _ensure_mu(conn) -> float | None:
    """The corpus mean, refreshing every baseline first if they predate the
    newest scraped rater — a scrape that died without finishing up (lid
    closed, connection dropped) otherwise leaves its raters at a zero
    offset until the next scrape happens to finish cleanly."""
    mu = db.taste_meta_get(conn, "mu")
    refreshed_at = db.taste_meta_get(conn, "baselines_at")
    latest = db.latest_rater_scrape(conn)
    if mu is None or (latest is not None and (refreshed_at is None or latest > refreshed_at)):
        mu = db.refresh_rater_baselines(conn, lambda_film=LAMBDA_FILM, lambda_rater=LAMBDA_RATER,
                                        now_iso=max(latest or "", datetime.now(timezone.utc).isoformat()))
    return mu


@dataclass
class Recruitment:
    """Who each screened film recruited, and whether each recruit would have
    been scraped without it — what lets --taste-eval test Josh's most
    distinctive ratings without the raters found *because* they matched
    him on exactly those films. Built by load_recruitment."""
    screened: set[str]                     # every screened slug
    unrecorded: set[str]                   # screened slugs with a page whose members aren't recorded yet
    recruits: dict[str, set[int]]          # slug -> scraped raters recorded on any of its pages (inferred too)
    pages_of_slug: dict[str, set[tuple]]   # slug -> its screening pages
    counted: dict[int, set[tuple]]         # rater -> pages they were (or, if unsure, may have been) on before queued
    unsure: set[int]                       # raters whose recorded pages don't reconcile with their count
    selection: dict[int, tuple[int | None, int]]  # rater -> (screening hits when queued, None if unknown; their run)
    cutoffs: dict[int, tuple[int, int]]    # run -> (hits, id) of the last rater that run took, of those known

    def kept_without(self, rater_id: int, removed: set[tuple]) -> bool:
        """Would this rater still have been scraped had the `removed` pages
        never been screened? Their queue position is replayed against the
        last rater their run took (screen_hits desc, id), ignoring that the
        same pages' other recruits would drop too — which could only have
        let them in more easily, so this errs toward leaving them out. For
        a rater whose recorded pages don't reconcile, every page they may
        have been on counts as lost; one whose hits when queued aren't known
        never counts as kept."""
        hits, run = self.selection.get(rater_id, (None, None))
        if hits is None:
            return False
        lost = len(self.counted.get(rater_id, set()) & removed)
        if not lost:
            return True
        cut_hits, cut_id = self.cutoffs[run]
        return (hits - lost, -rater_id) >= (cut_hits, -cut_id)

    def exclusions(self, slug: str, mode: str) -> set[int]:
        """The recruits of `slug` to leave out when predicting it:
        "counterfactual" only those who wouldn't have been scraped without
        its pages, "all" every one, "none" nobody (the leaky ceiling)."""
        recruits = self.recruits.get(slug, set())
        if mode == "all":
            return set(recruits)
        if mode == "counterfactual":
            removed = self.pages_of_slug.get(slug, set())
            return {r for r in recruits if not self.kept_without(r, removed)}
        return set()

    def dropped_without(self, slugs: set[str], mode: str) -> set[int]:
        """Raters to take out of the corpus altogether when every page of
        `slugs` is treated as never screened — the time split's version,
        where recruits found through *later* films shouldn't be around to
        predict anything."""
        recruits = set().union(*(self.recruits.get(s, set()) for s in slugs)) if slugs else set()
        if mode == "all":
            return recruits
        if mode == "counterfactual":
            removed = set().union(*(self.pages_of_slug.get(s, set()) for s in slugs)) if slugs else set()
            return {r for r in recruits if not self.kept_without(r, removed)}
        return set()


# Where a rater's page is known for certain: recorded as it was screened, or
# rebuilt from discovery timestamps. A re-fetch shows who's on page 1 *now*,
# and screening pages turn over within hours, so it proves nothing either way.
CERTAIN_SOURCES = {"screening", "first-found"}


def build_recruitment(timeline: list[tuple], hits: list[tuple], raters: list[tuple], followed: set[int],
                      same_score: list[tuple]) -> Recruitment:
    """Recruitment from db.screening_timeline, db.recorded_screen_hits,
    db.rater_recruitment and db.same_score_ratings rows.

    A rater counts as exact when their certain pages (CERTAIN_SOURCES)
    account for every screening hit — then those are all their pages. For
    anyone else, every page they might have been on (re-fetched, or a
    screened film they rated at its score after being discovered) is
    treated as theirs: they're a recruit of all those films, and all of
    them count as lost when replaying their place in the queue. That errs
    toward leaving them out, which only costs the engine.

    A run is one --scrape-raters session: every rater it took was queued
    after the same set of pages had been screened, so runs are told apart
    by how many pages predate a rater's scrape. A rater's hit count when
    queued is known for an exact rater, and — whatever their pages — for
    one queued after every page, since screen_hits is then exactly what
    they were queued with. Otherwise it's unknown and they're never kept.
    Each run's cut-off is taken over the raters whose counts are known,
    which can only put it above the true one: again the cautious side."""
    page_times = {tuple(row[:3]): _ts(row[3]) for row in timeline}
    screened = {row[0] for row in timeline}
    unrecorded = {row[0] for row in timeline if row[4] is None}
    pages_of_slug: dict[str, set[tuple]] = defaultdict(set)
    for page in page_times:
        pages_of_slug[page[0]].add(page)
    certain: dict[int, set[tuple]] = defaultdict(set)
    possible: dict[int, set[tuple]] = defaultdict(set)
    for slug, stars, page, rater_id, source in hits:
        (certain if source in CERTAIN_SOURCES else possible)[rater_id].add((slug, stars, page))
    for slug, stars, page, rater_id in same_score:
        possible[rater_id].add((slug, stars, page))

    times = sorted(page_times.values())
    last_page = times[-1] if times else None
    recruits: dict[str, set[int]] = defaultdict(set)
    counted: dict[int, set[tuple]] = {}
    selection: dict[int, tuple[int | None, int]] = {}
    cutoffs: dict[int, tuple[int, int]] = {}
    unsure: set[int] = set()
    for rater_id, status, count, discovered_at, scraped_at in raters:
        pages = set(certain.get(rater_id, set()))
        exact = len(pages) == count - (FOLLOWING_HITS if rater_id in followed else 0)
        if not exact:
            found = _ts(discovered_at)
            pages |= {page for page in possible.get(rater_id, set())
                      if page in page_times and page_times[page] >= found}
        if status == "scraped":
            for page in pages:
                recruits[page[0]].add(rater_id)
        if scraped_at is None:
            continue
        queued = _ts(scraped_at)
        counted[rater_id] = {page for page in pages if page_times[page] < queued}
        run = bisect_left(times, queued)
        if exact:
            hits_then = len(counted[rater_id]) + (FOLLOWING_HITS if rater_id in followed else 0)
        else:
            unsure.add(rater_id)
            hits_then = count if last_page is not None and last_page < queued else None
        selection[rater_id] = (hits_then, run)
        if hits_then is not None:
            cutoffs[run] = min(cutoffs.get(run, (hits_then, -rater_id)), (hits_then, -rater_id))
    return Recruitment(
        screened=screened, unrecorded=unrecorded, recruits=dict(recruits), pages_of_slug=dict(pages_of_slug),
        counted=counted, unsure=unsure, selection=selection,
        cutoffs={run: (h, -neg_id) for run, (h, neg_id) in cutoffs.items()},
    )


def load_recruitment(conn) -> Recruitment:
    raters = db.rater_recruitment(conn)
    return build_recruitment(db.screening_timeline(conn), db.recorded_screen_hits(conn), raters,
                             followed_rater_ids(raters, db.taste_meta_get(conn, "following_seeded_at")),
                             db.same_score_ratings(conn))


def stratified_folds(testable: list[str], screened: set[str], k: int, seed: int) -> list[set[str]]:
    """kfold, but with the screened films and the rest split separately, so
    every fold gets its share of Josh's most distinctive ratings."""
    return [a | b for a, b in zip(kfold([s for s in testable if s in screened], k, seed),
                                  kfold([s for s in testable if s not in screened], k, seed))]


def regressed_toward_mean(train: dict[str, float], base: dict[str, float]) -> tuple[float, float]:
    """(Josh's mean, slope) for the placebo, "Letterboxd average
    recalibrated to you": the least-squares fit of rating - mean on
    base - mean, through the origin, over the training films that have a
    base. It knows nothing about Josh's taste, only how his ratings scale
    against Letterboxd's — a slope under 1 pulls far-off films back toward
    his mean, over 1 stretches them. Any gain a taste row makes in stars of
    error has to beat what this gets for free; it can't reorder films, so
    for ranking the plain baseline is the comparison."""
    mean = sum(train.values()) / len(train)
    xs = [(base[s] - mean, r - mean) for s, r in train.items() if s in base]
    sxx = sum(x * x for x, _ in xs)
    return mean, (sum(x * y for x, y in xs) / sxx if sxx else 1.0)


def paired_bootstrap(preds: dict[str, float], baseline: dict[str, float], actual: dict[str, float],
                     clusters: dict[str, str], *, iterations: int = 1000, seed: int = 0) -> dict:
    """RMSE and Spearman of `preds` minus the same for `baseline`, on the
    films both cover, with 95% intervals from resampling whole clusters
    (a director's films together — they're not independent tests) with
    replacement."""
    slugs = sorted(set(preds) & set(baseline))
    groups: dict[str, list[str]] = defaultdict(list)
    for s in slugs:
        groups[clusters.get(s) or s].append(s)
    keys = sorted(groups)

    def delta(films: list[str]) -> tuple[float, float | None]:
        a = [(preds[s], actual[s]) for s in films]
        b = [(baseline[s], actual[s]) for s in films]
        sa, sb = spearman(a), spearman(b)
        return rmse(a) - rmse(b), (sa - sb if sa is not None and sb is not None else None)

    point = delta(slugs)
    rng = random.Random(seed)
    d_rmse, d_spearman = [], []
    for _ in range(iterations):
        films = [s for key in (rng.choice(keys) for _ in keys) for s in groups[key]]
        dr, ds = delta(films)
        d_rmse.append(dr)
        if ds is not None:
            d_spearman.append(ds)

    def interval(values: list[float]) -> tuple[float, float]:
        values = sorted(values)
        return values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]

    return {"films": len(slugs), "d_rmse": point[0], "rmse_ci": interval(d_rmse),
            "d_spearman": point[1], "spearman_ci": interval(d_spearman) if d_spearman else None}


def _neighbour_offsets(conn, similarities: list[tuple[int, str, int, float]], cfg: TasteParams, mu: float,
                       target_ids: list[int], film_means: dict[int, float] | None = None, **exclusion) -> dict[int, dict]:
    """film id -> db.rater_predictions row, for the targets enough of
    `cfg`'s neighbours rated. `exclusion`: offset_means / leave_out /
    bias_override, passed through."""
    neighbours = select_neighbours(similarities, cfg)
    if not neighbours:
        return {}
    rows = db.rater_predictions(
        conn, [n.rater_id for n in neighbours], [n.weight for n in neighbours], mu=mu,
        lambda_pred=cfg.lambda_pred, min_support=cfg.min_support, target_film_ids=target_ids,
        film_means=film_means, lambda_rater=LAMBDA_RATER, **exclusion,
    )
    return {row["film_id"]: row for row in rows}


REGRESSED_LABEL = "Letterboxd avg recalibrated to you (placebo)"
PLACEBO_TWINS_LABEL = "Placebo + twins (100 nbrs, damping 1)"
BASELINE_LABEL = "Letterboxd average + your offset"
LETTERBOXD_DEFAULT = replace(DEFAULT_PARAMS, baseline="letterboxd")
# Fixed before looking: the comparisons the conclusion rests on. Everything
# else in the grid is exploratory — picking its best cell would be picking
# on the test set.
KEY_METHODS = [LETTERBOXD_DEFAULT.label, PLACEBO_TWINS_LABEL, DEFAULT_PARAMS.label, SIMPLE_SWAP_LABEL,
               REGRESSED_LABEL, "Corpus consensus + your offset", "Your average"]
ENGINE_METHODS = [LETTERBOXD_DEFAULT.label, PLACEBO_TWINS_LABEL, DEFAULT_PARAMS.label, SIMPLE_SWAP_LABEL]


def _evaluate_split(conn, train: dict[str, float], test: list[str], actual: dict[str, float],
                    film_info: dict[str, tuple], community: dict[str, float], mu: float,
                    configs: list[TasteParams], screened: set[str], exclusions: dict[str, set[int]],
                    dropped: set[int]) -> tuple[dict[str, dict[str, float]], dict[str, set[str]], dict[str, int]]:
    """(method -> {slug: prediction}, method -> slugs its taste layer
    covered, baseline -> raters who qualified as similar) for one
    train/test split. `exclusions`: per test film, raters whose ratings of
    it are left out, with its corpus offset recomputed without them
    everywhere it's used; `dropped`: raters taken out of the corpus
    altogether (the time split's recruits of later films). `similar` also
    carries the placebo's fitted slope, under "slope"."""
    film_bias = {slug: info[1] for slug, info in film_info.items()}
    offset = my_offset(train, film_bias, mu)
    train_mean = sum(train.values()) / len(train)
    lb_offset = letterboxd_offset(train, community)
    # The taste layers built on the Letterboxd average fall back exactly as
    # the plain baseline does where there's no average, so any difference
    # between them is the taste layer's doing.
    lb_base = {s: community[s] + lb_offset if s in community else train_mean for s in test}
    mean, slope = regressed_toward_mean(train, {s: community[s] + lb_offset for s in train if s in community})
    regressed = {s: mean + slope * (lb_base[s] - mean) if s in community else train_mean for s in test}

    leave_out = [(film_info[s][0], r) for s in test for r in exclusions.get(s, ())]
    affected = [film_info[s][0] for s in test if exclusions.get(s)]
    override = db.leave_out_film_bias(conn, affected, leave_out, mu=mu, lambda_film=LAMBDA_FILM)
    test_bias = {s: override.get(film_info[s][0], film_bias[s]) for s in test}
    exclusion = {"leave_out": leave_out, "bias_override": override}

    def on_letterboxd(s: str, by_id: dict[int, dict]) -> float:
        row = by_id.get(film_info[s][0]) if s in community else None
        return clamp_rating(lb_base[s] + row["nb_offset"] if row else lb_base[s])

    preds: dict[str, dict[str, float]] = {
        "Your average": {s: train_mean for s in test},
        BASELINE_LABEL: {s: clamp_rating(lb_base[s]) for s in test},
        REGRESSED_LABEL: {s: clamp_rating(regressed[s]) for s in test},
        "Corpus consensus + your offset": {s: clamp_rating(mu + offset + test_bias[s]) for s in test},
    }
    covered: dict[str, set[str]] = {}
    similar: dict[str, int] = {"slope": slope}
    target_ids = [film_info[s][0] for s in test]

    corpus_configs = [c for c in configs if c.baseline == "corpus"]
    if corpus_configs:
        ids, residuals = my_residuals(train, film_info, mu, offset)
        similarities = [row for row in db.rater_similarities(conn, ids, residuals, mu=mu,
                                                             min_overlap=corpus_configs[0].min_overlap)
                        if row[0] not in dropped]
        similar["corpus"] = len(similarities)
        default_rows = None
        for cfg in corpus_configs:
            by_id = _neighbour_offsets(conn, similarities, cfg, mu, target_ids, **exclusion)
            method = {}
            for s in test:
                row = by_id.get(film_info[s][0])
                base = mu + offset + test_bias[s]
                method[s] = clamp_rating(base + row["nb_offset"] if row else base)
            preds[cfg.label] = method
            covered[cfg.label] = {s for s in test if film_info[s][0] in by_id}
            if cfg == DEFAULT_PARAMS:
                default_rows = by_id
        if default_rows is not None:
            preds[SIMPLE_SWAP_LABEL] = {s: on_letterboxd(s, default_rows) for s in test}
            covered[SIMPLE_SWAP_LABEL] = {s for s in test if s in community and film_info[s][0] in default_rows}

    letterboxd_configs = [c for c in configs if c.baseline == "letterboxd"]
    if letterboxd_configs:
        ids, residuals, means = letterboxd_residuals(train, film_info, community, lb_offset)
        similarities = [row for row in db.rater_similarities(conn, ids, residuals, mu=mu,
                                                             min_overlap=letterboxd_configs[0].min_overlap,
                                                             film_means=means)
                        if row[0] not in dropped]
        similar["letterboxd"] = len(similarities)
        # Every film Josh rated that has an average is centred on it,
        # screened ones included (they can be targets now). A neighbour's
        # own offset from Letterboxd is measured over the unscreened ones
        # only: the recruits gave Josh's exact rating on screened films,
        # which would pull their offsets toward his. Held-out films count
        # there — an average is public knowledge, not Josh's rating.
        film_means = {film_info[s][0]: community[s] for s in actual if s in film_info and s in community}
        offset_means = {film_info[s][0]: community[s] for s in actual
                        if s in film_info and s in community and s not in screened}
        for cfg in letterboxd_configs:
            by_id = _neighbour_offsets(conn, similarities, cfg, mu, target_ids, film_means,
                                       offset_means=offset_means, **exclusion)
            preds[cfg.label] = {s: on_letterboxd(s, by_id) for s in test}
            covered[cfg.label] = {s for s in test if s in community and film_info[s][0] in by_id}
            if cfg == LETTERBOXD_DEFAULT:
                # The same taste layer on the recalibrated base — so a gain
                # that's only recalibration shows up as a tie with the placebo.
                preds[PLACEBO_TWINS_LABEL] = {
                    s: clamp_rating(regressed[s] + by_id[film_info[s][0]]["nb_offset"])
                    if s in covered[cfg.label] else clamp_rating(regressed[s]) for s in test}
                covered[PLACEBO_TWINS_LABEL] = covered[cfg.label]
    return preds, covered, similar


def _metrics(pairs: list[tuple[float, float]]) -> dict:
    return {"rmse": rmse(pairs), "mae": mae(pairs), "spearman": spearman(pairs), "top_mean": top_share_mean(pairs)}


def evaluate(conn, my_ratings: dict[str, float], community: dict[str, float], watched_dates: dict[str, str | None],
             *, clusters: dict[str, str] | None = None, folds: int = 5, seed: int = 0,
             configs: list[TasteParams] | None = None) -> dict:
    """Hold out part of Josh's ratings, predict them from the rest, and
    score every method on the same films.

    Screened films are held out too — they're his most distinctive
    ratings, where a taste engine should matter most — but without the
    raters they recruited: those were found *because* they gave his exact
    rating there. The primary numbers leave out only the recruits who
    wouldn't have been scraped without that film ("counterfactual"); the
    same comparison is repeated leaving out every recruit ("all", a bound
    that costs the engine genuine agreers) and none ("none", the leak).
    A screened film whose recruits aren't recorded yet
    (--record-screen-hits) is held back entirely."""
    configs = configs or [DEFAULT_PARAMS, *[c for c in EVAL_GRID if c != DEFAULT_PARAMS], *LETTERBOXD_GRID]
    key_configs = [DEFAULT_PARAMS, LETTERBOXD_DEFAULT]
    clusters = clusters or {}
    mu = _ensure_mu(conn)
    if mu is None:
        raise ValueError("the rater corpus is empty — run --scrape-raters first")
    film_info = db.rater_film_lookup(conn, list(my_ratings))
    recruitment = load_recruitment(conn)
    screened = recruitment.screened
    held_back = sorted(s for s in my_ratings if s in film_info and s in recruitment.unrecorded)
    testable = sorted(s for s in my_ratings if s in film_info and s not in recruitment.unrecorded)
    if len(testable) < 5 * folds:
        raise ValueError(f"only {len(testable)} of your ratings are testable yet — scrape more of the corpus first")

    splits = [(f"{folds}-fold cross-validation", stratified_folds(testable, screened, folds, seed), False)]
    # A held-back film can't be put in the right half of a time split (it
    # might be one of the latest), so that split waits until none are.
    recent = recent_holdout(testable, watched_dates) if not held_back else set()
    if recent:
        splits.append(("Most recent 15% of your ratings, predicted from everything before", [recent], True))

    def run(test_sets: list[set[str]], by_time: bool, mode: str, cfgs: list[TasteParams], only_screened: bool):
        preds: dict[str, dict[str, float]] = defaultdict(dict)
        covered: dict[str, set[str]] = defaultdict(set)
        similar: dict[str, list[int]] = defaultdict(list)
        for test_set in test_sets:
            train = {s: r for s, r in my_ratings.items() if s not in test_set}
            test = sorted(s for s in test_set if s in screened or not only_screened)
            if not test:
                continue
            if by_time:
                # Raters found through films after the cut-off shouldn't be
                # in the corpus at all, for any film.
                dropped = recruitment.dropped_without({s for s in test_set if s in screened}, mode)
                exclusions = {s: dropped for s in test}
            else:
                dropped = set()
                exclusions = {s: recruitment.exclusions(s, mode) for s in test if s in screened}
            p, c, sim = _evaluate_split(conn, train, test, my_ratings, film_info, community, mu, cfgs, screened,
                                        exclusions, dropped)
            for method, values in p.items():
                preds[method].update(values)
            for method, values in c.items():
                covered[method] |= values
            for key, value in sim.items():
                similar[key].append(value)
        return preds, covered, similar

    results = []
    for name, test_sets, by_time in splits:
        films = sorted(set().union(*test_sets))
        preds, covered, similar = run(test_sets, by_time, "counterfactual", configs, False)
        methods = []
        for method, values in preds.items():
            pairs = [(values[s], my_ratings[s]) for s in films]
            methods.append({"method": method, **_metrics(pairs),
                            "coverage": len(covered[method]) / len(films) if method in covered else None})
        comparisons = [{"method": m, **paired_bootstrap(preds[m], preds[BASELINE_LABEL], my_ratings, clusters)}
                       for m in KEY_METHODS if m in preds and m != BASELINE_LABEL]
        vs_placebo = [{"method": m, **paired_bootstrap(preds[m], preds[REGRESSED_LABEL], my_ratings, clusters)}
                      for m in ENGINE_METHODS if m in preds]
        screened_films = [s for s in films if s in screened]
        rest = [s for s in films if s not in screened]
        on_screened = []
        if len(screened_films) >= 10:
            only = set(screened_films)

            def restrict(values: dict[str, float]) -> dict[str, float]:
                return {s: v for s, v in values.items() if s in only}
            for m in ENGINE_METHODS:
                if m in preds:
                    on_screened.append({
                        "method": m,
                        "vs_baseline": paired_bootstrap(restrict(preds[m]), restrict(preds[BASELINE_LABEL]),
                                                        my_ratings, clusters),
                        "vs_placebo": paired_bootstrap(restrict(preds[m]), restrict(preds[REGRESSED_LABEL]),
                                                       my_ratings, clusters),
                        "covered": len(covered.get(m, set()) & only),
                    })
        subsets = []
        if screened_films:
            for m in KEY_METHODS:
                if m not in preds:
                    continue
                row = {"method": m}
                for label, subset in (("screened", screened_films), ("rest", rest)):
                    d = sum((preds[m][s] - my_ratings[s]) ** 2 - (preds[BASELINE_LABEL][s] - my_ratings[s]) ** 2
                            for s in subset)
                    row[label] = {"n": len(subset), "d_mse_share": d / len(films),
                                  "covered": len(covered.get(m, set()) & set(subset)) if m in covered else None}
                subsets.append(row)
        robustness = []
        if screened_films:
            per_mode = {"counterfactual": preds}
            for mode in ("none", "all"):
                per_mode[mode] = run(test_sets, by_time, mode, key_configs, not by_time)[0]
            scope = films if by_time else screened_films
            for m in [BASELINE_LABEL, LETTERBOXD_DEFAULT.label, DEFAULT_PARAMS.label, SIMPLE_SWAP_LABEL,
                      "Corpus consensus + your offset"]:
                robustness.append({"method": m, **{
                    mode: _metrics([(per_mode[mode][m][s], my_ratings[s]) for s in scope])
                    for mode in ("none", "counterfactual", "all") if m in per_mode[mode]}})
        slopes = similar.pop("slope", [])
        results.append({
            "split": name, "films": len(films), "screened": len(screened_films), "by_time": by_time,
            "similar_raters": {b: min(c) for b, c in similar.items()},
            "slopes": (min(slopes), max(slopes)) if slopes else None,
            "vs_placebo": vs_placebo, "on_screened": on_screened,
            "actual_mean": sum(my_ratings[s] for s in films) / len(films),
            "methods": methods, "comparisons": comparisons, "subsets": subsets, "robustness": robustness,
            "predictions": {m: dict(v) for m, v in preds.items() if m in KEY_METHODS or m == BASELINE_LABEL},
            "covered_sets": {m: sorted(v) for m, v in covered.items() if m in KEY_METHODS},
        })

    recruited = {(s, r) for s in screened for r in recruitment.recruits.get(s, set())}
    left_out = {(s, r) for s in screened for r in recruitment.exclusions(s, "counterfactual")}
    return {"rated": len(my_ratings), "testable": len(testable), "screened_testable": len(set(testable) & screened),
            "held_back": held_back, "recruit_pairs": len(recruited), "recruit_pairs_left_out": len(left_out),
            "unsure_raters": len(recruitment.unsure), "splits": results}


def _signed(value: float, digits: int = 3) -> str:
    return f"{value:+.{digits}f}"


def _interval(result: dict, key: str, ci_key: str) -> str:
    if result[key] is None or result[ci_key] is None:
        return "—"
    low, high = result[ci_key]
    return f"{_signed(result[key])} [{_signed(low)}, {_signed(high)}]"


def render_evaluation(report: dict) -> str:
    lines = [
        f"{report['testable']:,} of your {report['rated']:,} ratings are testable (the corpus has the film), "
        f"{report['screened_testable']} of them screened — your most distinctive — and tested without the raters "
        f"they recruited: {report['recruit_pairs_left_out']:,} of {report['recruit_pairs']:,} (film, recruit) "
        f"pairs are left out; the rest are raters who'd have been scraped anyway. {report['unsure_raters']} raters' "
        f"screening pages can't all be pinned down, so every film they could have been recruited through counts.",
    ]
    if report["held_back"]:
        lines.append(f"{len(report['held_back'])} screened films held back — their recruits aren't recorded yet "
                     f"(run --record-screen-hits) — and the time split waits until none are.")
    lines.append("")
    for split in report["splits"]:
        similar = split["similar_raters"]
        similar_text = f"{similar.get('corpus', 0)} raters correlate with you"
        if "letterboxd" in similar:
            similar_text += f" ({similar['letterboxd']} measured against Letterboxd averages)"
        lines.append(f"{split['split']} — {split['films']:,} films ({split['screened']} screened), you rated them "
                     f"{split['actual_mean']:.2f}★ on average; {similar_text}.")
        if split["slopes"]:
            low, high = split["slopes"]
            lines.append(f"  The placebo's fitted slope on the Letterboxd average: {low:.2f}"
                         + (f"–{high:.2f}" if high - low >= 0.005 else "")
                         + (" (under 1: it pulls far-off films back toward your mean)." if high < 1 else
                            " (over 1: it stretches them)." if low > 1 else "."))
        lines.append(f"  {'':50} {'RMSE':>6} {'MAE':>6} {'Spearman':>9} {'Top-10%':>8} {'Covered':>8}")
        for m in split["methods"]:
            spearman_text = f"{m['spearman']:.3f}" if m["spearman"] is not None else "—"
            coverage_text = f"{m['coverage']:.0%}" if m["coverage"] is not None else ""
            mark = "•" if m["method"] in KEY_METHODS or m["method"] == BASELINE_LABEL else " "
            lines.append(f"  {mark} {m['method']:48} {m['rmse']:6.3f} {m['mae']:6.3f} {spearman_text:>9} "
                         f"{m['top_mean']:7.2f}★ {coverage_text:>8}")
        lines += ["  • chosen before looking — the rows the conclusion rests on; the rest of the grid is exploratory.",
                  "", f"  Against '{BASELINE_LABEL}', all films, 95% intervals (bootstrap over directors):",
                  f"    {'':48} {'Δ RMSE':>24} {'Δ Spearman':>24}"]
        for c in split["comparisons"]:
            lines.append(f"    {c['method']:48} {_interval(c, 'd_rmse', 'rmse_ci'):>24} "
                         f"{_interval(c, 'd_spearman', 'spearman_ci'):>24}")
        lines += ["", f"  Against the placebo ('{REGRESSED_LABEL}'), all films:"]
        for c in split["vs_placebo"]:
            lines.append(f"    {c['method']:48} {_interval(c, 'd_rmse', 'rmse_ci'):>24} "
                         f"{_interval(c, 'd_spearman', 'spearman_ci'):>24}")
        if split["on_screened"]:
            lines += ["", f"  The screened films only ({split['screened']}; your most distinctive ratings, where a taste "
                      "engine should matter most). Against the baseline, then the placebo:"]
            for row in split["on_screened"]:
                lines.append(f"    {row['method']:48} {_interval(row['vs_baseline'], 'd_rmse', 'rmse_ci'):>24} "
                             f"{_interval(row['vs_baseline'], 'd_spearman', 'spearman_ci'):>24}   "
                             f"covered {row['covered']}")
                lines.append(f"    {'':48} {_interval(row['vs_placebo'], 'd_rmse', 'rmse_ci'):>24} "
                             f"{_interval(row['vs_placebo'], 'd_spearman', 'spearman_ci'):>24}")
        if split["subsets"]:
            first = split["subsets"][0]
            lines += ["", "  Where each method's difference in squared error comes from, against the baseline (negative",
                      "  = better; the two columns add up to the whole). The screened films were picked for being far",
                      "  from the Letterboxd average, so a method that leans toward your mean gains there by construction:",
                      f"    {'':48} {'screened (' + str(first['screened']['n']) + ')':>16} "
                      f"{'rest (' + str(first['rest']['n']) + ')':>12}"]
            for row in split["subsets"]:
                lines.append(f"    {row['method']:48} {_signed(row['screened']['d_mse_share'], 4):>16} "
                             f"{_signed(row['rest']['d_mse_share'], 4):>12}")
        if split["robustness"]:
            scope = "all films" if split["by_time"] else "the screened films"
            lines += ["", f"  How much the recruits matter, on {scope} — RMSE / Spearman leaving out none of them (the",
                      "  leak), only those who wouldn't have been scraped without the film (primary), and every one:",
                      f"    {'':48} {'none':>15}   {'primary':>15}   {'every one':>15}"]
            for row in split["robustness"]:
                cells = []
                for mode in ("none", "counterfactual", "all"):
                    if mode in row:
                        sp = row[mode]["spearman"]
                        cells.append(f"{row[mode]['rmse']:.3f} / {sp:.3f}" if sp is not None else
                                     f"{row[mode]['rmse']:.3f} / —")
                lines.append(f"    {row['method']:48} " + "   ".join(f"{c:>15}" for c in cells))
        lines.append("")
    lines += [
        "RMSE/MAE: typical error in stars (lower is better). Spearman: how well the ranking matches yours (1 is",
        "perfect). Top-10%: what you actually rated the films each method ranked highest. Covered: films with",
        "enough neighbour ratings for the taste layer; the rest fall back to their baseline.",
        "",
        "How to read it. Ranking is what recommendations need: the engine shows your taste if its Spearman beats",
        f"'{BASELINE_LABEL}' on all films with an interval above 0 — the placebo can't change a ranking,",
        "so it's no help there. For error in stars, a gain only counts as taste if 'Placebo + twins' beats the",
        "placebo with an interval below 0; otherwise recalibrating to your scale gets the same for free. The",
        "screened-films block is where a taste engine should shine, and its numbers carry wider intervals.",
    ]
    return "\n".join(lines)


def films_only(rows: list[dict], picks: int, kind_of) -> tuple[list[dict], int]:
    """The first `picks` of `rows` that are neither TV nor gone from
    Letterboxd, and how many TV shows were passed over on the way.
    `kind_of(row)` is only asked about rows that could still make the cut,
    so a lookup that costs a request per film stops as soon as there are
    enough. A row it can't answer for (None) is kept, not guessed at."""
    kept: list[dict] = []
    skipped = 0
    for row in rows:
        if len(kept) >= picks:
            break
        kind = kind_of(row)
        if kind == "tv":
            skipped += 1
        elif kind != "gone":
            kept.append(row)
    return kept, skipped


def scrape_looks_active(last_activity: str | None, now: datetime) -> bool:
    return last_activity is not None and now - datetime.fromisoformat(last_activity) < SCRAPE_ACTIVE_WINDOW


class FilmKindLookup:
    """kind_of for films_only: a film's stored kind where it has one,
    otherwise its /film/<slug>/ page's TMDB link ("movie"/"tv"; "gone" for
    a 404), fetched through the same PoliteFetcher as the scrape. Each
    answer goes to `save` as soon as it's known, and a block to `on_block`
    the moment it happens, so a Ctrl-C or a dropped connection loses at
    most the page in flight. Fetching stops at the first block or failure,
    when `still_clear()` says another run has hit a block meanwhile, or at
    a page with no TMDB link — every film page has one, so that's the
    parser failing (Letterboxd's markup changing), not an answer, and it
    isn't stored. `stopped` says why; anything unchecked is kept."""

    def __init__(self, fetcher: PoliteFetcher | None, log=print, *, save=lambda film_id, kind: None,
                 on_block=lambda: None, still_clear=lambda: True):
        self.fetcher = fetcher
        self.found: dict[int, str] = {}
        self.stopped: str | None = None
        self._log = log
        self._save = save
        self._on_block = on_block
        self._still_clear = still_clear

    def __call__(self, row: dict) -> str | None:
        if row.get("tmdb_kind"):
            return row["tmdb_kind"]
        if row["film_id"] in self.found:
            return self.found[row["film_id"]]
        if self.fetcher is None or self.stopped:
            return None
        if not self._still_clear():
            self.stopped = "cooldown"
            self._log("Stopped checking picks for TV — Letterboxd has blocked another run meanwhile.")
            return None
        try:
            html = self.fetcher.get(f"https://letterboxd.com/film/{row['slug']}/")
        except LetterboxdBlockedError as exc:
            self.stopped = "blocked"
            self._on_block()
            self._log(f"Stopped checking picks for TV — Letterboxd is refusing requests ({exc}).")
            return None
        except (LetterboxdFetchError, RequestBudgetExhausted) as exc:
            self.stopped = "failed"
            self._log(f"Stopped checking picks for TV ({str(exc) or 'request cap reached'}).")
            return None
        if html is None:
            kind = "gone"
        else:
            kind = parse_tmdb_kind(html)
            if kind is None:
                self.stopped = "failed"
                self._log(f"Stopped checking picks for TV — /film/{row['slug']}/ has no TMDB link the parser "
                          f"recognises (has Letterboxd's markup changed?).")
                return None
        self.found[row["film_id"]] = kind
        self._save(row["film_id"], kind)
        if len(self.found) % 10 == 0:
            self._log(f"Checked {len(self.found)} picks for TV ({self.fetcher.requests} requests so far).")
        return kind


def recommend(conn, my_ratings: dict[str, float], seen: set[str], watchlist: set[str], *,
              params: TasteParams = DEFAULT_PARAMS, picks: int = 40, fetcher: PoliteFetcher | None = None,
              log=print, now=lambda: datetime.now(timezone.utc)) -> dict:
    """Neighbours, the best-predicted films Josh hasn't seen and hasn't
    watchlisted, and his watchlist ranked by prediction. Letterboxd lists
    TV alongside films, so with a `fetcher` each pick not already known to
    be a film is checked on its film page first — once ever, the answer is
    stored — and TV is left out. Not while a block's cooldown is running
    or a scrape looks active, and a block met here starts one, as it would
    for the scrape."""
    mu = _ensure_mu(conn)
    if mu is None:
        raise ValueError("the rater corpus is empty — run --scrape-raters first")
    film_info = db.rater_film_lookup(conn, sorted(set(my_ratings) | seen | watchlist))
    film_bias = {slug: info[1] for slug, info in film_info.items()}
    offset = my_offset(my_ratings, film_bias, mu)
    ids, residuals = my_residuals(my_ratings, film_info, mu, offset)
    neighbours = select_neighbours(
        db.rater_similarities(conn, ids, residuals, mu=mu, min_overlap=params.min_overlap), params)
    if not neighbours:
        return {"neighbours": [], "picks": [], "watchlist": [], "watchlist_unscored": len(watchlist)}

    nb_ids = [n.rater_id for n in neighbours]
    weights = [n.weight for n in neighbours]

    def with_prediction(rows: list[dict]) -> list[dict]:
        for row in rows:
            row["predicted"] = clamp_rating(mu + offset + row["bias"] + row["nb_offset"])
        return rows

    excluded = [film_info[s][0] for s in seen | watchlist | set(my_ratings) if s in film_info]
    pool = with_prediction(db.rater_predictions(
        conn, nb_ids, weights, mu=mu, lambda_pred=params.lambda_pred,
        min_support=max(params.min_support, MIN_SUPPORT_PICKS), exclude_film_ids=excluded,
        limit=picks * PICK_POOL))
    def still_clear() -> bool:
        return _cooldown_remaining(db.taste_meta_get(conn, "blocked_at"), now()) is None

    if fetcher is not None and not still_clear():
        log("Letterboxd blocked a recent run — not checking picks for TV until the cooldown ends.")
        fetcher = None
    elif fetcher is not None and scrape_looks_active(db.latest_scrape_activity(conn), now()):
        log("A --scrape-raters run looks active — not checking picks for TV alongside it.")
        fetcher = None
    lookup = FilmKindLookup(
        fetcher, log, save=lambda film_id, kind: db.set_rater_film_kinds(conn, {film_id: kind}),
        on_block=lambda: db.taste_meta_set(conn, "blocked_at", now().isoformat()), still_clear=still_clear)
    if fetcher is not None and any(not row.get("tmdb_kind") for row in pool[:picks]):
        log("Checking new picks for TV on Letterboxd (a film page every few seconds)...")
    top, tv_skipped = films_only(pool, picks, lookup)
    watchlist_ids = [film_info[s][0] for s in watchlist if s in film_info]
    ranked_watchlist = with_prediction(db.rater_predictions(
        conn, nb_ids, weights, mu=mu, lambda_pred=params.lambda_pred, min_support=params.min_support,
        target_film_ids=watchlist_ids)) if watchlist_ids else []
    return {"neighbours": neighbours, "picks": top, "watchlist": ranked_watchlist,
            "watchlist_unscored": len(watchlist) - len(ranked_watchlist), "tv_skipped": tv_skipped,
            "picks_unchecked": sum(1 for row in top if lookup(row) is None)}


def render_recommendations(result: dict, *, show_neighbours: int = 15, show_watchlist: int = 30) -> str:
    if not result["neighbours"]:
        return "No raters share enough rated films with you yet — scrape more of the corpus first."
    lines = [f"Your closest taste matches (of {len(result['neighbours'])} used):"]
    for n in result["neighbours"][:show_neighbours]:
        lines.append(f"  {n.username:28} {n.overlap:5} shared films   r={n.pearson:.2f}   weight {n.weight:.2f}")
    lines += ["", "Films you haven't seen or watchlisted, best predicted first:"]
    for row in result["picks"]:
        lines.append(f"  {row['predicted']:.1f}★  {row['name'] or row['slug']:55} "
                     f"{row['support']:3} matches rated it, avg {row['neighbour_mean']:.1f}★")
    if result.get("tv_skipped"):
        lines.append(f"  ({result['tv_skipped']} TV shows left out.)")
    if result.get("picks_unchecked"):
        lines.append(f"  ({result['picks_unchecked']} of these couldn't be checked for TV this time.)")
    lines += ["", "Your watchlist, best predicted first:"]
    for row in result["watchlist"][:show_watchlist]:
        lines.append(f"  {row['predicted']:.1f}★  {row['name'] or row['slug']:55} "
                     f"{row['support']:3} matches rated it")
    if result["watchlist_unscored"]:
        lines.append(f"  ...and {result['watchlist_unscored']} watchlist films too few of your matches have rated to score.")
    return "\n".join(lines)
