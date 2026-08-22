"""UI API Endpoints — connects the React frontend to the RAG backend."""

from __future__ import annotations

import datetime
import logging
import re
import uuid
import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.core.config import settings
from src.core.provider_client import ProviderRouter
from src.core.state import state_manager
from src.core.pipeline_metrics import get_score_summary, log_event as _log_pipeline_event
from src.models.schemas import ThinkingStep
from src.pipeline.query import QueryPipeline

logger = logging.getLogger(__name__)
router = APIRouter()


# Human-readable labels + display order for the provider picker. OpenRouter
# leads because it's the default soft pin.
_PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "gemini": "Gemini",
    "groq": "Groq",
    "nvidia_nim": "NVIDIA NIM",
}
_PROVIDER_ORDER = ["openrouter", "gemini", "groq", "nvidia_nim"]


_DOCUMENT_PROMPT = """You are a professional report writer. Convert the following conversation between a user and an AI assistant into a polished, standalone professional document in Markdown.

Requirements:
- Begin with a single H1 title (`# Title`) and a one-paragraph executive summary.
- Organize the content into logical sections with `##` headings; use `###` for sub-points.
- Write in clear, professional prose. Do NOT reproduce the chat turns verbatim and do NOT refer to "the user" or "the assistant".
- Preserve every fact, figure, and inline citation marker (like [1], [2]) exactly as they appear.
- Where the conversation contains quantitative data (comparisons, rankings, distributions, trends, totals), ADD a fitting chart as a fenced ```mermaid code block:
  - Bar/line data → `xychart-beta`
  - Proportions/shares → `pie`
  Only add a chart when the underlying numbers are actually present in the conversation. Never invent data. If nothing is chartable, add no charts.
- If the conversation already includes charts, keep and refine them.
- If a References/Sources list is present, keep it at the end.

Output ONLY the Markdown document — no preamble, no code fences around the whole thing.

Conversation:
---
{conversation}
---"""


_TITLE_PROMPT = """You write short, clear titles for chat conversations.

Given the first exchange below, reply with a concise title of 3 to 6 words that
captures the main topic. Use Title Case. Do NOT use quotes, a trailing period,
or the word "chat". If there is no real topic (e.g. only a greeting), reply with
exactly: New Chat

Conversation:
{conversation}

Title:"""


def _clean_title(raw: str) -> str:
    """Normalize an LLM title response into a clean, bounded title string."""
    text = (raw or "").strip()
    if not text:
        return ""
    # Take the first non-empty line only.
    text = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    # Drop a leading "Title:" / "Chat title -" the model may echo back.
    text = re.sub(r"^(chat\s+)?title\s*[:\-]\s*", "", text, flags=re.IGNORECASE)
    # Strip surrounding quotes and trailing sentence punctuation.
    text = text.strip().strip("\"'“”‘’").strip()
    text = text.rstrip(".!?,;:").strip()
    if len(text) > 60:
        text = text[:57].rstrip() + "..."
    return text


def _fallback_title(prompt: str) -> str:
    """Deterministic fallback when LLM titling is unavailable: trim the prompt."""
    trimmed = (prompt or "").strip()
    if len(trimmed) <= 48:
        return trimmed or "New Chat"
    return trimmed[:45].rstrip() + "..."


def _resolve_provider(requested: str | None) -> str | None:
    """Resolve the effective soft-pin provider for a request.

    Precedence: explicit request value → saved UI setting → app default.
    Returns None (no pin, "auto" routing) when the resolved value is "auto".
    """
    candidate = requested
    if candidate is None:
        candidate = state_manager.get_settings().get("provider")
    if candidate is None:
        candidate = settings.default_provider

    normalized = (candidate or "").strip().lower()
    return normalized if normalized and normalized != "auto" else None


class ChatCreate(BaseModel):
    title: str = "New Chat"

class ChatUpdate(BaseModel):
    title: str

class SendMessage(BaseModel):
    message: str
    # Optional soft-pin provider ("auto", "openrouter", "gemini", ...). When
    # omitted, the saved setting or app default is used.
    provider: str | None = None

class MessageFeedback(BaseModel):
    # "up", "down", or None to clear the rating.
    feedback: str | None = None
    # free-text feedback from the user
    comment: str | None = None

class IngestionCard(BaseModel):
    """A persisted ingestion-progress card (the step-by-step upload trace)."""
    id: str
    fileName: str
    status: str
    steps: list[dict[str, Any]]
    summary: dict[str, Any] | None = None
    content: str = ""
    createdAt: str


