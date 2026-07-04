import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from .brands import canonical_brand_name
from .countries import validate_country_code
from .models import OfferRecord

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")

# Guards against short normalized names (e.g. a hypothetical "TV" entry)
# producing false-positive substring matches against unrelated services.
_MIN_SUBSTRING_MATCH_LENGTH = 4


@dataclass(frozen=True)
class CountryConfig:
    country: str
    subscriptions: list[str]
    free_tier: list[str] = field(default_factory=list)


def normalize_service_name(name: str) -> str:
    lowered = name.lower().replace("+", "plus")
    return _NON_ALNUM_RE.sub("", lowered)


def service_matches(config_name: str, justwatch_clear_name: str) -> bool:
    a = normalize_service_name(config_name)
    b = normalize_service_name(justwatch_clear_name)
    if len(a) < _MIN_SUBSTRING_MATCH_LENGTH or len(b) < _MIN_SUBSTRING_MATCH_LENGTH:
        return a == b
    return a in b or b in a


def load_config(path: Path) -> dict[str, CountryConfig]:
    raw = yaml.safe_load(path.read_text())
    countries = raw.get("countries", {})

    result: dict[str, CountryConfig] = {}
    for raw_country, entry in countries.items():
        country = validate_country_code(raw_country)
        subscriptions = entry.get("subscriptions", []) or []
        free_tier = entry.get("free_tier", []) or []

        for name in subscriptions:
            if any(service_matches(name, other) for other in free_tier):
                print(f"warning: {country} config lists {name!r} in both subscriptions and free_tier")

        result[country] = CountryConfig(country=country, subscriptions=subscriptions, free_tier=free_tier)

    return result


def classify_offer(
    offer: OfferRecord, country_config: CountryConfig
) -> Literal["have", "free_tier", "new_possible"]:
    if any(service_matches(name, offer.package_clear_name) for name in country_config.subscriptions):
        return "have"
    if any(service_matches(name, offer.package_clear_name) for name in country_config.free_tier):
        return "free_tier"
    return "new_possible"


def canonical_display_name(offer: OfferRecord, country_config: CountryConfig) -> str:
    """The config's own name for a have/free_tier match (so "Amazon Prime Video",
    "... with Ads", "... Free with Ads" all collapse to one "Amazon Prime" line),
    or the raw JustWatch package name for anything else."""
    for name in country_config.subscriptions:
        if service_matches(name, offer.package_clear_name):
            return name
    for name in country_config.free_tier:
        if service_matches(name, offer.package_clear_name):
            return name
    return offer.package_clear_name


def load_favorites(path: Path) -> set[tuple[str, str]]:
    """Your real Letterboxd favourite-services list (config/favorites.yaml),
    as (canonical brand, country) pairs."""
    raw = yaml.safe_load(path.read_text())
    return {
        (canonical_brand_name(entry["service"]), validate_country_code(entry["country"]))
        for entry in raw.get("favorites", [])
    }
