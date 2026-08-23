# DataWeave Deep Architecture & Context Map

This document is the **definitive, granular guide** to the DataWeave project. Paired with `CLAUDE.md` (which dictates coding standards), this file explains *how everything works, from the smallest utility functions to the macro system architecture*. 

If you are an AI reading this, you now hold the complete blueprint of the system in your mind.

---

## 1. Core Architecture Philosophy
DataWeave is an enterprise-grade Retrieval-Augmented Generation (RAG) system built with a single, aggressive constraint: **It must operate entirely on forever-free-tier APIs while rivaling paid systems in accuracy.**

To achieve this, the architecture rejects single-provider dependency. Instead, it uses a **dynamic LLM routing pattern**. The backend acts as a smart orchestrator that categorizes workloads and distributes them across Google (Gemini), Groq, Nvidia NIM, OpenRouter, and Jina AI based on rate limits, availability, and task complexity.

Data persistence completely avoids SQL databases for application state. UI state is managed via flat JSON files (`/data`) with cross-platform advisory file locking, and vectors are handled by Qdrant (cloud or local). A separate Text-to-SQL stage optionally queries a live structured database (SQLite or MySQL) for data-driven answers.

---

## 2. Directory Map & The Engine Room (`src/core/`)

### Top-level repository layout

```text
DataWeave/
├── src/            # Python backend (FastAPI app + 14-stage RAG pipeline)
├── DataWeave_UI/   # React frontend source (Vite)
├── frontend/       # Compiled UI, served directly by FastAPI (build output)
├── config/         # providers.yaml — LLM routing rules & fallback chains
├── tests/          # pytest suite (unit + integration)
├── scripts/        # Dev/ops scripts (setup_db.py, manual smoke checks)
├── docs/           # Documentation (this file, DEPLOY.md, design-history/)
├── data/           # Runtime state — chats, uploads, SQLite DB (gitignored)
├── render.yaml     # Render deploy blueprint
├── pyproject.toml  # Dependencies, entry points, tooling config
└── CLAUDE.md       # Coding standards & conventions
```

The repository deliberately keeps runtime state (`data/`) out of version control:
document metadata is durable in Qdrant (see the document-identity reference), and
`data/` holds only local caches (chat history, uploaded files, the SQLite demo DB)
that any deployment recreates on its own.

### The Engine Room (`src/core/`)

The backend is a Python 3.11+ FastAPI application (`src/main.py`).

### `src/core/config.py`
The central nervous system. Uses `pydantic_settings` to load the `.env` file. It holds critical pipeline limits:
- `retrieval_top_k` (default 50): How many chunks to pull from Qdrant.
- `rerank_top_k` (default 25): How many chunks to feed to the final LLM (essential for a large context window model like Llama 3.3).
- `ocr_confidence_threshold`: Determines when to fallback to intensive OCR.
- `chunk_target_tokens` / `chunk_overlap_fraction`: Chunking parameters.
- `default_provider`: Soft-pin provider preference (`openrouter` by default; `auto` disables the pin).
- `openrouter_text_model` / `openrouter_vision_model`: Free-tier OpenRouter model IDs.
- `cors_allow_origins`: Comma-separated CORS allow-list (defaults to Vite dev server origins — never a wildcard).

### `src/core/state.py` (Database Replacement)
We use a JSON-based Data Access Object (DAO) pattern. The `UIStateManager` reads/writes entirely to the `data/` directory (`chats.json`, `messages.json`, `settings.json`). To ensure concurrency safety across asynchronous API calls, it implements file-level advisory locking via `file_lock.py` / `portalocker` (cross-platform: Linux, macOS, and Windows). Writes are atomic: data is written to a temp file first, then renamed, preventing corruption on crash. This ensures the app is highly portable, safe for parallel access, and has zero database setup costs.

### `src/core/file_lock.py` (Cross-Platform Locking)
Wraps `portalocker` into a clean context manager with `LockMode.SHARED` (read) and `LockMode.EXCLUSIVE` (write). This replaces the previous `fcntl`-based locking, which was Unix-only. Both `state.py` and `ingestion_registry.py` share this single abstraction so the locking strategy lives in one place.

