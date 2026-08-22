"""Computes the weekly streaming-digest email's content: what's newly
available or about to leave this week, split into your main (day-to-day)
services vs everything else. Rendering lives in html_email.py; this module
is data only, mirroring analysis.py's role for the other audit emails."""
from collections import defaultdict
from datetime import date, timedelta

from .config import CountryConfig
from .dashboard import _all_offers_for_film
from .state import StateDoc

DIGEST_WINDOW_DAYS = 7


def _add_to_group(group: list[dict], film, country: str, *, days: int | None = None) -> None:
    entry = next((e for e in group if e["film"].slug == film.slug), None)
    if entry is None:
        entry = {"film": film, "countries": []}
        if days is not None:
            entry["days"] = days
        group.append(entry)
    entry["countries"].append(country)
    if days is not None:
        entry["days"] = min(entry["days"], days)


def compute_weekly_digest(
    state: StateDoc,
    config: dict[str, CountryConfig],
    global_subscriptions: list[str],
    revisitable: set[str],
    main_services: list[str],
    *,
    today: date | None = None,
) -> dict:
    """Every (film, service) that newly became have/free this week, or is
    about to stop being have/free within the week — main_services get
    grouped-by-service billing (with poster art in the email), everything
    else collapses to one row per unique film listing every other service
    it's on, so a single title on 20 small regional services still shows up
    exactly once rather than not at all or 20 times."""
    today = today or date.today()
    week_ago = today - timedelta(days=DIGEST_WINDOW_DAYS)
    main_set = set(main_services)

    # --- additions (from the rolling recent_additions log — see main.py) ---
    additions = [
        a for a in state.recent_additions
        if a["added_at"] >= week_ago.isoformat() and a["classification"] in ("have", "free")
    ]

    main_additions: dict[str, list[dict]] = defaultdict(list)
    other_additions_by_slug: dict[str, dict] = {}

    for a in additions:
        film = state.films.get(a["slug"])
        if film is None:
            continue
        if a["brand"] in main_set:
            _add_to_group(main_additions[a["brand"]], film, a["country"])
        else:
            entry = other_additions_by_slug.setdefault(a["slug"], {"film": film, "services": defaultdict(list)})
            entry["services"][a["brand"]].append(a["country"])

    for group in main_additions.values():
        group.sort(key=lambda e: e["film"].title)
    other_additions = sorted(other_additions_by_slug.values(), key=lambda e: e["film"].title)

    # --- leaving (computed fresh from each film's live offers, same source
    # the dashboard's own "Leaving soon" section uses) ---
    main_leaving: dict[str, list[dict]] = defaultdict(list)
    other_leaving_by_slug: dict[str, dict] = {}

    for slug, film in state.films.items():
        for o in _all_offers_for_film(film, config, global_subscriptions, revisitable):
            if o["classification"] not in ("have", "free") or not o["available_to"]:
                continue
            try:
                days_left = (date.fromisoformat(o["available_to"]) - today).days
            except ValueError:
                continue
            if not (0 <= days_left <= DIGEST_WINDOW_DAYS):
                continue
            if o["brand"] in main_set:
                _add_to_group(main_leaving[o["brand"]], film, o["country"], days=days_left)
            else:
                entry = other_leaving_by_slug.setdefault(
                    slug, {"film": film, "services": defaultdict(list), "min_days": days_left})
                entry["services"][o["brand"]].append(o["country"])
                entry["min_days"] = min(entry["min_days"], days_left)

    for group in main_leaving.values():
        group.sort(key=lambda e: e["days"])
    other_leaving = sorted(other_leaving_by_slug.values(), key=lambda e: e["min_days"])

    total_added = (
        len({e["film"].slug for group in main_additions.values() for e in group})
        + len(other_additions)
    )
    total_leaving = (
        len({e["film"].slug for group in main_leaving.values() for e in group})
        + len(other_leaving)
    )

    return {
        "week_start": week_ago,
        "week_end": today,
        "main_services": main_services,
        "main_additions": dict(main_additions),
        "other_additions": other_additions,
        "main_leaving": dict(main_leaving),
        "other_leaving": other_leaving,
        "total_added": total_added,
        "total_leaving": total_leaving,
    }
