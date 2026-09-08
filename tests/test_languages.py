from watchlist_justwatch.languages import is_subtitled, language_name


def test_is_subtitled_false_for_english():
    assert is_subtitled("en") is False


def test_is_subtitled_false_for_unknown():
    assert is_subtitled(None) is False
    assert is_subtitled("") is False


def test_is_subtitled_true_for_non_english():
    assert is_subtitled("ko") is True
    assert is_subtitled("fr") is True


def test_language_name_known_code():
    assert language_name("ko") == "Korean"


def test_language_name_falls_back_to_raw_code_when_unknown():
    assert language_name("zz") == "zz"


def test_language_name_none_for_no_code():
    assert language_name(None) is None
    assert language_name("") is None
