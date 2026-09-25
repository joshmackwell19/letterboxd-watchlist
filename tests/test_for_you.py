import pytest

from watchlist_justwatch import for_you, taste
from watchlist_justwatch.dashboard import _for_you_data
from watchlist_justwatch.for_you import build_for_you, matches_summary, pick_candidates, split_name
from watchlist_justwatch.taste import Neighbour


def test_split_name_takes_the_year_off_the_end():
    assert split_name("Kill Bill: The Whole Bloody Affair (2004)", "x") == ("Kill Bill: The Whole Bloody Affair", 2004)
    assert split_name("Untitled", "untitled") == ("Untitled", None)
    assert split_name(None, "the-slug") == ("the-slug", None)


def _pool_row(film_id: int, slug: str, kind: str | None = None) -> dict:
    return {"film_id": film_id, "slug": slug, "name": slug.title() + " (1999)", "tmdb_kind": kind,
            "bias": 0.1, "nb_offset": 0.2, "support": 6, "neighbour_mean": 4.2}


def _details(kind: str | None = "movie", rating: float | None = 4.1) -> dict:
    return {"rating": rating, "poster_url": "p.jpg", "director": ["Someone"], "starring": ["A"], "synopsis": "s",
            "genre": ["Drama"], "runtime_minutes": 100, "tmdb_kind": kind, "tmdb_id": 7}


def test_pick_candidates_skips_tv_reuses_discovery_films_and_stops_once_there_are_enough():
    pool = [_pool_row(1, "known-tv", kind="tv"), _pool_row(2, "a-show"), _pool_row(3, "already-found"),
            _pool_row(4, "unrated"), _pool_row(5, "a-film"), _pool_row(6, "never-asked")]
    pages = {"a-show": _details("tv"), "unrated": _details(rating=None), "a-film": _details()}
    asked = []

    def fetch(slug):
        asked.append(slug)
        return pages[slug]

    candidates, kinds = pick_candidates(pool, {"already-found": {"title": "Already Found", "all_offers": []}},
                                        fetch, want=2)
    assert [c["slug"] for c in candidates] == ["already-found", "a-film"]
    # a film some discovery section found keeps its record; a new one is
    # built from its page, title and year from the corpus name
    assert candidates[0]["title"] == "Already Found"
    assert candidates[1]["title"] == "A-Film" and candidates[1]["year"] == 1999 and candidates[1]["tmdb_id"] == 7
    assert candidates[1]["director"] == "Someone"
    # already-known TV costs nothing; the walk stops at `want`
    assert asked == ["a-show", "unrated", "a-film"]
    assert kinds == {2: "tv", 4: "movie", 5: "movie"}


def _neighbour(i: int, username: str) -> Neighbour:
    return Neighbour(rater_id=i, username=username, overlap=100 + i, pearson=0.4, weight=0.5 - i / 100)


def test_matches_summary_names_nobody_but_sarah():
    neighbours = [_neighbour(i, f"member{i}") for i in range(12)]
    neighbours[2] = _neighbour(2, "SarahVeen")
    summary = matches_summary(neighbours, "sarahveen")
    assert summary["count"] == 12
    assert summary["sarah"] == {"rank": 3, "overlap": 102, "pearson": 0.4}
    assert len(summary["closest"]) == for_you.CLOSEST_SHOWN
    assert [m["is_sarah"] for m in summary["closest"]].index(True) == 2
    assert "member" not in repr(summary)


def test_matches_summary_without_sarah():
    assert matches_summary([_neighbour(0, "someone")], None)["sarah"] is None


