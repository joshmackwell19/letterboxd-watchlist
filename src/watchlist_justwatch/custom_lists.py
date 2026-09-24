"""User-defined watchlist subsets (config/custom_lists.yaml) — "Brian De
Palma", "Oscar Best Picture nominees", etc. — each shown as its own Home
section.

Membership is always recomputed against the *current* watchlist at
dashboard-build time rather than stored, so a film drops out of every list
the moment it leaves the watchlist (i.e. once it's logged as watched) with
no separate removal step. A list's members come from any combination of:

- `rules`: matched against metadata already tracked on every film
  (director/starring/year) — a film matches if ANY rule matches, and a rule
  matches only if ALL of its conditions do.
- `letterboxd_lists`: someone's Letterboxd list (`user/list/slug`) used as a
  source of truth for things the metadata can't express (award nominations,
  festival lineups). Fetched by the daily run and cached in the
  custom_list_sources table — see main.py's _refresh_custom_list_sources —
  since the dashboard build itself must stay network-free.
- `include`/`exclude`: manual per-slug overrides on top of the above.

`home: false` keeps a list off the Lists tab (Films-tab dropdown only —
the flag predates that tab, when these were Home sections), and `group`
sorts it under a heading in the dropdown and picks its chip on Lists.
"""
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_RULE_KEYS = {"director", "starring", "year_from", "year_to"}


@dataclass(frozen=True)
class CustomList:
    key: str
    name: str
    rules: list[dict] = field(default_factory=list)
    letterboxd_lists: list[str] = field(default_factory=list)
    include: frozenset[str] = frozenset()
    exclude: frozenset[str] = frozenset()
    home: bool = True
    group: str | None = None


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def normalize_person(name: str) -> str:
    """Accent/case-insensitive, so a rule written "Eric Rohmer" still
    matches Letterboxd's "Éric Rohmer"."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def normalize_list_path(path: str) -> str:
    """Accepts a full URL or a bare `user/list/slug`, with or without
    slashes — stored/looked up by the bare form."""
    path = path.strip()
    for prefix in ("https://", "http://"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    if path.startswith("letterboxd.com/"):
        path = path[len("letterboxd.com/"):]
    return path.strip("/")


def load_custom_lists(path: Path) -> list[CustomList]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    result: list[CustomList] = []
    seen_keys: set[str] = set()
    for entry in raw.get("lists", []) or []:
        key, name = entry.get("key"), entry.get("name")
        if not key or not name:
            raise ValueError(f"custom list entry needs both 'key' and 'name': {entry!r}")
        if key in seen_keys:
            raise ValueError(f"duplicate custom list key {key!r}")
        seen_keys.add(key)

        rules = []
        for rule in entry.get("rules", []) or []:
            unknown = set(rule) - _RULE_KEYS
            if unknown:
                # Fail loudly on a typo ("directer:") rather than silently
                # treating the rule as unconditional and matching everything.
                raise ValueError(f"custom list {key!r}: unknown rule condition(s) {sorted(unknown)}")
            if not rule:
                raise ValueError(f"custom list {key!r}: empty rule")
            rules.append({
                "director": {normalize_person(n) for n in _as_list(rule.get("director"))},
                "starring": {normalize_person(n) for n in _as_list(rule.get("starring"))},
                "year_from": rule.get("year_from"),
                "year_to": rule.get("year_to"),
            })

        result.append(CustomList(
            key=key, name=name, rules=rules,
            letterboxd_lists=[normalize_list_path(p) for p in _as_list(entry.get("letterboxd_lists"))],
            include=frozenset(_as_list(entry.get("include"))),
            exclude=frozenset(_as_list(entry.get("exclude"))),
            home=bool(entry.get("home", True)),
            group=entry.get("group"),
        ))
    return result


def all_source_paths(custom_lists: list[CustomList]) -> list[str]:
    return sorted({p for cl in custom_lists for p in cl.letterboxd_lists})


def _people(value) -> set[str]:
    """Films store director as a list; diary entries store it as one
    comma-joined string — accept either."""
    if not value:
        return set()
    names = value.split(",") if isinstance(value, str) else value
    return {normalize_person(n) for n in names if n and n.strip()}


def _rule_matches(rule: dict, director, starring, year: int | None) -> bool:
    if rule["director"] and not (rule["director"] & _people(director)):
        return False
    if rule["starring"] and not (rule["starring"] & _people(starring)):
        return False
    if rule["year_from"] is not None and (year is None or year < rule["year_from"]):
        return False
    if rule["year_to"] is not None and (year is None or year > rule["year_to"]):
        return False
    return True


def matches(cl: CustomList, slug: str, director, starring, year: int | None,
            sources: dict[str, set[str]]) -> bool:
    if slug in cl.exclude:
        return False
    if slug in cl.include:
        return True
    if any(slug in sources.get(p, ()) for p in cl.letterboxd_lists):
        return True
    return any(_rule_matches(rule, director, starring, year) for rule in cl.rules)


def total_groups(custom_lists: list[CustomList]) -> dict[str, tuple[list[str], list[str], list[str]]]:
    """key -> (sources, include, exclude) for every list whose full extent
    is actually knowable — only a purely source-backed list (e.g. "622 Best
    Picture nominees"). A rule-based list's full extent (every De Palma film
    ever made) isn't something the stored metadata can answer. Counted
    server-side by db.custom_list_source_totals."""
    return {
        cl.key: (list(cl.letterboxd_lists), sorted(cl.include), sorted(cl.exclude))
        for cl in custom_lists
        if cl.letterboxd_lists and not cl.rules
    }
