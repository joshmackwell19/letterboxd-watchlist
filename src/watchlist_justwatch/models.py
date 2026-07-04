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
