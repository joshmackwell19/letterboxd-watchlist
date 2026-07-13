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
    """Per-country subscriptions, with "global" (VPN-portable, e.g. Amazon
    Prime Video/MUBI/Disney Plus) merged into every country — so consumers
    scoped to just GB/AU/US still classify those correctly. For matching a
    global subscription against a country the app doesn't otherwise track
    (any of the other ~120 JustWatch countries), see load_global_subscriptions
    and is_have_anywhere."""
    raw = yaml.safe_load(path.read_text())
    global_subscriptions = raw.get("global", {}).get("subscriptions", []) or []
    countries = raw.get("countries", {})

    result: dict[str, CountryConfig] = {}
    for raw_country, entry in countries.items():
        country = validate_country_code(raw_country)
        subscriptions = list(entry.get("subscriptions", []) or []) + list(global_subscriptions)
        free_tier = entry.get("free_tier", []) or []

        for name in subscriptions:
            if any(service_matches(name, other) for other in free_tier):
                print(f"warning: {country} config lists {name!r} in both subscriptions and free_tier")

        result[country] = CountryConfig(country=country, subscriptions=subscriptions, free_tier=free_tier)

    return result


def load_global_subscriptions(path: Path) -> list[str]:
    """Raw "global" subscription names from services.yaml (VPN-portable
    services that count as "have" regardless of country)."""
    raw = yaml.safe_load(path.read_text())
    return list(raw.get("global", {}).get("subscriptions", []) or [])


def is_have_anywhere(
    package_clear_name: str, country: str, config: dict[str, CountryConfig], global_subscriptions: list[str]
) -> bool:
    """The PRIMARY "have" check, valid across all ~124 JustWatch countries
    (not just the 3 this app tracks per-country config for): true if the
    offer's service is one of your VPN-portable global subscriptions, or one
    of the single-region services you have in that specific country."""
    if any(service_matches(name, package_clear_name) for name in global_subscriptions):
        return True
    country_config = config.get(country)
    if country_config is None:
        return False
    return any(service_matches(name, package_clear_name)
                for name in country_config.subscriptions + country_config.free_tier)


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


def load_revisitable_services(path: Path) -> set[str]:
    """Services you've had before and could plausibly get again (friends,
    family, resubscribing) — config/revisitable_services.yaml."""
    raw = yaml.safe_load(path.read_text())
    return {canonical_brand_name(name) for name in raw.get("services", [])}


def load_dismissed_recommendations(path: Path) -> set[str]:
    """Slugs marked "not interested" from a home-page recommendation card —
    config/dismissed_recommendations.yaml, editable from Settings via the
    same Worker-write pattern as services.yaml."""
    raw = yaml.safe_load(path.read_text())
    return set(raw.get("dismissed", []) or [])