### `src/core/ingestion_registry.py` (Deduplication State)
Provides stateful tracking of ingested documents via **content-addressed** SHA-256 hashing (stored in `data/ingested_files.json`). Identity is the file's *content*, not its name: byte-identical re-uploads are skipped (no redundant API calls), while any new content becomes a distinct document — even if it shares a filename with an existing one. Uploads are stored in a unique per-upload subdirectory, so two different files that share a name (e.g. two people's `resume.pdf`) never overwrite each other on disk or collide in identity, and neither is ever destroyed. Re-uploading an edited file therefore adds a second document rather than replacing the first (delete the old one explicitly to remove it).

### `src/core/provider_client.py` (The Routing Engine)
This is the most critical file in the system (~750 lines). It implements:

- **`LLMProvider` Protocol**: A `typing.Protocol` defining the interface all providers must implement: `chat()`, `chat_stream()`, and `vision()`. Adding a new provider means implementing this protocol — no class hierarchy needed.
- **`OpenAICompatibleProvider`**: A single concrete class that handles NIM, Groq, and OpenRouter, since all three support the OpenAI chat completions format. This avoids pulling in provider-specific SDKs.
- **`GeminiProvider`**: Uses the Gemini REST API directly via `httpx` (not the SDK).
- **`ProviderRouter`**: The orchestrator that loads task→provider mappings dynamically from `config/providers.yaml`.
  - When a task (e.g., `reasoning`, `image_understanding`) is requested, the router attempts the `priority: 1` provider.
  - If it encounters a `429 Too Many Requests` or `503 Service Unavailable`, it gracefully catches the error and instantly retries against the next provider in the fallback chain.
  - Supports both standard generative responses (`chat()`) and **Server-Sent Events streaming** (`chat_stream()`) across all providers using a unified `AsyncGenerator` interface.
  - **Soft-pin support**: When `preferred_provider` is set (via `DEFAULT_PROVIDER` env or the UI picker), the chosen provider is injected at priority 0 for every task — but the rest of the chain remains as a safety net.

### `src/core/rate_limiter.py` (Free-Tier Guard)
Cross-provider rate limiter with per-provider tracking and exponential backoff. Each provider has its own RPM/RPD limits configured from conservative free-tier estimates:
- Gemini: 10 RPM, 1500 RPD
- Nvidia NIM: 30 RPM, 5000 RPD
- Groq: 30 RPM, 14400 RPD
- OpenRouter: 20 RPM, 200 RPD

When a provider's limits are exhausted, the `ProviderRouter` immediately falls through to the next — no blocking/waiting for the same provider to recover.

### `src/core/paths.py` (Security)
Path-safety helpers for handling untrusted, caller-supplied path fragments:
- **`safe_basename(filename)`**: Flattens an untrusted filename to a bare basename — `"../../etc/passwd"` collapses to `"passwd"`. Used for upload filenames.
- **`contained_path(root, subpath)`**: Resolves a nested sub-path and verifies it stays inside the served root. Used by the SPA catch-all route to prevent static file serving from leaking `../.env` or `/etc/passwd`.

### `src/core/confidence.py` (QA Gates)
Scores OCR and table-extraction output against multiple heuristics (garbage-character ratio, dictionary-word ratio, cross-checking OCR text against engine confidence) before allowing it into the vector store. Low-confidence extractions are flagged rather than silently corrupting retrieval.

### `src/core/db_client.py` & `src/core/sql_dialects.py` (Text-to-SQL)
Read-only Text-to-SQL execution engine. `db_client.py` handles connection management and query execution for SQLite and MySQL. `sql_dialects.py` provides a `DIALECTS` registry — each entry is a simple dict with prompt wording, sqlglot dialect name, and schema introspection query. Adding a new database engine is a one-entry addition, not a new class hierarchy.

### `src/core/query_cache.py` (Per-Chat Retrieval Cache)
A thread-safe, in-memory `RetrievalCache` (process-local singleton via `get_shared_retrieval_cache()`, mirroring `get_shared_rate_limiter()`) that caches the *retrieval* half of a query — vector chunks and SQL results — keyed by `(chat_id, normalized question)`. It does **not** cache the generated answer.

