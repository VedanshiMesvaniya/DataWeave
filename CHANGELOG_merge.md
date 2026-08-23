# Merge changelog — current_zip + zip.zip → DataWeave_merged.zip

Base: your `current_zip` (feat/db-setup), unchanged except for the edits below.
Everything not listed here is byte-identical to your current project — nothing
else was touched.

## Fixed
- **Mojibake** (double-encoded em-dashes/arrows, `Ã¢â¬â` etc.) cleaned across
  `src/pipeline/query.py` and `src/stages/s12b_sql_retrieval.py`, including one
  that was user-facing (the document-listing line and a "thinking" trace line).
- **Two real stale glossary entries**, caught by the new drift validator:
  - `party.company_name` → `party.party_name`
  - `revenue` formula referenced `rate` on `sales_order_products` (doesn't
    exist there) → corrected to `product.rate * sales_order_products.qty`
- **False-positive empty-result collapse**: a genuine single row with a
  legitimately NULL value (e.g. `SELECT discount_code WHERE id=123`) was being
  wrongly treated as "0 rows" and retried/dropped. Replaced the blunt
  all-NULL check with an AST check that only collapses when the query is
  actually an aggregate with no GROUP BY.
- **Two stale unit tests** (`test_mysql_parsing`,
  `test_mysql_groups_columns_by_table`) still asserted the old parenthesized
  schema format from before that was already changed to the compact format —
  updated to match current behavior. (Pre-existing failures, not introduced by
  this merge — confirmed by running them against your original zip first.)

## Added
- `src/core/sql_drift_validator.py` + `.github/workflows/sql-drift-check.yml`
  — runs on every push/PR, parses every glossary formula and all relationship
  edges with sqlglot and confirms they still resolve against
  `evals/dataweave/dataweave_schema.json`. Currently passing with 0 drift.
- 11 new glossary terms from zip.zip (`invoiced_revenue`, `tax_amount`,
  `transporter`, `quotation`, `vendor`, `opening_balance`,
  `dispatched_quantity`, `purchase_quantity`, `lead_inquiry`,
  `stock_adjustment`, `packed_cartons`) — each individually validated against
  your live schema columns before inclusion.
- 5 new business-rule bullets in the SQL system prompt: soft-delete filtering,
  financial-year handling, revenue-vs-invoiced disambiguation, name-vs-ID
  matching.
- SQL retry budget: 2 attempts → 3 (only affects the validation/execution-error
  retry path — the "empty result → one retry → fall back to documents"
  behavior is unchanged).
- Expanded the SQL safety blocklist with DoS-capable functions (`SLEEP`,
  `BENCHMARK`, `GET_LOCK`, etc.) and added `UNION` support to the safety
  checker (paired with the matching LIMIT-injection fix in `db_client.py` so a
  legitimate "combine two lists" query isn't blocked, without opening a gap in
  row-limit enforcement).
- `<think>...</think>` tag stripping in SQL response parsing — a second,
  independent safety net alongside the existing `enable_thinking: false` +
  token-floor fix, in case a thinking model ignores that flag.
- Bounded (256-entry) LRU result cache with deep-copy on read/write, replacing
  the unbounded dict — removes a slow memory-leak risk on a long-running
  process; behavior otherwise unchanged (still respects
  `sql_result_cache_ttl_seconds`).
- `last_query_status` attribute on `SQLRetriever` (`success` /
  `empty_result` / `not_applicable` / `failed`) for future observability —
  purely additive, doesn't change control flow.
- `_MAX_SCHEMA_CHARS` truncation-on-huge-schema behavior (already in
  current_zip) confirmed as the "hybrid" approach per your decision — **no
  Schema-RAG / vector top-k was reintroduced.**
- SQLite double-quote-literal edge case and a metric-word alias allowance
  (`total_revenue` aliasing an `amount` column no longer false-flags) in
  `sql_column_registry.py` — both strictly loosen false-positive warnings,
  never introduce new blocking behavior.
- `max_tokens` for SQL generation: 1024 → 2048 (more headroom for complex
  joins now that thinking-token leakage is already guarded against).

## Explicitly NOT changed (and why)
- **Provider config** (`provider_client.py`, `providers.yaml`): kept
  current_zip's version as-is. It's dated Aug 19 2026 with sourced reasoning
  per model, already has the anti-thinking-leak fix, and zip.zip's provider
  setup is the older snapshot — adopting it would be a downgrade.
- **Schema-RAG** (vector top-k + 1-hop expansion) from zip.zip: not adopted,
  per your decision. This is the same category of approach that caused the
  original silent-table-omission bug you'd already fixed once.
