"""Generate config/sql_relationships.json from a schema JSON.

Many production databases (this one included) ship with NO explicit FOREIGN KEY
constraints, so the Text-to-SQL layer's information_schema FK introspection comes
back empty and the model has to *guess* how tables join — which produces errors
like `Unknown column 'p.product_color_id' in 'on clause'`.

This script extracts inferred relationships from a schema JSON (as produced by the
schema reverse-engineering step) and writes them to config/sql_relationships.json,
which SQLRetriever injects into the SQL-generation prompt as an explicit join map.

Usage:
    python scripts/build_sql_relationships.py evals/dataweave/dataweave_schema.json
    python scripts/build_sql_relationships.py path/to/schema.json --out config/sql_relationships.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "config" / "sql_relationships.json"


def extract(schema: dict) -> list[dict]:
    """Pull relationship rows out of a schema JSON, normalized and deduped."""
    rels = schema.get("relationships", [])
    seen: set[tuple[str, str, str, str]] = set()
    out: list[dict] = []
    for r in rels:
        key = (r.get("from_table"), r.get("from_column"),
               r.get("to_table"), r.get("to_column"))
        if not all(key) or key in seen:
            continue
        seen.add(key)
        out.append({
            "from_table": key[0], "from_column": key[1],
            "to_table": key[2], "to_column": key[3],
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Build sql_relationships.json from a schema JSON")
    ap.add_argument("schema", type=Path, help="schema JSON with a 'relationships' array")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    rels = extract(schema)
    if not rels:
        print(f"No relationships found in {args.schema}. Nothing written.")
        return 1

    payload = {
        "note": (
            f"Inferred join map for database '{schema.get('database', '?')}'. "
            "The database has no explicit FOREIGN KEY constraints; these relationships "
            "are used to guide JOINs in generated SQL. Regenerate with "
            "scripts/build_sql_relationships.py."
        ),
        "relationships": rels,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    tables = {r["from_table"] for r in rels} | {r["to_table"] for r in rels}
    print(f"Wrote {len(rels)} relationships across {len(tables)} tables -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