- **Normalization**: `_normalize()` lowercases, collapses whitespace, and strips trailing punctuation, so "Hey what's the revenue?" and "hey whats the revenue" hit the same entry.
- **Scope**: keyed per `chat_id`, so a repeat only hits within the same chat session — a different chat, or no `chat_id` at all (e.g. the stateless `/api/query` endpoint), always fetches live and never shares cache state across conversations.
- **TTL**: entries expire after 30 minutes (`TTL_SECONDS`), so a long-running chat that drifts topic without an exact repeat question won't serve arbitrarily stale chunks.
- **Bounded growth**: capped at 50 entries per chat (`_MAX_ENTRIES_PER_CHAT`); once exceeded, the single oldest entry is evicted rather than maintaining a full LRU.
- **Filtered queries always bypass the cache** — a `document_type`/`source_file`/etc. filter changes what *should* be retrieved, so a prior unfiltered (or differently filtered) fetch can't safely be reused.
- **Invalidation**: `invalidate_all()` is called from every ingestion-mutating path in `src/api/upload.py` and `src/api/ui.py` (upload, batch upload, streamed upload, folder scan, document replace, document delete) — the underlying vector/SQL data changed, so every cached retrieval is now potentially stale. `invalidate_chat()` is called on chat deletion to free memory for chats that no longer exist.
- **Restart-safe by design**: it's a cost-saving optimization, not a correctness store — losing it on process restart is fine.

`QueryPipeline.query()` / `query_stream()` (`src/pipeline/query.py`) accept a `chat_id` and check the cache before running stages 11/12/14 (vector retrieve, rerank, SQL retrieval). On a hit, those stages are skipped and the cached chunks feed straight into stage 13 (generate); the streaming path surfaces this as a `"Reused earlier retrieval"` thinking step so the UI's reasoning trace still makes sense. Stage 13 (generation) is never cached — the answer is always produced fresh from whichever chunks (cached or freshly fetched) are in hand, so responses stay current even when retrieval is reused. `chat_id` is threaded through from `src/api/ui.py`'s `send_message` and `send_message_stream` endpoints.

### `src/core/speech.py` (Voice Input — Local Transcription)
`SpeechTranscriber` wraps a lazily-loaded `faster_whisper.WhisperModel` (`large-v3-turbo` by default). It is deliberately isolated from the rest of the engine room: its public surface is `transcribe_file(path) -> str` / `transcribe_file_async(path) -> str`, and it never imports `QueryPipeline`, the provider router, or Qdrant. This keeps voice input a pure "audio in, text out" utility that the query pipeline doesn't need to know exists.

- **Lazy singleton**: `get_transcriber()` returns a process-wide `SpeechTranscriber`, built at most once (guarded by a `threading.Lock`) so the (large) model is loaded once per process rather than per request. The first call also triggers the one-time download of the model weights from the public `openai/whisper-large-v3-turbo` Hugging Face repo; every call after that uses the on-disk cache.
- **CPU-first default**: `whisper_device` defaults to `"cpu"` with `whisper_compute_type="int8"` — deliberate, so a small consumer GPU isn't forced to share limited VRAM with embeddings/OCR/the rest of the pipeline. Configurable to `cuda`/`float16` via `.env` once headroom is confirmed.
- **Async-safe**: `transcribe_file_async` offloads the blocking, CPU-bound `model.transcribe()` call via `asyncio.to_thread`, so a transcription request never blocks the FastAPI event loop for other in-flight requests.
- **Failure mode**: raises `TranscriptionError` (not a bare exception) on missing dependency, model-load failure, or empty/undetectable speech, so `src/api/speech.py` can map it to a clean 422 instead of a 500.

### `src/api/speech.py` (Voice Input — Endpoint)
`POST /api/transcribe` accepts a multipart audio upload (webm/ogg/wav/mp3/m4a/mp4/flac), writes it to a temporary file, enforces `max_audio_upload_mb`, transcribes it, and returns `{"text": "..."}`. It sits at the same architectural layer as `upload.py` and `query.py` but talks only to `src/core/speech.py`. The frontend inserts the returned text into the existing composer input — the transcript is just another string until the user presses Send, at which point it flows through the ordinary chat/query path like typed text.