- **"Return empty result as a confirmed chunk" behavior** from zip.zip
  (instead of falling back to document search): not adopted. This is a real
  design trade-off current_zip's own code explicitly argued against — trusting
  an empty SQL result as authoritative risks a confident "no data" answer
  overriding a correct document-based one. Not something to change silently;
  flag if you want this reconsidered separately.
- `schema_ingestion.py` / `s11_vector_store.py` schema-RAG enrichment code
  from zip.zip: skipped, since it only feeds the Schema-RAG path that wasn't
  adopted — including it would just be dead code.
- Scoped-relationships formatting from zip.zip: skipped — only useful when
  schema is trimmed to a subset of tables; with full-schema-always, "active
  tables" is effectively all tables, so it would add complexity for no
  benefit.

## Test status
- `py_compile` passes on every edited file.
- Full suite: 218 passed, 3 skipped, 5 failed — all 5 failures are
  environmental (no live MySQL/SQLite DB, no Jina API key, no `qdrant_client`
  installed in this sandbox, one pre-existing stale test asserting old model
  names from before your last provider update) and were failing identically
  before this merge. None were introduced by these changes.
- `python -m src.core.sql_drift_validator` passes with 0 drift errors.

## What I'd suggest deciding on next (not applied)
- Whether to also adopt zip.zip's "confirmed-empty chunk" behavior instead of
  the current fallback-to-documents-on-empty path — that's a real semantic
  trade-off, not a bug fix, and deserves its own decision like the schema-RAG
  question did.

---

## Addendum — 2026-08-22 (post-merge, unrelated to the merge above)

Everything above this line documents the original current_zip + zip.zip merge
and is left as-is for accuracy. This addendum logs a later, independent
change made directly on `feat/modify`, not part of that merge.

### Added
- `src/core/query_cache.py` — an in-memory, per-chat `RetrievalCache` that
  skips vector retrieval, reranking, and SQL generation for a repeat (or
  trivially reworded) question within the same chat, reusing the previously
  fetched chunks. Generation (the answer itself) is never cached and always
  runs fresh. Keyed by `(chat_id, normalized question)`, 30-minute TTL,
  50-entry-per-chat cap, filtered queries always bypass it, and it's fully
  invalidated on any ingestion mutation (upload, replace, delete, folder
  scan) or chat deletion.
- `tests/test_query_cache.py` — 9 tests covering normalization, hit/miss
  scoping, no-chat-id no-op, invalidation, and TTL expiry.
- `chat_id` threaded through `QueryPipeline.query()` / `query_stream()` and
  `src/api/ui.py`'s `send_message` / `send_message_stream` endpoints; a cache
  hit surfaces as a `"Reused earlier retrieval"` step in the streaming
  reasoning trace.

### Test status
- Full suite: 230 passed (4 pre-existing failures reproduced identically
  with this change reverted — unrelated test-order pollution in
  `test_sql_retrieval.py` / `test_provider_fallback.py`, not caused by this
  change).

### Docs updated alongside this change
- `README.md` — new subsection under the 14-stage pipeline; `query_cache.py`
  added to the repo-layout tree.
- `docs/ARCHITECTURE.md` — new core-engine entry for `query_cache.py`; a note
  in the Retrieval Pipeline section on where the cache check sits relative to
  stages 12–14.

---

## Addendum 2 — 2026-08-22 (voice input, unrelated to the changes above)

### Added
- `src/core/speech.py` — `SpeechTranscriber` / `get_transcriber()`, a thin,
  lazily-loaded wrapper around `faster_whisper.WhisperModel`
  (`large-v3-turbo` by default, CPU + int8 out of the box). Deliberately
  isolated from the RAG stack: it only knows `audio path -> text`, never
  imports `QueryPipeline`/the provider router/Qdrant, and the query pipeline
  never imports it either — transcribed text re-enters the system as an
  ordinary question string via the existing chat/query endpoints, same as
  typed input.
- `src/api/speech.py` — `POST /api/transcribe`: multipart audio upload ->
  `{"text": "..."}`. Audio is written to a temp file for the duration of the
  request and always deleted afterwards; nothing is persisted. Rejects
  unsupported extensions and oversized uploads (`MAX_AUDIO_UPLOAD_MB`,
  default 25MB).
- New settings in `src/core/config.py`: `whisper_model_size`,
  `whisper_device` (`cpu`/`cuda`), `whisper_compute_type`
  (`int8`/`float16`), `max_audio_upload_mb`. Documented in `.env.example`.
