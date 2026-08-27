from __future__ import annotations

import os

import requests


PUSHOVER_MESSAGES_URL = "https://api.pushover.net/1/messages.json"


def send_pushover(
    message: str,
    title: str = "quant_investment",
    *,
    timeout_seconds: float = 10.0,
) -> str:
    """Send one Pushover alert and return its request identifier."""
    message = message.strip()
    if not message:
        raise ValueError("Pushover message cannot be empty.")

    token = os.getenv("PUSHOVER_APP_TOKEN")
    user = os.getenv("PUSHOVER_USER_KEY")
    if not token or not user:
        raise RuntimeError(
            "PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY must both be configured."
        )

    response = requests.post(
        PUSHOVER_MESSAGES_URL,
        data={"token": token, "user": user, "title": title, "message": message},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    request_id = payload.get("request")
    if payload.get("status") != 1 or not isinstance(request_id, str):
        raise RuntimeError("Pushover did not confirm message acceptance.")
    return request_id
