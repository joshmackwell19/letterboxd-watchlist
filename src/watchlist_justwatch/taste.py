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
from collections import Counter
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


@dataclass(frozen=True)
class TasteParams:
    min_overlap: int = 20      # shared rated films before a rater's correlation counts at all
    lambda_sim: float = 25.0   # correlation * overlap / (overlap + lambda_sim)
    neighbours: int = 100      # how many of the most similar raters predict
    lambda_pred: float = 1.0   # damping toward the baseline when few neighbours rated a film
    min_support: int = 3       # neighbours who rated a film before it gets a prediction at all

    @property
    def label(self) -> str:
        return f"Taste twins ({self.neighbours} nbrs, damping {self.lambda_pred:g})"


DEFAULT_PARAMS = TasteParams()
EVAL_GRID = [replace(DEFAULT_PARAMS, neighbours=n, lambda_pred=lp)
             for n in (30, 100, 300) for lp in (0.5, 1.0, 2.0)]


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
    in flight. Returns why it stopped: done / cooldown / blocked / budget /
    network / interrupted."""
    conn = db.connect(database_url)
    scraped_this_run = 0
    outcome = "done"
    try:
        remaining = _cooldown_remaining(db.taste_meta_get(conn, "blocked_at"), now())
        if remaining is not None:
            hours = remaining.total_seconds() / 3600
            log(f"Letterboxd blocked the last run — not starting for another {hours:.1f}h.")
            return "cooldown"

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
            db.add_rater_candidates(conn, screen_hits(members, round(stars * 2), {username}), now().isoformat())
            db.mark_screened(conn, slug, stars, 1, now().isoformat())
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


def _screened_slugs(conn) -> set[str]:
    return {slug for slug, _, _ in db.screened_pages(conn)}


def _evaluate_split(conn, train: dict[str, float], test: list[str], actual: dict[str, float],
                    film_info: dict[str, tuple], community: dict[str, float], mu: float,
                    configs: list[TasteParams]) -> tuple[dict[str, list[tuple[float, float]]], dict[str, int], int]:
    """(method -> [(predicted, actual)], method -> films the CF layer
    covered, raters who qualified as similar) for one train/test split."""
    film_bias = {slug: info[1] for slug, info in film_info.items()}
    offset = my_offset(train, film_bias, mu)
    train_mean = sum(train.values()) / len(train)
    community_diffs = [r - community[s] for s, r in train.items() if s in community]
    community_offset = sum(community_diffs) / len(community_diffs) if community_diffs else 0.0

    pairs: dict[str, list[tuple[float, float]]] = {
        "Your average": [(train_mean, actual[s]) for s in test],
        "Letterboxd average + your offset": [
            (clamp_rating(community[s] + community_offset) if s in community else train_mean, actual[s])
            for s in test],
        "Corpus consensus + your offset": [(clamp_rating(mu + offset + film_bias[s]), actual[s]) for s in test],
    }
    coverage: dict[str, int] = {}

    ids, residuals = my_residuals(train, film_info, mu, offset)
    params = configs[0]
    similarities = db.rater_similarities(conn, ids, residuals, mu=mu, min_overlap=params.min_overlap)
    target_ids = [film_info[s][0] for s in test]
    for cfg in configs:
        neighbours = select_neighbours(similarities, cfg)
        rows = db.rater_predictions(
            conn, [n.rater_id for n in neighbours], [n.weight for n in neighbours], mu=mu,
            lambda_pred=cfg.lambda_pred, min_support=cfg.min_support, target_film_ids=target_ids,
        ) if neighbours else []
        by_id = {row["film_id"]: row for row in rows}
        method_pairs = []
        for s in test:
            row = by_id.get(film_info[s][0])
            base = mu + offset + film_bias[s]
            method_pairs.append((clamp_rating(base + row["nb_offset"] if row else base), actual[s]))
        pairs[cfg.label] = method_pairs
        coverage[cfg.label] = len(by_id)
    return pairs, coverage, len(similarities)


def evaluate(conn, my_ratings: dict[str, float], community: dict[str, float], watched_dates: dict[str, str | None],
             *, folds: int = 5, seed: int = 0, configs: list[TasteParams] | None = None) -> dict:
    """Hold out part of Josh's ratings, predict them from the rest, and
    score every method on the same films. Films used for screening are
    never held out: their candidates were found *because* they gave Josh's
    exact rating, so predicting them back would flatter the engine."""
    configs = configs or [DEFAULT_PARAMS, *[c for c in EVAL_GRID if c != DEFAULT_PARAMS]]
    mu = _ensure_mu(conn)
    if mu is None:
        raise ValueError("the rater corpus is empty — run --scrape-raters first")
    film_info = db.rater_film_lookup(conn, list(my_ratings))
    screened = _screened_slugs(conn)
    testable = sorted(s for s in my_ratings if s in film_info and s not in screened)
    if len(testable) < 5 * folds:
        raise ValueError(f"only {len(testable)} of your ratings are testable yet — scrape more of the corpus first")

    splits: list[tuple[str, list[set[str]]]] = [(f"{folds}-fold cross-validation", kfold(testable, folds, seed))]
    recent = recent_holdout(testable, watched_dates)
    if recent:
        splits.append(("Most recent 15% of your ratings, predicted from everything before", [recent]))

    results = []
    for name, test_sets in splits:
        pooled: dict[str, list[tuple[float, float]]] = {}
        covered: Counter = Counter()
        similar_counts = []
        for test_set in test_sets:
            train = {s: r for s, r in my_ratings.items() if s not in test_set}
            pairs, coverage, similar = _evaluate_split(conn, train, sorted(test_set), my_ratings, film_info,
                                                       community, mu, configs)
            for method, method_pairs in pairs.items():
                pooled.setdefault(method, []).extend(method_pairs)
            covered.update(coverage)
            similar_counts.append(similar)
        total = sum(len(t) for t in test_sets)
        methods = []
        for method, method_pairs in pooled.items():
            methods.append({
                "method": method, "rmse": rmse(method_pairs), "mae": mae(method_pairs),
                "spearman": spearman(method_pairs), "top_mean": top_share_mean(method_pairs),
                "coverage": covered[method] / total if method in covered else None,
            })
        results.append({"split": name, "films": total, "similar_raters": min(similar_counts),
                        "actual_mean": sum(my_ratings[s] for t in test_sets for s in t) / total,
                        "methods": methods})
    return {"rated": len(my_ratings), "testable": len(testable), "screened_excluded": len(screened & set(my_ratings)),
            "splits": results}


def render_evaluation(report: dict) -> str:
    lines = [f"{report['testable']:,} of your {report['rated']:,} ratings are testable "
             f"(the corpus has the film, and it wasn't used for screening — "
             f"{report['screened_excluded']} were).", ""]
    for split in report["splits"]:
        lines.append(f"{split['split']} — {split['films']:,} films, you rated them {split['actual_mean']:.2f}★ "
                     f"on average; {split['similar_raters']} raters correlate with you.")
        lines.append(f"  {'':44} {'RMSE':>6} {'MAE':>6} {'Spearman':>9} {'Top-10%':>8} {'Covered':>8}")
        for m in split["methods"]:
            spearman_text = f"{m['spearman']:.3f}" if m["spearman"] is not None else "—"
            coverage_text = f"{m['coverage']:.0%}" if m["coverage"] is not None else ""
            lines.append(f"  {m['method']:44} {m['rmse']:6.3f} {m['mae']:6.3f} {spearman_text:>9} "
                         f"{m['top_mean']:7.2f}★ {coverage_text:>8}")
        lines.append("")
    lines += [
        "RMSE/MAE: typical error in stars (lower is better). Spearman: how well the ranking matches yours",
        "(1 is perfect). Top-10%: what you actually rated the films each method ranked highest — the number",
        "that matters for recommendations. Covered: films with enough neighbour ratings for the taste layer;",
        "the rest fall back to corpus consensus. The engine earns its keep only if it beats",
        "'Letterboxd average + your offset'.",
    ]
    return "\n".join(lines)


def recommend(conn, my_ratings: dict[str, float], seen: set[str], watchlist: set[str], *,
              params: TasteParams = DEFAULT_PARAMS, picks: int = 40) -> dict:
    """Neighbours, the best-predicted films Josh hasn't seen and hasn't
    watchlisted, and his watchlist ranked by prediction."""
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
    top = with_prediction(db.rater_predictions(
        conn, nb_ids, weights, mu=mu, lambda_pred=params.lambda_pred,
        min_support=max(params.min_support, MIN_SUPPORT_PICKS), exclude_film_ids=excluded, limit=picks))
    watchlist_ids = [film_info[s][0] for s in watchlist if s in film_info]
    ranked_watchlist = with_prediction(db.rater_predictions(
        conn, nb_ids, weights, mu=mu, lambda_pred=params.lambda_pred, min_support=params.min_support,
        target_film_ids=watchlist_ids)) if watchlist_ids else []
    return {"neighbours": neighbours, "picks": top, "watchlist": ranked_watchlist,
            "watchlist_unscored": len(watchlist) - len(ranked_watchlist)}


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
    lines += ["", "Your watchlist, best predicted first:"]
    for row in result["watchlist"][:show_watchlist]:
        lines.append(f"  {row['predicted']:.1f}★  {row['name'] or row['slug']:55} "
                     f"{row['support']:3} matches rated it")
    if result["watchlist_unscored"]:
        lines.append(f"  ...and {result['watchlist_unscored']} watchlist films too few of your matches have rated to score.")
    return "\n".join(lines)
