"""Unit tests for the Gemini provider request construction.

These lock in the fix for answer truncation: Gemini 2.5 thinking models draw
reasoning tokens from the ``maxOutputTokens`` budget, so thinking must be
disabled for text generation or the visible answer gets cut off mid-sentence.
"""

import pytest

from src.core.provider_client import GeminiProvider
from src.core.rate_limiter import RateLimiter


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _CapturingHTTP:
    """Fake httpx client that records the last request body and streamed body."""

    def __init__(self, payload):
        self._payload = payload
        self.last_body = None

    async def post(self, url, json=None, headers=None):
        self.last_body = json
        return _FakeResponse(self._payload)

    def stream(self, method, url, json=None, headers=None):
        self.last_body = json
        payload = self._payload

        class _StreamCtx:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            def raise_for_status(self_inner):
                return None

            async def aiter_lines(self_inner):
                import json as _json
                yield "data: " + _json.dumps(payload)

        return _StreamCtx()


def _make_provider(payload):
    provider = GeminiProvider(RateLimiter())
    provider._api_key = "fake-key"
    provider._http = _CapturingHTTP(payload)
    return provider


_ANSWER_PAYLOAD = {
    "candidates": [{"content": {"parts": [{"text": "A complete answer."}]}}]
}


@pytest.mark.asyncio
async def test_chat_disables_thinking():
    provider = _make_provider(_ANSWER_PAYLOAD)

    text = await provider.chat(
        [{"role": "user", "content": "hi"}],
        model="gemini-2.5-flash",
        max_tokens=64,
    )

    assert text == "A complete answer."
    cfg = provider._http.last_body["generationConfig"]
    # Thinking must be off so the whole budget goes to the visible answer.
    assert cfg["thinkingConfig"]["thinkingBudget"] == 0
    # A short caller budget is floored to a usable minimum.
    assert cfg["maxOutputTokens"] >= 1024


@pytest.mark.asyncio
async def test_chat_stream_disables_thinking():
    provider = _make_provider(_ANSWER_PAYLOAD)

    parts = []
    async for chunk in provider.chat_stream(
        [{"role": "user", "content": "hi"}],
        model="gemini-2.5-flash",
        max_tokens=64,
    ):
        parts.append(chunk)

    assert "".join(parts) == "A complete answer."
    cfg = provider._http.last_body["generationConfig"]
    assert cfg["thinkingConfig"]["thinkingBudget"] == 0
