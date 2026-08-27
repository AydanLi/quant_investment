import pytest

from services import pushover


def test_send_pushover_requires_credentials_and_posts_message(monkeypatch):
    monkeypatch.delenv("PUSHOVER_APP_TOKEN", raising=False)
    monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)
    with pytest.raises(RuntimeError):
        pushover.send_pushover("test")

    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "a" * 30)
    monkeypatch.setenv("PUSHOVER_USER_KEY", "b" * 30)
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": 1, "request": "request-id"}

    def post(url, *, data, timeout):
        captured.update(url=url, data=data, timeout=timeout)
        return Response()

    monkeypatch.setattr(pushover.requests, "post", post)

    assert pushover.send_pushover("  data blocked  ") == "request-id"
    assert captured == {
        "url": pushover.PUSHOVER_MESSAGES_URL,
        "data": {
            "token": "a" * 30,
            "user": "b" * 30,
            "title": "quant_investment",
            "message": "data blocked",
        },
        "timeout": 10.0,
    }
