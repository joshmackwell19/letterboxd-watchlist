"""JustWatch has no introspection-friendly locale-list endpoint (introspection is
disabled on their GraphQL API), so this list was verified empirically (2026-07)
by passing it to offers_for_countries() and confirming a clean, error-free
response. Re-verify here if that call ever starts erroring on one of these.
"""

ALL_JUSTWATCH_COUNTRIES: frozenset[str] = frozenset({
    "AD", "AE", "AG", "AL", "AR", "AT", "AU", "AZ", "BA", "BB", "BE", "BG", "BH",
    "BM", "BO", "BR", "BS", "BY", "BZ", "CA", "CH", "CL", "CO", "CR", "CY", "CZ",
    "DE", "DK", "DO", "DZ", "EC", "EE", "EG", "ES", "FI", "FJ", "FR", "GB", "GF",
    "GG", "GI", "GR", "GT", "HK", "HN", "HR", "HU", "ID", "IE", "IL", "IM", "IN",
    "IQ", "IS", "IT", "JE", "JM", "JO", "JP", "KE", "KR", "KW", "LB", "LC", "LI",
    "LT", "LU", "LV", "LY", "MA", "MC", "MD", "ME", "MK", "MT", "MU", "MX", "MY",
    "MZ", "NI", "NL", "NO", "NZ", "OM", "PA", "PE", "PF", "PG", "PH", "PK", "PL",
    "PS", "PT", "PY", "QA", "RO", "RS", "RU", "SA", "SE", "SG", "SI", "SK", "SM",
    "SN", "SV", "TC", "TD", "TH", "TR", "TT", "TW", "TZ", "UA", "UG", "US", "UY",
    "VA", "VE", "VG", "YE", "ZA", "ZM", "ZW",
})

QUALIFYING_MONETIZATION_TYPES: frozenset[str] = frozenset({"FLATRATE", "ADS", "FREE"})


def validate_country_code(code: str) -> str:
    normalized = code.strip().upper()
    if normalized not in ALL_JUSTWATCH_COUNTRIES:
        raise ValueError(
            f"{code!r} is not a JustWatch country code. "
            f"Use ISO codes like 'gb', 'au', 'us' (not 'uk')."
        )
    return normalized
