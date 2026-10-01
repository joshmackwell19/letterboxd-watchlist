from datetime import datetime

import pytest

from watchlist_justwatch import cinemas
from watchlist_justwatch.cinemas import (
    CinemaFetchError,
    _parse_cinemaguide,
    _parse_clusterflick,
    _parse_barbican,
    _parse_barbican_time,
    _parse_hr_min_duration,
    _parse_pcc_datetime,
    _parse_prince_charles,
    _parse_riverside,
    _parse_vue,
    VUE_SITES,
    VueProgramme,
    drop_past_showings,
    match_watchlist_film,
)
from watchlist_justwatch.models import FilmState


def _film(slug: str, title: str, year: int | None = None) -> FilmState:
    return FilmState(slug=slug, title=title, year=year, entry_id=None, confidence="exact", last_checked="")


# ---------- match_watchlist_film ----------

def test_match_exact_title():
    films = {"taxi-driver": _film("taxi-driver", "Taxi Driver", 1976)}
    assert match_watchlist_film("Taxi Driver", 1976, films) == "taxi-driver"


def test_match_ignores_punctuation_and_case():
    films = {"in-the-mood-for-love": _film("in-the-mood-for-love", "In the Mood for Love", 2000)}
    assert match_watchlist_film("IN THE MOOD FOR LOVE!", 2000, films) == "in-the-mood-for-love"


def test_match_strips_leading_article():
    films = {"the-godfather": _film("the-godfather", "The Godfather", 1972)}
    assert match_watchlist_film("Godfather", 1972, films) == "the-godfather"


def test_match_no_match_returns_none():
    films = {"taxi-driver": _film("taxi-driver", "Taxi Driver", 1976)}
    assert match_watchlist_film("Some Other Film", None, films) is None


def test_match_year_tolerant_disambiguation():
    films = {
        "old-one": _film("old-one", "Same Title", 1990),
        "new-one": _film("new-one", "Same Title", 2020),
    }
    assert match_watchlist_film("Same Title", 2021, films) == "new-one"


def test_match_without_year_falls_back_to_first_candidate():
    films = {"only-one": _film("only-one", "Unique Title", 2015)}
    assert match_watchlist_film("Unique Title", None, films) == "only-one"


# ---------- Prince Charles Cinema parsing ----------

_PCC_HTML = """
<div class="jacro-event movie-tabs row 35mm">
  <div class="film_list-outer">
    <div class="film_img"><a href="#"><img src="https://example.com/poster.jpg"></a></div>
    <div class="jacrofilm-list-content">
      <a class="liveeventtitle" href="#">Taxi Driver</a>
      <div class="running-time"><span>1976</span><span>113mins</span><span>USA</span><span>(18)</span><span>Crime</span></div>
      <div class="film-info"><span>Directed by Martin Scorsese</span><span>Starring Robert De Niro</span></div>
      <div class="jacro-formatted-text"><p>A cabbie loses his mind.</p></div>
    </div>
    <div class="performance-list-items-outer">
      <ul class="performance-list-items">
        <div class="heading">Tuesday 8th September</div>
        <li class="35mm">
          <a class="film_book_button" href="https://example.com/book/1"><span class="time">3:15 pm</span></a>
        </li>
        <div class="heading">Wednesday 9th September</div>
        <li class="35mm">
          <a class="film_book_button" href="https://example.com/book/2"><span class="time">6:00 pm</span></a>
        </li>
      </ul>
    </div>
  </div>
</div>
"""


def test_parse_prince_charles_extracts_all_fields():
    now = datetime(2026, 9, 8, 12, 0)
    showings = _parse_prince_charles(_PCC_HTML, now)
    assert len(showings) == 2
    first = showings[0]
    assert first["title"] == "Taxi Driver"
    assert first["year"] == 1976
    assert first["duration_minutes"] == 113
    assert first["director"] == "Martin Scorsese"
    assert first["synopsis"] == "A cabbie loses his mind."
    assert first["poster_url"] == "https://example.com/poster.jpg"
    assert first["booking_url"] == "https://example.com/book/1"
    assert first["showtime"] == "2026-09-08T15:15:00"
    assert showings[1]["showtime"] == "2026-09-09T18:00:00"


def test_parse_pcc_datetime_rolls_over_to_next_year_when_date_has_passed():
    # Fetched in late December for a listing that only makes sense in January.
    now = datetime(2026, 12, 20, 12, 0)
    result = _parse_pcc_datetime("Tuesday 5th January", "3:00 pm", now)
    assert result.year == 2027
    assert result.month == 1
    assert result.day == 5


def test_parse_pcc_datetime_malformed_returns_none():
    now = datetime(2026, 9, 8, 12, 0)
    assert _parse_pcc_datetime("garbage", "3:00 pm", now) is None
    assert _parse_pcc_datetime("Tuesday 8th September", "not a time", now) is None


