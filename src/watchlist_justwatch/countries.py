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

COUNTRY_NAMES: dict[str, str] = {
    "AD": "Andorra", "AE": "United Arab Emirates", "AG": "Antigua and Barbuda", "AL": "Albania",
    "AR": "Argentina", "AT": "Austria", "AU": "Australia", "AZ": "Azerbaijan",
    "BA": "Bosnia and Herzegovina", "BB": "Barbados", "BE": "Belgium", "BG": "Bulgaria",
    "BH": "Bahrain", "BM": "Bermuda", "BO": "Bolivia", "BR": "Brazil", "BS": "Bahamas",
    "BY": "Belarus", "BZ": "Belize", "CA": "Canada", "CH": "Switzerland", "CL": "Chile",
    "CO": "Colombia", "CR": "Costa Rica", "CY": "Cyprus", "CZ": "Czech Republic",
    "DE": "Germany", "DK": "Denmark", "DO": "Dominican Republic", "DZ": "Algeria",
    "EC": "Ecuador", "EE": "Estonia", "EG": "Egypt", "ES": "Spain", "FI": "Finland",
    "FJ": "Fiji", "FR": "France", "GB": "United Kingdom", "GF": "French Guiana",
    "GG": "Guernsey", "GI": "Gibraltar", "GR": "Greece", "GT": "Guatemala", "HK": "Hong Kong",
    "HN": "Honduras", "HR": "Croatia", "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland",
    "IL": "Israel", "IM": "Isle of Man", "IN": "India", "IQ": "Iraq", "IS": "Iceland",
    "IT": "Italy", "JE": "Jersey", "JM": "Jamaica", "JO": "Jordan", "JP": "Japan",
    "KE": "Kenya", "KR": "South Korea", "KW": "Kuwait", "LB": "Lebanon", "LC": "Saint Lucia",
    "LI": "Liechtenstein", "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia", "LY": "Libya",
    "MA": "Morocco", "MC": "Monaco", "MD": "Moldova", "ME": "Montenegro",
    "MK": "North Macedonia", "MT": "Malta", "MU": "Mauritius", "MX": "Mexico",
    "MY": "Malaysia", "MZ": "Mozambique", "NI": "Nicaragua", "NL": "Netherlands",
    "NO": "Norway", "NZ": "New Zealand", "OM": "Oman", "PA": "Panama", "PE": "Peru",
    "PF": "French Polynesia", "PG": "Papua New Guinea", "PH": "Philippines", "PK": "Pakistan",
    "PL": "Poland", "PS": "Palestine", "PT": "Portugal", "PY": "Paraguay", "QA": "Qatar",
    "RO": "Romania", "RS": "Serbia", "RU": "Russia", "SA": "Saudi Arabia", "SE": "Sweden",
    "SG": "Singapore", "SI": "Slovenia", "SK": "Slovakia", "SM": "San Marino",
    "SN": "Senegal", "SV": "El Salvador", "TC": "Turks and Caicos Islands", "TD": "Chad",
    "TH": "Thailand", "TR": "Turkey", "TT": "Trinidad and Tobago", "TW": "Taiwan",
    "TZ": "Tanzania", "UA": "Ukraine", "UG": "Uganda", "US": "United States", "UY": "Uruguay",
    "VA": "Vatican City", "VE": "Venezuela", "VG": "British Virgin Islands", "YE": "Yemen",
    "ZA": "South Africa", "ZM": "Zambia", "ZW": "Zimbabwe",
}


def country_name(code: str) -> str:
    return COUNTRY_NAMES.get(code, code)


def validate_country_code(code: str) -> str:
    normalized = code.strip().upper()
    if normalized not in ALL_JUSTWATCH_COUNTRIES:
        raise ValueError(
            f"{code!r} is not a JustWatch country code. "
            f"Use ISO codes like 'gb', 'au', 'us' (not 'uk')."
        )
    return normalized
