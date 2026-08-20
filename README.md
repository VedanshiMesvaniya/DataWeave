# DataWeave

DataWeave is an enterprise-grade Retrieval-Augmented Generation (RAG) system built with a single, aggressive constraint: **It must operate entirely on forever-free-tier APIs while rivaling paid, state-of-the-art systems in accuracy, resiliency, and user experience.**

By implementing a dynamic LLM routing pattern, DataWeave rejects single-provider dependency. The system intelligently categorizes workloads and distributes them across free tiers from Google (Gemini), Groq, Nvidia NIM, OpenRouter, and Jina AI based on rate limits, availability, and task complexity.

![Stack](https://img.shields.io/badge/Stack-FastAPI%20|%20React%20|%20Qdrant-success)
![Architecture](https://img.shields.io/badge/Architecture-14--Stage_Pipeline-blue)

---

## 🌟 Key Features

### 🧠 Intelligent Provider Routing
A resilient `ProviderRouter` directs workloads to the best free-tier model for the job. If it encounters a `429 Too Many Requests` (common on free tiers) or a `503 Service Unavailable`, it gracefully catches the error and instantly retries the payload against the secondary provider in the fallback chain. A dedicated `RateLimiter` (`src/core/rate_limiter.py`) tracks per-provider RPM/RPD limits with exponential backoff so the router can preemptively fall through before hitting 429s.
- **General QA:** Gemini 2.5 Flash → Groq (Llama 3.3 70B) → Nvidia NIM (Qwen 3.5 397B)
- **Reasoning:** Groq (Llama 3.3 70B) → Gemini 2.5 Flash → Nvidia NIM (Llama 3 70B)
- **Vision & Layout:** Gemini 2.5 Flash → Nvidia NIM (Llama 3.2 90B Vision / Nemotron Nano 12B)
- **Extraction:** Nvidia NIM (Qwen 3.5 397B) → Gemini 2.5 Flash → Groq (Llama 3.1 8B)
- **Summarization:** Nvidia NIM (Kimi K2.6) → Gemini 2.5 Flash
- **OpenRouter (Aggregator):** Acts as a configurable soft-pin provider — when selected, it's preferred for every task but still falls back to the task-specific chain. Uses free-tier models by default (`meta-llama/llama-3.3-70b-instruct:free`, `meta-llama/llama-3.2-11b-vision-instruct:free`).
- **Embeddings:** Jina Embeddings V3 (1024-dim dense + sparse)
- **Reranking:** Jina Cross-Encoder Reranker
- **Vector DB:** Qdrant Cloud / Local Memory
- **Streaming Support:** Full Server-Sent Events (SSE) support across all generators for real-time streaming.

### 🚀 V2 Architecture Upgrades
- **Content-Addressed Ingestion (Deduplication):** Documents are identified by the SHA-256 of their *content*, not their filename. Re-uploading byte-identical content costs zero API credits, while any new content becomes a distinct document — even under an existing filename. Two different files that share a name (e.g. two people's `resume.pdf`) are kept as separate documents and never overwrite each other, so a same-name upload can't cause data loss.
- **True Hybrid Search (RRF):** Queries are powered by Qdrant's Reciprocal Rank Fusion, seamlessly merging semantic dense vectors with exact-keyword sparse vectors (via Jina `return_sparse`).
- **Metadata Filtering:** Constrain searches with surgical precision (e.g., by document type, filename, or page range).
- **Parallel Batching:** Ingest folders of documents concurrently without violating rate limits using an `asyncio.Semaphore`.
- **Live Pipeline Streaming:** The frontend syncs with the 14-stage pipeline in real-time via the `/upload/stream` SSE endpoint.
- **Confidence Gates (QA):** OCR and table-extraction output is scored against multiple heuristics (garbage-character ratio, dictionary-word ratio, cross-checking OCR text against the reported engine confidence) before it's allowed into the vector store — low-confidence extractions get flagged rather than silently corrupting retrieval later (`src/core/confidence.py`).
- **Cross-Provider Rate Limiter:** Per-provider RPM/RPD tracking with exponential backoff (`src/core/rate_limiter.py`), enabling the router to preemptively switch providers before exhausting free-tier quotas.
- **Path Traversal Protection:** All upload filenames and static file paths are sanitized via `src/core/paths.py` (`safe_basename` for uploads, `contained_path` for static serving) to prevent directory traversal attacks.

### 📄 Multi-Format Ingestion
DataWeave supports ingestion of multiple document formats beyond PDF:
- **PDF** — Full pipeline with OCR, layout analysis, table extraction, and visual analysis
- **DOCX** — Microsoft Word documents (via `python-docx`)
- **PPTX** — PowerPoint presentations (via `python-pptx`)
- **XLSX / XLS** — Excel spreadsheets (via `openpyxl`)
- **CSV / TSV** — Tabular data files
- **Markdown, Plaintext, HTML, XML, JSON** — Text-based formats
- **Images** — Direct image analysis via Vision LLM

### 📄 14-Stage Ingestion Pipeline
Documents are processed through a rigorous pipeline designed for high accuracy. If a stage fails, the document is discarded without corrupting the vector database.
1. **File Detection** (`s01`): Secure MIME type validation via `python-magic` and `filetype`.
2. **Classification** (`s02`): Zero-shot LLM classification (e.g., Financial Report vs. Scientific Paper).
3. **Parsing** (`s03`): Primary text extraction using PyMuPDF, pdfplumber, python-docx, python-pptx, and openpyxl.
4. **OCR (Fallback)** (`s04`): Uses `OCR.space` API to extract text from scanned PDFs or images.
5. **Layout Analysis** (`s05`): Passes dense pages to Gemini Vision to determine reading order.
6. **Tables** (`s06`): Heuristics (camelot-py) and vision models to accurately extract tabular data into Markdown format.
7. **Visual Analysis — Charts & Graphs** (`s07_s08`): Asynchronously slices charts/graphs from the PDF and sends them to a Vision LLM to generate descriptive captions, making images "searchable". Uses `asyncio.gather` bounded by a semaphore for parallel processing.
8. **Chunking** (`s09`): Semantic, token-based chunking with fractional overlap.
9. **Embeddings** (`s10`): Jina V3 Embeddings (1024-dimensional dense vectors + exact-keyword sparse vectors).
10. **Vector Store** (`s11`): Final commit to Qdrant Cloud. Enables Reciprocal Rank Fusion (RRF) for true hybrid search.

### 🔍 Retrieval Pipeline (Stages 12–14)
When a user submits a prompt:
11. **Retrieve** (`s12`): Embeds the query, performs Hybrid Search (Dense + Sparse) with optional Metadata Filters, and pulls 50 chunks via RRF fusion.
12. **Rerank** (`s13`): Sends the 50 chunks to Jina's Cross-Encoder Reranker to re-order them based on deep contextual relevance.
13. **Generate** (`s14`): Formats the top 25 chunks into a context block, feeds it to the `ProviderRouter`, and yields the answer chunk-by-chunk via SSE streaming with citation footnotes.
14. **SQL Retrieval** (`s12b`, optional): Text-to-SQL stage for answering questions against a live structured database (SQLite or MySQL). Queries are AST-validated as read-only `SELECT` via `sqlglot` and capped to a maximum row count.

### 💻 Modern React UI
A beautiful, unified single-page React interface served directly by the FastAPI backend. Features include:
- **Real-Time Generative Streaming:** SSE-powered chat with typing effects. Raw streamed chunks are seamlessly replaced with fully formatted Markdown and citation footnotes upon completion.
- **Thinking Traces:** Collapsible reasoning traces (understand → retrieve → rank → write) displayed above each answer, streamed live and persisted with messages — like Claude thinking blocks.
- **Mermaid Diagram Rendering:** Inline SVG rendering of Mermaid charts (xychart-beta, pie, flowchart, etc.) generated by the LLM, with theme-aware coloring and responsive sizing. Survives streaming — falls back to raw source until the diagram syntax is complete.
- **PDF / Document Export:** Two export modes — raw chat transcript and LLM-restructured professional document — rendered to HTML with embedded Mermaid SVGs and printed via the browser's print engine.
- **Provider Picker:** In-app selector to switch between LLM providers (OpenRouter, Gemini, Groq, NVIDIA NIM). The chosen provider is soft-pinned for the session but the pipeline still falls back on rate limits.
- **Theme Support:** Light, dark, and system-auto themes with CSS custom properties and `sonner` toast notifications that adapt to the active theme.
- **In-App Ingestion:** The sidebar includes a file upload component with drag-and-drop, allowing users to upload and ingest documents directly into the vector database.
- **Chat Management:** Full CRUD for conversations — create, rename, and delete chats (`POST` / `PATCH` / `DELETE /api/chats/{chat_id}`), each with persisted message history.
- **Documents & Settings Pages:** Documents view lists ingested files via `/api/documents`; Settings page persists UI preferences via `/api/settings`.

---

## 📂 Project Structure

DataWeave is architected for extreme modularity. Here is the layout of the repository to help you navigate the codebase:

```text
globle_mind/
├── DataWeave_UI/              # React frontend source code (Vite)
│   ├── src/
│   │   ├── components/        # Reusable UI components
│   │   │   ├── Chat.jsx       # Main chat UI wrapper (messages + input)
│   │   │   ├── Sidebar.jsx    # Chat history + file upload (from /api/chats)
│   │   │   ├── Message.jsx    # Individual message renderer (react-markdown)
│   │   │   ├── Header.jsx     # Top navigation bar
│   │   │   ├── InputBox.jsx   # Chat input with send controls
│   │   │   ├── Layout.jsx     # Page layout wrapper
│   │   │   ├── MermaidDiagram.jsx  # Inline SVG Mermaid chart renderer
│   │   │   ├── ThinkingTrace.jsx   # Collapsible reasoning trace display
│   │   │   ├── IngestionCard.jsx   # Document upload progress card
│   │   │   ├── ErrorBoundary.jsx   # React error boundary
│   │   │   ├── rehypeCitations.js  # rehype plugin for [1] citation superscripts
│   │   │   ├── Button.jsx     # Reusable button component
│   │   │   ├── Card.jsx       # Reusable card component
│   │   │   └── Loader.jsx     # Loading spinner
│   │   ├── pages/             # Route-level page components
│   │   │   ├── Home.jsx       # Main chat page
│   │   │   ├── Documents.jsx  # Ingested documents list
│   │   │   ├── Settings.jsx   # UI preferences & provider picker
│   │   │   └── About.jsx      # About page
│   │   ├── services/          # API communication layer
│   │   │   ├── api.js         # Axios wrapper for FastAPI endpoints
│   │   │   └── http.js        # HTTP client configuration
│   │   ├── store/
│   │   │   └── store.js       # Zustand state management
│   │   ├── styles/
│   │   │   ├── globals.css    # Design system + all component styles
│   │   │   └── markdown.css   # Markdown rendering styles
│   │   └── utils/
│   │       ├── pdfExport.js   # Chat/document PDF export via print engine
│   │       └── theme.js       # Light/dark/system theme resolution
│   ├── index.html
│   ├── vite.config.js
│   └── package.json
├── config/
│   └── providers.yaml         # Dynamic LLM routing rules & fallback chains
├── data/                      # Local state (Zero SQL for app state!)
│   ├── ingested_files.json    # Deduplication registry (SHA-256 state)
│   ├── chats.json             # Chat metadata (portalocker locked)
│   ├── messages.json          # Chat message history
│   ├── settings.json          # UI preferences (theme, provider, etc.)
│   ├── live_data.db           # SQLite database for Text-to-SQL stage
│   ├── uploads/               # Raw uploaded files (pre-processing)
│   └── processed/             # Post-processing artifacts
├── docs/                      # Project documentation
│   ├── ARCHITECTURE.md        # Deep-dive system mechanics & directory map
│   ├── DEPLOY.md              # Container/PaaS deployment guide
│   ├── document-identity.html # Document identity, versioning & dedup reference
│   └── design-history/        # Original free-API design research (historical)
├── frontend/                  # Compiled React UI (Served directly by FastAPI)
├── src/                       # Core Python Backend
│   ├── api/                   # FastAPI Endpoints
│   │   ├── ui.py              # Full UI API: chats CRUD, streaming, settings,
│   │   │                      #   provider listing, document export
│   │   ├── upload.py          # Supports /upload/batch and /upload/stream
│   │   └── query.py           # Handles RAG queries with metadata filters
│   ├── core/                  # Core Engine Logic
│   │   ├── config.py          # System limits, env vars, pydantic_settings
│   │   ├── provider_client.py # The Multi-LLM Routing Engine (Protocol + 4 providers)
│   │   ├── rate_limiter.py    # Per-provider RPM/RPD tracking with backoff
│   │   ├── confidence.py      # OCR/table extraction QA gates
│   │   ├── db_client.py       # Read-only Text-to-SQL execution (SQLite / MySQL)
│   │   ├── sql_dialects.py    # Per-engine dialect facts (prompt wording, sqlglot, schema query)
│   │   ├── state.py           # JSON-backed UI state manager (UIStateManager)
│   │   ├── file_lock.py       # Cross-platform advisory locking (portalocker)
│   │   ├── paths.py           # Path traversal protection (safe_basename, contained_path)
│   │   └── ingestion_registry.py # Idempotency / deduplication logic
│   ├── models/
│   │   └── schemas.py         # Pydantic data models (per-stage typed outputs)
│   ├── pipeline/              # Orchestrators
│   │   ├── ingestion.py       # Executes Stages 1–11 sequentially
│   │   └── query.py           # Executes Stages 12–14 (retrieve → rerank → generate)
│   ├── stages/                # The 14 Atomic RAG Stages
│   │   ├── s01_file_detection.py
│   │   ├── s02_classification.py
│   │   ├── s03_parsing.py
│   │   ├── s04_ocr.py
│   │   ├── s05_layout.py
│   │   ├── s06_tables.py
│   │   ├── s07_s08_visuals.py
│   │   ├── s09_chunking.py
│   │   ├── s10_embeddings.py
│   │   ├── s11_vector_store.py
│   │   ├── s12_s13_s14_retrieval.py
│   │   └── s12b_sql_retrieval.py  # Text-to-SQL: NL2SQL + AST validation
│   ├── cli.py                 # Command Line Interface
│   └── main.py                # FastAPI Server Entrypoint
├── tests/                     # Comprehensive test suite (pytest)
│   ├── test_pipeline_e2e_integration.py  # E2E isolated registry tests
│   ├── test_sql_dialects.py    # Dialect registry, schema formatting, query paths
│   ├── test_stages.py          # Individual stage unit tests
│   ├── test_query_pipeline.py  # Query pipeline tests
│   ├── test_citations_and_routing.py  # Citation extraction & provider routing
│   ├── test_provider_gemini.py # Gemini provider-specific tests
│   ├── test_file_lock.py       # File locking concurrency tests
│   └── test_path_safety.py     # Path traversal protection tests
├── scripts/                   # Dev & ops scripts (not shipped in the image)
│   ├── setup_db.py            # Loads the sample CSV into data/live_data.db
│   ├── sql_smoke_test.py      # Manual Text-to-SQL smoke check (hits live APIs)
│   └── adversarial_smoke_test.py # Manual SQL-injection / routing smoke check
├── Dockerfile                 # Container image (FastAPI + bundled UI)
├── render.yaml                # Render blueprint (auto-deploy)
├── CLAUDE.md                  # AI coding standards & conventions
├── requirements.txt           # Runtime deps installed as a backstop to pyproject
└── pyproject.toml             # Python dependencies & project config
```

---

## 🚀 Getting Started

### 1. Prerequisites
- Python 3.11+
- Node.js & npm (for building the UI)
- API Keys for the free tiers (see below)

### 2. Installation
Clone the repository and install the Python dependencies into a virtual environment:
```text
# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install the backend in editable mode
pip install -e .
```

### 3. Environment Setup
Copy the example `.env` file and fill in your API keys:
```text
cp .env.example .env
```
Required keys: `GEMINI_API_KEY`, `NVIDIA_NIM_API_KEY`, `GROQ_API_KEY`, `JINA_API_KEY`, `QDRANT_URL`, `QDRANT_API_KEY`.

Optional keys:
- `OPENROUTER_API_KEY` — Enables the OpenRouter aggregator provider (free-tier models included by default).
- `OCR_SPACE_API_KEY` — Enables OCR fallback for scanned PDFs.

**Optional — Default Provider Pin:**
Set `DEFAULT_PROVIDER` to control which provider the system prefers by default. Options: `openrouter` (default), `gemini`, `groq`, `nvidia_nim`, or `auto` (uses the task-optimized routes as authored in `providers.yaml`).

**Optional — Text-to-SQL live data:** By default, `DB_ENGINE=sqlite` and no further setup is needed beyond having a `data/live_data.db` file. To point the Text-to-SQL stage at MySQL instead, set:
```text
DB_ENGINE=mysql
DB_HOST=your-mysql-host
DB_PORT=3306
DB_NAME=your-database-name
DB_READONLY_USER=readonly_user
DB_READONLY_PASSWORD=your-readonly-password
```
`DB_READONLY_USER` should be a dedicated MySQL user with **only** `SELECT` granted (`GRANT SELECT ON your_db.* TO 'readonly_user'@'%';`) — this is the actual enforcement mechanism for write-prevention on MySQL, not just the query validation in code.

### 4. Build the Frontend
The React UI is designed to be served statically by FastAPI. You must build it first:
```text
cd DataWeave_UI
npm install
npm run build
cd ..
```

---

## 💡 Usage

### 1. Start the Server
Run the FastAPI backend, which will mount and serve the built React UI:
```text
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```
Open your browser and navigate to **http://localhost:8000**.

### 2. Ingest Documents
Before you can chat with a document, you must ingest it into the vector database. 

**Option A (UI):** Click the "Upload" button directly in the sidebar of the web application. Drag and drop or select files — the UI shows real-time progress as each pipeline stage completes.

**Option B (CLI):** Use the built-in command line tool in a new terminal window:
```text
# Using the installed command
globle-mind ingest path/to/your/document.pdf

# Or running the module directly
python -m src.cli ingest path/to/your/document.pdf
```
**Option C (Drop-folder automation):** Drop files into the watched folder (`data/inbox/` by default) and trigger a scan — every new file is ingested automatically. Because identity is content-addressed, scans are **idempotent**: files already ingested are skipped, so it's safe to leave them in the folder and re-scan.
```text
# Trigger a scan on demand (returns a summary, incl. a ready-to-display message)
curl -X POST http://localhost:8000/api/ingest/folder
```
Automate it with two optional env vars (see `.env.example`):
- `AUTO_INGEST_ON_STARTUP=true` — scan the folder once each time the server starts.
- `AUTO_INGEST_INTERVAL_SECONDS=300` — re-scan the folder every N seconds in the background (`0` disables it).

*Note: Because of the deep visual analysis, complex PDFs (like Model Cards or Financial Reports) may take several minutes to ingest. The pipeline uses `asyncio` parallel processing and will automatically route around rate limits if needed!*

### 3. Chat!
Head back to `http://localhost:8000`, open a chat, and ask deep analytical questions. DataWeave will automatically:
1. Embed your query.
2. Retrieve the top 50 chunks via Hybrid Search (Dense + Sparse RRF).
3. Rerank them to the top 25 chunks using a Jina Cross-Encoder.
4. Synthesize a fully cited answer, streaming it back to your screen in real-time with a visible thinking trace.

### 4. Export
From within any chat, you can export the conversation in two modes:
- **Chat Transcript:** A formatted PDF of the conversation as-is.
- **Professional Document:** The LLM restructures the chat into a polished report with executive summary, logical sections, and auto-generated Mermaid charts where data warrants them.

### 5. Other CLI Commands
Beyond `ingest`, the CLI supports:
```text
# Query the pipeline directly from the terminal (no UI needed)
globle-mind query "What were the key findings in the Q3 report?"

# Start the FastAPI server (equivalent to the uvicorn command above)
globle-mind serve

# Check which LLM providers are currently reachable
globle-mind health
```

---

## ⚙️ Configuration & Extensibility

- **RAG Limits:** You can tweak the context limits (e.g., retrieving 50 chunks, reranking to 25) in `src/core/config.py`.
- **Dynamic Routing Rules:** Model routing priorities and fallback chains can be edited entirely without touching Python code by updating `config/providers.yaml`.
- **Provider Selection:** Set `DEFAULT_PROVIDER` in `.env` or use the in-app provider picker to switch providers on the fly. OpenRouter is the default soft pin; set to `auto` to let the task-optimized routes decide.
- **State Persistence:** Data persistence completely avoids SQL databases for application state. UI state is managed via flat JSON files (`/data`), and vectors are handled by Qdrant (cloud or local).
- **Text-to-SQL (Live Data):** A separate retrieval stage (`s12b_sql_retrieval.py`) lets the assistant answer questions against a live, structured database — independent of the JSON-backed UI state above. It supports **SQLite** (default, local file at `data/live_data.db`) and **MySQL**, selected via the `DB_ENGINE` env var (`sqlite` or `mysql`; see `.env.example`). Every generated query is AST-validated as a read-only `SELECT` (via `sqlglot`) and capped to a maximum row count before execution, and — for MySQL — should be run against a dedicated read-only database user (`GRANT SELECT` only) as the actual last line of defense at the database level. Adding a new engine (e.g. SQL Server) means adding one entry to the `DIALECTS` registry in `src/core/sql_dialects.py`, not a new class hierarchy.
- **Adding a New LLM Provider:** Implement the `LLMProvider` protocol (defined in `src/core/provider_client.py`) — `chat()`, `chat_stream()`, and `vision()` — register it in `ProviderRouter`, and add entries in `config/providers.yaml`.

## 📚 Architecture Details
For a deep dive into the internal mechanics, state management, and file structure of DataWeave, please read the comprehensive **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**. Deployment instructions live in **[docs/DEPLOY.md](docs/DEPLOY.md)**, and the document identity/versioning model is documented in **[docs/document-identity.html](docs/document-identity.html)**.