# ---------- Barbican parsing ----------

_BARBICAN_HTML = """
<div class="cinema-listing-card">
  <div class="cinema-listing-card__media"><img src="/poster.jpg"></div>
  <div class="cinema-listing-card__content">
    <h2 class="cinema-listing-card__title"><a href="#">Bitter Christmas</a></h2>
    <p><strong>Pedro Almodóvar</strong><span> is back with a new film.</span></p>
    <div class="cinema-listing-card__tags"><div class="cinema-listing-card__tag">1hr 51mins</div></div>
  </div>
  <div class="cinema-listing-card__instances">
    <div class="cinema-instance-list">
      <div class="cinema-instance-list__instance">
        <a href="https://tickets.example.com/1"><span>icon</span><span>6.10pm</span></a>
      </div>
    </div>
  </div>
</div>
"""


def test_parse_barbican_extracts_fields_and_resolves_relative_poster_url():
    from datetime import date
    showings = _parse_barbican(_BARBICAN_HTML, date(2026, 9, 8))
    assert len(showings) == 1
    s = showings[0]
    assert s["title"] == "Bitter Christmas"
    assert s["duration_minutes"] == 111
    assert s["synopsis"] == "Pedro Almodóvar is back with a new film."
    assert s["poster_url"] == "https://www.barbican.org.uk/poster.jpg"
    assert s["booking_url"] == "https://tickets.example.com/1"
    assert s["showtime"] == "2026-09-08T18:10:00"


def test_parse_hr_min_duration_variants():
    assert _parse_hr_min_duration("1hr 51mins") == 111
    assert _parse_hr_min_duration("45mins") == 45
    assert _parse_hr_min_duration("2hr") == 120
    assert _parse_hr_min_duration("no duration here") is None


def test_parse_barbican_time_handles_dot_separator():
    from datetime import date
    result = _parse_barbican_time(date(2026, 9, 8), "6.10pm")
    assert result.isoformat() == "2026-09-08T18:10:00"


# ---------- Vue parsing ----------

_VUE_DATA = {
    "result": [
        {
            "filmTitle": "Spider-Man: Brand New Day",
            "releaseDate": "2026-07-29T00:00:00",
            "director": "Destin Daniel Cretton",
            "synopsisShort": "Peter Parker fights crime.",
            "runningTime": 145,
            "posterImageSrc": "https://example.com/spiderman.jpg",
            "showingGroups": [
                {"sessions": [
                    {"startTime": "2026-09-08T14:25:00", "bookingUrl": "/book-tickets/summary/10046/1"},
                ]},
            ],
        },
        {"filmTitle": "", "showingGroups": []},
    ],
}


def test_parse_vue_extracts_fields_and_resolves_relative_booking_url():
    showings = _parse_vue(_VUE_DATA, "Vue Fulham Broadway")
    assert len(showings) == 1
    s = showings[0]
    assert s["cinema"] == "Vue Fulham Broadway"
    assert s["title"] == "Spider-Man: Brand New Day"
    assert s["year"] == 2026
    assert s["duration_minutes"] == 145
    assert s["director"] == "Destin Daniel Cretton"
    assert s["showtime"] == "2026-09-08T14:25:00"
    assert s["booking_url"] == "https://www.myvue.com/book-tickets/summary/10046/1"


def test_parse_vue_skips_entries_with_no_title():
    assert _parse_vue({"result": [{"filmTitle": "", "showingGroups": []}]}, "Vue Fulham Broadway") == []


# ---------- Vue via CinemaGuide ----------

_CINEMAGUIDE_DATA = {
    "all_screenings_on_all_dates": {"tab_date_label": "ALL DATES", "film_data": [
        {"title": "theodyssey", "screenings_data": [
            {"screenings_date_label": "THU 24 SEPT", "screenings": [
                {"time": "2026-09-24T18:05:00.000Z",
                 "link": "https://www.myvue.com//book-tickets/summary/10046/HO1/1",
                 "venue_name": "Vue London - Fulham Broadway"},
            ]},
            {"screenings_date_label": "SAT 05 DEC", "screenings": [
                {"time": "2026-12-05T18:30:00.000Z", "link": None,
                 "venue_name": "Vue London - Fulham Broadway"},
            ]},
        ]},
        {"title": "nometa", "screenings_data": [
            {"screenings": [{"time": "2026-09-24T18:00:00.000Z"}]},
        ]},
    ]},
    "film_meta_data_map": {
        "theodyssey": {"display_film_title": "The Odyssey", "length_in_minutes": 173,
                       "description": "Odysseus heads home.", "image_link": "https://example.com/o.jpg"},
    },
}