**No audio is persisted, on either side of the request.** The temp file is created with `delete=False` only so it can be reopened by faster-whisper by path, and is unconditionally removed via `os.unlink()` in a `finally` block that runs whether transcription succeeds, fails, or raises — it never lands in `data/` or any other durable location. On the frontend, the recorded `Blob` is a local variable inside `useVoiceRecorder.js`'s `onstop` handler; once the upload resolves (or rejects), nothing in the app still references it, so it's immediately eligible for garbage collection. Net effect: the actual recording exists for roughly the lifetime of a single request and nowhere longer.

---

## 3. The API Layer (`src/api/`)

### `src/api/ui.py` (UI Backend — The Largest API File)
This is the primary API surface for the React frontend (~505 lines). It handles:
- **Chat CRUD**: `POST /api/chats` (create), `GET /api/chats` (list), `PATCH /api/chats/{id}` (rename), `DELETE /api/chats/{id}` (delete).
- **Message Streaming**: `POST /api/chats/{id}/messages/stream` — accepts a user message, runs the full QueryPipeline, and yields SSE events (`thinking`, `chunk`, `done`) back to the UI.
- **Provider Listing**: `GET /api/providers` — returns available providers with human-readable labels and availability status for the UI provider picker.
- **Settings**: `GET /api/settings`, `PUT /api/settings` — persists UI preferences (theme, default provider).
- **Document Export**: `POST /api/chats/{id}/export/document` — feeds the conversation to an LLM with a structured prompt to produce a polished professional report in Markdown, including auto-generated Mermaid charts where data warrants them.

### `src/api/upload.py`
Handles document ingestion via two endpoints:
- `POST /api/upload/batch` — Upload multiple files for background ingestion.
- `POST /api/upload/stream` — Upload with real-time SSE progress (stage-by-stage updates).

### `src/api/query.py`
Direct RAG query endpoint (bypasses the chat/UI state layer):
- `POST /api/query` — One-shot question → answer with optional metadata filters.

### `src/api/speech.py`
Voice input endpoint, independent of the chat/query state layer:
- `POST /api/transcribe` — Uploaded audio clip → `{"text": "..."}` via local Whisper (`src/core/speech.py`). See the Voice Input entry in section 2 for details.

---

## 4. The 14-Stage RAG Pipeline (`src/stages/`)

The ingest and query logic is rigorously decoupled into 14 explicit stages, ensuring atomic processing and easy debugging.

### Ingestion Pipeline (`src/pipeline/ingestion.py`)
Documents are passed sequentially through stages 1–11 in memory. If a stage fails, the document is discarded without corrupting the database.

* **s01_file_detection.py**: Wraps `python-magic` and `filetype` to securely validate MIME types, preventing spoofed files. Supports PDF, DOCX, PPTX, XLSX, CSV, TSV, Markdown, Plaintext, HTML, XML, JSON, and images.
* **s02_classification.py**: Uses zero-shot LLM classification to categorize the document (e.g., Financial Report vs. Scientific Paper). This informs downstream extraction rules.
* **s03_parsing.py**: Primary text extraction using PyMuPDF and pdfplumber (for PDFs), `python-docx` (DOCX), `python-pptx` (PPTX), and `openpyxl` (XLSX).
* **s04_ocr.py**: Fallback module. Uses `OCR.space` API to extract text from scanned PDFs or images.
* **s05_layout.py**: Passes dense pages to Gemini 2.5 Flash Vision to determine reading order and distinguish headers/footers from main prose.
* **s06_tables.py**: Uses heuristics (`camelot-py`) and vision models to accurately extract tabular data into Markdown format.
* **s07_s08_visuals.py**: Slices charts, graphs, and images from the PDF and sends them to a Vision LLM to generate deep descriptive captions. Utilizes `asyncio.gather` bounded by a semaphore to process multiple visuals in parallel for maximum throughput.
* **s09_chunking.py**: Implements semantic, token-based chunking with fractional overlap to maintain sentence boundaries.
* **s10_embeddings.py**: Sends chunks to Jina AI's V3 Embedding model, converting text to 1024-dimensional floating-point vectors **alongside exact-keyword Sparse Vectors**.
* **s11_vector_store.py**: The final commit. Pushes the multi-vectors and payload metadata to Qdrant Cloud. Enables **Reciprocal Rank Fusion (RRF)** for true hybrid search.