class _FakeDb:
    """The handful of db functions build_for_you calls, answering from dicts."""

    def __init__(self, ids: dict[str, int]):
        self.ids = ids
        self.kinds_saved = {}

    def rater_film_lookup(self, conn, slugs):
        return {s: (self.ids[s], 0.0, 10, s.title()) for s in slugs if s in self.ids}

    def rater_similarities(self, conn, ids, residuals, *, mu, min_overlap, film_means=None):
        return [(1, "sarahveen", 145, 0.37), (2, "stranger", 80, 0.44)]

    def rater_predictions(self, conn, nb_ids, weights, *, mu, lambda_pred, min_support, target_film_ids=None,
                          exclude_film_ids=None, limit=None, **_):
        if target_film_ids is not None:   # the watchlist
            return [self._row(fid, nb) for fid, nb in ((self.ids["seen-watchlisted"], 0.9),
                                                      (self.ids["wl-good"], 0.6), (self.ids["wl-ok"], 0.1))
                    if fid in target_film_ids]
        return [self._row(self.ids["pick-tv"], 0.8, kind="tv"), self._row(self.ids["pick-film"], 0.7),
                self._row(self.ids["pick-nowhere"], 0.6)]

    def _row(self, film_id, nb_offset, kind=None):
        slug = next(s for s, i in self.ids.items() if i == film_id)
        return {"film_id": film_id, "slug": slug, "name": slug.title() + " (2001)", "bias": 0.0,
                "nb_offset": nb_offset, "support": 7, "neighbour_mean": 4.3, "tmdb_kind": kind}

    def set_rater_film_kinds(self, conn, kinds):
        self.kinds_saved.update(kinds)

    def neighbour_fans(self, conn, nb_ids, film_ids):
        return {fid: 5 for fid in film_ids}

    def taste_because(self, conn, nb_ids, target_ids, loved_ids, **_):
        # like the real query, only ever answers with favourites it was asked about
        return {self.ids["wl-good"]: [(self.ids["loved-1"], 4)]} if self.ids["loved-1"] in loved_ids else {}

    def taste_fans_also_loved(self, conn, source_ids, candidate_ids, **_):
        assert self.ids["seen-watchlisted"] not in candidate_ids
        # like the real query, only sources and candidates it was given
        wanted = [self.ids["pick-film"], self.ids["wl-ok"]]
        kept = [c for c in wanted if c in candidate_ids]
        return {self.ids["wl-good"]: kept} if self.ids["wl-good"] in source_ids and kept else {}

    def rater_corpus_summary(self, conn):
        return {"raters": {"scraped": 372, "candidate": 9}, "ratings": 139749, "films": 25085}


def test_build_for_you_assembles_the_days_payload(monkeypatch):
    slugs = ["seen-watchlisted", "wl-good", "wl-ok", "pick-tv", "pick-film", "pick-nowhere", "loved-1"]
    fake = _FakeDb({s: i for i, s in enumerate(slugs, start=100)})
    for name in ("rater_film_lookup", "rater_similarities", "rater_predictions", "set_rater_film_kinds",
                 "neighbour_fans", "taste_because", "taste_fans_also_loved", "rater_corpus_summary"):
        monkeypatch.setattr(for_you.db, name, getattr(fake, name))
    monkeypatch.setattr(taste, "_ensure_mu", lambda conn: 3.5)

    diary = {f"rated-{i}": {"personal_rating": 3.0 + (i % 5) * 0.5, "title": f"Rated {i}"} for i in range(60)}
    diary["loved-1"] = {"personal_rating": 5.0, "title": "Loved One", "poster_url": "l.jpg"}
    diary["seen-watchlisted"] = {"personal_rating": 4.0, "title": "Seen"}
    enriched = []

    def enrich(candidates):
        enriched.extend(c["slug"] for c in candidates)
        # pick-nowhere has no offers anywhere, so availability drops it
        keep = [c for c in candidates if c["slug"] != "pick-nowhere"]
        return [c["slug"] for c in keep], {c["slug"]: {**c, "all_offers": [{"brand": "MUBI"}]} for c in keep}

    payload, new_films = build_for_you(
        None, diary=diary, josh_watchlist={"seen-watchlisted", "wl-good", "wl-ok"},
        known_slugs={"seen-watchlisted", "wl-good", "wl-ok"}, discovery_films={}, sarah_username="SarahVeen",
        generated_at="2026-09-25T08:00:00+00:00", enrich=enrich,
        fetch_details=lambda slug: {**_details(), "tmdb_kind": "movie"},
    )
    assert enriched == ["pick-film", "pick-nowhere"]      # the TV pick never got that far
    assert set(new_films) == {"pick-film"}
    assert payload["picks"] == ["pick-film"]
    # already logged, so left out of tonight even though it scores best
    assert payload["watchlist"] == ["wl-good", "wl-ok"]
    assert payload["scores"]["wl-good"]["because"] == ["loved-1"]
    assert payload["scores"]["wl-good"]["lovers"] == 5
    assert payload["scores"]["wl-good"]["predicted"] == pytest.approx(3.5 + payload["your_offset"] + 0.6, abs=0.01)
    assert payload["loved"] == {"loved-1": {"title": "Loved One", "poster_url": "l.jpg"}}
    assert payload["fans_also_loved"] == {"wl-good": ["pick-film", "wl-ok"]}
    assert payload["matches"]["sarah"]["rank"] == 2
    assert payload["corpus"] == {"raters": 372, "ratings": 139749}
    assert fake.kinds_saved[fake.ids["pick-film"]] == "movie"


