import re
from collections import defaultdict

# Order matters: multi-word qualifiers before their shorter substrings
# (e.g. "Standard with Ads" before the generic "with Ads"), so we don't leave
# a dangling qualifier word behind. Never strips "Plus"/"+" — that's part of
# the brand name itself (Disney Plus, Paramount Plus, AMC+, MGM+).
_SUFFIXES = [
    r" Standard with Ads$", r" Basic with Ads$", r" Free with Ads$", r" with Ads$",
    r" Amazon Channel$", r" Apple TV Channel$", r" Roku Premium Channel$", r" Roku Channel$",
    r" Premium Plus$", r" Premium$", r" Essential$", r" Free$",
]

_ALIASES = {
    "amazon video": "Amazon Prime Video",
}

# Real brand names that happen to end in a string our generic suffix
# stripping would otherwise mangle (e.g. "The Roku Channel" is Roku's own
# free app, not a channel bundle add-on for some other service — don't
# strip " Roku Channel" off it).
_PROTECTED = {"the roku channel"}

# JustWatch's own placeholder/aggregator entry, not a real consumer service —
# excluded everywhere, not just demoted to "other services".
JUNK_BRANDS = {"justwatch tv"}

# Real but not "major" in the sense of being a recognizable consumer brand —
# mostly public-domain/classic-film aggregators with outsized country counts
# from broad licensing, not genuine popularity. Still shown in "other
# services" / the full service table, just excluded from main columns.
NOT_MAJOR_BRANDS = {"artiflix", "filmbox plus", "cultpix", "filmzie", "public domain movies", "flixolé"}


def is_junk_brand(name: str) -> bool:
    return name.lower() in JUNK_BRANDS


def is_major_brand(name: str) -> bool:
    return name.lower() not in NOT_MAJOR_BRANDS


def canonical_brand_name(clear_name: str) -> str:
    """Collapse JustWatch package-name variants (ad tiers, channel bundles,
    price tiers) down to one real-world brand, e.g. "Paramount Plus Basic
    with Ads" / "Paramount+ Amazon Channel" both -> "Paramount Plus"."""
    if clear_name.lower() in _PROTECTED:
        return clear_name

    name = clear_name
    changed = True
    while changed:
        changed = False
        for pattern in _SUFFIXES:
            new_name = re.sub(pattern, "", name, flags=re.IGNORECASE).strip()
            if new_name and new_name != name:
                name = new_name
                changed = True

    if name.endswith("+"):
        name = name[:-1].strip() + " Plus"

    return _ALIASES.get(name.lower(), name)


def group_offers_by_brand(offers) -> dict[str, set[str]]:
    """Canonical brand -> set of countries, with junk brands dropped."""
    by_brand: dict[str, set[str]] = defaultdict(set)
    for offer in offers:
        brand = canonical_brand_name(offer.package_clear_name)
        if is_junk_brand(brand):
            continue
        by_brand[brand].add(offer.country)
    return by_brand


def group_offers_by_brand_and_country(offers) -> dict[str, dict[str, set[str]]]:
    """Canonical brand -> country -> set of monetization types seen there,
    with junk brands dropped. Lets callers tell "needs a subscription"
    (FLATRATE present) apart from "free to watch" (only ADS/FREE)."""
    by_brand: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for offer in offers:
        brand = canonical_brand_name(offer.package_clear_name)
        if is_junk_brand(brand):
            continue
        by_brand[brand][offer.country].add(offer.monetization_type)
    return by_brand