@router.get("/overview")
async def get_overview() -> dict[str, Any]:
    """Overview stats for the UI dashboard."""
    # Check if providers are available
    provider_router = ProviderRouter()
    has_llm = any(p.is_available for p in provider_router._providers.values())
    
    return {
        "backendStatus": "online",
        "ollamaStatus": "inactive",  # We are using cloud providers
        "vectorStatus": "ready",
        "modelLabel": "Auto-routed via ProviderRouter" if has_llm else "No Providers Configured",
        "contextTokens": 8192,
        "privacyLabel": "Zero-Cost Free Tier API",
    }


@router.get("/chats")
async def get_chats() -> list[dict[str, Any]]:
    """List all chats."""
    return state_manager.get_chats()


@router.post("/chats")
async def create_chat(chat_data: ChatCreate) -> dict[str, Any]:
    """Create a new chat."""
    chat = {
        "id": f"chat-{uuid.uuid4().hex[:8]}",
        "title": chat_data.title,
        "updatedAt": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    state_manager.create_chat(chat)
    return chat


@router.patch("/chats/{chat_id}")
async def update_chat(chat_id: str, chat_data: ChatUpdate) -> dict[str, Any]:
    """Rename a chat."""
    updated = state_manager.update_chat(chat_id, {"title": chat_data.title})
    if not updated:
        raise HTTPException(status_code=404, detail="Chat not found")
    return updated


@router.delete("/chats/{chat_id}")
async def delete_chat(chat_id: str) -> dict[str, str]:
    """Delete a chat."""
    state_manager.delete_chat(chat_id)
    from src.core.query_cache import get_shared_retrieval_cache
    get_shared_retrieval_cache().invalidate_chat(chat_id)
    return {"status": "success"}


@router.get("/chats/{chat_id}/messages")
async def get_messages(chat_id: str) -> list[dict[str, Any]]:
    """Get all messages for a chat."""
    return state_manager.get_messages(chat_id)


@router.post("/chats/{chat_id}/messages")
async def send_message(chat_id: str, msg: SendMessage) -> dict[str, Any]:
    """Send a message to a chat, process it via RAG, and return the response."""
    # Capture prior turns for conversational context BEFORE adding this message.
    history = state_manager.get_messages(chat_id)

    # Save the user's message
    user_message = {
        "id": f"msg-u-{uuid.uuid4().hex[:8]}",
        "role": "user",
        "content": msg.message,
        "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
        "chatId": chat_id,
    }
    state_manager.add_message(chat_id, user_message)

    try:
        # Fresh pipeline per request — avoids accumulated RateLimiter backoff
        # bleeding across unrelated queries and biasing provider selection.
        pipeline = QueryPipeline(preferred_provider=_resolve_provider(msg.provider))
        result = await pipeline.query(msg.message, history=history, chat_id=chat_id)


        # Save the assistant's message
        assistant_message = {
            "id": f"msg-a-{uuid.uuid4().hex[:8]}",
            "role": "assistant",
            "content": result.answer,
            "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
            "chatId": chat_id,
            "citations": [c.model_dump() for c in result.citations],
            "modelUsed": result.model_used,
            "usage": result.usage.model_dump(),
        }
        state_manager.add_message(chat_id, assistant_message)

        # Update chat modified time
        state_manager.update_chat(chat_id, {"updatedAt": datetime.datetime.now(datetime.UTC).isoformat()})
        
        return assistant_message

    except Exception as e:
        logger.exception("Failed to process message")
        error_message = {
            "id": f"msg-e-{uuid.uuid4().hex[:8]}",
            "role": "assistant",
            "content": "Sorry, I wasn't able to process your request. Our AI providers may be temporarily unavailable — please try again in a moment.",
            "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
            "chatId": chat_id,
        }
        state_manager.add_message(chat_id, error_message)
        return error_message


@router.post("/chats/{chat_id}/messages/stream")
async def send_message_stream(chat_id: str, msg: SendMessage):
    """Send a message to a chat and stream the RAG response via SSE."""
    # Capture prior turns for conversational context BEFORE adding this message.
    history = state_manager.get_messages(chat_id)

    # Save the user's message
    user_message = {
        "id": f"msg-u-{uuid.uuid4().hex[:8]}",
        "role": "user",
        "content": msg.message,
        "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
        "chatId": chat_id,
    }
    state_manager.add_message(chat_id, user_message)

    pipeline = QueryPipeline(preferred_provider=_resolve_provider(msg.provider))

    async def event_generator():
        try:
            async for chunk in pipeline.query_stream(msg.message, history=history, chat_id=chat_id):
                if isinstance(chunk, ThinkingStep):
                    # A reasoning step — stream it live for the "thinking" block.
                    yield f"data: {json.dumps({'type': 'thinking', 'step': chunk.model_dump()})}\n\n"
                elif isinstance(chunk, str):
                    yield f"data: {json.dumps({'type': 'chunk', 'text': chunk})}\n\n"
                else:
                    # Final QueryResult
                    assistant_message = {
                        "id": f"msg-a-{uuid.uuid4().hex[:8]}",
                        "role": "assistant",
                        "content": chunk.answer,
                        "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
                        "chatId": chat_id,
                        "citations": [c.model_dump() for c in chunk.citations],
                        "modelUsed": chunk.model_used,
                        "thinking": [t.model_dump() for t in chunk.thinking],
                        "usage": chunk.usage.model_dump(),
                    }
                    state_manager.add_message(chat_id, assistant_message)
                    state_manager.update_chat(chat_id, {"updatedAt": datetime.datetime.now(datetime.UTC).isoformat()})
                    
                    yield f"data: {json.dumps({'type': 'done', 'message': assistant_message})}\n\n"
        except Exception as e:
            logger.exception("Failed to process stream message")
            error_message = {
                "id": f"msg-e-{uuid.uuid4().hex[:8]}",
                "role": "assistant",
                "content": "Sorry, I wasn't able to process your request. Our AI providers may be temporarily unavailable.",
                "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
                "chatId": chat_id,
            }
            state_manager.add_message(chat_id, error_message)
            yield f"data: {json.dumps({'type': 'error', 'message': error_message})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/chats/{chat_id}/messages/ingestion")
