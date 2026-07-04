"""Email notifications via Resend.

Requires RESEND_API_KEY and NOTIFY_EMAIL in .env. Uses Resend's shared
onboarding@resend.dev sender, which works without domain verification as long
as NOTIFY_EMAIL matches the address that owns the Resend account.
"""

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


def send_email(subject: str, text_body: str, *, retries: int = 2) -> None:
    load_dotenv()
    api_key = os.environ["RESEND_API_KEY"]
    to_address = os.environ["NOTIFY_EMAIL"]

    payload = {
        "from": FROM_ADDRESS,
        "to": [to_address],
        "subject": subject,
        "text": text_body,
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    last_error = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(RESEND_API_URL, json=payload, headers=headers, timeout=15)
            if response.ok:
                return
            last_error = response.text
        except requests.RequestException as exc:
            last_error = str(exc)
        if attempt < retries:
            time.sleep(2)

    raise EmailError(f"Email send failed after {retries + 1} attempt(s): {last_error}")


def send_if_configured(subject: str, text_body: str) -> bool:
    if not is_configured():
        return False
    send_email(subject, text_body)
    return True
