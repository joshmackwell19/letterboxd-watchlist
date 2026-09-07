"""ISO 639-1 codes for the languages this watchlist actually sees (TMDB's
own `original_language` field, sourced via tmdb_client.original_language) —
extend as new ones turn up rather than trying to enumerate all ~180 up
front, same approach as countries.py's COUNTRY_NAMES."""

LANGUAGE_NAMES: dict[str, str] = {
    "en": "English", "fr": "French", "es": "Spanish", "de": "German", "it": "Italian",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "cn": "Chinese", "ru": "Russian",
    "pt": "Portuguese", "hi": "Hindi", "ar": "Arabic", "sv": "Swedish", "da": "Danish",
    "nl": "Dutch", "no": "Norwegian", "fi": "Finnish", "pl": "Polish", "tr": "Turkish",
    "th": "Thai", "he": "Hebrew", "el": "Greek", "cs": "Czech", "hu": "Hungarian",
    "ro": "Romanian", "uk": "Ukrainian", "id": "Indonesian", "vi": "Vietnamese",
    "fa": "Persian", "ta": "Tamil", "te": "Telugu", "ml": "Malayalam", "bn": "Bengali",
    "is": "Icelandic", "ca": "Catalan", "eu": "Basque", "sr": "Serbian", "hr": "Croatian",
    "bg": "Bulgarian", "sk": "Slovak", "lt": "Lithuanian", "lv": "Latvian", "et": "Estonian",
    "ka": "Georgian", "mn": "Mongolian", "ur": "Urdu", "tl": "Tagalog", "la": "Latin",
    "yi": "Yiddish", "xx": "No spoken dialogue",
}


def language_name(code: str | None) -> str | None:
    if not code:
        return None
    return LANGUAGE_NAMES.get(code, code)


def is_subtitled(code: str | None) -> bool:
    """Whether an English-speaking viewer would need subtitles — based on
    TMDB's `original_language` (the film's primary production language), not
    Letterboxd's own `inLanguage` list, which just enumerates every language
    heard at all (e.g. The Godfather's is ['la', 'en', 'it']) and so can't
    tell a predominantly-English film with a few foreign lines apart from an
    actually-foreign film."""
    return bool(code) and code != "en"