async def persist_ingestion_card(chat_id: str, card: IngestionCard) -> dict[str, Any]:
    """Persist a finished ingestion-progress card so it survives a reload.

    The card streams live during upload (via /upload/stream); once ingestion
    finishes the frontend calls this to keep the step-by-step trace in the chat
    permanently, like a normal message.
    """
    message = {
        "id": card.id,
        "role": "assistant",
        "kind": "ingestion",
        "fileName": card.fileName,
        "status": card.status,
        "steps": card.steps,
        "summary": card.summary,
        "content": card.content,
        "createdAt": card.createdAt,
        "chatId": chat_id,
    }
    state_manager.add_message(chat_id, message)
    state_manager.update_chat(chat_id, {"updatedAt": datetime.datetime.now(datetime.UTC).isoformat()})
    return {"status": "ok"}


@router.post("/chats/{chat_id}/messages/{message_id}/feedback")
async def set_message_feedback(
    chat_id: str, message_id: str, body: MessageFeedback
) -> dict[str, Any]:
    """Persist a thumbs up/down rating and/or comment on an assistant message."""
    if body.feedback not in (None, "up", "down"):
        raise HTTPException(status_code=400, detail="feedback must be 'up', 'down', or null")

    updated = state_manager.set_message_feedback(chat_id, message_id, body.feedback, body.comment)
    if not updated:
        raise HTTPException(status_code=404, detail="Message not found")

    if body.feedback or body.comment:
        # Find the message to log its content
        messages = state_manager.get_messages(chat_id)
        msg = next((m for m in messages if m.get("id") == message_id), {})
        
        event_type = f"user_feedback_{body.feedback}" if body.feedback else "user_feedback_comment"
        score_delta = 0
        if body.feedback == "up":
            score_delta = +1
        elif body.feedback == "down":
            score_delta = -1

        _log_pipeline_event(
            event_type,
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "comment": body.comment,
                "answer_preview": (msg.get("content") or "")[:200],
                "model_used": msg.get("modelUsed"),
            },
            score_delta=score_delta,
        )

    return {"status": "ok", "feedback": body.feedback}


