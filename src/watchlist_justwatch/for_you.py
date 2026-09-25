"""The For you tab's daily data: the taste engine (taste.py) run for the
dashboard. Built once a day in run(), because it needs the rater corpus
(only ever aggregated server-side), Letterboxd film pages and JustWatch for
the new picks, and stored whole as meta 'for_you' — so --dashboard stays
network-free and only reads it.

Four things, all from the same neighbours --taste-recommend uses:
- an estimate for every watchlist film enough of them have rated;
- new picks — films Josh hasn't seen or watchlisted, best estimate first,
  TV left out — which go through the same enrichment as a discovery
  section and ship as discovery films, so quick look, the film page and
  today's-config availability all work on them unchanged;
- "because you loved": for each estimated film, the favourites of Josh's
  that its fans among his neighbours love unusually often;
- "if you like this, see…": for every film the page ships, the unseen ones
  its fans across the whole corpus love unusually often.

Other members' usernames never leave this module except Sarah's, who is
named on purpose: the dashboard is a public page.
"""

import re

from . import db, taste

PICKS = 24
# Best-predicted films examined per pick wanted: some are TV, some have no
# Letterboxd rating, some aren't streaming anywhere tracked.
PICK_POOL = 3
# Headroom past PICKS for the availability check, which drops films with no
# tracked offer anywhere (same as a discovery section).
PICK_HEADROOM = 6
# Below this many rated films, correlations are noise.
MIN_OWN_RATINGS = 50
CLOSEST_SHOWN = 10
NAME_YEAR_RE = re.compile(r"^(?P<title>.+) \((?P<year>\d{4})\)$")


def split_name(name: str | None, slug: str) -> tuple[str, int | None]:
    """rater_films.name ("Seven Samurai (1954)") -> (title, year)."""
    match = NAME_YEAR_RE.match(name or "")
    if match:
        return match.group("title"), int(match.group("year"))
    return name or slug, None


def pick_candidates(pool: list[dict], discovery_films: dict[str, dict], fetch_details, *,
                    want: int, warn=lambda msg: None) -> tuple[list[dict], dict[int, str]]:
    """Walks the best-predicted films in order until `want` look like films
    worth recommending: (candidates in the shape similar.py's enrichment
    takes, {film id: tmdb kind} learned on the way, for rater_films). A film
    already known to be TV is skipped without a request; one some discovery
    section already recommends today reuses that record; anything else costs
    one film-page request, which also says whether it's really TV. A page
    that didn't load, or a film with no Letterboxd rating yet, is skipped
    for today rather than guessed at.

    Only a film positively known to be one gets through. A loaded page with
    no TMDB link the parser recognises means Letterboxd's markup changed —
    every film page has one — so that's warned about once, and the rest of
    the walk only takes films already cached as "movie" rather than spend a
    request per film on an answer it can't read."""
    candidates: list[dict] = []
    kinds: dict[int, str] = {}
    unrecognised = False
    for row in pool:
        if len(candidates) >= want:
            break
        cached = row.get("tmdb_kind")
        if cached in ("tv", "gone"):
            continue
        slug = row["slug"]
        if slug in discovery_films:
            candidates.append({**discovery_films[slug], "slug": slug})
            continue
        if unrecognised and cached != "movie":
            continue
        details = fetch_details(slug)
        kind = details.get("tmdb_kind")
        if kind in ("movie", "tv"):
            kinds[row["film_id"]] = kind
        if details.get("rating") is None:
            continue
        if kind is None and not unrecognised:
            unrecognised = True
            warn(f"For you: /film/{slug}/ has no TMDB link the parser recognises (has Letterboxd's markup "
                 f"changed?) — leaving out picks not already known to be films")
        if (kind or cached) != "movie":
            continue
        title, year = split_name(row.get("name"), slug)
        candidates.append({
            "slug": slug, "title": title, "year": year, "tmdb_id": details.get("tmdb_id"),
            "rating": details["rating"], "poster_url": details["poster_url"],
            "director": ", ".join(details["director"]) if details["director"] else None,
            "starring": details["starring"], "synopsis": details["synopsis"], "genre": details["genre"],
            "runtime_minutes": details["runtime_minutes"],
        })
    return candidates, kinds


def matches_summary(neighbours: list[taste.Neighbour], sarah_username: str | None) -> dict:
    """The taste-matches panel: how many, the closest few anonymised, and
    where Sarah comes (if she's one of them at all)."""
    sarah = (sarah_username or "").lower()
    sarah_rank = next((i for i, n in enumerate(neighbours) if sarah and n.username.lower() == sarah), None)
    return {
        "count": len(neighbours),
        "sarah": None if sarah_rank is None else {
            "rank": sarah_rank + 1, "overlap": neighbours[sarah_rank].overlap,
            "pearson": round(neighbours[sarah_rank].pearson, 2),
        },
        "closest": [{"overlap": n.overlap, "weight": round(n.weight, 3), "is_sarah": i == sarah_rank}
                    for i, n in enumerate(neighbours[:CLOSEST_SHOWN])],
    }


