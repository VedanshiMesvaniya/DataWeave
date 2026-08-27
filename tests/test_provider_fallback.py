"""Tests for provider fallback + rate-limit cooldown wiring.

These lock in the fix for wasted free-tier requests: after a provider returns
429 the router must mark it rate-limited so subsequent calls in the same request
skip it, instead of re-firing a doomed request at the head of the chain every
time.
"""

from __future__ import annotations

import time

import pytest

from src.core.provider_client import (
    ProviderOption,
    ProviderRouter,
    TaskRoute,
    _rate_limit_retry_after,
)
from src.core.rate_limiter import ProviderLimits, RateLimiter, get_shared_rate_limiter


class _Err429(Exception):
    """Mimics an SDK/HTTP 429 error (exposes ``status_code``)."""

    status_code = 429

    class _Resp:
        headers = {"retry-after": "7"}

    response = _Resp()


class _Err404(Exception):
    """Mimics a permanent 'dead model' error (404 / EOL slug)."""

    status_code = 404

    class _Resp:
        headers: dict = {}

    response = _Resp()


class _FakeProvider:
    """Minimal LLMProvider that goes through the shared RateLimiter like the real ones.

    ``raise_429``  — always fail transiently (429).
    ``permanent``  — always fail permanently (404 / dead slug).
    ``fail_times`` — fail transiently the first N calls, then succeed (models a
                     provider that recovers after its short backoff).
    """

    def __init__(
        self, name: str, rate_limiter: RateLimiter, *,
        raise_429: bool = False, permanent: bool = False, fail_times: int = 0, text: str = "ok",
    ) -> None:
        self._name = name
        self._rl = rate_limiter
        self._raise_429 = raise_429
        self._permanent = permanent
        self._fail_times = fail_times
        self._text = text
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_available(self) -> bool:
        return True

    async def chat(self, messages, *, model, temperature=0.0, max_tokens=4096, response_format=None, usage=None):
        # Real providers acquire a slot first — that's where an active cooldown
        # gets enforced (acquire raises), so the fake must do the same.
        await self._rl.acquire(self._name)
        self.calls += 1
        if self._permanent:
            raise _Err404()
        if self._raise_429:
            raise _Err429()
        if self.calls <= self._fail_times:
            raise _Err429()
        return self._text


def _make_router(rate_limiter: RateLimiter, providers: dict) -> ProviderRouter:
    router = ProviderRouter(preferred_provider="auto")  # no soft pin
    router._rate_limiter = rate_limiter
    router._providers = providers
    router._routes = {
        "general_qa": TaskRoute([ProviderOption("a", "m-a", 1), ProviderOption("b", "m-b", 2)])
    }
    return router


# ---------------------------------------------------------------------------
# _rate_limit_retry_after
# ---------------------------------------------------------------------------

def test_retry_after_detects_429_and_honours_header():
    assert _rate_limit_retry_after(_Err429()) == 7.0


def test_retry_after_ignores_non_429():
    assert _rate_limit_retry_after(RuntimeError("boom")) is None
    assert _rate_limit_retry_after(TimeoutError()) is None


def test_retry_after_caps_absurd_header():
    class _Big(Exception):
        status_code = 429

        class _Resp:
            headers = {"retry-after": "99999"}

        response = _Resp()

    assert _rate_limit_retry_after(_Big()) == 60.0  # capped


# ---------------------------------------------------------------------------
# Router cooldown behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_429_provider_is_skipped_on_next_call():
    limits = {"a": ProviderLimits(rpm=100, rpd=1000), "b": ProviderLimits(rpm=100, rpd=1000)}
    rl = RateLimiter(limits=limits)
    a = _FakeProvider("a", rl, raise_429=True)
    b = _FakeProvider("b", rl, text="from-b")
    router = _make_router(rl, {"a": a, "b": b})

    # Call 1: a 429s → router records cooldown → falls back to b.
    result1 = await router.chat("general_qa", messages=[{"role": "user", "content": "hi"}])
    assert result1 == "from-b"
    assert a.calls == 1 and b.calls == 1
    assert rl._get_state("a").backoff_until > time.time()

    # Call 2: a is in cooldown, so acquire() rejects it before any network call —
    # a must NOT be hit again; b serves directly.
    result2 = await router.chat("general_qa", messages=[{"role": "user", "content": "hi"}])
    assert result2 == "from-b"
    assert a.calls == 1  # unchanged — the doomed provider was not re-fired
    assert b.calls == 2


