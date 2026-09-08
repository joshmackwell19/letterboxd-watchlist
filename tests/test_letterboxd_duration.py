from watchlist_justwatch.letterboxd import _parse_duration_minutes


def test_parse_duration_hours_and_minutes():
    assert _parse_duration_minutes("PT2H13M") == 133


def test_parse_duration_minutes_only():
    assert _parse_duration_minutes("PT45M") == 45


def test_parse_duration_hours_only():
    assert _parse_duration_minutes("PT1H") == 60


def test_parse_duration_none():
    assert _parse_duration_minutes(None) is None


def test_parse_duration_empty_string():
    assert _parse_duration_minutes("") is None


def test_parse_duration_malformed():
    assert _parse_duration_minutes("not a duration") is None


def test_parse_duration_zero_is_none():
    # PT0M shouldn't happen for a real film, but "no runtime" should never
    # come back as 0 rather than None — 0 is falsy in a way that could slip
    # past a `if minutes:` check elsewhere and read as "unknown", not "an
    # instant film".
    assert _parse_duration_minutes("PT0M") is None
