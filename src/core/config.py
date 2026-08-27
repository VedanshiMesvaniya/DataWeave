"""Configuration management — loads .env and providers.yaml, exposes typed settings."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- Provider API keys ---
    gemini_api_key: str = ""
    nvidia_nim_api_key: str = ""
    groq_api_key: str = ""
    ocr_space_api_key: str = ""
    jina_api_key: str = ""
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    openrouter_api_key: str = ""

    # --- Provider selection ---
    # Default soft pin applied when the request doesn't specify one. The chosen
    # provider is preferred for every task, but the pipeline still falls back to
    # the rest of each task's chain when it's rate-limited or down. "auto"
    # disables the pin and uses the task-optimized routes as authored.
    default_provider: str = "openrouter"
    # OpenRouter is an aggregator with no fixed per-task model, so a pin needs an
    # explicit model. Free-tier IDs are fine for the MVP/demo; swap for paid
    # production models via env without touching code.
    #
    # NOTE (August 27, 2026): the previous defaults here
    # (meta-llama/llama-3.3-70b-instruct:free and the 3.2 vision variant) were
    # part of OpenRouter's entire free Meta Llama tier, which was delisted in
    # the week of July 27, 2026 — confirmed via OpenRouter's public model API.
    # Replaced with models confirmed live on OpenRouter's free catalog as of
    # August 3, 2026.
    openrouter_text_model: str = "openai/gpt-oss-20b:free"
    openrouter_vision_model: str = "nvidia/nemotron-nano-12b-v2-vl:free"

    # --- Live data / Text-to-SQL ---
    # "sqlite" (default, uses the local live_data.db file) or "mysql".
    db_engine: str = "sqlite"
    db_host: str = ""
    db_port: int = 3306
    db_name: str = ""
    db_readonly_user: str = ""
    db_readonly_password: str = ""

    # --- Runtime paths ---
    upload_dir: Path = Field(default_factory=lambda: DATA_DIR / "uploads")
    processed_dir: Path = Field(default_factory=lambda: DATA_DIR / "processed")
    # Drop-folder watched by the auto-ingestion service. Any file placed here is
    # ingested on the next scan; content-addressed dedup makes repeat scans a
    # no-op, so files can safely stay in the folder.
    auto_ingest_dir: Path = Field(default_factory=lambda: DATA_DIR / "inbox")

    # --- Auto-ingestion automation ---
    # Scan the drop-folder once on startup.
    auto_ingest_on_startup: bool = False
    # Re-scan the drop-folder every N seconds in the background (0 disables the
    # periodic loop; the manual /ingest/folder endpoint always works regardless).
    auto_ingest_interval_seconds: int = 0

    # --- Pipeline defaults ---
    ocr_confidence_threshold: float = 0.75
    chunk_target_tokens: int = 500
    chunk_overlap_fraction: float = 0.12
    retrieval_top_k: int = 50
    rerank_top_k: int = 25
    # How many of the top reranked chunks are actually fed into the generation
    # prompt. Reranking still scores the wider rerank_top_k set for accuracy,
    # but only the best few carry the answer — feeding all of them mostly buys
    # input tokens and latency, not quality. Exhaustive ("list every X")
    # queries bypass this cap to preserve recall. Floored at 2 so short-document
    # answers never starve.
    generation_context_k: int = 5
    # Rough token budget for the context block placed in the generation
    # prompt (see _build_context). Estimated at ~3 chars/token — deliberately
    # conservative since content varies (markdown tables and non-English text
    # tokenize denser than plain English prose), and this must hold across
    # several providers (Gemini, Groq, NVIDIA NIM, OpenRouter) with different
    # tokenizers and different real context-window sizes. Lower this if you're
    # hitting provider context-window errors; raise it only if you've
    # confirmed headroom on every provider in your routing chain.
    max_context_tokens: int = 6000
    # Hard timeout for a single live-DB query (src/core/db_client.py). 10s was
    # too tight for real aggregation queries (JOIN + GROUP BY over a view) on
    # non-trivial data volume — raised to 25s. If queries are still timing out,
    # the fix is indexing the join/filter columns on your DB, not raising this
    # further; a large flat timeout just makes every failure slower to surface.
    db_query_timeout_seconds: float = 25.0
    # Max number of query embeddings held in the process-scoped LRU cache.
    # Covers the same question asked repeatedly (e.g. repeated SQL queries
    # on analytical dashboards). Each entry is one dense vector (~4 KB for
    # 1024-d float32) plus a sparse vector — 256 entries ≈ 1 MB.
    embedding_cache_size: int = 256

    # How long a SQL retrieval result is cached (keyed on exact question text,
    # per-process — see SQLRetriever._result_cache). Avoids re-running an
    # expensive query for every user asking the same question, which matters
    # most when you can't add DB-side indexes yourself (e.g. a third-party
    # client DB with no DDL access granted). 0 disables caching entirely.
    sql_result_cache_ttl_seconds: float = 300.0
    # Near-duplicate suppression: boilerplate shared across documents (a company
    # motto, a repeated project preamble, a standard disclaimer) is stored once
    # per document, so a query matching it can retrieve the same passage many
    # times and crowd out unique content. After retrieval, a chunk whose text
    # overlaps a higher-ranked chunk by at least this Jaccard ratio is dropped,
    # keeping the best-scored representative. Exact repeats are always collapsed;
    # this threshold governs only the fuzzy case. Set to >1.0 to disable fuzzy
    # matching (exact-only); lower it toward ~0.8 to be more aggressive.
    dedup_near_duplicate_threshold: float = 0.9

    # --- Speech-to-text (voice input) ---
    # Local, free transcription via faster-whisper — no API key needed. The
    # model is downloaded from the public openai/whisper-large-v3-turbo repo on
    # Hugging Face the first time it's used, then cached on disk.
    whisper_model_size: str = "large-v3-turbo"
    # "cpu" or "cuda". Defaults to CPU: on small (<=4-6GB) consumer GPUs, giving
    # Whisper the GPU competes with everything else (embeddings, OCR, the rest
    # of the pipeline) for scarce VRAM, while CPU + int8 is fast enough for
    # interactive voice input. Override to "cuda" once you've benchmarked
    # headroom on your hardware.
    whisper_device: str = "cpu"
    # ctranslate2 compute type. "int8" is the right default for CPU (smallest
    # memory footprint, minimal accuracy loss); use "float16" if whisper_device
    # is "cuda".
    whisper_compute_type: str = "int8"
    # Max size of an uploaded audio clip, in megabytes. A generous cap for
    # voice *questions* (not long recordings) that also protects the server
    # from oversized uploads.
    max_audio_upload_mb: int = 25

    # --- CORS ---
    # Comma-separated allow-list of browser origins permitted to call the API
    # cross-origin. The bundled UI is same-origin (needs nothing here); the
    # defaults just cover the Vite dev server. A "*" wildcard is intentionally
    # NOT the default — see the CORS note in main.py.
    cors_allow_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    model_config = {"env_file": str(PROJECT_ROOT / ".env"), "env_file_encoding": "utf-8"}

    @field_validator("db_engine", mode="before")
    @classmethod
    def normalize_db_engine(cls, value: Any) -> Any:
        """Accept case/whitespace variants from .env without changing behavior."""
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("db_host", "db_name", "db_readonly_user", mode="before")
    @classmethod
    def strip_db_fields(cls, value: Any) -> Any:
        """Trim accidental whitespace from connection fields."""
        if isinstance(value, str):
            return value.strip()
        return value

    @property
    def cors_allow_origins_list(self) -> list[str]:
        """Parse the comma-separated CORS origins into a clean list."""
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    def ensure_dirs(self) -> None:
        """Create runtime directories if they don't exist."""
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.auto_ingest_dir.mkdir(parents=True, exist_ok=True)


def load_provider_config() -> dict[str, Any]:
    """Load the provider routing configuration from providers.yaml."""
    config_path = CONFIG_DIR / "providers.yaml"
    if not config_path.exists():
        logger.warning("providers.yaml not found at %s — using empty config", config_path)
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


# Module-level singleton — import this wherever config is needed.
settings = Settings()
