"""The layer-3 pre-flight must classify the provider, not just ping it.

Deterministic: no network. ``urlopen`` is replaced, so what is tested is the
*classification* — which is the part that was wrong. The old probe called
``GET /models``, which a key with no balance answers 200; the tests then ran and
failed with four assertions about the model's output, none of which named the
cause.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from tests.e2e.provider_probe import probe_provider


class _Ok:
    status = 200

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code: int, message: str = "") -> urllib.error.HTTPError:
    body = json.dumps({"error": {"message": message}}).encode() if message else b"{}"
    return urllib.error.HTTPError(
        url="https://x/v1/chat/completions", code=code, msg="err",
        hdrs=None, fp=io.BytesIO(body),
    )


def test_a_serving_provider_is_reported_usable(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Ok())
    ok, reason = probe_provider("https://api.example.com/v1", "some-model", "k")
    assert ok is True
    assert reason == ""


def test_an_unconfigured_provider_says_what_to_set(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not touch the network when unconfigured")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    ok, reason = probe_provider("", "", None)
    assert ok is False
    assert "TCAD_E2E_BASE_URL" in reason


@pytest.mark.parametrize("code, expected", [
    (401, "rejected"),
    (402, "cannot pay"),
    (404, "no model named"),
    (429, "rate limited"),
    (500, "refused the request"),
])
def test_a_provider_that_cannot_serve_is_blocked_with_its_reason(monkeypatch, code, expected):
    """The whole point. Each of these used to look like "reachable"."""
    def _raise(*a, **k):
        raise _http_error(code, "Insufficient Balance")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    ok, reason = probe_provider("https://api.example.com/v1", "m", "k")
    assert ok is False
    assert expected in reason
    assert "BLOCKED" in reason, reason
    assert f"HTTP {code}" in reason
    assert "Insufficient Balance" in reason, "the provider's own words must survive"


def test_an_unreachable_provider_is_an_environment_condition(monkeypatch):
    def _raise(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    ok, reason = probe_provider("http://127.0.0.1:11434/v1", "m", None)
    assert ok is False
    assert "BLOCKED" in reason
    assert "OSError" in reason


def test_the_probe_asks_for_a_completion_not_a_model_list(monkeypatch):
    """Guard against a regression to the cheap-but-wrong check."""
    seen = {}

    def _capture(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        return _Ok()

    monkeypatch.setattr("urllib.request.urlopen", _capture)
    probe_provider("https://api.example.com/v1", "m", "k")
    assert seen["url"].endswith("/chat/completions"), seen["url"]
    assert seen["body"]["model"] == "m"
    assert seen["body"]["max_tokens"] == 1