def test_build_for_you_needs_enough_of_your_own_ratings():
    diary = {f"f{i}": {"personal_rating": 4.0} for i in range(for_you.MIN_OWN_RATINGS - 1)}
    assert build_for_you(None, diary=diary, josh_watchlist=set(), known_slugs=set(), discovery_films={},
                         sarah_username=None, generated_at="t", fetch_details=None, enrich=None) == (None, {})


# ---------- the dashboard's view of it ----------

def _stored() -> dict:
    return {
        "generated_at": "2026-09-25T08:00:00+00:00", "corpus": {"raters": 372, "ratings": 139749},
        "your_offset": 0.14, "matches": {"count": 100, "sarah": None, "closest": []}, "loved": {},
        "scores": {s: {"predicted": 4.5} for s in ("wl", "pick", "dismissed-pick", "dropped", "dismissed-wl")},
        "watchlist": ["dropped", "wl", "dismissed-wl"],
        "picks": ["pick", "dismissed-pick", "dropped", "wl"],
        "fans_also_loved": {"wl": ["dismissed-pick", "pick"], "pick": ["dropped"]},
    }


def test_for_you_data_only_names_films_the_page_can_open():
    shipped = {slug: {} for slug in ("wl", "pick", "dismissed-pick", "dismissed-wl")}
    data = _for_you_data(_stored(), shipped, {"wl", "dismissed-wl"}, {"dismissed-pick", "dismissed-wl"})
    # "dropped" left the watchlist since the morning run; dismissing only
    # ever hides a recommendation, never a film on your own watchlist
    assert data["watchlist"] == ["wl", "dismissed-wl"]
    assert data["picks"] == ["pick"]
    assert data["fans_also_loved"] == {"wl": ["pick"]}
    assert set(data["scores"]) == {"wl", "pick", "dismissed-wl"}
    assert data["corpus"]["raters"] == 372


def test_for_you_data_before_the_first_run():
    assert _for_you_data(None, {}, set(), set()) is None


def test_pick_candidates_only_takes_films_known_to_be_films():
    # A loaded page with no TMDB link the parser recognises: warned about
    # once, left out, and no more requests spent on films not cached as one.
    pool = [_pool_row(1, "unreadable"), _pool_row(2, "cached-film", kind="movie"), _pool_row(3, "never-asked")]
    asked, warnings = [], []

    def fetch(slug):
        asked.append(slug)
        return _details(kind=None)

    candidates, kinds = pick_candidates(pool, {}, fetch, want=5, warn=warnings.append)
    assert [c["slug"] for c in candidates] == ["cached-film"]
    assert asked == ["unreadable", "cached-film"]
    assert len(warnings) == 1 and "markup" in warnings[0]
    assert kinds == {}


def test_build_for_you_keeps_dismissed_films_out_of_picks(monkeypatch):
    slugs = ["seen-watchlisted", "wl-good", "wl-ok", "pick-tv", "pick-film", "pick-nowhere", "loved-1"]
    fake = _FakeDb({s: i for i, s in enumerate(slugs, start=100)})
    for name in ("rater_film_lookup", "rater_similarities", "rater_predictions", "set_rater_film_kinds",
                 "neighbour_fans", "taste_because", "taste_fans_also_loved", "rater_corpus_summary"):
        monkeypatch.setattr(for_you.db, name, getattr(fake, name))
    monkeypatch.setattr(taste, "_ensure_mu", lambda conn: 3.5)
    diary = {f"rated-{i}": {"personal_rating": 3.0 + (i % 5) * 0.5} for i in range(60)}
    fetched = []

    def fetch(slug):
        fetched.append(slug)
        return _details()

    payload, _ = build_for_you(
        None, diary=diary, josh_watchlist={"wl-good", "wl-ok"}, known_slugs={"wl-good", "wl-ok"},
        discovery_films={}, sarah_username=None, generated_at="t", fetch_details=fetch,
        enrich=lambda cands: ([c["slug"] for c in cands], {c["slug"]: {**c, "all_offers": [{}]} for c in cands}),
        dismissed={"pick-film"},
    )
    assert "pick-film" not in payload["picks"] and "pick-film" not in fetched
    assert payload["picks"] == ["pick-nowhere"]


def test_for_you_data_drops_suggestions_already_logged():
    # A payload carried forward from a failed run predates the latest diary.
    shipped = {slug: {} for slug in ("wl", "pick", "dismissed-pick", "dismissed-wl")}
    data = _for_you_data(_stored(), shipped, {"wl", "dismissed-wl"}, set(), seen={"pick", "dismissed-wl"})
    assert data["picks"] == ["dismissed-pick"]
    assert data["watchlist"] == ["wl"]
    assert data["fans_also_loved"] == {"wl": ["dismissed-pick"]}
    # a film you've seen can still show why the engine rates it as it does
    assert "pick" in data["scores"]
