from pathlib import Path

import pytest

from watchlist_justwatch.custom_lists import (
    CustomList, load_custom_lists, matches, normalize_list_path, source_total,
)
from watchlist_justwatch.dashboard import _build_home_sections, _custom_list_sections
from watchlist_justwatch.models import FilmState
from watchlist_justwatch.state import StateDoc


def _film(slug: str, **kwargs) -> FilmState:
    defaults = dict(
        slug=slug, title=slug.title(), year=2020, entry_id="e1", confidence="exact",
        last_checked="2026-09-08T00:00:00Z",
    )
    defaults.update(kwargs)
    return FilmState(**defaults)


def _load(tmp_path: Path, text: str) -> list[CustomList]:
    path = tmp_path / "lists.yaml"
    path.write_text(text)
    return load_custom_lists(path)


# ---------- load_custom_lists / matches ----------

def test_director_rule_is_accent_and_case_insensitive(tmp_path):
    [cl] = _load(tmp_path, "lists:\n  - {key: r, name: R, rules: [{director: eric rohmer}]}\n")
    assert matches(cl, "a", ["Éric Rohmer"], [], 1969, {})
    assert not matches(cl, "b", ["Claude Chabrol"], [], 1969, {})


def test_rule_conditions_are_anded_and_rules_are_ored(tmp_path):
    [cl] = _load(tmp_path, """
lists:
  - key: nw
    name: NW
    rules:
      - {director: [Jean-Luc Godard], year_from: 1958, year_to: 1973}
      - {starring: Anna Karina}
""")
    assert matches(cl, "a", ["Jean-Luc Godard"], [], 1960, {})
    assert not matches(cl, "b", ["Jean-Luc Godard"], [], 2014, {})   # outside the year window
    assert not matches(cl, "c", ["Jean-Luc Godard"], [], None, {})   # unknown year never matches a window
    assert matches(cl, "d", ["Someone Else"], ["Anna Karina"], 1990, {})


def test_diary_style_comma_joined_director_string_matches(tmp_path):
    [cl] = _load(tmp_path, "lists:\n  - {key: d, name: D, rules: [{director: Brian De Palma}]}\n")
    assert matches(cl, "a", "Brian De Palma, Someone Else", [], 1983, {})


def test_source_membership_and_manual_overrides(tmp_path):
    [cl] = _load(tmp_path, """
lists:
  - key: o
    name: O
    letterboxd_lists: [https://letterboxd.com/someone/list/best-picture/]
    include: [extra]
    exclude: [nope]
""")
    sources = {"someone/list/best-picture": {"a", "nope"}}
    assert cl.letterboxd_lists == ["someone/list/best-picture"]
    assert matches(cl, "a", [], [], None, sources)
    assert matches(cl, "extra", [], [], None, sources)
    assert not matches(cl, "nope", [], [], None, sources)
    assert not matches(cl, "b", [], [], None, sources)
    assert source_total(cl, sources) == 2   # a + extra, nope excluded


def test_unknown_rule_key_fails_loudly(tmp_path):
    with pytest.raises(ValueError, match="directer"):
        _load(tmp_path, "lists:\n  - {key: x, name: X, rules: [{directer: Someone}]}\n")


def test_missing_file_means_no_lists(tmp_path):
    assert load_custom_lists(tmp_path / "absent.yaml") == []


def test_normalize_list_path():
    assert normalize_list_path("https://letterboxd.com/u/list/x/") == "u/list/x"
    assert normalize_list_path("/u/list/x/") == "u/list/x"


def test_repo_config_loads():
    lists = load_custom_lists(Path(__file__).parent.parent / "config" / "custom_lists.yaml")
    assert {cl.key for cl in lists} >= {"brian-de-palma", "al-pacino", "french-new-wave", "oscar-best-picture"}


# ---------- _custom_list_sections ----------

def test_section_orders_watchable_first_and_counts_seen_from_diary(tmp_path):
    [cl] = _load(tmp_path, "lists:\n  - {key: dp, name: De Palma, rules: [{director: Brian De Palma}]}\n")
    state = StateDoc(
        films={
            "blow-out": _film("blow-out", director=["Brian De Palma"], rating=4.0),
            "sisters": _film("sisters", director=["Brian De Palma"], rating=3.5),
            "other": _film("other", director=["Someone"]),
        },
        diary={"scarface": {"director": "Brian De Palma", "starring": [], "year": 1983},
               "heat": {"director": "Michael Mann", "starring": [], "year": 1995}},
    )
    offers = {"sisters": [{"classification": "have"}]}

    [section] = _custom_list_sections(state, offers, [cl], {})

    assert section["key"] == "list:dp"
    assert [f["slug"] for f in section["films"]] == ["sisters", "blow-out"]
    assert section["subtitle"] == "2 on your watchlist · 1 seen"


def test_source_backed_section_reports_seen_out_of_total(tmp_path):
    [cl] = _load(tmp_path, "lists:\n  - {key: o, name: Oscars, letterboxd_lists: [u/list/bp]}\n")
    state = StateDoc(films={"a": _film("a")}, diary={"b": {"year": 2000}})
    [section] = _custom_list_sections(state, {}, [cl], {"u/list/bp": {"a", "b", "c"}})
    assert section["subtitle"] == "1 on your watchlist · 1 of 3 seen"


def test_list_with_no_watchlist_members_is_omitted(tmp_path):
    [cl] = _load(tmp_path, "lists:\n  - {key: p, name: Pacino, rules: [{starring: Al Pacino}]}\n")
    state = StateDoc(films={"a": _film("a", starring=["Someone"])})
    assert _custom_list_sections(state, {}, [cl], {}) == []


def test_custom_list_films_do_not_starve_other_home_sections(tmp_path):
    [cl] = _load(tmp_path, "lists:\n  - {key: dp, name: De Palma, rules: [{director: Brian De Palma}]}\n")
    state = StateDoc(films={"a": _film("a", director=["Brian De Palma"], rating=4.5)})
    offers = {"a": [{"classification": "have", "available_to": None}]}

    sections = _build_home_sections(state, offers, {}, {}, set(), {}, [cl], {})

    keys = [s["key"] for s in sections]
    assert "list:dp" in keys and "top_rated" in keys


# ---------- Films-tab dropdown data ----------

def test_dashboard_data_tags_rows_and_lists_only_populated_lists(tmp_path):
    from watchlist_justwatch.dashboard import build_dashboard_data

    lists = _load(tmp_path, """
lists:
  - {key: dp, name: De Palma, rules: [{director: Brian De Palma}]}
  - {key: p, name: Pacino, rules: [{starring: Al Pacino}]}
""")
    state = StateDoc(
        films={"a": _film("a", director=["Brian De Palma"]), "b": _film("b")},
        josh_watchlist={"a", "b"},
    )

    data = build_dashboard_data(state, set(), {}, [], set(), custom_lists=lists, list_sources={})

    rows = {r["slug"]: r for r in data["films"]}
    assert rows["a"]["custom_lists"] == ["dp"]
    assert rows["b"]["custom_lists"] == []
    assert data["custom_lists"] == [{"key": "dp", "name": "De Palma", "count": 1}]
