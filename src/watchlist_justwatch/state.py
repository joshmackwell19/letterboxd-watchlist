import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .justwatch_client import CACHEABLE_CONFIDENCE
from .models import FilmState, OfferRecord

SCHEMA_VERSION = 1


@dataclass
class StateDoc:
    schema_version: int = SCHEMA_VERSION
    last_run_at: str | None = None
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


def _offer_to_dict(offer: OfferRecord) -> dict:
    return {
        "country": offer.country,
        "monetization_type": offer.monetization_type,
        "package_technical_name": offer.package_technical_name,
        "package_clear_name": offer.package_clear_name,
        "package_id": offer.package_id,
        "url": offer.url,
    }


def _offer_from_dict(data: dict) -> OfferRecord:
    return OfferRecord(
        country=data["country"],
        monetization_type=data["monetization_type"],
        package_technical_name=data["package_technical_name"],
        package_clear_name=data["package_clear_name"],
        package_id=data["package_id"],
        url=data["url"],
    )


def _film_to_dict(film: FilmState) -> dict:
    return {
        "title": film.title,
        "year": film.year,
        "entry_id": film.entry_id,
        "confidence": film.confidence,
        "last_checked": film.last_checked,
        "offers": [_offer_to_dict(o) for o in film.offers],
        "rating": film.rating,
        "poster_url": film.poster_url,
        "director": film.director,
        "starring": film.starring,
        "synopsis": film.synopsis,
    }


def _film_from_dict(slug: str, data: dict) -> FilmState:
    return FilmState(
        slug=slug,
        title=data["title"],
        year=data["year"],
        entry_id=data["entry_id"],
        confidence=data["confidence"],
        last_checked=data["last_checked"],
        offers=[_offer_from_dict(o) for o in data.get("offers", [])],
        rating=data.get("rating"),
        poster_url=data.get("poster_url"),
        director=data.get("director", []),
        starring=data.get("starring", []),
        synopsis=data.get("synopsis"),
    )


def load_state(path: Path) -> StateDoc:
    if not path.exists():
        return StateDoc()

    data = json.loads(path.read_text())
    films = {slug: _film_from_dict(slug, film_data) for slug, film_data in data.get("films", {}).items()}
    return StateDoc(
        schema_version=data.get("schema_version", SCHEMA_VERSION),
        last_run_at=data.get("last_run_at"),
        films=films,
        recent_watches=data.get("recent_watches", []),
        recommendation_sections=data.get("recommendation_sections", []),
        discovery_films=data.get("discovery_films", {}),
        recent_additions=data.get("recent_additions", []),
    )


def save_state(path: Path, state: StateDoc) -> None:
    data = {
        "schema_version": state.schema_version,
        "last_run_at": state.last_run_at,
        "films": {slug: _film_to_dict(film) for slug, film in state.films.items()},
        "recent_watches": state.recent_watches,
        "recommendation_sections": state.recommendation_sections,
        "discovery_films": state.discovery_films,
        "recent_additions": state.recent_additions,
    }

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp_path, path)


def get_cached_entry_id(state: StateDoc, slug: str) -> tuple[str | None, str | None]:
    film = state.films.get(slug)
    if film is None or film.entry_id is None or film.confidence not in CACHEABLE_CONFIDENCE:
        return None, None
    return film.entry_id, film.confidence