### Retrieval Pipeline (`src/pipeline/query.py`)
When a user submits a prompt, it triggers stages 12–14. The `QueryPipeline` orchestrates the flow and emits `ThinkingStep` events for the UI's reasoning trace. Before running retrieval, it checks the per-chat `RetrievalCache` (`src/core/query_cache.py`, see above) for an exact-or-trivially-reworded repeat of the same question in the same chat; on a hit, stages 12 and 14 are skipped and the cached chunks are reused, while stage 13 (generation) always runs fresh. Filtered queries never consult the cache.

* **s12_s13_s14_retrieval.py**: 
  - **Stage 12 (Retrieve):** Embeds the user prompt, queries Qdrant using **Hybrid Search (Dense + Sparse)** with optional **Metadata Filters**, and pulls 50 chunks via RRF fusion. Includes document diversity enforcement and exhaustive query detection.
  - **Stage 13 (Rerank):** Sends the 50 chunks to Jina's Cross-Encoder Reranker to re-order them based on deep contextual relevance to the prompt.
  - **Stage 14 (Generate):** Formats the top 25 chunks into a massive context block and feeds it to the `ProviderRouter`. Enforces citation generation via footnotes (e.g., `[1]`), and yields the generative answer chunk-by-chunk for real-time UI streaming. Detects visualization queries and can generate Mermaid chart markup.

* **s12b_sql_retrieval.py** (Text-to-SQL):
  - Translates natural language to SQL using an LLM with the database schema as context.
  - Every generated query is AST-validated via `sqlglot` to ensure it's a read-only `SELECT`.
  - Results are capped to a maximum row count.
  - Results are injected into the RAG context alongside vector-retrieved chunks.

---

## 5. Frontend Architecture (`DataWeave_UI/`)

The frontend is a single-page React application built with Vite. It is completely decoupled from the AI logic and acts purely as a presentation layer.

### Integration with Backend
Instead of running a separate Node server, we use `npm run build` to compile the React code into static files in the `/frontend/` directory. FastAPI (`src/main.py`) mounts the `/assets` subdirectory and serves `index.html` on the catch-all route. The catch-all route uses `contained_path()` to validate requested file paths, preventing directory traversal attacks.

### Key Directories
- **`src/pages/`**: Route-level components — `Home.jsx` (chat), `Documents.jsx` (file list), `Settings.jsx` (preferences + provider picker), `About.jsx`.
- **`src/components/`**: Reusable UI components:
  - `Chat.jsx`: Main chat wrapper for messages and input.
  - `Sidebar.jsx`: Chat history list + file upload with drag-and-drop. Loaded from `/api/chats`.
  - `Message.jsx`: Renders individual messages using `react-markdown` with custom renderers for code blocks, Mermaid diagrams, and citation superscripts.
  - `MermaidDiagram.jsx`: Renders Mermaid code blocks as inline SVG diagrams. Survives streaming (falls back to raw source until syntax is valid). Charts are sized and coloured from CSS theme variables; xy-charts widen with category count and scroll horizontally.
  - `ThinkingTrace.jsx`: A collapsible reasoning trace displayed above answers. Shows pipeline steps (understand → retrieve → rank → write), streamed live. Auto-expands during streaming, collapses when done. Persisted with messages for later review.
  - `IngestionCard.jsx`: Document upload progress card showing stage-by-stage ingestion status.
  - `ErrorBoundary.jsx`: React error boundary to catch and display component crashes gracefully.
  - `rehypeCitations.js`: A dependency-free rehype plugin that wraps inline `[1]` citation markers in `<sup class="citation-ref">` superscripts. Skips link text and list markers.
  - `Header.jsx`: Top navigation bar.
  - `InputBox.jsx`: Chat input with send controls and a microphone button (voice input) beside Send — shows a pulsing red state while recording and a spinner while the clip is being transcribed.
  - `VoiceWaveform.jsx`: A full-width reactive line shown in place of the text input while recording — an SVG path traced from the mic's actual time-domain waveform (via `useVoiceRecorder`'s `getWaveform()`, backed by a Web Audio `AnalyserNode`'s `getByteTimeDomainData`), so it genuinely moves up and down with voice pitch/loudness rather than looping a canned animation. Uses a fixed viewBox with `preserveAspectRatio="none"` to stretch to any composer width without a `ResizeObserver`, and `vector-effect="non-scaling-stroke"` to keep the line thickness constant under that non-uniform scale. The path's `d` attribute is written directly (not via React state) inside a `requestAnimationFrame` loop, so a 50-60 fps audio readout doesn't force component re-renders. Color comes from `var(--text-secondary)`, so it themes for light/dark automatically.
  - `Layout.jsx`: Page layout wrapper (sidebar + content area).
