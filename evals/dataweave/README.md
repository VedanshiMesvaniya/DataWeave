# DataWeave Text-to-SQL Evaluation

An LLM eval for the Text-to-SQL pipeline, grounded in the real **`dataweave`**
ERP schema (70 tables, reverse-engineered from the phpMyAdmin dump). It tests the
questions a real, non-technical business owner would ask — phrased casually —
plus deliberately **hard, twisted** questions that exploit the schema's traps.

## Why these questions are hard

The schema has traps that make "obvious" SQL wrong. The eval targets each one:

| Trap | Example question | What a correct answer must do |
|------|------------------|-------------------------------|
| **No money column on orders** | "total value of all our sales orders?" | derive `SUM(product.rate * qty)` — there is no `total` column |
| **`stock.qty` is `VARCHAR`** | "how much stock do we have?" | `CAST` before summing |
| **Soft deletes everywhere** | "how many products are there?" | exclude `deleted_at IS NOT NULL` |
| **`party` is customer *and* supplier** | "how many suppliers?" | infer supplier = `DISTINCT party_id` in `purchase` |
| **Planned vs actual production** | "how accurate is our planning?" | compare `production.qty` vs `actual_production.apq` |
| **Partial dispatch** | "what's pending to dispatch?" | ordered qty − dispatched qty, per product |
| **3 contacts per party** | "whose birthday this month?" | check `birthdate1`, `birthdate2`, `birthdate3` |
| **Enum codes** | "which cartons are unverified?" | `carton_verify_status = 'P'` |
| **Financial-year partitioning** | "this year's sales" | current `financial_year`, not calendar year |
| **Doc-looking but in DB** | "terms & conditions on categories?" | it's `category.terms_condition` — answer from SQL, not docs |

It also checks **routing** (SQL vs document vs both vs abstain) and
**adversarial** inputs (prompt injection, destructive requests, credential
probes, subjective/contradictory questions).

## Files

- `questions.jsonl` — the question bank (generated). One JSON object per line.
- `build_questions.py` — regenerates `questions.jsonl`. Edit here, not the JSONL.
- `run_eval.py` — offline validator + live LLM-as-judge runner.
- `dataweave_schema.json` — the schema the questions are grounded in.

Each question record:

```json
{"id": "gm-014", "domain": "Sales", "difficulty": "hard_twisted", "type": "trap",
 "question": "what's the total value of all our sales orders?",
 "route": "SQL", "tables": ["sales_order_products", "product"],
 "twist": "there is no amount column; must derive SUM(product.rate*qty)",
 "rubric": "Must NOT invent sales_order.total; derives value from rate*qty."}
```

`route` is the expected routing outcome: `SQL`, `DOC`, `BOTH`, or `ABSTAIN`.

## Running it

### 1. Offline — validate the dataset (no DB, no API keys)

```bash
python evals/dataweave/run_eval.py --offline
```

Confirms every referenced table exists in the schema and prints coverage by
difficulty / route / domain. Good for CI.

### 2. Live — run against the real pipeline + LLM judge

Needs the live DB (`data/live_data.db` or MySQL via env) and provider API keys,
exactly like the app.

```bash
# everything
python evals/dataweave/run_eval.py --out evals/dataweave/results.json

# just the hard ones
python evals/dataweave/run_eval.py --difficulty hard_twisted

# a quick 10-question smoke test
python evals/dataweave/run_eval.py --limit 10
```

For each question the runner:
1. sends it through `QueryPipeline.query()`,
2. records which route actually fired (SQL / DOC / BOTH / ABSTAIN) and the
   generated SQL,
3. grades the answer with an **LLM-as-judge** against the rubric + twist,
4. prints per-difficulty scores, routing accuracy, twist-handled rate, and the
   lowest-scoring questions to fix first.

The judge is a separate `ProviderRouter` so grading is independent of the model
that produced the answer. Pick the judge model with `--judge-task`
(default `classification`).

## Reading the results

- **Routing accuracy** — is the SQL-vs-document decision correct? (This is the
  metric the recent routing fix targets: SQL/both questions must not silently
  fall back to document-only.)
- **Twist-handled rate** — did the pipeline handle the schema trap, or produce
  syntactically fine but semantically wrong SQL?
- **Score by difficulty** — `layman_easy` should be near-perfect; `hard_twisted`
  is where a weak schema prompt shows.

## Extending

Add questions in `build_questions.py` (`_curated()` for depth, `_TABLE_NOUNS`
for per-table breadth), then:

```bash
python evals/dataweave/build_questions.py     # regenerate questions.jsonl
python evals/dataweave/run_eval.py --offline  # re-validate
```
