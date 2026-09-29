from watchlist_justwatch import main
from watchlist_justwatch.html_email import DASHBOARD_URL, newly_streaming_notifications
from watchlist_justwatch.models import FilmState
from watchlist_justwatch.notify import _is_push_service


def _film(slug: str, title: str = "Close-Up", year: int = 1990) -> FilmState:
    return FilmState(slug=slug, title=title, year=year, entry_id="e", confidence="exact",
                     last_checked="2026-09-29T00:00:00Z", offers=[])


def test_one_notification_per_film_opening_its_page():
    films = [(_film("close-up"), {("MUBI", "US"): "subscription", ("Netflix", "GB"): "have"})]
    [notification] = newly_streaming_notifications(films)
    assert notification["title"] == "Now streaming: Close-Up (1990)"
    # The service you have leads, and says so.
    assert notification["body"] == "On Netflix (yours), MUBI"
    assert notification["url"] == f"{DASHBOARD_URL}?film=close-up"


def test_a_big_day_is_one_summary():
    films = [(_film(f"f{i}", title=f"Film {i}"), {("MUBI", "US"): "subscription"}) for i in range(7)]
    [notification] = newly_streaming_notifications(films)
    assert notification["title"] == "7 watchlist films now streaming"
    assert notification["body"].endswith("and 2 more")
    assert notification["url"] == DASHBOARD_URL


def test_only_known_push_services_are_sent_to():
    assert _is_push_service("https://web.push.apple.com/abc")
    assert _is_push_service("https://wns2-db5p.notify.windows.com/w/?token=x")
    assert not _is_push_service("http://web.push.apple.com/abc")
    assert not _is_push_service("https://web.push.apple.com.evil.test/abc")
    assert not _is_push_service("https://evil.test/abc")


def _stub(monkeypatch, *, subscriptions, sent, errors=()):
    emails, deleted = [], []
    monkeypatch.setattr(main, "push_is_configured", lambda: True)
    monkeypatch.setattr(main, "load_push_subscriptions", lambda _url: subscriptions)
    monkeypatch.setattr(main, "send_push", lambda subs, notes: (sent, ["gone"], list(errors)))
    monkeypatch.setattr(main, "delete_push_subscriptions", lambda _url, endpoints: deleted.extend(endpoints))
    monkeypatch.setattr(main, "send_if_configured", lambda subject, *a, **k: emails.append(subject))
    return emails, deleted


FILMS = [(_film("close-up"), {("MUBI", "US"): "subscription"})]


def test_push_replaces_the_email(monkeypatch):
    emails, deleted = _stub(monkeypatch, subscriptions=[{"endpoint": "x"}], sent=1)
    main._notify_newly_streaming("db", FILMS, [], lambda msg: None)
    assert emails == []
    assert deleted == ["gone"]


def test_email_until_a_device_subscribes(monkeypatch):
    emails, _ = _stub(monkeypatch, subscriptions=[], sent=0)
    main._notify_newly_streaming("db", FILMS, [], lambda msg: None)
    assert emails == ["Now streaming: Close-Up (1990)"]


def test_email_when_every_push_failed(monkeypatch):
    emails, _ = _stub(monkeypatch, subscriptions=[{"endpoint": "x"}], sent=0, errors=["500: boom"])
    warnings = []
    main._notify_newly_streaming("db", FILMS, [], warnings.append)
    assert emails == ["Now streaming: Close-Up (1990)"]
    assert warnings == ["push notification failed (500: boom)"]


def test_vapid_subject_is_one_py_vapid_signs_with():
    from py_vapid import Vapid, _check_sub
    from watchlist_justwatch.notify import VAPID_SUBJECT

    assert _check_sub(VAPID_SUBJECT)
    Vapid.from_raw(b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAE").sign(
        {"sub": VAPID_SUBJECT, "aud": "https://web.push.apple.com"})
