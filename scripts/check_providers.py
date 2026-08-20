"""Provider / model health check.

Pings EVERY (provider, model) pair referenced by the routing config with a tiny
request and reports its real status, so a dead or renamed model slug is caught
here — at your desk, deliberately — instead of silently at runtime as a 404/410
that collapses a task's whole fallback chain.

Needs the same API keys the app uses (read from the environment / .env). Makes
one small call per model, so it consumes a little quota.

Usage:
    python scripts/check_providers.py
    python scripts/check_providers.py --task reasoning     # only models for one task
    python scripts/check_providers.py --json out.json      # machine-readable output

Status meanings:
    ALIVE     - the model answered; safe to route to
    DEAD      - 404/410/400 model-not-found or end-of-life; REMOVE/replace the slug
    THROTTLED - 429 rate limit; the model exists and works, just rate-limited now
    AUTH      - 401/403; API key missing or lacks access to this model
    ERROR     - anything else (network, timeout, unexpected)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _classify(exc: Exception) -> tuple[str, str]:
    """Map an exception to (status, short_detail)."""
    status_code = getattr(exc, "status_code", None)
    # openai-style errors expose .status_code; httpx via .response.status_code
    if status_code is None:
        resp = getattr(exc, "response", None)
        status_code = getattr(resp, "status_code", None)
    msg = str(exc)
    low = msg.lower()

    if status_code in (404, 410) or "not found" in low or "end of life" in low or "gone" in low:
        return "DEAD", msg
    if status_code == 429 or "rate limit" in low or "too many requests" in low or "backoff" in low:
        return "THROTTLED", msg
    if status_code in (401, 403) or "unauthorized" in low or "forbidden" in low or "api key" in low:
        return "AUTH", msg
    if status_code == 400 or "does not exist" in low or "invalid model" in low:
        return "DEAD", msg
    return "ERROR", msg


async def _ping(provider, model: str) -> tuple[str, str, float]:
    """Return (status, detail, latency_seconds) for one model."""
    from src.models.schemas import TokenUsage
    t0 = time.monotonic()
    try:
        text = await provider.chat(
            [{"role": "user", "content": "Reply with the single word: OK"}],
            model=model,
            temperature=0.0,
            max_tokens=5,
            usage=TokenUsage(),
        )
        dt = time.monotonic() - t0
        return "ALIVE", (text or "").strip()[:40], dt
    except Exception as e:  # noqa: BLE001 — we classify everything
        dt = time.monotonic() - t0
        status, detail = _classify(e)
        return status, detail[:200], dt


def _collect_targets(task_filter: str | None) -> dict[str, set[str]]:
    """provider_name -> set(models) referenced by the routing config."""
    from src.core.provider_client import get_shared_routes

    routes = get_shared_routes()
    targets: dict[str, set[str]] = defaultdict(set)
    for task_name, route in routes.items():
        if task_filter and task_name != task_filter:
            continue
        for opt in route.options:
            targets[opt.provider_name].add(opt.model)
    return targets


async def main_async(task_filter: str | None, json_out: Path | None) -> int:
    # A FRESH limiter so each model gets a real call — the shared limiter would
    # let one model's 429 backoff reject the next model on the same provider.
    from src.core.rate_limiter import RateLimiter
    from src.core.provider_client import _build_providers, get_shared_routes

    providers = _build_providers(RateLimiter())
    targets = _collect_targets(task_filter)

    if not providers:
        print("No providers configured — set GROQ_API_KEY / GEMINI_API_KEY / "
              "NVIDIA_NIM_API_KEY / OPENROUTER_API_KEY in your environment.")
        return 1

    print(f"Checking {sum(len(m) for m in targets.values())} model(s) across "
          f"{len(targets)} provider(s)...\n")

    results: list[dict] = []
    icon = {"ALIVE": "✓", "THROTTLED": "~", "DEAD": "✗", "AUTH": "🔑", "ERROR": "!"}

    for provider_name in sorted(targets):
        provider = providers.get(provider_name)
        for model in sorted(targets[provider_name]):
            if provider is None:
                status, detail, dt = "AUTH", "no API key configured for this provider", 0.0
            else:
                status, detail, dt = await _ping(provider, model)
            results.append({"provider": provider_name, "model": model,
                            "status": status, "detail": detail, "latency_s": round(dt, 2)})
            print(f"  {icon.get(status, '?')} {status:10} {provider_name:11} {model:42} "
                  f"{dt:5.2f}s  {detail if status != 'ALIVE' else ''}")

    # Per-task: does any provider work?
    routes = get_shared_routes()
    status_by = {(r["provider"], r["model"]): r["status"] for r in results}
    print("\nTask readiness (does each task have at least one ALIVE model?):")
    broken_tasks = []
    for task_name, route in sorted(routes.items()):
        if task_filter and task_name != task_filter:
            continue
        statuses = [status_by.get((o.provider_name, o.model), "?") for o in route.options]
        alive = statuses.count("ALIVE")
        throttled = statuses.count("THROTTLED")
        ok = alive > 0 or throttled > 0  # throttled still recovers
        flag = "OK " if ok else "DOWN"
        if not ok:
            broken_tasks.append(task_name)
        print(f"  [{flag}] {task_name:24} alive={alive} throttled={throttled} "
              f"total={len(statuses)}")

    counts = defaultdict(int)
    for r in results:
        counts[r["status"]] += 1
    print(f"\nSummary: {dict(counts)}")
    if broken_tasks:
        print(f"\n⚠ Tasks with NO working model (fix these slugs): {', '.join(broken_tasks)}")

    if json_out:
        json_out.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nWrote machine-readable results -> {json_out}")

    # Non-zero exit if any task is fully down, so this can gate CI/health.
    return 2 if broken_tasks else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Ping every configured provider/model")
    ap.add_argument("--task", default=None, help="only check models for this task")
    ap.add_argument("--json", type=Path, default=None, help="write results as JSON")
    args = ap.parse_args()
    return asyncio.run(main_async(args.task, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
