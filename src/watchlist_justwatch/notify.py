"""Notifications: Web Push to the home-screen dashboard (see below), and
email via Resend.

Email requires RESEND_API_KEY and NOTIFY_EMAIL in .env. Uses Resend's shared
onboarding@resend.dev sender, which works without domain verification as long
as NOTIFY_EMAIL matches the address that owns the Resend account.
"""

import json
import os
import time

import requests
from dotenv import load_dotenv

RESEND_API_URL = "https://api.resend.com/emails"
FROM_ADDRESS = "Letterboxd Watchlist <onboarding@resend.dev>"


class EmailError(Exception):
    pass


def is_configured() -> bool:
    load_dotenv()
    return bool(os.getenv("RESEND_API_KEY") and os.getenv("NOTIFY_EMAIL"))


def send_email(subject: str, text_body: str, *, html_body: str | None = None, retries: int = 2,
               attachments: list[dict] | None = None) -> None:
    load_dotenv()
    api_key = os.environ["RESEND_API_KEY"]
    to_address = os.environ["NOTIFY_EMAIL"]

    payload = {
        "from": FROM_ADDRESS,
        "to": [to_address],
        "subject": subject,
        "text": text_body,
    }
    if html_body is not None:
        payload["html"] = html_body
    if attachments:
        payload["attachments"] = attachments
    headers = {"Authorization": f"Bearer {api_key}"}

    last_error = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(RESEND_API_URL, json=payload, headers=headers, timeout=30)
            if response.ok:
                return
            last_error = response.text
        except requests.RequestException as exc:
            last_error = str(exc)
        if attempt < retries:
            time.sleep(2)

    raise EmailError(f"Email send failed after {retries + 1} attempt(s): {last_error}")


def send_if_configured(subject: str, text_body: str, *, html_body: str | None = None) -> bool:
    if not is_configured():
        return False
    send_email(subject, text_body, html_body=html_body)
    return True


# ---------------------------------------------------------------- Web Push
#
# Notifications to the dashboard saved to a phone's home screen (iOS 16.4+
# delivers Web Push only to a home-screen web app, not to a Safari tab). The
# page subscribes with the public key below — public by design, it ships in
# the page — and the Worker stores the subscription in Postgres; the private
# half is VAPID_PRIVATE_KEY, in .env and GitHub secrets, and signs each send.
VAPID_PUBLIC_KEY = "BI_zLeNdZvuR-EN51VoVCx-H_SwKQVZsfi-ofwNGzBP1npMBZ9_3Z8Ct6yPnZizKXSHehl_QAUY3rrqN-4y0KY0"
# Apple rejects a send whose VAPID "sub" claim isn't a mailto: or https: URL,
# and py_vapid only takes an https one that's a bare origin — no path.
VAPID_SUBJECT = "https://joshmackwell19.github.io"
# The push services a subscription may point at. The Worker checks the same
# list before storing one; checked again here so a row that got in some
# other way can't make the daily run POST somewhere arbitrary.
PUSH_SERVICE_HOSTS = ("web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com",
                      ".notify.windows.com")


def push_is_configured() -> bool:
    load_dotenv()
    return bool(os.getenv("VAPID_PRIVATE_KEY"))


def _is_push_service(endpoint: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and any(
        host.endswith(allowed) if allowed.startswith(".") else host == allowed for allowed in PUSH_SERVICE_HOSTS
    )


def send_push(subscriptions: list[dict], notifications: list[dict]) -> tuple[int, list[str], list[str]]:
    """Sends each notification ({title, body, url, tag}) to each subscription.
    Returns (how many sends succeeded, endpoints the push service says no
    longer exist — the caller deletes those, other failures)."""
    from pywebpush import WebPushException, webpush

    load_dotenv()
    private_key = os.environ["VAPID_PRIVATE_KEY"]
    sent, gone, errors = 0, [], []
    for subscription in subscriptions:
        if not _is_push_service(subscription["endpoint"]):
            errors.append(f"not a known push service: {subscription['endpoint'][:60]}")
            continue
        for notification in notifications:
            try:
                webpush(subscription, json.dumps(notification), vapid_private_key=private_key,
                        vapid_claims={"sub": VAPID_SUBJECT}, ttl=24 * 60 * 60, timeout=30)
                sent += 1
            except WebPushException as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (404, 410):
                    gone.append(subscription["endpoint"])
                    break
                errors.append(f"{status}: {exc}")
    return sent, gone, errors
