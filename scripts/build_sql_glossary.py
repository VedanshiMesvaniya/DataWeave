#!/usr/bin/env python3
"""Build a schema-aware column glossary for Text-to-SQL.

Reads the existing config/sql_glossary.json (business terms) and
evals/dataweave/dataweave_schema.json (schema), and produces a new
config/sql_column_glossary.json mapping business terms to exact table.column paths.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent.resolve()
REPO = HERE.parent
SCHEMA_FILE = REPO / "evals" / "dataweave" / "dataweave_schema.json"
OLD_GLOSSARY_FILE = REPO / "config" / "sql_glossary.json"
OUT_FILE = REPO / "config" / "sql_column_glossary.json"

# Manual overrides for terms that need complex logic or CASTs.
OVERRIDES = {
    "revenue": {
        "maps_to": "SUM(sales_order_products.rate * sales_order_products.qty)",
        "note": "No revenue column exists. Must derive from rate × qty.",
        "synonyms": ["total sales", "sales value", "turnover"],
    },
    "stock_quantity": {
        "maps_to": "CAST(stock.qty AS UNSIGNED)",
        "note": "stock.qty is VARCHAR — CAST before aggregating.",
        "synonyms": ["inventory", "on-hand", "stock level", "available quantity"],
    },
    "customer": {
        "maps_to": "party.company_name",
        "note": "party table holds both customers and suppliers.",
        "synonyms": ["client", "buyer", "account"],
    },
}


def build_glossary() -> None:
    print(f"Reading schema from {SCHEMA_FILE}")
    schema_data = json.loads(SCHEMA_FILE.read_text())

    print(f"Reading base glossary from {OLD_GLOSSARY_FILE}")
    base_glossary = json.loads(OLD_GLOSSARY_FILE.read_text())

    glossary = {}

    # 1. Apply overrides
    for term, data in OVERRIDES.items():
        glossary[term] = data

    # 2. Add exact matches from schema
    for table in schema_data["tables"]:
        table_name = table["name"]
        for col in table["columns"]:
            col_name = col["name"]
            if col_name in ["id", "created_at", "updated_at", "deleted_at"]:
                continue

            # Look for synonyms in base glossary
            syns = base_glossary.get(col_name, [])

            # Simple heuristic names
            term_name = f"{table_name}_{col_name}"
            if term_name not in glossary and col_name not in OVERRIDES:
                glossary[term_name] = {
                    "maps_to": f"{table_name}.{col_name}",
                    "note": "",
                    "synonyms": syns,
                }

    print(f"Writing column glossary with {len(glossary)} terms to {OUT_FILE}")
    OUT_FILE.write_text(json.dumps(glossary, indent=2))


if __name__ == "__main__":
    build_glossary()
