from watchlist_justwatch import movielens


def test_match_tmdb_ids_checks_the_year_and_uses_the_cache():
    calls = []

    def search(title, year):
        calls.append((title, year))
        return {"Tár": {"id": 817758, "release_date": "2022-10-07"},
                "Solaris": {"id": 2759, "release_date": "2002-11-27"}}.get(title)

    cache = {"whiplash-2014": 244786}
    entries = {"tar-2022": {"title": "Tár", "year": "2022"},
               "solaris": {"title": "Solaris", "year": "1972"},  # TMDB's best guess is the remake
               "whiplash-2014": {"title": "Whiplash", "year": "2014"},
               "nothing": {"title": "No Such Film", "year": "2001"}}
    assert movielens.match_tmdb_ids(entries, search, cache, log=lambda *_: None) == {
        "tar-2022": 817758, "solaris": None, "whiplash-2014": 244786, "nothing": None}
    assert ("Whiplash", 2014) not in calls
    # No year-filtered result falls back to an unfiltered search.
    assert ("No Such Film", None) in calls
    assert cache["solaris"] is None


def test_links_and_qualifying_users(tmp_path):
    (tmp_path / "links.csv").write_text("movieId,imdbId,tmdbId\n1,1,817758\n2,2,244786\n3,3,\n4,4,99\n5,5,817758\n")
    (tmp_path / "ratings.csv").write_text(
        "userId,movieId,rating,timestamp\n"
        "10,1,4.5,0\n10,2,5.0,0\n10,4,1.0,0\n"
        "11,1,3.0,0\n11,4,2.0,0\n"
    )
    movies = movielens.movie_slugs(str(tmp_path / "links.csv"), {817758: "tar-2022", 244786: "whiplash-2014"})
    assert movies == {1: "tar-2022", 2: "whiplash-2014"}
    assert movielens.qualifying_users(str(tmp_path / "ratings.csv"), set(movies), 2) == {10}


def test_import_refuses_a_remote_database():
    assert movielens.is_local("postgresql://localhost/movielens")
    assert movielens.is_local("postgresql:///movielens")
    assert not movielens.is_local("postgresql://user:pw@ep-x.neon.tech/neondb")


def test_unambiguous_drops_a_tmdb_id_two_films_matched():
    assert movielens.unambiguous({"a": 1, "b": 2, "c": 2, "d": None}) == {1: "a"}
