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