def build_for_you(conn, *, diary: dict[str, dict], josh_watchlist: set[str], known_slugs: set[str],
                  discovery_films: dict[str, dict], sarah_username: str | None, generated_at: str,
                  fetch_details, enrich, dismissed: set[str] = frozenset(), warn=lambda msg: None,
                  picks: int = PICKS) -> tuple[dict | None, dict[str, dict]]:
    """(meta 'for_you', {slug: discovery-film record} for picks that weren't
    already discovery films) — or (None, {}) when there's too little to go
    on: too few of Josh's own ratings, an empty corpus, or nobody whose
    ratings track his. `known_slugs` is every film the page will ship
    (both watchlists and the discovery films); `enrich` is similar.py's
    availability check, bound to today's config. A film dismissed from Home
    stays out of the picks and the "see…" lists, as it does out of every
    discovery section (never a watchlist film, which dismissing can't hide)."""
    mine = taste.my_ratings_from_diary(diary)
    if len(mine) < MIN_OWN_RATINGS:
        return None, {}
    mu = taste._ensure_mu(conn)
    if mu is None:
        return None, {}
    params = taste.DEFAULT_PARAMS
    seen = set(diary)

    film_info = db.rater_film_lookup(conn, sorted(set(mine) | seen | josh_watchlist | known_slugs))
    offset = taste.my_offset(mine, {slug: info[1] for slug, info in film_info.items()}, mu)
    ids, residuals = taste.my_residuals(mine, film_info, mu, offset)
    neighbours = taste.select_neighbours(
        db.rater_similarities(conn, ids, residuals, mu=mu, min_overlap=params.min_overlap), params)
    if not neighbours:
        return None, {}
    nb_ids = [n.rater_id for n in neighbours]
    weights = [n.weight for n in neighbours]

    def predictions(**kwargs) -> list[dict]:
        return db.rater_predictions(conn, nb_ids, weights, mu=mu, lambda_pred=params.lambda_pred, **kwargs)

    def estimate(row: dict) -> float:
        return round(taste.clamp_rating(mu + offset + row["bias"] + row["nb_offset"]), 2)

    watch_ids = [film_info[s][0] for s in josh_watchlist if s in film_info]
    watch_rows = predictions(min_support=params.min_support, target_film_ids=watch_ids) if watch_ids else []

    excluded = [film_info[s][0] for s in seen | josh_watchlist | set(mine) if s in film_info]
    pool = predictions(min_support=max(params.min_support, taste.MIN_SUPPORT_PICKS),
                       exclude_film_ids=excluded, limit=picks * PICK_POOL)
    pool = [row for row in pool if row["slug"] not in dismissed]
    candidates, kinds = pick_candidates(pool, discovery_films, fetch_details, want=picks + PICK_HEADROOM,
                                        warn=warn)
    db.set_rater_film_kinds(conn, kinds)
    fresh = [c for c in candidates if c["slug"] not in discovery_films]
    _, new_films = enrich(fresh) if fresh else ([], {})
    pool_by_slug = {row["slug"]: row for row in pool}
    pick_slugs = [c["slug"] for c in candidates if c["slug"] in discovery_films or c["slug"] in new_films][:picks]
    pick_rows = [pool_by_slug[slug] for slug in pick_slugs]

    # Every film id the page will know about, now that the picks are in.
    for slug in pick_slugs:
        film_info.setdefault(slug, (pool_by_slug[slug]["film_id"], pool_by_slug[slug]["bias"], 0, None))
    slug_by_id = {info[0]: slug for slug, info in film_info.items()}

    scored = watch_rows + pick_rows
    scored_ids = [row["film_id"] for row in scored]
    fans_by_id = db.neighbour_fans(conn, nb_ids, scored_ids) if scored_ids else {}
    loved = {slug for slug, rating in mine.items() if rating >= 4.5 and slug in film_info}
    because_by_id = db.taste_because(conn, nb_ids, scored_ids, [film_info[s][0] for s in loved]) if scored_ids else {}

    shipped = (known_slugs | set(pick_slugs)) & set(film_info)
    suggestable = (josh_watchlist | ((set(discovery_films) | set(pick_slugs)) - dismissed)) - seen
    fans_also_loved = db.taste_fans_also_loved(
        conn, [film_info[s][0] for s in shipped], [film_info[s][0] for s in suggestable if s in film_info])

    scores = {}
    for row in scored:
        scores[row["slug"]] = {
            "predicted": estimate(row), "support": row["support"],
            "lovers": fans_by_id.get(row["film_id"], 0), "neighbour_mean": round(row["neighbour_mean"], 2),
            "because": [slug_by_id[fid] for fid, _ in because_by_id.get(row["film_id"], [])],
        }
    watchlist_order = [row["slug"] for row in sorted(watch_rows, key=lambda r: -estimate(r))
                       if row["slug"] not in seen]
    loved_used = {slug for entry in scores.values() for slug in entry["because"]}
    corpus = db.rater_corpus_summary(conn)
    payload = {
        "generated_at": generated_at,
        "corpus": {"raters": corpus["raters"].get("scraped", 0), "ratings": corpus["ratings"]},
        "your_offset": round(offset, 2),
        "matches": matches_summary(neighbours, sarah_username),
        "scores": scores,
        "watchlist": watchlist_order,
        "picks": pick_slugs,
        "fans_also_loved": {slug_by_id[sid]: [slug_by_id[c] for c in cands]
                            for sid, cands in fans_also_loved.items()},
        "loved": {slug: {"title": diary[slug].get("title") or slug, "poster_url": diary[slug].get("poster_url")}
                  for slug in sorted(loved_used) if slug in diary},
    }
    return payload, new_films