def test_parse_cinemaguide_converts_utc_to_london_wall_clock():
    showings = _parse_cinemaguide(_CINEMAGUIDE_DATA)
    # BST in September (UTC+1), GMT in December (UTC+0) — stored the same
    # way every other venue's showtimes are: naive London time.
    assert [s["showtime"] for s in showings] == ["2026-09-24T19:05:00", "2026-12-05T18:30:00"]


def test_parse_cinemaguide_extracts_fields_and_skips_films_with_no_title():
    s = _parse_cinemaguide(_CINEMAGUIDE_DATA)[0]
    assert s["cinema"] == "Vue Fulham Broadway"
    assert s["title"] == "The Odyssey"
    assert s["duration_minutes"] == 173
    assert s["synopsis"] == "Odysseus heads home."
    assert s["booking_url"] == "https://www.myvue.com/book-tickets/summary/10046/HO1/1"
    assert all(x["title"] == "The Odyssey" for x in _parse_cinemaguide(_CINEMAGUIDE_DATA))


def test_parse_cinemaguide_treats_zero_runtime_as_unknown():
    data = {**_CINEMAGUIDE_DATA, "film_meta_data_map": {
        "theodyssey": {**_CINEMAGUIDE_DATA["film_meta_data_map"]["theodyssey"], "length_in_minutes": 0}}}
    assert _parse_cinemaguide(data)[0]["duration_minutes"] is None


def test_parse_cinemaguide_files_each_screening_under_its_own_site():
    data = {"all_screenings_on_all_dates": {"film_data": [{"title": "theodyssey", "screenings_data": [
        {"screenings": [
            {"time": "2026-10-02T18:00:00.000Z", "venue_name": "Vue London - Westfield (Shepherd's Bush)"},
            {"time": "2026-10-02T19:00:00.000Z", "venue_name": "Vue London - West End (Leicester Square)"},
            {"time": "2026-10-02T20:00:00.000Z", "venue_name": "Vue London - Piccadilly Circus"},
            # A venue nobody asked about is dropped, not filed under another.
            {"time": "2026-10-02T21:00:00.000Z", "venue_name": "Vue London - Westfield Stratford"},
        ]},
    ]}]}, "film_meta_data_map": _CINEMAGUIDE_DATA["film_meta_data_map"]}
    assert [s["cinema"] for s in _parse_cinemaguide(data)] == [
        "Vue Shepherd's Bush", "Vue West End", "Vue Piccadilly"]


def test_vue_programme_falls_back_to_one_cinemaguide_request_for_every_site(monkeypatch):
    opened, requested = [], []

    def refused():
        opened.append(1)
        raise CinemaFetchError("Vue what's-on page request failed (HTTP 403)")

    def cinemaguide(sites):
        requested.append(sites)
        return {"Vue Fulham Broadway": [{"title": "A"}], "Vue West End": [{"title": "B"}]}

    monkeypatch.setattr(cinemas, "_open_vue_session", refused)
    monkeypatch.setattr(cinemas, "_fetch_vue_cinemaguide", cinemaguide)
    programme = VueProgramme()
    fulham, shepherds_bush, west_end, _ = VUE_SITES
    assert programme.fetch(fulham) == [{"title": "A"}]
    assert programme.fetch(west_end) == [{"title": "B"}]
    # A site missing from the response fails alone, so run() carries
    # forward yesterday's listing for that site only.
    with pytest.raises(CinemaFetchError, match="HTTP 403.*no showings for Vue Shepherd's Bush"):
        programme.fetch(shepherds_bush)
    # Vue's challenge answers every site alike: asked once, not per site.
    assert len(opened) == 1
    assert len(requested) == 1


def test_vue_programme_uses_the_direct_api_while_vue_answers(monkeypatch):
    monkeypatch.setattr(cinemas, "_open_vue_session", lambda: "session")
    monkeypatch.setattr(cinemas, "_fetch_vue_direct",
                        lambda session, site, now=None: [{"cinema": site.name, "session": session}])
    monkeypatch.setattr(cinemas, "_fetch_vue_cinemaguide", lambda sites: pytest.fail("fell back"))
    programme = VueProgramme()
    assert [programme.fetch(site)[0]["cinema"] for site in VUE_SITES] == [site.name for site in VUE_SITES]


def test_vue_programme_raises_with_both_reasons_when_both_routes_fail(monkeypatch):
    def refused():
        raise CinemaFetchError("direct 403")

    def empty(sites):
        raise CinemaFetchError("CinemaGuide returned no Vue showings")

    monkeypatch.setattr(cinemas, "_open_vue_session", refused)
    monkeypatch.setattr(cinemas, "_fetch_vue_cinemaguide", empty)
    programme = VueProgramme()
    for site in VUE_SITES:
        with pytest.raises(CinemaFetchError, match="direct 403.*no Vue showings"):
            programme.fetch(site)


