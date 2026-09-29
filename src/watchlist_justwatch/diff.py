from dataclasses import dataclass, field

from .config import CountryConfig, classify_offer
from .models import FilmState, OfferRecord
from .state import StateDoc


@dataclass
class ReportEntry:
    film: FilmState
    offer: OfferRecord


@dataclass
class Report:
    new_have: list[ReportEntry] = field(default_factory=list)
    new_free_tier: list[ReportEntry] = field(default_factory=list)
    new_possible: list[ReportEntry] = field(default_factory=list)
    unmatched: list[FilmState] = field(default_factory=list)
    new_films: list[FilmState] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.new_have or self.new_free_tier or self.new_possible
                    or self.unmatched or self.new_films)


def diff_film_offers(previous: FilmState | None, current: FilmState) -> list[OfferRecord]:
    previous_keys = {o.diff_key() for o in previous.offers} if previous is not None else set()
    return [o for o in current.offers if o.diff_key() not in previous_keys]


def build_report(previous_state: StateDoc, current_state: StateDoc, config: dict[str, CountryConfig]) -> Report:
    report = Report()
    # On a true first run (no prior baseline at all), every film looks "new" —
    # that's already what the full have/free_tier/new_possible audit is for,
    # so skip new_films there rather than duplicating all 300+ films again.
    is_first_run = not previous_state.films

    for slug, current_film in current_state.films.items():
        previous_film = previous_state.films.get(slug)
        is_new_addition = previous_film is None and not is_first_run

        if is_new_addition:
            report.new_films.append(current_film)
            # Its dedicated section above already gives the full breakdown —
            # don't also repeat it piecemeal in the classified sections below.
            if current_film.confidence in ("unmatched", "low_confidence"):
                report.unmatched.append(current_film)
            continue

        new_offers = diff_film_offers(previous_film, current_film)
        for offer in new_offers:
            country_config = config.get(offer.country)
            if country_config is None:
                continue  # stored for future-proofing, not one of the configured countries

            classification = classify_offer(offer, country_config)
            entry = ReportEntry(film=current_film, offer=offer)
            if classification == "have":
                report.new_have.append(entry)
            elif classification == "free_tier":
                report.new_free_tier.append(entry)
            else:
                report.new_possible.append(entry)

        if current_film.confidence in ("unmatched", "low_confidence"):
            report.unmatched.append(current_film)

    return report


def newly_streaming(
    previous_snapshot: dict[str, dict[tuple[str, str], str]],
    current_snapshot: dict[str, dict[tuple[str, str], str]],
    countries: set[str],
) -> dict[str, dict[tuple[str, str], str]]:
    """slug -> today's {(brand, country): classification}, for each film that
    streamed nowhere in the tracked countries yesterday and does today — the
    only thing the daily email is sent for. Snapshots are
    dashboard.compute_offer_snapshot's, so "streaming" means any
    subscription/free offer on a real brand, whether or not it's a service
    you have. A film with no previous snapshot is a new watchlist addition,
    not a change in availability, so it never counts."""
    result = {}
    for slug, offers in current_snapshot.items():
        previous = previous_snapshot.get(slug)
        if previous is None or any(country in countries for _brand, country in previous):
            continue
        tracked = {key: cls for key, cls in offers.items() if key[1] in countries}
        if tracked:
            result[slug] = tracked
    return result
