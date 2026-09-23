from dataclasses import dataclass, field


@dataclass(frozen=True)
class WatchlistFilm:
    slug: str
    title: str
    year: int | None


@dataclass(frozen=True)
class MatchResult:
    slug: str
    entry_id: str | None
    matched_title: str | None
    matched_year: int | None
    confidence: str  # "exact" | "year_tolerant" | "low_confidence" | "unmatched"


@dataclass(frozen=True)
class OfferRecord:
    country: str
    monetization_type: str
    package_technical_name: str
    package_clear_name: str
    package_id: int
    url: str
    # Date (YYYY-MM-DD) this specific offer expires, when JustWatch knows
    # one — usually only set for rentals/licensed windows, not open-ended
    # subscription flatrate offers, so this is often None even for
    # something you have. Powers the "leaving soon" home section.
    available_to: str | None = None

    def diff_key(self) -> tuple[str, str, str]:
        return (self.country, self.package_technical_name, self.monetization_type)


@dataclass
class FilmState:
    slug: str
    title: str
    year: int | None
    entry_id: str | None
    confidence: str
    last_checked: str
    offers: list[OfferRecord] = field(default_factory=list)
    rating: float | None = None
    poster_url: str | None = None
    director: list[str] = field(default_factory=list)
    starring: list[str] = field(default_factory=list)
    synopsis: str | None = None
    genre: list[str] = field(default_factory=list)
    # ISO 639-1 code (TMDB's "original_language", not Letterboxd's own
    # inLanguage list — see tmdb_client.search_movie for why).
    original_language: str | None = None
    runtime_minutes: int | None = None
    # TMDB's id for this film. Falls out of the same search call
    # original_language already makes, and is what lets the dashboard ask
    # the Worker for TMDB's own view of a film (its real "similar", the
    # director's whole filmography) instead of only what's already tracked.
    tmdb_id: int | None = None
