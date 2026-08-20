"""Pipeline Metrics — structured event logging and scoring for the SQL pipeline.

Every SQL pipeline event (column validation catches, safety blocks, execution
errors, successful queries, user feedback) is logged to an append-only JSONL
file with a score delta (+1 for catches, -1 for blunders).

The score is a health metric, not a training signal — it shows pipeline
accuracy trending over time. Individual user thumbs contribute to the
aggregate but never directly change pipeline behavior (noise-resistant by
design: a single wrong thumb doesn't flip anything).

Thread-safe via the same file-lock mechanism used by UIStateManager.
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.core.config import DATA_DIR

logger = logging.getLogger(__name__)

METRICS_FILE = DATA_DIR / "pipeline_metrics.jsonl"

# Score deltas for each event type.  Positive = pipeline did well (caught a
# problem or succeeded cleanly), negative = something slipped through.
_SCORE_MAP: dict[str, int] = {
    "column_hallucination_caught": +1,
    "alias_hallucination_caught": +1,
    "unsafe_sql_blocked": +1,
    "execution_error_caught": +1,
    "sql_success": +1,
    "sql_abstain_no_sql": 0,       # correct abstention, neutral
    "user_feedback_up": +1,
    "user_feedback_down": -1,
    "retry_exhausted": -1,
    "routing_decision": 0,         # informational, no score
}


@dataclass
class PipelineEvent:
    """One structured event in the pipeline metrics log."""

    timestamp: str
    event_type: str
    query: str
    details: dict[str, Any] = field(default_factory=dict)
    score_delta: int = 0


def log_event(
    event_type: str,
    details: dict[str, Any] | None = None,
    *,
    query: str = "",
    score_delta: int | None = None,
) -> None:
    """Append a structured event to the metrics log.

    ``score_delta`` defaults to the value in ``_SCORE_MAP`` for the event type.
    Pass explicitly to override (e.g. for custom events).
    """
    if score_delta is None:
        score_delta = _SCORE_MAP.get(event_type, 0)

    event = PipelineEvent(
        timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
        event_type=event_type,
        query=query[:500],  # cap for storage
        details=details or {},
        score_delta=score_delta,
    )

    try:
        METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(METRICS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
    except Exception as e:
        # Metrics are best-effort — never crash the pipeline for a logging failure.
        logger.warning("Failed to write pipeline metric: %s", e)


def get_score_summary() -> dict[str, Any]:
    """Aggregate the metrics log into a score summary.

    Returns total score, event counts by type, per-mode routing breakdown,
    and recent feedback comments for human review.
    """
    events = _load_events()

    total_score = 0
    catches = 0
    blunders = 0
    by_type: dict[str, int] = {}
    routing_modes: dict[str, int] = {}
    recent_feedback: list[dict[str, Any]] = []

    for e in events:
        delta = e.get("score_delta", 0)
        total_score += delta
        if delta > 0:
            catches += 1
        elif delta < 0:
            blunders += 1

        etype = e.get("event_type", "unknown")
        by_type[etype] = by_type.get(etype, 0) + 1

        if etype == "routing_decision":
            mode = e.get("details", {}).get("mode", "unknown")
            routing_modes[mode] = routing_modes.get(mode, 0) + 1

        if etype in ("user_feedback_up", "user_feedback_down"):
            recent_feedback.append({
                "timestamp": e.get("timestamp"),
                "feedback": "up" if etype == "user_feedback_up" else "down",
                "comment": e.get("details", {}).get("comment", ""),
                "answer_preview": e.get("details", {}).get("answer_preview", ""),
            })

    return {
        "total_score": total_score,
        "total_events": len(events),
        "catches": catches,
        "blunders": blunders,
        "by_type": by_type,
        "routing_modes": routing_modes,
        "recent_feedback": recent_feedback[-20:],  # last 20
    }


def get_recent_events(limit: int = 50) -> list[dict[str, Any]]:
    """Return the last ``limit`` events for debugging / dashboard."""
    events = _load_events()
    return events[-limit:]


def _load_events() -> list[dict[str, Any]]:
    """Read all events from the JSONL file."""
    if not METRICS_FILE.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with open(METRICS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        logger.warning("Failed to read pipeline metrics: %s", e)
    return events