- **`src/services/`**:
  - `api.js`: An Axios wrapper that communicates with FastAPI endpoints (chats, messages, streaming, upload, settings, providers, export, voice transcription).
  - `http.js`: HTTP client base configuration.
- **`src/store/store.js`**: Zustand state management (~27KB). Manages chats, messages, streaming state, settings, and provider preferences.
- **`src/styles/`**: Global Vanilla CSS. `globals.css` (~61KB) contains the complete design system with CSS custom properties for light/dark theming, component-specific scoped classes, and responsive layouts. `markdown.css` styles rendered Markdown content.
- **`src/utils/`**:
  - `pdfExport.js`: PDF export via the browser's print engine. Renders Markdown to HTML, converts Mermaid code blocks to inline SVGs, and opens a hidden iframe for printing. Supports two modes: raw chat transcript and LLM-restructured professional document.
  - `theme.js`: Theme resolution — supports `light`, `dark`, and `system` (auto-detects OS preference via `prefers-color-scheme`).
  - `useVoiceRecorder.js`: React hook wrapping the browser's `MediaRecorder` and Web Audio APIs. `startRecording`/`stopRecording`/`toggleRecording` drive a small state machine (`idle` → `recording` → `transcribing` → `idle`); while recording, a `MediaStreamSource` feeds an `AnalyserNode` (connected only to the analyser, never to `audioContext.destination`, so nothing is played back) that `getWaveform()` samples on demand — reading `getByteTimeDomainData` and normalizing it to -1..1 for the waveform line. On stop, the recorded `Blob` is sent to `transcribeAudio()` (`api.js` → `POST /api/transcribe`) and the resulting text is handed back via an `onTranscript` callback; the `Blob` and the `AudioContext` are both released immediately afterwards (`cleanupAudioAnalysis()` closes the context, and the blob simply falls out of scope). `Chat.jsx` uses `onTranscript` to append the transcript into the existing composer value — voice input never bypasses the normal text box.

### UI/UX Details
- **Real-Time Generative Streaming**: The React UI (`api.js` and `store.js`) intercepts Server-Sent Events via `POST /api/chats/{id}/messages/stream`. It displays chunks iteratively with a typing effect. Upon stream completion, it seamlessly replaces the chunks with a beautifully formatted markdown message including a `**Sources:**` citation block.
- **Thinking Traces**: Each streaming response begins with `thinking` SSE events that populate the `ThinkingTrace` component above the answer. The trace auto-expands during streaming and collapses when done, persisted with the message for future review.
- **Mermaid Charts**: When the LLM generates a fenced `mermaid` code block, `MermaidDiagram.jsx` renders it as an inline SVG. During streaming, the component gracefully falls back to showing raw source until the syntax becomes valid — the chat never crashes on a half-written diagram.
- **Provider Picker**: The Settings page and/or header expose a provider selector (OpenRouter, Gemini, Groq, NVIDIA NIM) powered by `GET /api/providers`. The chosen provider is persisted via `/api/settings` and soft-pinned in the `ProviderRouter`.
- **Theming**: CSS custom properties drive light/dark/system themes. The `Toaster` (sonner) adapts its background and border colors to the active theme.
- **Routing**: React Router with `BrowserRouter`. Routes: `/` and `/chat` → Home, `/documents` → Documents, `/settings` → Settings, `/about` → About. Catch-all redirects to `/`.
- **In-App Ingestion**: The sidebar natively includes a File Upload component (`IngestionCard.jsx`) allowing users to upload and ingest documents directly into the vector database via `POST /api/upload`.