@pytest.fixture
def _instant_sleep(monkeypatch):
    """Fake clock so backoff-retry tests don't actually wait.

    The router's ``asyncio.sleep`` and the RateLimiter's ``time.time`` share one
    virtual clock: sleeping advances it, so a provider's 429 backoff genuinely
    expires on the retry pass (as it would in real time) without the test
    burning real seconds. Returns the list of wait durations requested."""
    import src.core.provider_client as pc
    import src.core.rate_limiter as rl_mod

    clock = {"t": 10_000.0}
    waited: list[float] = []

    async def _fake_sleep(seconds):
        waited.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(rl_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(pc.asyncio, "sleep", _fake_sleep)
    return waited


@pytest.mark.asyncio
async def test_all_transient_failures_retry_then_raise(_instant_sleep):
    """When every provider is only throttled (429), the router waits out the
    shortest backoff and retries the whole chain before finally giving up."""
    limits = {"a": ProviderLimits(rpm=100, rpd=1000), "b": ProviderLimits(rpm=100, rpd=1000)}
    rl = RateLimiter(limits=limits)
    a = _FakeProvider("a", rl, raise_429=True)
    b = _FakeProvider("b", rl, raise_429=True)
    router = _make_router(rl, {"a": a, "b": b})

    with pytest.raises(RuntimeError, match="All providers exhausted"):
        await router.chat("general_qa", messages=[{"role": "user", "content": "hi"}])

    # Both providers were retried (two passes), and a wait happened between them.
    assert a.calls == 2 and b.calls == 2
    assert len(_instant_sleep) == 1


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried(_instant_sleep):
    """A dead model (404) is dropped for the rest of the call — never re-fired —
    and if every provider is permanently dead there's no pointless wait."""
    rl = RateLimiter(limits={"a": ProviderLimits(rpm=100, rpd=1000),
                             "b": ProviderLimits(rpm=100, rpd=1000)})
    a = _FakeProvider("a", rl, permanent=True)
    b = _FakeProvider("b", rl, permanent=True)
    router = _make_router(rl, {"a": a, "b": b})

    with pytest.raises(RuntimeError, match="All providers exhausted"):
        await router.chat("general_qa", messages=[{"role": "user", "content": "hi"}])

    assert a.calls == 1 and b.calls == 1     # not retried
    assert len(_instant_sleep) == 0          # no wait for hopeless providers


@pytest.mark.asyncio
async def test_transient_provider_recovers_on_retry(_instant_sleep):
    """A provider that 429s once but would succeed on retry is given that retry,
    so a transient throttle doesn't hard-fail the request."""
    rl = RateLimiter(limits={"a": ProviderLimits(rpm=100, rpd=1000),
                             "b": ProviderLimits(rpm=100, rpd=1000)})
    a = _FakeProvider("a", rl, permanent=True)          # dead — dropped
    b = _FakeProvider("b", rl, fail_times=1, text="from-b")  # 429 once, then OK

    router = _make_router(rl, {"a": a, "b": b})
    result = await router.chat("general_qa", messages=[{"role": "user", "content": "hi"}])

    assert result == "from-b"
    assert a.calls == 1        # dead provider tried once, then dropped
    assert b.calls == 2        # failed once, retried, succeeded
    assert len(_instant_sleep) == 1


def test_is_transient_failure_classification():
    from src.core.provider_client import _is_transient_failure
    assert _is_transient_failure(_Err429()) is True
    assert _is_transient_failure(_Err404()) is False
    assert _is_transient_failure(RuntimeError("model has reached its end of life")) is False
    assert _is_transient_failure(RuntimeError("Provider 'x' daily rate limit exhausted")) is False
    assert _is_transient_failure(RuntimeError("Provider 'x' is currently rate-limited (backoff).")) is True


def test_default_routes_include_openrouter_for_sql_generation():
    router = ProviderRouter(preferred_provider="auto")
    reasoning = [(opt.provider_name, opt.model) for opt in router._get_route("reasoning").options]
    classification = [
        (opt.provider_name, opt.model)
        for opt in router._get_route("semantic_classification").options
    ]

    assert ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free") in reasoning
    assert ("openrouter", "openai/gpt-oss-20b:free") in classification


# ---------------------------------------------------------------------------
# Process-scoped shared limiter — quota/backoff must span requests
# ---------------------------------------------------------------------------

def test_shared_rate_limiter_is_a_singleton():
    assert get_shared_rate_limiter() is get_shared_rate_limiter()


def test_routers_share_one_rate_limiter_across_requests():
    """Two routers (two 'requests') must consult the same limiter, so a 429
    backoff recorded by one is seen by the next — the crux of the ARCH-1 fix."""
    r1 = ProviderRouter(preferred_provider="auto")
    r2 = ProviderRouter(preferred_provider="auto")
    assert r1._rate_limiter is r2._rate_limiter is get_shared_rate_limiter()

    # A cooldown recorded via one router's limiter is visible to the other.
    r1._rate_limiter.report_429("gemini", retry_after=30.0)
    assert r2._rate_limiter._get_state("gemini").backoff_until > time.time()
    # Clean up shared state so the cooldown doesn't leak into other tests.
    r1._rate_limiter._get_state("gemini").backoff_until = 0.0


def test_usage_snapshot_includes_unused_providers_with_zeros():
    rl = RateLimiter(limits={"gemini": ProviderLimits(rpm=10, rpd=1500)})
    snap = rl.usage_snapshot(["gemini", "groq"])
    assert snap["gemini"] == {
        "rpm_used": 0,
        "rpm_limit": 10,
        "rpd_used": 0,
        "rpd_limit": 1500,
        "backoff_seconds": 0.0,
    }
    # A provider with no explicit limits still appears (default limits).
    assert "groq" in snap