@router.post("/chats/{chat_id}/document")
async def generate_chat_document(chat_id: str) -> dict[str, Any]:
    """Restructure a chat into a professional Markdown document (with charts).

    An LLM turns the conversation into a titled, sectioned report and adds
    Mermaid charts where the data supports them — even if the chat itself never
    rendered one. The frontend renders the returned Markdown to a formatted PDF.
    """
    messages = state_manager.get_messages(chat_id)
    turns: list[str] = []
    for m in messages:
        if m.get("kind") == "ingestion" or m.get("status") == "loading":
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        role = "User" if m.get("role") == "user" else "Assistant"
        turns.append(f"{role}: {content}")

    if not turns:
        raise HTTPException(status_code=400, detail="This chat has no content to build a document from.")

    conversation = "\n\n".join(turns)[:12000]

    try:
        provider_router = ProviderRouter()
        markdown = await provider_router.chat(
            "general_qa",
            messages=[{"role": "user", "content": _DOCUMENT_PROMPT.format(conversation=conversation)}],
            temperature=0.4,
            max_tokens=4096,
        )
    except Exception:
        logger.exception("Document generation failed for chat %s", chat_id)
        raise HTTPException(status_code=502, detail="Could not generate the document. Please try again.")

    markdown = (markdown or "").strip()
    # Strip a stray outer ```markdown fence if the model wrapped the whole doc.
    markdown = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", markdown).strip()
    if not markdown:
        raise HTTPException(status_code=502, detail="The generated document was empty. Please try again.")

    # Derive a title from the first H1, else fall back to the chat title.
    title_match = re.search(r"^#\s+(.+)$", markdown, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Document"

    return {"markdown": markdown, "title": title}


@router.post("/chats/{chat_id}/title")
async def generate_chat_title(chat_id: str) -> dict[str, Any]:
    """Generate a concise, topic-aware title from a chat's first exchange.

    Uses a fast, cheap model (fast_support route) so it never adds meaningful
    latency. Persists the result and returns it. Falls back to a trimmed first
    message if the model is unavailable or returns nothing usable.
    """
    messages = state_manager.get_messages(chat_id)
    first_user = next((m for m in messages if m.get("role") == "user"), None)
    if not first_user or not (first_user.get("content") or "").strip():
        return {"title": None}

    first_assistant = next((m for m in messages if m.get("role") == "assistant"), None)

    conversation = f"User: {first_user['content'][:600]}"
    if first_assistant and (first_assistant.get("content") or "").strip():
        conversation += f"\nAssistant: {first_assistant['content'][:600]}"

    title = ""
    try:
        provider_router = ProviderRouter()
        raw = await provider_router.chat(
            "fast_support",
            messages=[{"role": "user", "content": _TITLE_PROMPT.format(conversation=conversation)}],
            temperature=0.3,
            max_tokens=20,
        )
        title = _clean_title(raw)
    except Exception:
        logger.warning("Title generation LLM call failed — falling back to trimmed prompt")

    if not title or title.lower() == "new chat":
        title = _fallback_title(first_user["content"])

    state_manager.update_chat(chat_id, {"title": title})
    return {"title": title}


def _doc_view(entry: dict[str, Any], version_count: int = 1) -> dict[str, Any]:
    """Shape a registry entry into the document view the frontend expects."""
    return {
        "id": entry.get("document_id", ""),
        "name": entry.get("filename", "Unknown"),
        "sizeBytes": entry.get("file_size_bytes", 0),
        "chunks": entry.get("total_chunks", 0),
        "ingestedAt": entry.get("created_at", ""),
        "lineageRoot": entry.get("lineage_root", entry.get("document_id", "")),
        "supersedes": entry.get("supersedes"),
        "versionCount": version_count,
    }


@router.get("/documents")
async def get_documents() -> list[dict[str, Any]]:
    """List the ingested documents (active versions only).

    Reads from the ingestion registry (ingested_files.json) — the single source
    of truth the ingestion pipeline populates. Only the current (active) version
    of each lineage is listed; superseded versions are hidden here but remain
    queryable via ``/documents/{id}/versions``.
    """
    from src.core.ingestion_registry import IngestionRegistry

    registry = IngestionRegistry()
    all_entries = registry.get_all().values()

    # Count versions per lineage so the UI can show "v3" affordances.
    version_counts: dict[str, int] = {}
    for e in all_entries:
        root = e.get("lineage_root", e.get("document_id", ""))
        version_counts[root] = version_counts.get(root, 0) + 1

    documents = [
        _doc_view(e, version_counts.get(e.get("lineage_root", e.get("document_id", "")), 1))
        for e in all_entries
        if e.get("active", True)
    ]
    documents.sort(key=lambda d: d.get("ingestedAt", ""), reverse=True)
    return documents


@router.get("/documents/{document_id}/versions")
async def get_document_versions(document_id: str) -> list[dict[str, Any]]:
    """Return the full version history of a document's lineage, oldest first."""
    from src.core.ingestion_registry import IngestionRegistry

    registry = IngestionRegistry()
    entry = registry.get_by_document_id(document_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Document not found")

    root = entry.get("lineage_root", document_id)
    versions = registry.get_versions(root)
    return [
        {**_doc_view(v, len(versions)), "active": v.get("active", True)}
        for v in versions
    ]


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str) -> dict[str, Any]:
    """Delete a document version from both the vector store and the registry."""
    from src.core.ingestion_registry import IngestionRegistry
    from src.stages.s11_vector_store import QdrantStore

    registry = IngestionRegistry()
    if registry.get_by_document_id(document_id) is None:
        raise HTTPException(status_code=404, detail="Document not found")

    try:
        await QdrantStore().delete_document(document_id)
    except Exception:
        logger.exception("Failed to delete vectors for document %s", document_id)
        raise HTTPException(status_code=500, detail="Failed to delete document vectors")

    registry.unregister(document_id)
    from src.core.query_cache import get_shared_retrieval_cache
    get_shared_retrieval_cache().invalidate_all()
    return {"status": "deleted", "document_id": document_id}


@router.get("/providers")
async def get_providers() -> dict[str, Any]:
    """List selectable model providers for the settings picker.

    Only providers with a configured API key are offered, plus an always-present
    "Auto" option. OpenRouter leads the list and is the default soft pin; if it
    isn't configured, the effective default degrades to "auto".
    """
    provider_router = ProviderRouter()
    available = {
        name for name, provider in provider_router._providers.items() if provider.is_available
    }
    ordered = [n for n in _PROVIDER_ORDER if n in available] + [
        n for n in sorted(available) if n not in _PROVIDER_ORDER
    ]

    options = [{"id": "auto", "label": "Auto (recommended)"}]
    options += [{"id": n, "label": _PROVIDER_LABELS.get(n, n)} for n in ordered]

    default = (settings.default_provider or "auto").strip().lower()
    if default != "auto" and default not in available:
        default = "auto"

    return {"providers": options, "default": default}


@router.get("/providers/usage")
async def get_provider_usage() -> dict[str, Any]:
    """Live per-provider quota usage for the settings usage meter.

    Reads the process-wide RateLimiter (shared across all requests), so the
    RPM/RPD figures reflect real, cumulative traffic rather than a single
    request. Only providers with a configured API key are reported; each entry
    carries used/limit for both the per-minute and per-day windows plus any
    remaining 429 backoff.
    """
    from src.core.rate_limiter import get_shared_rate_limiter

    provider_router = ProviderRouter()
    available = [
        name for name, provider in provider_router._providers.items() if provider.is_available
    ]
    snapshot = get_shared_rate_limiter().usage_snapshot(available)

    ordered = [n for n in _PROVIDER_ORDER if n in available] + [
        n for n in sorted(available) if n not in _PROVIDER_ORDER
    ]
    providers = []
    for name in ordered:
        s = snapshot.get(name, {})
        providers.append(
            {
                "id": name,
                "label": _PROVIDER_LABELS.get(name, name),
                "rpmUsed": s.get("rpm_used", 0),
                "rpmLimit": s.get("rpm_limit", 0),
                "rpdUsed": s.get("rpd_used", 0),
                "rpdLimit": s.get("rpd_limit", 0),
                "backoffSeconds": s.get("backoff_seconds", 0),
            }
        )
    return {"providers": providers}


@router.get("/pipeline/metrics")
async def get_pipeline_metrics() -> dict[str, Any]:
    """Aggregated pipeline performance: total score, catches, blunders, breakdown."""
    return get_score_summary()


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    """Get UI settings."""
    return state_manager.get_settings()


@router.post("/settings")
async def save_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Save UI settings."""
    return state_manager.save_settings(settings)


@router.post("/settings/sync-schema")
async def sync_schema() -> dict[str, Any]:
    """Sync the live database schema into the vector store for Schema RAG.

    Fetches all tables from the configured database, embeds each table's
    CREATE TABLE statement as a separate chunk, and upserts them into
    Qdrant.  Old schema chunks are deleted first to avoid stale data.
    """
    from src.pipeline.schema_ingestion import sync_live_schema

    try:
        result = await sync_live_schema()
        return result
    except Exception as e:
        logger.error("Schema sync failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Schema sync failed: {e}")