---

## 6. Security Considerations

- **Path Traversal**: Upload filenames are flattened via `safe_basename()`. Static file serving validates paths via `contained_path()` — attacker-controlled `full_path` values like `../.env` or `/etc/passwd` are rejected before any file is served.
- **CORS**: An explicit allow-list is configured via `CORS_ALLOW_ORIGINS` (defaults to Vite dev server only). A wildcard `*` is intentionally not the default. Credentials are disabled (no cookie/session auth).
- **Text-to-SQL**: Generated queries are AST-validated as read-only `SELECT` via `sqlglot`. For MySQL, a dedicated read-only database user with only `GRANT SELECT` is the actual last line of defense.
- **File Locking**: Advisory file locks prevent concurrent write corruption across async API calls.

---

## 7. End-to-End Traces

### Trace A: Ingesting a PDF via CLI
1. User runs `dataweave ingest Model_Card.pdf`.
2. `src/cli.py` triggers `IngestionPipeline.process()`.
3. The PDF is classified, parsed, OCR'd.
4. Gemini Flash analyzes 40 charts in the PDF. Google throws a `429 Too Many Requests`.
5. `ProviderRouter` catches the 429 and dynamically fails over to Nvidia NIM's Vision model to finish the charts.
6. Jina embeds 96 chunks into 1024-dim dense + sparse vectors.
7. Stage 11 pushes all 96 chunks to Qdrant. The document is now active.

### Trace B: A User Chat Query
1. User types "Compare benchmarks" in the React UI.
2. React fires `fetch` to `POST /api/chats/chat-123/messages/stream` using a stream reader.
3. `api/ui.py` creates a "user" message in `messages.json` and calls `QueryPipeline.query_stream()`.
4. Stage 12 embeds "Compare benchmarks" and pulls 50 chunks from Qdrant via Hybrid Search.
5. Stage 13 reranks them to the top 25 chunks.
6. Stage 14 heuristically identifies this prompt as a `reasoning` task.
7. `ProviderRouter` sends the prompt + 25 chunks to Groq's Llama 3.3 70B (priority 1 for reasoning) invoking the `chat_stream` generator.
8. `ui.py` streams SSE events back to the UI: `{"type": "thinking", ...}` for reasoning trace steps, then `{"type": "chunk", "text": "..."}` for answer tokens.
9. React receives the `thinking` events and populates the `ThinkingTrace` component (auto-expanded). Then receives `chunk` events and visually types the response iteratively in real-time.
10. Once the LLM completes, `QueryPipeline` yields the final `QueryResult` object, extracting citations and mapping them to `[1]` format.
11. `ui.py` sends the final formatted `{"type": "done", "message": ...}` payload via SSE, updating `messages.json`. React swaps the raw streamed message with the polished Markdown text, citation superscripts, and any inline Mermaid charts rendered as SVGs.

### Trace C: Document Export
1. User clicks "Export as Document" in the chat header.
2. React calls `POST /api/chats/{id}/export/document`.
3. `ui.py` gathers all messages for the chat and feeds them to an LLM with a structured prompt requesting: H1 title, executive summary, logical sections, preserved citations, and Mermaid charts where data warrants.
4. The LLM returns a polished Markdown document.
5. React receives the Markdown, `pdfExport.js` converts it to HTML (with `marked`), renders all Mermaid code blocks as inline SVGs, and opens a hidden iframe with `window.print()`.
6. The browser's print engine handles pagination, page breaks, and fonts — producing a clean PDF with embedded charts.