- `faster-whisper>=1.0` added to `pyproject.toml` dependencies. No API key
  required — the model downloads once from the public
  `openai/whisper-large-v3-turbo` Hugging Face repo and is cached on disk.
- Frontend: a microphone button next to Send in `InputBox.jsx`
  (`.composer__mic` in `globals.css` — idle / pulsing-red while recording /
  spinner while transcribing). `DataWeave_UI/src/utils/useVoiceRecorder.js`
  wraps the browser's `MediaRecorder` API and posts the recorded clip to
  `/api/transcribe` via a new `transcribeAudio()` in `services/api.js`.
  `Chat.jsx` appends the returned text into the existing composer value —
  voice input is never sent straight to the RAG pipeline; the user can edit
  or delete it before pressing Send, exactly like typed text.
- `tests/test_speech.py` — 6 tests covering the missing-dependency path,
  successful transcription (segment joining), empty-speech rejection,
  load-once-per-process behavior, singleton reuse, and the async wrapper.
  faster-whisper itself is mocked; no model weights are downloaded in tests.

### Test status
- `tests/test_speech.py`: 6/6 passed.
- Frontend: `npm run build` completes cleanly with the new mic button and
  hook in place; built output in `frontend/` regenerated accordingly.

### Docs updated alongside this change
- `README.md` — new "Voice input (speech-to-text)" subsection and a bullet
  under Frontend highlights; `speech.py` / `useVoiceRecorder.js` added to the
  repo-layout tree.
- `docs/ARCHITECTURE.md` — new entries for `src/core/speech.py` and
  `src/api/speech.py` in sections 2 and 3; `InputBox.jsx`, `api.js`, and the
  new `useVoiceRecorder.js` updated in the Frontend Architecture section.

---

## Addendum 3 — 2026-08-23 (live waveform + explicit no-audio-retention, follow-up to Addendum 2)

### Added
- `DataWeave_UI/src/components/VoiceWaveform.jsx` — a 5-bar live equalizer
  shown next to the mic button while recording (Claude-style), driven by the
  actual mic signal rather than a canned animation.
- `useVoiceRecorder.js` extended with a Web Audio `AnalyserNode` (via a
  `MediaStreamSource` on the same mic stream already used for recording,
  connected only to the analyser — never to `audioContext.destination`, so
  nothing is played back or stored) and a new `getLevels()` accessor that the
  waveform component polls from its own `requestAnimationFrame` loop. Kept
  out of React state deliberately, since a 30-60fps audio readout shouldn't
  force a composer re-render every frame.
- `InputBox.jsx` / `Chat.jsx` updated to render `VoiceWaveform` next to the
  mic button (`composer__mic-group`) and thread `getMicLevels` through.
- `.composer__mic-group` / `.composer__waveform` / `.composer__waveform-bar`
  in `globals.css`.

### Changed (no behavior change, added explicit guarantees)
- `useVoiceRecorder.js`: added `cleanupAudioAnalysis()`, closing the
  `AudioContext` on stop/error alongside the existing stream cleanup;
  `mediaRecorderRef` and the chunks array are explicitly nulled/cleared in
  `onstop` right after the final `Blob` is built, so the recorded audio
  bytes exist in exactly one place (the local `blob` variable) for exactly
  as long as the transcription upload takes, then fall out of scope.
- Confirmed (no code change needed — already true) that
  `src/api/speech.py`'s temp file is deleted in a `finally` block that runs
  on every code path (success, `TranscriptionError`, or unexpected
  exception) before the response returns, so no audio is ever persisted to
  `data/` or anywhere else server-side.

### Test status
- `npm run build` passes cleanly with the new component and hook changes;
  built output in `frontend/` regenerated accordingly.
- No backend changes in this addendum — `tests/test_speech.py` (Addendum 2)
  still covers the transcription service untouched.

### Docs updated alongside this change
- `README.md` — Voice input subsection expanded with an explicit
  "audio is never kept" paragraph describing both sides of the guarantee.
- `docs/ARCHITECTURE.md` — `VoiceWaveform.jsx` added to the Frontend
  Architecture component list; `useVoiceRecorder.js` description updated for
  the analyser/cleanup behavior; `speech.py` API entry expanded with the same
  explicit no-persistence guarantee.

---

## Addendum 4 — 2026-08-23 (waveform redesign: full-width reactive line, replacing the small dot/bar equalizer)

### Changed
- `VoiceWaveform.jsx` rewritten from a small 5-bar equalizer next to the mic
  button into a full-width SVG line trace that replaces the text input
  itself while recording. The path is built from the mic's actual
  time-domain waveform (an oscilloscope-style readout), not a bucketed
  volume level, so it visibly tracks voice pitch and loudness rather than
  just "louder = taller bars."
