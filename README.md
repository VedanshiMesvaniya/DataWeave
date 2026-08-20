# DataWeave

DataWeave is a self-hosted Retrieval-Augmented Generation (RAG) platform built around one hard rule: every request must be served through free-tier language model APIs while still matching the accuracy, resilience, and polish of a paid stack.

Instead of locking into a single LLM vendor, DataWeave routes each request through a multi-provider engine. Depending on the type of work — classification, vision, reasoning, extraction, or summarization — the router picks the strongest currently-available free-tier model across Google Gemini, Groq, NVIDIA NIM, and OpenRouter, then falls back automatically if a provider throttles or errors out.

![Stack](https://img.shields.io/badge/Stack-FastAPI%20|%20React%20|%20Qdrant-success)
![Architecture](https://img.shields.io/badge/Architecture-14--Stage_Pipeline-blue)

---

## Highlights

### Smart multi-provider routing
Every LLM call passes through a `ProviderRouter` that knows the fallback order for its task type. When a provider answers with a rate-limit or outage error, the router immediately re-issues the same request to the next provider in line, so a single flaky vendor never stalls a chat response. A companion `RateLimiter` (`src/core/rate_limiter.py`) watches per-provider requests-per-minute and requests-per-day budgets with exponential backoff, so the system steps around a provider before it gets throttled rather than after.

### Task-aware model selection
Routing decisions live in `config/providers.yaml`, not in application code, so the model lineup can change without a redeploy. As of the last routing review, the live configuration looks like this:

| Task | Primary | Fallback chain |
|---|---|---|
| General Q&A | Gemini Flash | Groq (open-weight 120B) → NVIDIA NIM (Nemotron Ultra) → OpenRouter |
| Reasoning / SQL generation | NVIDIA NIM (Nemotron Ultra, 1M context) | Groq → Gemini Flash → OpenRouter |
| Vision, layout & tables | Gemini Flash | NVIDIA NIM (Llama Vision / Nemotron Nano VL) |
| Extraction | NVIDIA NIM (Nemotron Super) | Gemini Flash → Groq |
| Summarization | NVIDIA NIM (Kimi K2) | Gemini Flash |
| Lightweight classification | Groq (open-weight 20B) | Gemini Flash-Lite → OpenRouter |
| OpenRouter (aggregator) | Soft-pinned when selected in Settings | Falls back to the task chain above on rate limits |

Because this table is generated from live configuration rather than hard-coded, always treat `config/providers.yaml` as the source of truth — it is re-checked against provider catalogs regularly and models get swapped the moment one stops being served on its free tier.

- **Embeddings:** Jina Embeddings V3 (1024-dim dense vectors, with sparse vectors for keyword matching)
- **Reranking:** Jina's cross-encoder reranker
- **Vector storage:** Qdrant, either the managed cloud tier or a local instance
- **Streaming:** every generation path streams over Server-Sent Events, so answers appear token-by-token

### Architecture-level upgrades
- **Content-addressed ingestion:** documents are identified by the SHA-256 hash of their contents, not their filename. Uploading the same bytes twice costs nothing extra; two different files that happen to share a filename (two people's `resume.pdf`, for instance) are stored as separate documents and never clobber one another.
- **True hybrid retrieval:** Qdrant's Reciprocal Rank Fusion blends dense semantic vectors with sparse keyword vectors from Jina's `return_sparse` output.
- **Metadata filtering:** narrow a search by document type, filename, or page range.
- **Concurrent ingestion:** entire folders of documents are processed in parallel under an `asyncio.Semaphore`, so throughput doesn't come at the cost of blowing past provider rate limits.
- **Live progress streaming:** the UI mirrors the 14-stage pipeline in real time over the `/upload/stream` endpoint.
- **Confidence gating:** OCR and table-extraction output is scored on heuristics like garbage-character ratio and dictionary-word ratio before it's allowed anywhere near the vector store, so a bad scan degrades gracefully instead of poisoning retrieval later (`src/core/confidence.py`).
- **Path-traversal hardening:** uploaded filenames and static file paths are sanitized through `src/core/paths.py`.

### Document ingestion
Beyond plain PDF, the pipeline accepts:
- **PDF** — full treatment: OCR, layout detection, table extraction, and visual analysis
- **DOCX** — via `python-docx`
- **PPTX** — via `python-pptx`
- **XLSX / XLS** — via `openpyxl`
- **CSV / TSV** — tabular data
- **Markdown, plain text, HTML, XML, JSON** — text formats
- **Images** — analyzed directly by a vision model

### The 14-stage pipeline
A document that fails a stage is discarded cleanly rather than being allowed to corrupt the vector store.

**Ingestion (stages 1–11):**
1. **File detection** — MIME-type verification via `python-magic` and `filetype`
2. **Classification** — zero-shot categorization (e.g. financial report vs. scientific paper)
3. **Parsing** — text extraction via PyMuPDF, pdfplumber, python-docx, python-pptx, openpyxl
4. **OCR fallback** — OCR.space handles scanned pages and images
5. **Layout analysis** — a vision model determines reading order on dense pages
6. **Table extraction** — heuristics plus vision fallback turn tables into Markdown
7. **Visual analysis** — charts and graphs are sliced out and captioned by a vision model, processed concurrently with `asyncio.gather`
8. **Chunking** — semantic, token-aware chunking with overlap
9. **Embedding** — Jina V3 produces dense and sparse vectors
10. **Vector storage** — chunks land in Qdrant, ready for hybrid search

**Retrieval (stages 12–14), triggered on every chat turn:**
11. **Retrieve** — the query is embedded and matched against the store with RRF-fused hybrid search, pulling the top 50 candidate chunks
12. **Rerank** — Jina's cross-encoder reorders those 50 by contextual relevance
13. **Generate** — the top 25 chunks are assembled into a prompt, routed through the provider chain, and streamed back with citation markers
14. **SQL retrieval (optional)** — a parallel text-to-SQL stage answers questions against a live SQLite or MySQL database; every generated query is parsed and validated as a read-only `SELECT` via `sqlglot` and capped at a maximum row count before it ever touches the database

### Frontend
A single-page React application, served straight out of FastAPI:
- Streaming chat with live typing, upgraded to fully rendered Markdown and citation footnotes once a response completes
- Collapsible reasoning traces shown above each answer (understand → retrieve → rank → write)
- Inline Mermaid diagram rendering (flowcharts, pie charts, xy-charts) that degrades gracefully mid-stream until the syntax is complete
- Two export modes: a plain transcript PDF, or an LLM-restructured report with an executive summary and auto-generated diagrams
- An in-app provider picker to soft-pin a preferred LLM vendor for the session
- Two themes — **Midnight** (dark) and **Day Light** (light) — switchable from Settings
- Drag-and-drop ingestion straight from the sidebar
- Full chat CRUD (create, rename, delete) with persisted history
- A documents view and a settings page, both backed by their own API endpoints

---

## Repository layout

```text
DataWeave/
├── DataWeave_UI/               # React frontend (Vite)
│   ├── src/
│   │   ├── components/         # Chat.jsx, Sidebar.jsx, Message.jsx, Header.jsx, ...
│   │   ├── pages/               # Home.jsx, Documents.jsx, Settings.jsx, About.jsx
│   │   ├── services/            # api.js, http.js — talk to the FastAPI backend
│   │   ├── store/store.js       # Zustand state management
│   │   ├── styles/               # globals.css, markdown.css
│   │   └── utils/                # pdfExport.js, theme.js
│   ├── index.html / vite.config.js / package.json
├── config/
│   └── providers.yaml           # Task-to-model routing table (edit this, not the code)
├── data/                        # Local state — no SQL server needed for app data
│   ├── ingested_files.json      # Dedup registry (content hashes)
│   ├── chats.json / messages.json
│   ├── settings.json
│   ├── live_data.db             # SQLite backend for the SQL-retrieval stage
│   ├── uploads/ / processed/
├── docs/
│   ├── ARCHITECTURE.md          # Full internal walkthrough
│   ├── DEPLOY.md                # Container / PaaS deployment guide
│   └── document-identity.html   # How document identity & dedup work
├── frontend/                    # Built UI output, served by FastAPI
├── src/
│   ├── api/                     # ui.py, upload.py, query.py — FastAPI routes
│   ├── core/                    # config.py, provider_client.py, rate_limiter.py,
│   │                             # confidence.py, db_client.py, sql_dialects.py,
│   │                             # state.py, file_lock.py, paths.py, ingestion_registry.py
│   ├── models/schemas.py        # Pydantic models for every stage's output
│   ├── pipeline/                # ingestion.py, query.py — stage orchestration
│   ├── stages/                  # s01 ... s14, one file per pipeline stage
│   ├── cli.py                   # Command-line entrypoint
│   └── main.py                  # FastAPI app entrypoint
├── tests/                       # pytest suite
├── scripts/                     # setup_db.py, smoke tests
├── Dockerfile
├── render.yaml                  # Render.com deploy blueprint
├── requirements.txt
└── pyproject.toml
```

---

## Running it yourself

### Step 1 — Install prerequisites
You'll need:
- **Python 3.11 or newer**
- **Node.js and npm** (to build the frontend)
- Free-tier API keys for the providers below (all have no-cost signup)

### Step 2 — Clone and set up a virtual environment
```bash
git clone <this-repository-url>
cd DataWeave

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -e .
```

### Step 3 — Configure environment variables
```bash
cp .env.example .env
```
Then open `.env` and fill in the keys you need.

**Required for the core pipeline to run:**
- `GEMINI_API_KEY` — Google AI Studio
- `NVIDIA_NIM_API_KEY` — build.nvidia.com
- `GROQ_API_KEY` — console.groq.com
- `JINA_API_KEY` — embeddings and reranking
- `QDRANT_URL` and `QDRANT_API_KEY` — vector storage

**Optional:**
- `OPENROUTER_API_KEY` — enables the OpenRouter aggregator as a soft-pinned provider
- `OCR_SPACE_API_KEY` — enables OCR for scanned documents
- `DEFAULT_PROVIDER` — force a preferred provider (`gemini`, `groq`, `nvidia_nim`, `openrouter`, or `auto` to just follow `providers.yaml`)

**Optional — connect a live SQL database for the text-to-SQL stage:**
```text
DB_ENGINE=mysql
DB_HOST=your-mysql-host
DB_PORT=3306
DB_NAME=your-database-name
DB_READONLY_USER=readonly_user
DB_READONLY_PASSWORD=your-readonly-password
```
Leave `DB_ENGINE=sqlite` (the default) if you don't need this — it will use `data/live_data.db` automatically. If you do use MySQL, create the read-only user with `GRANT SELECT ON your_db.* TO 'readonly_user'@'%';` — this is what actually enforces read-only access at the database level, not just the query validation in code.

### Step 4 — Build the frontend
The backend serves the compiled React app, so build it before starting the server:
```bash
cd DataWeave_UI
npm install
npm run build
cd ..
```

### Step 5 — Start the server
```bash
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```
Visit **http://localhost:8000** in your browser.

### Step 6 — Add documents
Pick whichever fits your workflow:

- **From the UI:** click Upload in the sidebar and drag files in — you'll see each pipeline stage complete in real time.
- **From the CLI:**
  ```bash
  python -m src.cli ingest path/to/your/document.pdf
  # or, if installed as a package:
  globle-mind ingest path/to/your/document.pdf
  ```
- **Drop-folder automation:** place files in `data/inbox/` and trigger a scan on demand:
  ```bash
  curl -X POST http://localhost:8000/api/ingest/folder
  ```
  Because ingestion is content-addressed, re-scanning is safe — already-ingested files are skipped automatically. To automate this fully, set in `.env`:
  - `AUTO_INGEST_ON_STARTUP=true` — scan once when the server boots
  - `AUTO_INGEST_INTERVAL_SECONDS=300` — rescan on a timer (`0` turns this off)

Large or image-heavy documents can take a few minutes given the depth of the visual analysis — the pipeline processes what it can in parallel and reroutes around rate limits automatically.

### Step 7 — Chat with your documents
Open a chat and ask a question. Behind the scenes DataWeave will embed the query, pull the top 50 chunks via hybrid search, rerank down to the top 25, and stream back a cited answer with a visible reasoning trace.

### Step 8 — Export a conversation
From any chat, export either:
- a plain transcript, or
- a restructured report with a summary and auto-generated diagrams where the data supports it

### Other CLI commands
```bash
# Ask a question directly from the terminal, no UI required
globle-mind query "What were the key findings in the Q3 report?"

# Start the server (same as the uvicorn command above)
globle-mind serve

# Check which providers are currently reachable with your keys
globle-mind health
```

---

## Configuration notes

- **Retrieval limits** (chunks retrieved, chunks kept after reranking) live in `src/core/config.py`.
- **Model routing** lives entirely in `config/providers.yaml` — edit the YAML to change which model handles which task, no code changes needed.
- **Provider preference** can be set via `DEFAULT_PROVIDER` in `.env`, or switched live from the Settings page in the UI.
- **Application state** (chats, messages, settings, dedup registry) is stored as flat JSON files under `data/` — no SQL server required for the app itself.
- **Text-to-SQL** is a separate, optional stage that talks to your own structured database (SQLite by default, or MySQL). Every generated query is validated as a read-only `SELECT` before execution, and for MySQL you should also enforce this with a dedicated read-only database user as a second line of defense. Adding a new database engine means adding one entry to the dialect registry in `src/core/sql_dialects.py`.
- **Adding a new LLM provider:** implement the `LLMProvider` protocol in `src/core/provider_client.py` (`chat()`, `chat_stream()`, `vision()`), register it with `ProviderRouter`, then add it to `config/providers.yaml`.

## Further reading
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — full internal architecture and state-management walkthrough
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — deployment guide
- [`docs/document-identity.html`](docs/document-identity.html) — how document identity and deduplication work
