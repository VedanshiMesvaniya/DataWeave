# Zero-Cost Enterprise RAG Architecture: Free-API Edition
**Redesign of the Agentic RAG Research Platform document pipeline — accuracy-first, $0 budget**
*Researched and written July 2026. Free-tier terms move fast (see the "Free Tier Volatility" callout in Stage 0) — re-verify the numbers in this doc against the provider's live dashboard before you wire anything into production.*

---

## 0. Reading your original document, and the one hard truth about "free"

Your existing architecture document is well-reasoned as a *local-model* pipeline: it leans on self-hosted OCR/layout/embedding models where the constraint is compute, not cost. The redesign you're asking for inverts that constraint — you want the ceiling raised (max accuracy, multi-API chaining, no simplicity bias) while the floor stays at exactly $0. That's a legitimate, buildable target, but it comes with one truth that has to shape every decision below:

> **Free API tiers are not a stable foundation. They are a *fleet* you route across.** In the last seven months alone: Google cut Gemini free-tier quotas twice (50–80% in December 2025, then removed Pro from the free tier entirely in April 2026); Mistral quietly retired the free OCR trial in June 2026 (it's now $1–2 per 1,000 pages — cheap, but not free); OpenRouter's free model roster turns over on a roughly weekly cadence (models silently lose their `:free` tag); Hugging Face's serverless free credits shrank to token amounts that only cover light testing. None of this means "free doesn't work" — it means a pipeline that hard-codes one provider per stage will break within weeks. Every stage below is therefore designed as a **primary + fallback + escalation chain**, not a single API call, and the routing layer treats provider identity as a config value, not an architectural assumption.

This is the single biggest correction to your original design: **provider redundancy is not optional polish, it's the load-bearing wall.**

---

## 1. Ranked priorities, restated as design constraints

1. **Accuracy > everything except cost.** Where two free options differ meaningfully in accuracy, take the slower/more complex one.
2. **$0 hard floor.** Anything that requires a card-on-file trial credit, a time-boxed promo, or a "free while in beta" clause is flagged as *fragile-free*, not *free*, and kept out of the default path.
3. **Speed is not a factor.** This unlocks multi-pass verification (OCR twice, cross-check with a second model, self-consistency reasoning) that a latency-constrained pipeline couldn't afford.
4. **Chaining is encouraged.** Several stages below deliberately run two free models and reconcile disagreements rather than trusting one output.

---

## 2. The free-API landscape as of July 2026 — what's actually still free

| Provider | Still genuinely free? | What you get | The catch |
|---|---|---|---|
| **Google Gemini API (AI Studio)** | Yes, Flash tier only | Gemini 2.5/3 Flash + Flash-Lite: ~10–15 RPM, 250–1,500 RPD, 1M TPM, 1M-token context, multimodal (image/PDF/audio/video in), JSON mode, function calling | **Pro models lost free access in April 2026.** Free-tier prompts may be used to train Google's models — don't route sensitive documents through it on the free key. Quotas have been cut twice in 7 months; treat every number here as a snapshot. |
| **NVIDIA NIM (build.nvidia.com)** | Yes | 100+ hosted open-weight models (DeepSeek, Qwen, Llama, Mistral Large, GLM, Kimi, Nemotron) behind one OpenAI-compatible endpoint, no card required, ~40 RPM account-wide (shared across whatever model you call) | Rate limit is small and shared across *every* model you use simultaneously, not per-model. Some model families need a separate one-click registration. No production SLA. |
| **Groq** | Yes | Llama, GPT-OSS, Whisper, DeepSeek-R1-Distill, Qwen — LPU hardware, extremely fast (300–1,000 tok/s) | ~30 RPM / 6,000 TPM / 1,000–14,400 RPD depending on model. Open-weight models only — no proprietary frontier models. Very limited vision support (Llama 3.2 11B Vision only). |
| **OpenRouter free router (`:free` models)** | Yes, but volatile | Rotating access to Qwen3-Coder, DeepSeek, Llama, GLM, Nemotron, and others through one API | ~20 RPM / 200 RPD **per model**. Individual models lose `:free` status with no warning — this is the least stable link in the whole stack, use it as a *bonus* lane, never a *load-bearing* one. |
| **Cloudflare Workers AI + Vectorize** | Yes | 10,000 "Neurons"/day of hosted open-model inference + a genuinely free vector database (30M queried dimensions/month, 5M stored dimensions) | Small daily compute allowance; best used for the vector store, not as your primary LLM. |
| **OCR.space** | Yes | Dedicated OCR API, 25,000 requests/month, Engine 2 (90–95% accuracy, fast) and Engine 3 (highest accuracy, handwriting + Markdown tables, slower, lower volume ceiling) | 1MB file size cap on the free API tier (5MB on the web UI). US-hosted on free tier (matters if you care about data residency). |
| **Jina AI (Embeddings / Reranker / Reader)** | Yes | 1–10M free tokens on signup, then a **forever-free rate-limited tier** (100 RPM / 100K TPM / 2 concurrent) across embeddings, reranking, and URL-to-Markdown reading | Not a trial — genuinely permanent, just throttled. This is one of the few "forever free" tiers left for embeddings/reranking. |
| **Qdrant Cloud** | Yes | 1GB RAM / 4GB disk free cluster ≈ ~1M vectors at 768 dimensions, no query or write metering, built-in hybrid (dense+sparse/BM25) search | Free cluster has no HA — fine for a project, not for a paying SLA. |
| **Cohere (trial key)** | Fragile-free | 1,000 API calls/month across Command, Embed, Rerank | Explicitly **non-production/non-commercial** per Cohere's own terms — use for evaluation only, don't build the default path on it. |
| **Hugging Face Inference Providers** | Fragile-free | Nominal monthly routed credits (now small — think cents, not dollars), plus a rate-limited legacy Serverless API and ZeroGPU compute (~5 min/day) for Spaces | Free tier has been squeezed hard; still valuable for *self-hosting open-weight models on ZeroGPU* (Donut, LayoutLMv3, PaddleOCR, table-transformer) rather than as a metered inference API. |
| **Mistral OCR** | **No longer free** | The best benchmarked OCR API on the market (~94.9% internal accuracy vs. ~83–89% for Google/Azure), unmatched on tables, handwriting, forms, embedded-image extraction | The Le Chat free trial was retired June 2026. It's now $1–2/1,000 pages (~$1 with batch discount) — genuinely the cheapest paid OCR that exists, but it fails your $0 constraint. Flagged below as an **optional paid escape hatch**, not part of the default chain. |
| **Together AI / DeepInfra / SambaNova Cloud / Cerebras / GitHub Models** | Verify before relying | Historically offered free/trial credits or a limited free tier on selected open models | Terms on these five change often enough (and weren't independently re-verifiable at full confidence in this pass) that I'm not building the default path on them — treat them as *bonus fallback lanes* to add once you've confirmed current terms on their dashboards, not primary infrastructure. |

**Practical reading of this table:** your genuinely durable free foundation is **Gemini Flash + NVIDIA NIM + Groq + OCR.space + Jina + Qdrant Cloud + Cloudflare (Workers AI/Vectorize)**. OpenRouter's free router is a good *bonus* lane for extra throughput and model diversity, not something to depend on. Cohere and HF Inference credits are useful for evaluation, not production defaults. Mistral OCR is the one "if you ever loosen the $0 constraint by even a cent, do this first" upgrade — it alone would fix most of the accuracy gaps in the free-only stack.

---

## 3. Stage-by-stage design

### Stage 1 — File Detection

**Best tool: local, not an API.** File-type detection is a deterministic, offline problem — spending an API call on it would be pure over-engineering. Use `python-magic` (libmagic bindings) or the pure-Python `filetype` library to read magic bytes, cross-checked against the file extension and, for ambiguous cases, MIME sniffing.

- **Alternative:** `mimetypes` (stdlib) as a zero-dependency fallback when `libmagic` isn't installable in your environment.
- **Why local:** magic-byte detection is 100% deterministic for the formats in your list (PDF, DOCX, PPTX, XLSX, images, etc.) — routing this through an LLM would add latency and a failure point for zero benefit.
- **Pros:** instant, free, offline, no rate limit ever.
- **Cons:** none material — the only edge case is a corrupted/truncated file, which should fail closed (route to a manual-review bucket) rather than guess.

### Stage 2 — Document Classification

Two sub-problems, handled differently:

**2a. Structural classification (native-text PDF vs. scanned vs. mixed vs. Office format vs. plain text)** — also local and deterministic. Open the file with PyMuPDF (`fitz`) or `pdfplumber`, extract the text layer, and compute a **text-density heuristic**: characters-per-page above a threshold (e.g., >100 meaningful characters/page, non-garbage ratio) → native PDF; near-zero → scanned; a page-by-page mix (common in scientific papers with scanned figure pages, or contracts with a scanned signature page) → mixed, and each page gets tagged individually rather than the whole document being force-classified one way.

**2b. Semantic classification (is this an invoice, contract, scientific paper, financial report, form...)** — *this* is where a free LLM earns its call, because it changes downstream chunking and extraction behavior (e.g., invoices want key-value extraction, contracts want clause-level chunking, scientific papers want section+figure-aware chunking).
- **Best free tool:** Gemini 2.5 Flash-Lite (free tier) — cheap, fast enough, handles a first-page-plus-metadata prompt well, returns structured JSON classification.
- **Alternative:** Groq `llama-3.1-8b-instant` — even faster, free, good enough for a coarse category label.
- **Why:** both are free, both support JSON-mode/structured output, and this call is cheap (a few hundred tokens of input, a handful out) so it doesn't stress either provider's daily cap even at real document volumes.
- **Pros:** decouples downstream routing logic from brittle filename/heuristic guessing.
- **Cons:** adds one LLM round-trip per document; mitigate by running Stage 2b only on the first 1–2 pages, not the whole document.

### Stage 3 — Parsing (by document type)

This is the stage your original document under-specified as "one parser" — the redesign is a genuine per-type decision tree.

| Document type | Primary free path | Fallback | Why |
|---|---|---|---|
| **Native PDF (real text layer)** | PyMuPDF/`pdfplumber` direct text + layout extraction (local, free, instant) | Gemini 2.5 Flash on the raw PDF bytes (Gemini accepts PDF natively and preserves reading order well) if the local extraction looks garbled (bad font encoding, embedded Type-3 fonts, RTL scripts) | Native-PDF text extraction is a solved local problem 95% of the time; only escalate to an API when the text layer itself is broken. |
| **Scanned PDF / image-only** | OCR.space Engine 2 (fast, 90–95% on clean scans) | Escalate to Engine 3 (handwriting/tables) or a vision-LLM OCR pass (Qwen3-VL via NVIDIA NIM, or Gemini 2.5 Flash) if Engine 2's confidence is low or the page is flagged as handwritten/low-DPI | Two-tier OCR avoids burning your (small) Engine-3/vision-model quota on every clean scanned invoice — reserve the expensive pass for pages that actually need it. |
| **Mixed PDF** | Route page-by-page using the Stage 2a per-page tags — native pages go through the text-layer path, scanned pages go through the OCR chain, then results are merged in original page order | — | Treating a mixed document as one type (as most simple pipelines do) is exactly the kind of accuracy-costing shortcut your priorities explicitly reject. |
| **DOC / DOCX** | `python-docx` (DOCX) / `LibreOffice --headless` conversion to DOCX then `python-docx` (legacy DOC) — both local and free | Gemini 2.5 Flash on a rendered-to-image version, only if the DOCX contains complex embedded objects that `python-docx` can't reach (rare) | Office XML is a solved, well-documented format; there's no accuracy reason to route it through a paid/rate-limited API. |
| **PPT / PPTX** | `python-pptx` for text/notes/tables (local) + render each slide to PNG via LibreOffice and run it through the vision model (Qwen3-VL or Gemini Flash) for on-slide charts/diagrams `python-pptx` can't parse | — | Slides are visual by nature — text extraction alone misses the diagrams, which is often where the actual information is. |
| **XLS / XLSX / CSV / TSV** | `openpyxl` / `pandas` (local, free, exact) | — | Spreadsheets are structured data; there's no OCR/vision step needed unless a sheet embeds a chart image, in which case route that image through Stage 7. |
| **Markdown / TXT / HTML / XML / JSON** | Direct parse (`markdown-it`, stdlib `html.parser`/`lxml`, stdlib `json`) — all local, all free, all deterministic | — | These are already machine-readable; running them through any API would be pure waste. |
| **Scientific papers** | Native-PDF path (Stage 3 row 1) **plus** a dedicated pass through Stage 6 (tables) and Stage 7 (charts/equations) since these documents are disproportionately figure/table/equation-dense | Gemini 2.5 Flash for LaTeX-style equation transcription (multimodal models handle inline math notably better than OCR engines) | Equations and multi-panel figures are the accuracy-critical part of scientific PDFs — the body text is usually the easy part. |
| **Financial reports / legal contracts / invoices / forms** | Native/scanned path per Stage 3 rows above, **plus mandatory Stage 6 table extraction** (financial reports and invoices are table-dense) and a structured-field extraction pass with Gemini Flash or Qwen (JSON-mode, schema-constrained) for forms/invoices where key-value pairs matter more than prose | — | These document types fail silently if you treat them as "just text" — a mis-parsed line-item table in a financial report is a worse failure than a mis-parsed paragraph. |

### Stage 4 — OCR

**Which free OCR API is most accurate right now?** Among genuinely free options, **OCR.space Engine 3** is the strongest dedicated OCR engine (handwriting-capable, Markdown table output, 200+ languages), with **Engine 2** as the faster/higher-volume workhorse for clean printed text. Above that tier, **vision-LLM OCR** (prompting Qwen3-VL or Gemini 2.5 Flash to transcribe an image with a structured-output instruction) now genuinely rivals dedicated OCR engines on messy real-world documents, because these models bring document *understanding* (they know what a table looks like) rather than pure character recognition.

*(Mistral OCR remains the best OCR API that exists at any price — 94.9% in its own internal benchmark vs. ~83% for Google Document AI and ~89% for Azure — but it stopped being free in June 2026. If you ever relax the $0 constraint even slightly, this is the first upgrade to make, at ~$1–2 per 1,000 pages.)*

**When should OCR be skipped?** Whenever Stage 2a already found a usable text layer. Running OCR on a native PDF is not just wasted quota, it's a *quality regression* — you'd be replacing a perfect text layer with a lossy re-recognition of the same text.

**Fallback OCR chain, in order:**
1. **OCR.space Engine 2** — fast first pass on every scanned/image page.
2. **Confidence gate** — OCR.space returns per-line confidence; if average confidence is low, or the output has a high non-dictionary/garbage-character ratio, or the page was flagged as handwritten in Stage 2, escalate.
3. **OCR.space Engine 3** — handwriting- and table-aware re-pass.
4. **Vision-LLM OCR (Qwen3-VL via NVIDIA NIM, or Gemini 2.5 Flash)** — final escalation for pages that still look wrong; prompt it to transcribe *and* self-report a confidence/uncertainty flag on ambiguous words, which is something classical OCR engines can't do.
5. **Reconciliation** — if step 3 and step 4 disagree materially on a page, keep both outputs in the metadata and flag the page for human review rather than silently picking one. This costs nothing extra and prevents silent corruption.

### Stage 5 — Layout Analysis

**Best free approach for preserving headings/tables/columns/reading order/footnotes/captions/lists:** there is no single dedicated "layout API" that's both free and best-in-class anymore (Google Document AI's Enterprise OCR/Layout Parser is accurate but not free; LayoutLMv3/Donut are free but require self-hosting). The pragmatic free-tier answer is a **two-layer approach**:

1. **Structural layer (native PDFs):** PyMuPDF's block/line/span geometry (bounding boxes, font size, indentation) gives you heading-level detection, column ordering, and footnote-region detection heuristically, for free, with no API call. This handles the majority of "reading order + headings + columns" cases in native PDFs directly.
2. **Semantic layer (scanned pages, or native pages where the heuristic is uncertain):** feed the page image to Qwen3-VL or Gemini 2.5 Flash with a structured-output prompt asking for a hierarchical Markdown representation (headings as `#`/`##`, tables as Markdown/HTML tables, captions attached to their figures, footnotes tagged separately, reading order flattened left-to-right-top-to-bottom across columns). Multimodal LLMs are, as of mid-2026, genuinely good at this because they can *see* the page the way a human does rather than inferring structure from a token stream.

This mirrors what Mistral OCR does natively (structure-aware Markdown output with HTML table reconstruction) — you're reconstructing that capability for free by combining a geometry heuristic with a vision-LLM prompt instead of getting it from one purpose-built call.

### Stage 6 — Table Extraction

Recommended decision tree, matching the shape you asked for:

```
Has the page got a native PDF text layer?
├── YES → does it contain a detectable table region (via PyMuPDF/pdfplumber
│         line-and-rectangle geometry, or Camelot's lattice/stream detection)?
│         ├── YES (simple grid table) → Camelot (lattice mode) / pdfplumber
│         │         table extraction — free, local, exact for ruled tables
│         ├── YES (borderless / irregular table) → Camelot (stream mode);
│         │         if output looks malformed → escalate to vision model below
│         └── NO table detected → skip, nothing to extract
└── NO (scanned page / image) → vision-model table extraction:
          prompt Qwen3-VL or Gemini 2.5 Flash to return the table as
          Markdown or HTML (with colspan/rowspan preserved) directly
          from the page image — this is the free-tier equivalent of what
          Mistral OCR 3 does natively with HTML table reconstruction
```

Camelot/pdfplumber cost nothing and are exact on well-formed ruled tables — always try them first on native PDFs. Borderless and multi-row-header tables (common in financial reports) are where local libraries genuinely struggle, and that's exactly where a vision-LLM pass earns its keep, because it's reasoning about the table visually rather than trying to infer cell boundaries from whitespace geometry.

### Stage 7 — Charts & Graphs

**Which free vision model performs best on charts/graphs right now, and why?**

Based on current benchmarks and vendor reporting (mid-2026):
- **Qwen3-VL** (open-weight, free via NVIDIA NIM/OpenRouter/self-host) is the strongest *free* option specifically for OCR-in-image, chart/document parsing, and mathematical-visual reasoning — it beats or matches Gemini 2.5 Pro on several major vision benchmarks and leads open models on math-heavy chart reading (e.g., MathVision-style tasks).
- **Gemini 2.5/3 Flash** (free tier) is the strongest free *closed* option — weaker than Qwen3-VL on some chart/OCR-specific benchmarks but stronger on very-long-context, multi-page, multi-chart documents (1M-token context lets you reason across a whole report's worth of charts in one call).
- **InternVL3** is close behind on general image understanding but trails Qwen3-VL specifically on OCR/chart/math — a fine secondary/cross-check model, not the primary.
- **Llama 4 Scout/Maverick vision and Llama 3.2 11B Vision** (free via Groq/NIM) are usable but generally behind both of the above on document-specific chart reading; good as a fast, extremely cheap tertiary opinion when you want a 3-way vote rather than a strong standalone choice.

**Recommendation:** Qwen3-VL as primary (via NVIDIA NIM's free endpoint, ~40 RPM shared cap), Gemini 2.5 Flash as the escalation path for very-long or very-high-context chart-heavy documents, and — because speed doesn't matter to you — optionally cross-check disagreements with InternVL3 or a Llama-Vision pass for genuinely high-stakes documents (financial reports, scientific papers) where a wrong chart reading would materially matter.

### Stage 8 — Image Understanding (general images, not just charts)

- **Best free VLM:** Qwen3-VL, same reasoning as Stage 7 — it's the strongest all-round free option for document-embedded images (photos in reports, diagrams, screenshots, product images in catalogs).
- **Fallback:** Gemini 2.5 Flash — particularly valuable when an image needs to be reasoned about *in context* with surrounding document text (Gemini's native multimodal input lets you pass the image plus nearby page text in one call, which improves grounding).
- **Second fallback:** NVIDIA's Nemotron Nano VL (free via NIM/OpenRouter) — a smaller, faster reasoning VLM specifically noted for multi-image comprehension, useful when a single page has several images/figures that need to be understood together.

### Stage 9 — Chunking

**Recommendation: hybrid layout-aware + semantic chunking**, not any single strategy in isolation. Reasoning:

- **Pure recursive/character-based chunking** (what most "simple" pipelines default to) is the wrong choice for your document mix — it will happily cut a table in half or split a clause mid-sentence in a legal contract, which is a real accuracy cost given your stated priorities.
- **Pure semantic chunking** (embedding-similarity breakpoints) is good for prose-heavy sections (contract clauses, paper discussion sections) but has no concept of "this is a table, don't touch it."
- **Pure hierarchical chunking** (one chunk per document→section→subsection node) is the right *skeleton* but too coarse alone — a subsection can still be several thousand tokens of prose that needs further splitting.

**The hybrid approach, concretely:**
1. Start from the Stage 5 structural output (headings, sections, tables, figures, footnotes already tagged).
2. **Tables and figures become atomic chunks** — never split, always kept whole (up to a size ceiling; oversized tables get a separate "table summary" chunk plus a pointer to the full table stored as structured JSON alongside the vector store, not crammed into the embedding).
3. **Prose sections** are semantically chunked within their structural boundaries — target ~300–800 tokens with ~10–15% overlap, using embedding-similarity breakpoints (don't cross a heading boundary even if the embeddings look continuous).
4. **Every chunk carries hierarchy metadata** (document → section → subsection, page number, chunk type: prose/table/figure-caption/footnote) enabling **parent-document retrieval** later — you can retrieve a precise small chunk for relevance but expand to the parent section for the LLM's context if the question needs more surrounding detail.

This is more engineering than a naive splitter, but it's the correct trade for your stated priority order (accuracy first, speed irrelevant).

### Stage 10 — Embeddings

Ranked by accuracy-first, free-only:

| Rank | Model / API | Why |
|---|---|---|
| 1 | **Jina Embeddings v3/v4** (free forever tier: 100 RPM/100K TPM after initial free-token grant) | Best price-quality ratio in the market (v3 scores within ~2 MTEB points of models 5–9x its paid price); v4 adds native multimodal (text+image) embeddings, useful for embedding chart/figure captions alongside their images. Genuinely permanent free tier, not a trial. |
| 2 | **Gemini embeddings (`text-embedding-004`/Gemini Embedding, free tier)** | Free, unlimited-feeling in practice at document-processing volumes, decent quality (~63 MTEB), and convenient if you're already calling Gemini elsewhere in the pipeline (one fewer provider to manage). |
| 3 | **NVIDIA NIM NV-Embed** (free, ~40 RPM shared) | Strong quality, but shares NIM's small account-wide rate limit with every other NIM call in your pipeline — best used as an overflow/cross-check embedder, not primary at volume. |
| 4 | **Cohere Embed (trial key)** | High quality, especially multilingual, but explicitly non-production per Cohere's terms — use only for offline evaluation/benchmarking your embedding choice, not in the live pipeline. |

**Recommendation:** Jina v3/v4 as primary (it's the only one of these that's both high-quality *and* unambiguously permanent-free), Gemini embeddings as the fallback/overflow lane when Jina's RPM is exhausted.

### Stage 11 — Vector DB

- **Best free hosted:** **Qdrant Cloud** — the free cluster (1GB RAM/4GB disk, ≈1M vectors at 768 dimensions) has no query or write metering (unlike Pinecone's free tier, which caps you on read/write *units*, not just storage — a heavy-query workload can exhaust Pinecone's free allowance faster than Qdrant's). Qdrant also ships built-in hybrid (dense+sparse/BM25) search and metadata filtering, which you need for Stage 12 anyway.
- **Runner-up / edge-friendly alternative:** **Cloudflare Vectorize** (30M queried vector-dimensions/month, 5M stored dimensions, free forever) — pairs naturally if you're also using Cloudflare Workers AI, and is a genuinely permanent free tier, though with a harder ceiling than Qdrant's resource-based model.
- **Best self-hosted (if you outgrow the free cloud tier and still want $0):** self-hosted **Qdrant** via Docker on your own machine, or **pgvector** on a free-tier Postgres instance (e.g., Supabase's free 500MB database) if you'd rather not run a separate vector-specific service. pgvector with HNSW indexing is genuinely competitive up to a few million vectors and lets you keep vectors alongside your structured metadata (extracted tables, document classification, OCR confidence flags) in one database.

**Recommendation:** Qdrant Cloud free tier as primary; plan the migration path to self-hosted Qdrant (same engine, same query API, zero code change) as the free-tier exit ramp if you ever exceed 1GB.

### Stage 12 — Hybrid Retrieval

**Recommendation: Dense + Sparse (BM25) hybrid, fused, followed by reranking. Skip ColBERT/multi-vector for now.**

- **BM25 (sparse)** catches exact-term matches — critical for your document mix, because legal contracts, financial reports, and invoices are full of exact identifiers (clause numbers, account numbers, ticker symbols, invoice IDs) that dense embeddings routinely fuzzy-match away from.
- **Dense (semantic) embeddings** catch conceptual/paraphrased matches — necessary for the "find the section that discusses X" queries dense retrieval is built for.
- **Hybrid fusion (e.g., Reciprocal Rank Fusion, built into Qdrant's hybrid query API)** combines both without you having to hand-tune a weighting.
- **ColBERT / true multi-vector late-interaction retrieval** is the more accurate approach in the research literature, but for a document set of the size a single engineer/researcher builds and maintains, it's disproportionate engineering and infra cost (larger index, more complex serving) for a marginal accuracy gain over hybrid+rerank — this is one of the places in your original brief I'd flag as **over-engineering risk** if pursued by default. Keep it as a documented future upgrade, gated behind an actual evaluation showing hybrid+rerank isn't good enough, not a day-one requirement.
- **Multi-vector in the lighter sense (parent-child chunk retrieval from Stage 9)** — yes, use this; it's cheap and directly addresses the table/figure/atomic-chunk design from Stage 9.

### Stage 13 — Reranking

- **Best free reranker: Jina Reranker v3** — a 0.6B-parameter model with a genuinely permanent free tier (same 100 RPM/100K TPM as Jina's embeddings), state-of-the-art on BEIR-style benchmarks for its size class, and it shares an API key with your Stage 10 embeddings — one fewer provider to manage.
- **Fallback: Cohere Rerank (trial key)** — higher-ceiling quality on some benchmarks but capped at 1,000 calls/month and explicitly non-production, so treat it as an occasional-escalation/evaluation tool, not the default path.
- **Pattern:** retrieve broadly (top 20–50 candidates via Stage 12's hybrid search), rerank down to the top 5–8 that actually go into the LLM's context. This two-stage retrieve-then-rerank pattern is standard practice and is where most of your realized accuracy gain in the whole retrieval stack will come from — it's worth more than almost any other single stage upgrade.

### Stage 14 — LLM (answering, reasoning, citations, JSON extraction, summarization)

No single free model is best at everything — route by task:

| Task | Recommended free model | Why |
|---|---|---|
| **General Q&A / answering** | Gemini 2.5 Flash (free tier) | Strong all-rounder, 1M context lets you stuff a lot of retrieved chunks in when needed, native JSON mode. |
| **Multi-hop reasoning / complex synthesis** | DeepSeek (V4/R1-class reasoning, via NVIDIA NIM free or Groq's R1-Distill) | Reasoning-tuned models consistently outperform general chat models on multi-step, multi-document synthesis tasks — this is the category where DeepSeek's line has repeatedly matched or beaten larger closed models in independent evaluation. |
| **Citation-grounded answers** | Gemini 2.5 Flash or Qwen (via NIM), with a strict "quote only from provided context, cite chunk IDs" system prompt | Long-context + JSON-mode models are more reliable at faithfully citing chunk IDs than shorter-context or purely conversational models; verify with a cheap second-model cross-check on high-stakes answers (contracts, financial figures) since hallucinated citations are the costliest failure mode in a RAG system. |
| **JSON / structured extraction** (invoices, forms, financial line items) | Qwen (via NVIDIA NIM or Groq) or Gemini Flash-Lite, both with strict JSON-mode/schema constraints | Qwen's instruction-following on structured-output tasks is a particular strength of the family; Flash-Lite is the cheaper/faster option when the schema is simple. |
| **Summarization** (long documents, long-context needs) | Kimi (Moonshot's K2 line, via NVIDIA NIM free) | Kimi is specifically built and marketed around long-horizon, long-context tasks — a natural fit for summarizing a 200-page financial report or full scientific paper in one pass rather than map-reducing across chunks. |
| **Fast/high-volume, low-complexity steps** (classification, routing, short extraction — the Stage 2b/6/13 support calls) | Groq `llama-3.1-8b-instant` or Gemini Flash-Lite | Free, extremely fast, and the quality bar for these support tasks is much lower than for the final answer — don't spend your good models' quota here. |
| **Bilingual / multilingual documents** | GLM (Zhipu, via NVIDIA NIM free) | Notably strong on Chinese/English bilingual instruction-following and reasoning; worth routing to specifically when Stage 2b classification detects non-English-dominant content. |

**Comparison summary (free-tier lens):** Gemini Flash is your best default generalist (context length + native multimodality + JSON mode make it the least-friction choice for most calls). DeepSeek and Qwen (both free via NVIDIA NIM) are your reasoning/extraction specialists. Kimi is your long-context summarization specialist. GLM is your multilingual fallback. Groq's Llama models are your speed/volume workhorse for the cheap support calls that don't need a frontier-adjacent model. Route by task, not by habit — this task-specific routing is worth more accuracy than picking one "best" model and using it everywhere.

---

## 4. The Intelligent Routing System

This is the part your original brief was most insistent on, and rightly so — a single parser is the single biggest accuracy mistake a document pipeline can make. Below is the full routing tree, matching the shape you sketched, filled in with the free-tier decisions from Section 3.

```
                                   ┌───────────────────┐
                                   │   Incoming file    │
                                   └─────────┬──────────┘
                                             │
                                   [Stage 1] File-type detection
                                   (python-magic / filetype — local, free)
                                             │
                                   [Stage 2a] Structural classification
                                   (PyMuPDF text-density heuristic — local)
                                             │
                     ┌───────────────────────┼───────────────────────┬─────────────────────┐
                     │                       │                       │                      │
              Native PDF /             Scanned PDF /           Mixed PDF              Office / structured
              text-layer OK            image-only               (page-by-page)         (DOCX/PPTX/XLSX/CSV/
                     │                       │                       │                  MD/TXT/HTML/XML/JSON)
                     │                       │                       │                      │
        [Stage 2b] Semantic doc-type   [Stage 4] OCR chain:    Route EACH page      [Stage 3] Local parsers
        classification (Gemini         OCR.space Engine 2      through the Native    (python-docx / pptx /
        Flash-Lite / Groq 8B) —        → confidence gate →     or Scanned branch     openpyxl / stdlib) +
        tags: invoice / contract /     Engine 3 (handwriting/  per its own tag,      vision pass on embedded
        scientific paper / financial   tables) → vision-LLM    then MERGE in         charts/images/slide
        report / form / general        OCR (Qwen3-VL / Gemini  original page order   diagrams (Stage 7/8)
                     │                  Flash) if still low          │                      │
                     │                  confidence                  │                      │
                     │                       │                       │                      │
                     └───────────┬───────────┴───────────┬───────────┘                      │
                                 │                        │                                  │
                     [Stage 5] Layout analysis   [Stage 6] Table extraction:                 │
                     (PyMuPDF geometry for       native→Camelot/pdfplumber;                  │
                     native pages; Qwen3-VL/      scanned/borderless→vision-LLM               │
                     Gemini structured-Markdown   table-to-Markdown/HTML                      │
                     for scanned pages)                    │                                  │
                                 │                          │                                  │
                     [Stage 7] Charts & graphs   [Stage 8] Image/figure understanding          │
                     → Qwen3-VL primary,          → Qwen3-VL primary, Gemini Flash /           │
                     Gemini Flash escalation,      Nemotron-Nano-VL fallback                   │
                     InternVL3/Llama-Vision                                                    │
                     cross-check on high-stakes                                                │
                     docs                                                                      │
                                 │                          │                                  │
                                 └──────────────┬───────────┴──────────────────────────────────┘
                                                 │
                                   [MERGE] Unified structured document
                                   (Markdown/JSON: headings, prose, tables,
                                   figure captions, footnotes, OCR-confidence
                                   flags, doc-type tag — all with page/section
                                   provenance metadata)
                                                 │
                                   [Stage 9] Hybrid chunking
                                   (structural skeleton + semantic prose
                                   splitting; tables/figures atomic;
                                   parent/child hierarchy metadata attached)
                                                 │
                                   [Stage 10] Embedding
                                   (Jina v3/v4 primary → Gemini embeddings
                                   overflow)
                                                 │
                                   [Stage 11] Vector store
                                   (Qdrant Cloud free cluster — hybrid
                                   dense+sparse index)
                                                 │
                                        -- ingestion ends here --
                                                 │
                                        -- query time begins --
                                                 │
                                   [Stage 12] Hybrid retrieval
                                   (BM25 + dense, RRF-fused, top 20-50)
                                                 │
                                   [Stage 13] Reranking
                                   (Jina Reranker v3 → top 5-8)
                                                 │
                                   [Stage 14] Task-routed LLM generation
                                   (Gemini Flash / DeepSeek / Qwen / Kimi /
                                   GLM / Groq-Llama, chosen by query type)
                                                 │
                                   ┌─────────────┴─────────────┐
                                   │  Answer + cited chunk IDs   │
                                   └─────────────────────────────┘
```

**The routing principle that makes this "intelligent" rather than a flowchart of guesses:** every branch point above is driven by a *measurable signal* computed in an earlier stage (text density, OCR confidence score, garbage-character ratio, doc-type classification, table-detection geometry), not by file extension alone. This is what lets a single scanned page inside an otherwise-native PDF get routed correctly instead of forcing the whole 40-page document down the expensive OCR path because one page was scanned.

---

## 5. Challenging the assumptions

**Where two free APIs can be chained for a real accuracy gain:**
- OCR.space (fast first pass) -> Qwen3-VL vision-OCR (escalation only on low-confidence pages) — you get OCR.space's speed on the 80% of pages that are clean, and vision-LLM-grade understanding on the 20% that aren't, without paying the compute/rate-limit cost of running the expensive model on everything.
- Hybrid retrieval (BM25+dense) -> reranker (Jina) — this is the single highest-value chain in the entire pipeline; skipping the reranker is the most common corner-cutting mistake in RAG builds, free-tier or not.
- Two independent OCR/vision outputs on the *same* high-stakes page (e.g., a financial table) reconciled by a third, cheap LLM call that flags disagreement rather than silently picking one — costs one extra free call, catches the failure mode that hurts most in this document mix (a silently wrong number in a financial report).

**Where one API consistently beats another (from the research above, not vendor marketing):**
- Qwen3-VL over InternVL3 specifically for OCR/chart/document-parsing tasks — InternVL3 is competitive on general image understanding but measurably behind on the exact skills this pipeline needs most.
- Qdrant's free tier over Pinecone's free tier for this use case — Pinecone's free allowance is metered on read/write *units* (which scale with query volume and metadata-filter complexity), while Qdrant's is metered on cluster resources (flat regardless of query volume) — for a document-heavy, query-heavy RAG system, Qdrant's model is much harder to accidentally exhaust.
- Jina's reranker/embeddings over Cohere's trial for anything beyond evaluation — Cohere's is explicitly non-production per its own terms; Jina's is a genuine forever-free tier at a comparable quality tier.

**Where a fallback parser is worth adding:**
- Native-PDF text extraction *always* needs a "text layer looks garbled" fallback to a vision-LLM read — broken font encoding in native PDFs is common enough (especially in older scanned-then-OCR'd-once-badly documents someone re-saved as "native" PDF) that skipping this check will silently corrupt a meaningful minority of your "easy" documents.
- Camelot/pdfplumber table extraction needs a vision-LLM fallback for borderless and multi-row-header tables — this is explicitly built into Stage 6 above and is worth the extra branch.

**Where you're over-engineering (if pursued by default):**
- ColBERT / true multi-vector late-interaction retrieval — real accuracy upside in the literature, disproportionate infra and index-size cost for a single-engineer-scale document set. Document it as a future upgrade gated on an actual failed-retrieval evaluation, not a day-one build item.
- Running every single page through both OCR.space *and* a vision-LLM OCR pass unconditionally, rather than gating the vision-LLM pass behind a confidence check — doubles your rate-limit consumption for a marginal gain on the ~80% of pages that were already fine.
- A fully generic classifier for all 15+ document *types* in your list with dozens of custom extraction schemas from day one — start with the 4-5 categories that actually recur in your corpus (scientific papers, financial reports, contracts, invoices/forms, general) and add schema-specific handling incrementally; a 15-way classifier prompted before you've seen real documents is speculative complexity.

**Where you're under-engineering (a real gap in the brief as written):**
- **No confidence/QA gate before chunking.** Free-tier OCR (90-95% on clean docs, worse on messy ones) and vision-LLM extraction will make mistakes that propagate silently into your vector store if nothing checks for them. The confidence-gate and dual-OCR-reconciliation steps above aren't optional polish — without them, "highest possible accuracy" is aspirational, not actual.
- **No provider-fallback abstraction.** Given how fast free-tier terms have moved in the last seven months (documented in Section 2), hard-coding "call the Gemini API" instead of "call whichever provider is next in the LLM-task-routing config" will mean rewriting integration code every time a provider tightens its free tier — which, on this evidence, happens roughly every one to two months across *some* provider in your stack. Build the OpenAI-compatible-client abstraction once, config-drive the model/provider list per task, and this becomes a config edit instead of a code change.
- **No handling for what happens when every free option for a stage is simultaneously rate-limited** (plausible during a batch-processing run). The pipeline needs a queue-and-retry layer with exponential backoff *across providers*, not just within one provider's 429 handling — e.g., if Gemini Flash is rate-limited, fall through to Groq's Llama or NVIDIA NIM's Qwen for that specific call rather than blocking.

---

## 6. Reliability, maintenance, and rate-limit summary

| Stage | Primary (free) | Reliability | Maintenance burden | Notes |
|---|---|---|---|---|
| 1. File detection | Local (python-magic) | Very high | Near zero | No API, no rate limit, ever. |
| 2. Classification | Local heuristic + Gemini Flash-Lite | High | Low-medium | Local half is rock-solid; LLM half inherits Gemini's quota volatility. |
| 3. Parsing | Local libs + Gemini/Qwen escalation | High | Low-medium | Same pattern — local-first keeps most volume off any API. |
| 4. OCR | OCR.space -> Qwen3-VL/Gemini | Medium | Medium | OCR.space's 25K/month cap is the tightest ceiling in the whole stack at real volume — budget for it explicitly. |
| 5. Layout | PyMuPDF geometry + vision-LLM | High | Medium | |
| 6. Tables | Camelot/pdfplumber + vision-LLM | High | Medium | |
| 7-8. Charts/images | Qwen3-VL (NIM) -> Gemini Flash | Medium | Medium | NVIDIA NIM's shared 40 RPM account-wide cap is a real constraint once Stages 4-8 are all drawing from it simultaneously — spread across NIM, OpenRouter free, and Groq's limited vision to avoid one shared bucket becoming the bottleneck. |
| 9. Chunking | Local logic | Very high | Low-medium | No external dependency. |
| 10. Embeddings | Jina -> Gemini | High | Low | Jina's forever-free tier is the most stable link in the retrieval half of the stack. |
| 11. Vector DB | Qdrant Cloud | High | Low | Free cluster has no HA — acceptable for a project, plan the self-hosted migration path before it matters. |
| 12. Hybrid retrieval | Qdrant native | Very high | Low | |
| 13. Reranking | Jina Reranker v3 | High | Very low | Shares Jina's key/quota with Stage 10 — one provider relationship to manage. |
| 14. LLM (answering) | Gemini/DeepSeek/Qwen/Kimi/GLM/Groq, task-routed | Medium | High | Highest maintenance stage in the pipeline — six providers, each with independently-moving free-tier terms; this is where the config-driven fallback abstraction from Section 5 pays for itself most. |

**Overall reliability of the free-only stack: solid for a research/portfolio-scale project, genuinely fragile at production scale.** The single highest-leverage engineering investment isn't any one stage above — it's the provider-abstraction/fallback layer, because it's what keeps every other stage's individual fragility from becoming *your* downtime.

---

## 7. The "Frankenstein pipeline" — my actual recommendation

If I were building this today, at $0, optimizing purely for the highest document-understanding accuracy achievable within free-tier limits, here's the concrete stack:

1. **Ingestion & routing:** Python service with `python-magic` + PyMuPDF for detection/classification/native-PDF parsing (all local, zero API spend on the majority of "easy" content).
2. **OCR:** OCR.space Engine 2 as the volume workhorse, gated escalation to Engine 3, gated escalation to Qwen3-VL (via NVIDIA NIM) for anything still uncertain — with disagreement reconciliation flagged rather than silently resolved.
3. **Layout & tables:** PyMuPDF geometry + Camelot/pdfplumber for native content; Qwen3-VL structured-Markdown prompting for scanned/borderless/complex cases.
4. **Charts, graphs, images:** Qwen3-VL primary, Gemini 2.5 Flash for long-context/multi-chart escalation, occasional InternVL3 cross-check on high-stakes financial/scientific documents.
5. **Chunking:** hybrid structural + semantic, tables/figures atomic, full parent-child hierarchy metadata.
6. **Embeddings:** Jina v3/v4 primary, Gemini embeddings overflow.
7. **Vector store:** Qdrant Cloud free cluster, hybrid dense+BM25 index, with a documented self-hosted-Qdrant migration path.
8. **Retrieval:** hybrid search top-30 -> Jina Reranker v3 down to top 6.
9. **Generation:** Gemini 2.5 Flash as default, DeepSeek/Qwen (via NIM) for reasoning-heavy and extraction tasks, Kimi (via NIM) for long-document summarization, GLM (via NIM) for multilingual content, Groq Llama for the cheap high-volume support calls (classification, routing).
10. **The layer that makes all of the above survive contact with reality:** a provider-agnostic OpenAI-compatible client wrapper with a config-driven model list per task and automatic cross-provider fallback on rate-limit/failure, plus a lightweight confidence/QA gate between extraction and chunking that flags (rather than silently accepts) low-confidence OCR, disagreeing dual-model outputs, and malformed table extractions for review.

**Why this combination and not a simpler one:** every provider above is either genuinely forever-free (Jina, Qdrant free cluster, OCR.space, Groq, NVIDIA NIM) or Google's actively-maintained Flash tier (still free, still generous enough for real document volume even after two rounds of quota cuts) — I deliberately did *not* build the default path around OpenRouter's rotating free models, Cohere's trial, or HF's shrunken inference credits, because those are the three links in the free-tier landscape least likely to still look like this in three months. Where the brief asked for chaining APIs for accuracy (OCR escalation, dual-model chart reconciliation, hybrid retrieval + rerank), I did that; where chaining would have added engineering cost with no measurable accuracy return (ColBERT, unconditional double-OCR, a 15-way document classifier before you've seen real data), I flagged it as a documented future upgrade rather than a default.

**The trade-off you're accepting with this design:** meaningfully more integration code than a "one API does everything" pipeline (you're managing roughly seven independent free-tier relationships instead of one paid one), and a hard ceiling on daily document-processing volume set by the tightest link in the chain (realistically OCR.space's 25,000 requests/month, or NVIDIA NIM's shared 40 RPM once several stages are drawing on it concurrently). If your actual volume ever exceeds what this stack can sustain for free, the single cheapest unlock — smaller than any cloud infrastructure spend — is turning on Mistral OCR at roughly $1-2 per 1,000 pages; everything else in this design was chosen so that decision stays optional rather than forced.
