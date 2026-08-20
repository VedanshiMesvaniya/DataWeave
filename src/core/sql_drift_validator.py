"""SQL Drift Validator — verifies that glossary mappings and relationship maps

match the active database schema without stale, dropped, or misspelled column references.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_FILE = REPO_ROOT / "evals" / "dataweave" / "dataweave_schema.json"
GLOSSARY_FILE = REPO_ROOT / "config" / "sql_column_glossary.json"
RELATIONSHIPS_FILE = REPO_ROOT / "config" / "sql_relationships.json"


def load_schema_tables_and_columns(schema_path: Path | None = None) -> dict[str, set[str]]:
    """Return {table_name: set_of_column_names} from the schema JSON."""
    path = schema_path or SCHEMA_FILE
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    tables: dict[str, set[str]] = {}
    for t in data.get("tables", []):
        t_name = t["name"].lower()
        cols = {c["name"].lower() for c in t.get("columns", [])}
        tables[t_name] = cols
    return tables


def validate_glossary_drift(
    tables: dict[str, set[str]] | None = None,
    glossary_path: Path | None = None,
) -> list[str]:
    """Validate that every maps_to expression in the column glossary references real tables/columns."""
    schema_tables = tables if tables is not None else load_schema_tables_and_columns()
    if not schema_tables:
        return ["Schema is empty or could not be loaded."]

    path = glossary_path or GLOSSARY_FILE
    if not path.exists():
        return [f"Glossary file {path} not found."]

    try:
        glossary = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"Failed to parse glossary JSON: {e}"]

    errors: list[str] = []

    for term, details in glossary.items():
        if not isinstance(details, dict):
            continue
        maps_to = details.get("maps_to", "").strip()
        if not maps_to:
            continue

        # Check if maps_to is a simple table.column or an expression (e.g. SUM(table.col * ...))
        try:
            ast = sqlglot.parse_one(f"SELECT {maps_to} FROM dummy", read="mysql")
            cols = list(ast.find_all(exp.Column))
            if not cols and "." in maps_to:
                parts = maps_to.split(".")
                if len(parts) == 2:
                    t_name, c_name = parts[0].lower().strip(), parts[1].lower().strip()
                    if t_name not in schema_tables:
                        errors.append(f"Glossary term '{term}' references unknown table '{t_name}' in '{maps_to}'")
                    elif c_name not in schema_tables[t_name]:
                        errors.append(f"Glossary term '{term}' references unknown column '{c_name}' on table '{t_name}' in '{maps_to}'")
            else:
                for col in cols:
                    t_name = (col.table or "").lower().strip()
                    c_name = (col.name or "").lower().strip()
                    if t_name and t_name != "dummy":
                        if t_name not in schema_tables:
                            errors.append(f"Glossary term '{term}' references unknown table '{t_name}' in '{maps_to}'")
                        elif c_name and c_name not in schema_tables[t_name]:
                            errors.append(f"Glossary term '{term}' references unknown column '{c_name}' on table '{t_name}' in '{maps_to}'")
        except Exception:
            # Fallback simple split if sqlglot parse fails on custom fragments
            if "." in maps_to:
                parts = maps_to.split(".")
                if len(parts) == 2:
                    t_name, c_name = parts[0].lower().strip(), parts[1].lower().strip()
                    if t_name in schema_tables and c_name not in schema_tables[t_name]:
                        errors.append(f"Glossary term '{term}' references unknown column '{c_name}' on table '{t_name}' in '{maps_to}'")

    return errors


def validate_relationships_drift(
    tables: dict[str, set[str]] | None = None,
    relationships_path: Path | None = None,
) -> list[str]:
    """Validate that every edge in the relationships map connects valid tables and columns."""
    schema_tables = tables if tables is not None else load_schema_tables_and_columns()
    if not schema_tables:
        return ["Schema is empty or could not be loaded."]

    path = relationships_path or RELATIONSHIPS_FILE
    if not path.exists():
        return [f"Relationships file {path} not found."]

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rels = data.get("relationships") if isinstance(data, dict) else data
    except Exception as e:
        return [f"Failed to parse relationships JSON: {e}"]

    if not rels:
        return []

    errors: list[str] = []

    for idx, r in enumerate(rels):
        from_table = (r.get("from_table") or "").lower().strip()
        from_col = (r.get("from_column") or "").lower().strip()
        to_table = (r.get("to_table") or "").lower().strip()
        to_col = (r.get("to_column") or "").lower().strip()

        if not (from_table and from_col and to_table and to_col):
            errors.append(f"Relationship #{idx} is missing fields: {r}")
            continue

        if from_table not in schema_tables:
            errors.append(f"Relationship #{idx}: from_table '{from_table}' not in schema")
        elif from_col not in schema_tables[from_table]:
            errors.append(f"Relationship #{idx}: from_column '{from_col}' not on table '{from_table}'")

        if to_table not in schema_tables:
            errors.append(f"Relationship #{idx}: to_table '{to_table}' not in schema")
        elif to_col not in schema_tables[to_table]:
            errors.append(f"Relationship #{idx}: to_column '{to_col}' not on table '{to_table}'")

    return errors


def validate_glossary_and_relationships(
    tables: dict[str, set[str]] | None = None,
) -> list[str]:
    """Run full drift validation across both glossary and relationships."""
    schema_tables = tables if tables is not None else load_schema_tables_and_columns()
    glossary_errors = validate_glossary_drift(schema_tables)
    rel_errors = validate_relationships_drift(schema_tables)
    return glossary_errors + rel_errors


if __name__ == "__main__":
    errors = validate_glossary_and_relationships()
    if errors:
        print(f"FAILED: Found {len(errors)} drift errors:")
        for err in errors:
            print(f" - {err}")
        raise SystemExit(1)
    else:
        print("OK: Zero drift detected in glossary and relationships!")