# ---------- BFI via Clusterflick ----------

_CLUSTERFLICK_DATA = [
    {
        "category": "movie", "title": "25th Anniversary: Donnie Darko",
        "overview": {"directors": ["Richard Kelly"], "duration": 6780000, "year": "2001"},
        "performances": [
            # 18:30 BST
            {"time": 1790789400000, "bookingUrl": "https://whatson.bfi.org.uk/imax/Online/a"},
            {"time": None},
        ],
        "themoviedb": {"id": 141, "title": "Donnie Darko", "summary": "TMDB's, not ours to store."},
        "url": "https://whatson.bfi.org.uk/imax/Online/b",
    },
    {"category": "talk", "title": "In Conversation", "overview": {},
     "performances": [{"time": 1790789400000}]},
    {"category": "multiple-movies", "title": "Double Bill", "overview": {"year": ""},
     "themoviedbs": [{"id": 1}, {"id": 2}],
     "performances": [{"time": 1790789400000}], "url": "https://whatson.bfi.org.uk/c"},
]


def test_parse_clusterflick_keeps_only_its_own_fields_and_the_tmdb_id():
    s, double_bill = _parse_clusterflick(_CLUSTERFLICK_DATA, "BFI IMAX")
    assert s == {
        "cinema": "BFI IMAX", "title": "25th Anniversary: Donnie Darko", "year": 2001,
        "showtime": "2026-09-30T18:30:00", "duration_minutes": 113, "director": "Richard Kelly",
        # TMDB's metadata is excluded from Clusterflick's licence — only the
        # id is kept, as a join key.
        "synopsis": None, "poster_url": None,
        "booking_url": "https://whatson.bfi.org.uk/imax/Online/a", "tmdb_id": 141,
    }
    # No single film to resolve to; the event's own page when a
    # performance has no booking link.
    assert double_bill["tmdb_id"] is None
    assert double_bill["year"] is None
    assert double_bill["booking_url"] == "https://whatson.bfi.org.uk/c"


def test_fetch_clusterflick_fails_on_an_empty_programme(monkeypatch):
    monkeypatch.setattr(cinemas, "_get_json", lambda url, **_: [])
    with pytest.raises(CinemaFetchError, match="no showings for BFI Southbank"):
        cinemas.fetch_clusterflick("BFI Southbank")


# ---------- drop_past_showings ----------

def test_drop_past_showings_keeps_only_what_has_not_started():
    showings = [{"showtime": "2026-09-24T18:00:00"}, {"showtime": "2026-09-24T19:00:00"},
                {"showtime": "2026-09-25T10:00:00"}]
    kept = drop_past_showings(showings, now=datetime(2026, 9, 24, 19, 0))
    assert [s["showtime"] for s in kept] == ["2026-09-24T19:00:00", "2026-09-25T10:00:00"]


# ---------- Riverside parsing ----------

_RIVERSIDE_DATA = [
    {
        "slot_tag": "Cinema", "title": "Pressure", "duration": "100 minutes",
        "search_text": "blurb...Director:Anthony MarasCast:Andrew Scott, Brendan Fraser",
        "text": "The 72 hours that shaped the world.&nbsp;More&hellip;",
        "image_url": "https://example.com/pressure.jpg",
        "url": "https://riversidestudios.co.uk/whats-on/pressure/",
        "performances": {"1789081200": [{"timestamp": "1789150200"}]},
    },
    {"slot_tag": False, "title": "Rehearsal Room", "duration": "240 minutes", "performances": {}},
]


def test_parse_riverside_filters_to_cinema_only():
    showings = _parse_riverside(_RIVERSIDE_DATA)
    assert len(showings) == 1
    assert showings[0]["title"] == "Pressure"


def test_parse_riverside_extracts_duration_director_and_unescapes_synopsis():
    s = _parse_riverside(_RIVERSIDE_DATA)[0]
    assert s["duration_minutes"] == 100
    assert s["director"] == "Anthony Maras"
    assert s["synopsis"] == "The 72 hours that shaped the world.\xa0More…"
    assert s["poster_url"] == "https://example.com/pressure.jpg"
    assert s["booking_url"] == "https://riversidestudios.co.uk/whats-on/pressure/"


def test_parse_riverside_converts_timestamp_to_iso():
    s = _parse_riverside(_RIVERSIDE_DATA)[0]
    # 18:10 UTC is 19:10 in London in September — not whatever the runner's
    # own clock says (UTC on Actions, which put every showing an hour early).
    assert s["showtime"] == "2026-09-11T19:10:00"
