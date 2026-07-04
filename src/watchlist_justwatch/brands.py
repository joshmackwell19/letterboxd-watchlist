import re

# Order matters: multi-word qualifiers before their shorter substrings
# (e.g. "Standard with Ads" before the generic "with Ads"), so we don't leave
# a dangling qualifier word behind. Never strips "Plus"/"+" — that's part of
# the brand name itself (Disney Plus, Paramount Plus, AMC+, MGM+).
_SUFFIXES = [
    r" Standard with Ads$", r" Basic with Ads$", r" Free with Ads$", r" with Ads$",
    r" Amazon Channel$", r" Apple TV Channel$", r" Roku Premium Channel$", r" Roku Channel$",
    r" Premium$", r" Essential$", r" Free$",
]

_ALIASES = {
    "amazon video": "Amazon Prime Video",
}


def canonical_brand_name(clear_name: str) -> str:
    """Collapse JustWatch package-name variants (ad tiers, channel bundles,
    price tiers) down to one real-world brand, e.g. "Paramount Plus Basic
    with Ads" / "Paramount+ Amazon Channel" both -> "Paramount Plus"."""
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