- `useVoiceRecorder.js`: analyser now reads `getByteTimeDomainData` (256
  samples, normalized to -1..1) via a new `getWaveform()` accessor, replacing
  the old frequency-bucketed `getLevels()`. `FFT_SIZE` raised from 64 to 256
  for a smoother line.
- `InputBox.jsx`: while `micStatus === 'recording'`, the `TextareaAutosize`
  is swapped out for `<VoiceWaveform>` entirely (matching the reference
  "whole box" look) rather than showing a small waveform alongside the
  textbox; the mic button itself stays in the footer, still pulsing red.
  `getMicLevels` prop renamed to `getMicWaveform` throughout
  (`Chat.jsx` → `InputBox.jsx` → `VoiceWaveform.jsx`).
- `globals.css`: `.composer__mic-group` / `.composer__waveform` /
  `.composer__waveform-bar` replaced with `.composer__waveform-full` (the
  `<svg>` container, `width: 100%`) and `.composer__waveform-line` (the
  `<path>` — `stroke: var(--text-secondary)` for automatic light/dark
  theming, `vector-effect: non-scaling-stroke` to keep line thickness
  correct under the SVG's non-uniform `preserveAspectRatio="none"` scale).

### Test status
- `npm run build` passes cleanly; built output in `frontend/` regenerated.
- No backend changes in this addendum.

### Docs updated alongside this change
- `README.md` — Voice input subsection and Frontend-highlights bullet
  updated to describe the full-width reactive line instead of the small
  equalizer.
- `docs/ARCHITECTURE.md` — `VoiceWaveform.jsx` and `useVoiceRecorder.js`
  entries in the Frontend Architecture section rewritten for the new
  time-domain/SVG-line approach.

---

## Addendum 5 — 2026-08-23 (waveform restyle: filled gradient blob, Siri-style, on-theme colors)

### Changed
- `VoiceWaveform.jsx` rewritten again: from a single stroked line trace into
  a filled, closed gradient shape (mirrored top/bottom envelopes around a
  center line) that sits as a thin line at rest and swells into smooth,
  rounded lobes with loudness — matching a Siri-style voice-input reference.
  Loudness is computed as RMS over `getWaveform()`'s samples (not a raw
  instantaneous waveform value), eased frame-to-frame (`SMOOTHING = 0.35`)
  so it swells/settles smoothly, and pushed through a small 64-point
  scrolling history buffer so multiple lobes move across the box as you
  speak rather than one static shape reacting to only the current instant.
  The closed outline is smoothed with a lightweight "quadratic through
  midpoints" technique (cheap enough to rebuild every animation frame) and
  filled via a horizontal `<linearGradient>`.
- **Color palette is fully theme-driven, not hardcoded**: the gradient's
  stops are `var(--text-secondary)` (faint, at the flat edges) →
  `var(--blob-a)` → `var(--accent)` → `var(--blob-b)` → `var(--blob-c)` →
  `var(--text-secondary)`. These are the same CSS variables the app already
  uses for its ambient background blobs (`globals.css`, all three themes:
  default dark, `academic-dark`, `academic-light`), so the waveform
  automatically matches whichever theme/color scheme is active with zero
  new color definitions.
- A second copy of the same path, blurred (`filter: blur(3.5px)`) and at
  lower opacity, renders beneath the crisp fill for a soft glow layer.
- Gradient `<linearGradient>` id is generated via `useId()` (React-Compiler-
  safe / render-pure), not `Math.random()`, since a raw random value read
  during render was flagged by the repo's stricter hook-purity lint rules.
- `globals.css`: `.composer__waveform-line` replaced with
  `.composer__waveform-blob` / `.composer__waveform-glow`; container
  min-height raised from 24px to 56px to give the blob room to swell
  without clipping (intentionally taller than the single-line textarea it
  replaces, matching the reference's visual presence).
- No change to `useVoiceRecorder.js` in this addendum — still exposes
  `getWaveform()` (time-domain samples), which this component now consumes
  differently (RMS-over-history instead of point-by-point line tracing).

### Test status
- `npm run build` passes cleanly; built output in `frontend/` regenerated.
- No backend changes in this addendum.

### Docs updated alongside this change
- `README.md` — Voice input subsection and Frontend-highlights bullet
  updated to describe the filled gradient blob and its theme-driven colors.
- `docs/ARCHITECTURE.md` — `VoiceWaveform.jsx` entry in the Frontend
  Architecture section rewritten for the blob/gradient/glow approach.
