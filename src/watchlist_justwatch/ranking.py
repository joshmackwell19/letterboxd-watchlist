"""The year's ranked list — the Rank tab's game (see rank_engine.js for how
the order itself is built; that part runs in the page).

This side keeps the roster: which films belong to a year's list. The diary
table can't answer that on its own, because it holds one row per film with
only the latest watch — a January rewatch of something first seen in
November would move its `watched_date` into the new year and silently drop
it from the old list. So a film joins `ranking_roster` the first time its
diary entry is dated in that year, and nothing here ever removes it.

Everything in this module is pure (no database, no network): `plan_roster_sync`
decides what should change, `db.apply_ranking_sync` writes it.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date

UNRATED_SORT_STARS = 3.25  # an unrated film queues after the 3½s — same as rank_engine.js


def ranking_year(today: date) -> int:
    """The year being ranked. January still belongs to the year before: the
    last watches of December get logged late, and the list is finished and
    uploaded in the new year. February starts the new one (seeded then with
    whatever January's diary already holds)."""
    return today.year - 1 if today.month == 1 else today.year


@dataclass
class RosterPlan:
    inserts: list[dict] = field(default_factory=list)
    updates: list[dict] = field(default_factory=list)   # {slug, <changed columns>}
    reopened: list[str] = field(default_factory=list)   # slugs to take back out of the placed order

    def __bool__(self) -> bool:
        return bool(self.inserts or self.updates or self.reopened)


def plan_roster_sync(diary: dict[str, dict], roster: dict[str, dict], year: int) -> RosterPlan:
    """What the roster needs to match the diary.

    diary: slug -> diary entry (db's `diary` table). roster: slug -> the
    year's existing rows.

    - A film watched this year that isn't listed yet is added. On the very
      first sync (an empty roster) that's the whole year so far, added
      quietly; afterwards it's a new log, flagged to be offered first in the
      game (`queue_first`) and to be notified about (`notified` False).
    - A film already listed whose diary date has moved later *within the
      year* was rewatched: it's reopened — taken out of the placed order so
      it gets placed again — with the same flags as a new log.
    - A changed star rating or a poster that has since been fetched is
      copied across without reopening anything.
    """
    plan = RosterPlan()
    seeding = not roster
    prefix = f"{year}-"
    for slug in sorted(diary):
        entry = diary[slug]
        watched = entry.get("watched_date")
        existing = roster.get(slug)
        if existing is None:
            if not watched or not watched.startswith(prefix):
                continue
            plan.inserts.append({
                "slug": slug,
                "title": entry.get("title") or slug,
                "film_year": entry.get("year"),
                "poster_url": entry.get("poster_url"),
                "stars": entry.get("personal_rating"),
                "watched_date": watched,
                "is_rewatch": bool(entry.get("is_rewatch")),
                "queue_first": not seeding,
                "notified": seeding,
            })
            continue

        changes: dict = {}
        if entry.get("poster_url") and entry["poster_url"] != existing.get("poster_url"):
            changes["poster_url"] = entry["poster_url"]
        if entry.get("title") and entry["title"] != existing.get("title"):
            changes["title"] = entry["title"]
        if existing.get("film_year") is None and entry.get("year") is not None:
            changes["film_year"] = entry["year"]
        in_year = bool(watched) and watched.startswith(prefix)
        # A rating read while the diary entry is dated in a later year is that
        # year's opinion — the old list keeps the one it was ranked with.
        if in_year and entry.get("personal_rating") is not None \
                and entry["personal_rating"] != existing.get("stars"):
            changes["stars"] = entry["personal_rating"]
        if in_year and watched > (existing.get("watched_date") or ""):
            changes.update(watched_date=watched, is_rewatch=True, queue_first=True, notified=False)
            plan.reopened.append(slug)
        if changes:
            plan.updates.append({"slug": slug, **changes})
    return plan


def full_order(roster: list[dict], placed: list[str]) -> list[str]:
    """Every film on the roster, ranked: the placed ones in their order, each
    waiting one at the bottom of its star rating. The same rule as
    rank_engine.js's fullOrder, so `--export-ranking` and the page's Export
    button write the same file."""
    by_slug = {film["slug"]: film for film in roster}
    order = [slug for slug in placed if slug in by_slug]
    done = set(order)

    def stars(slug: str) -> float:
        value = by_slug[slug].get("stars")
        return UNRATED_SORT_STARS if value is None else value

    waiting = sorted((slug for slug in by_slug if slug not in done),
                     key=lambda slug: (-stars(slug), by_slug[slug].get("watched_date") or "", slug))
    for slug in waiting:
        at = next((i for i, other in enumerate(order) if stars(other) < stars(slug)), len(order))
        order.insert(at, slug)
    return order


def ranking_csv(roster: list[dict], placed: list[str]) -> str:
    """The list as Letterboxd's importer reads it: file order is list order,
    and LetterboxdURI pins each row to the exact film."""
    by_slug = {film["slug"]: film for film in roster}
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Title", "Year", "LetterboxdURI"])
    for slug in full_order(roster, placed):
        film = by_slug[slug]
        writer.writerow([film.get("title") or slug, film.get("film_year") or "",
                         f"https://letterboxd.com/film/{slug}/"])
    return out.getvalue()


def place_it_notifications(films: list[dict], year: int, dashboard_url: str, limit: int = 3) -> list[dict]:
    """Web Push payloads for films newly on the roster (or rewatched): each
    opens the Rank tab, where they're first in the queue."""
    url = f"{dashboard_url}?view=rank"
    if len(films) > limit:
        names = ", ".join(film["title"] for film in films[:5])
        more = f" and {len(films) - 5} more" if len(films) > 5 else ""
        return [{"title": f"{len(films)} films to place in your {year} ranking", "body": names + more,
                 "url": url, "tag": "rank-place"}]
    return [place_it_notification(film, year, dashboard_url) for film in films]


def place_it_notification(film: dict, year: int, dashboard_url: str) -> dict:
    verb = "Rewatched" if film.get("is_rewatch") else "Logged"
    return {
        "title": f"Place it: {film['title']}",
        "body": f"{verb} — a few taps puts it in your {year} ranking.",
        "url": f"{dashboard_url}?view=rank",
        "tag": f"rank-{film['slug']}",
    }
