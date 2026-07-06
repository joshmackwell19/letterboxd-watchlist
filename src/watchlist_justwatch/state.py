from dataclasses import dataclass, field

from .justwatch_client import CACHEABLE_CONFIDENCE
from .models import FilmState

SCHEMA_VERSION = 1


@dataclass
class StateDoc:
    schema_version: int = SCHEMA_VERSION
    last_run_at: str | None = None
    # Date (YYYY-MM-DD) the stale-rotation JustWatch batch last actually ran —
    # caps that batch to once per calendar day regardless of how many times
    # the workflow is triggered that day (see main.py).
    last_justwatch_check_date: str | None = None
    films: dict[str, FilmState] = field(default_factory=dict)
    # Last few watched films (from the Letterboxd profile) plus their
    # director/cast, used to correlate home-page recommendations — refreshed
    # each run.
    recent_watches: list[dict] = field(default_factory=list)
    # because_you_watched/same_director/same_cast — [{"key","header","slugs"}].
    # Correlated across all of TMDB (not just the watchlist) so discovery
    # sections can surface films you haven't added yet, which needs network
    # calls (TMDB/Letterboxd/JustWatch) precomputed here since the dashboard
    # itself must stay network-free to regenerate.
    recommendation_sections: list[dict] = field(default_factory=list)
    # slug -> same shape as a films_by_slug entry, for films the sections
    # above surfaced that aren't already on the watchlist.
    discovery_films: dict[str, dict] = field(default_factory=dict)
    # Rolling log of newly-detected have/free offers, newest first, capped —
    # a single day's diff is often too small to fill a "recently added" list.
    recent_additions: list[dict] = field(default_factory=list)
    # Every film ever logged as watched (slug -> title/year/rating/poster/
    # director/starring/synopsis, enriched once and cached forever) — no
    # per-entry watch dates (see fetch_watched_films), but enough to exclude
    # already-seen films from discovery and to power "worth a rewatch".
    diary: dict[str, dict] = field(default_factory=dict)


def get_cached_entry_id(state: StateDoc, slug: str) -> tuple[str | None, str | None]:
    film = state.films.get(slug)
    if film is None or film.entry_id is None or film.confidence not in CACHEABLE_CONFIDENCE:
        return None, None
    return film.entry_id, film.confidence
