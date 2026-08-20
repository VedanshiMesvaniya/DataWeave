"""Load a phpMyAdmin / MariaDB SQL dump into data/live_data.db (SQLite).

Usage:
    python scripts/load_mysql_dump.py path/to/dump.sql

Key design decisions:
  - Quote-aware semicolon splitter: never splits inside a string literal,
    so INSERT rows containing HTML/CSS (with embedded semicolons, &nbsp;, etc.)
    are kept intact instead of being shredded into garbage fragments.
  - Two-pass transpilation: sqlglot first, regex fallback second.
  - CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE so re-runs are safe.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import sqlglot

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "live_data.db"


# ── Quote-aware statement splitter ───────────────────────────────────────────

def split_statements(sql: str) -> list[str]:
    """Split on semicolons while respecting single-quoted string literals.

    A naive sql.split(';') breaks INSERT statements whose string values
    contain semicolons (HTML, CSS, etc.), producing fragments that look like
    SQL but aren't. This parser tracks open/close quotes correctly.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]

        if in_string:
            buf.append(ch)
            if ch == "\\":             # escape sequence inside string
                i += 1
                if i < n:
                    buf.append(sql[i])
            elif ch == "'":
                if i + 1 < n and sql[i + 1] == "'":   # '' escape
                    buf.append(sql[i + 1])
                    i += 1
                else:
                    in_string = False
        else:
            if ch == "'":
                in_string = True
                buf.append(ch)
            elif ch == ";":
                stmt = "".join(buf).strip()
                if stmt:
                    statements.append(stmt)
                buf = []
            elif ch == "-" and i + 1 < n and sql[i + 1] == "-":
                # Line comment — skip to end of line
                while i < n and sql[i] != "\n":
                    i += 1
                continue
            else:
                buf.append(ch)
        i += 1

    # Last statement (no trailing semicolon)
    stmt = "".join(buf).strip()
    if stmt:
        statements.append(stmt)

    return statements


# ── Pre-processing: strip phpMyAdmin / MySQL session noise ────────────────────

_CONDITIONAL_COMMENT = re.compile(r"/\*!.*?\*/\s*;?", re.DOTALL)
_SET_STMT = re.compile(r"^\s*SET\s+[^;]+;", re.IGNORECASE | re.MULTILINE)
_TRANSACTION = re.compile(
    r"^\s*(START\s+TRANSACTION|COMMIT|LOCK\s+TABLES?|UNLOCK\s+TABLES?)\s*;",
    re.IGNORECASE | re.MULTILINE,
)

def preprocess(sql: str) -> str:
    sql = _CONDITIONAL_COMMENT.sub("", sql)
    sql = _SET_STMT.sub("", sql)
    sql = _TRANSACTION.sub("", sql)
    return sql


# ── Manual MySQL→SQLite cleanup (fallback when sqlglot can't parse) ───────────

_BACKTICK      = re.compile(r"`([^`]+)`")
_UNSIGNED      = re.compile(r"\bUNSIGNED\b", re.IGNORECASE)
_ZEROFILL      = re.compile(r"\bZEROFILL\b", re.IGNORECASE)
_AUTO_INC      = re.compile(r"\bAUTO_INCREMENT\b", re.IGNORECASE)
_CHAR_SET      = re.compile(r"\bCHARACTER\s+SET\s+\S+", re.IGNORECASE)
_COLLATE       = re.compile(r"\bCOLLATE\s+\S+", re.IGNORECASE)
_COL_COMMENT   = re.compile(r"\bCOMMENT\s+'(?:''|[^'])*'", re.IGNORECASE)
_ON_UPDATE     = re.compile(r"\bON\s+UPDATE\s+\S+", re.IGNORECASE)
_ENUM_SET      = re.compile(r"\b(?:ENUM|SET)\s*\([^)]+\)", re.IGNORECASE)
_INT_WIDTH     = re.compile(r"\b(TINYINT|SMALLINT|MEDIUMINT|BIGINT|INT)\s*\(\d+\)", re.IGNORECASE)
_KEY_LINE      = re.compile(r"^\s*(?:UNIQUE\s+)?(?:KEY|INDEX)\s+[^\n]+", re.IGNORECASE | re.MULTILINE)
_TABLE_OPTS    = re.compile(
    r"\)\s*(?:ENGINE\s*=\s*\w+|AUTO_INCREMENT\s*=\s*\d+|DEFAULT\s+CHARSET\s*=\s*\w+|"
    r"COLLATE\s*=\s*\w+|ROW_FORMAT\s*=\s*\w+|COMMENT\s*=\s*'(?:''|[^'])*'|\s)+\s*$",
    re.IGNORECASE,
)
_TRAILING_COMMA = re.compile(r",(\s*\))", re.DOTALL)


def manual_cleanup(stmt: str) -> str:
    s = _BACKTICK.sub(r'"\1"', stmt)
    s = _UNSIGNED.sub("", s)
    s = _ZEROFILL.sub("", s)
    s = _AUTO_INC.sub("", s)
    s = _CHAR_SET.sub("", s)
    s = _COLLATE.sub("", s)
    s = _COL_COMMENT.sub("", s)
    s = _ON_UPDATE.sub("", s)
    s = _ENUM_SET.sub("TEXT", s)
    s = _INT_WIDTH.sub(r"\1", s)
    s = _KEY_LINE.sub("", s)
    s = _TABLE_OPTS.sub(")", s)
    # Clean up trailing commas before closing paren left by removed KEY lines
    s = _TRAILING_COMMA.sub(r"\1", s)
    s = re.sub(r"\n{3,}", "\n", s)

    # Ensure CREATE TABLE uses IF NOT EXISTS so re-runs don't error
    s = re.sub(r"\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS\b)",
               "CREATE TABLE IF NOT EXISTS ", s, flags=re.IGNORECASE)
    # Use INSERT OR IGNORE to skip duplicate primary keys on re-run
    s = re.sub(r"\bINSERT\s+INTO\b", "INSERT OR IGNORE INTO", s, flags=re.IGNORECASE)

    return s.strip()


# ── Transpilation ─────────────────────────────────────────────────────────────

def transpile_statement(stmt: str) -> str | None:
    """Return a SQLite-compatible statement, or None to skip entirely."""
    upper = stmt.upper().lstrip()

    # Skip statements SQLite has no equivalent for
    if upper.startswith(("ALTER TABLE", "CREATE DATABASE", "CREATE SCHEMA", "USE ",
                          "DROP DATABASE", "CREATE INDEX", "CREATE UNIQUE INDEX")):
        return None

    # Try sqlglot first (handles most well-formed MySQL syntax)
    try:
        results = sqlglot.transpile(
            stmt, read="mysql", write="sqlite", error_level=sqlglot.ErrorLevel.WARN
        )
        if results and results[0].strip():
            out = results[0]
            # Patch IF NOT EXISTS and INSERT OR IGNORE even on sqlglot output
            out = re.sub(r"\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS\b)",
                         "CREATE TABLE IF NOT EXISTS ", out, flags=re.IGNORECASE)
            out = re.sub(r"\bINSERT\s+INTO\b", "INSERT OR IGNORE INTO", out, flags=re.IGNORECASE)
            return out
    except Exception:
        pass

    # sqlglot failed — use regex cleanup
    return manual_cleanup(stmt)


# ── Loader ────────────────────────────────────────────────────────────────────

def load(dump_path: Path) -> None:
    print(f"Reading {dump_path} …")
    raw = dump_path.read_text(encoding="utf-8", errors="replace")

    print("Pre-processing …")
    cleaned = preprocess(raw)

    print("Splitting statements (quote-aware) …")
    statements = split_statements(cleaned)
    print(f"  {len(statements)} statements found")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)

    ok = skipped = errors = 0
    error_summary: dict[str, int] = {}   # error message → count

    with con:
        for stmt in statements:
            sqlite_stmt = transpile_statement(stmt)
            if sqlite_stmt is None:
                skipped += 1
                continue
            try:
                con.execute(sqlite_stmt)
                ok += 1
            except sqlite3.OperationalError as e:
                errors += 1
                key = str(e)[:80]
                error_summary[key] = error_summary.get(key, 0) + 1

    con.close()

    print(f"\nDone. Loaded into {DB_PATH}")
    print(f"  ✓ executed : {ok}")
    print(f"  ⊘ skipped  : {skipped}  (ALTER TABLE / CREATE DATABASE / etc.)")
    print(f"  ✗ errors   : {errors}")

    if error_summary:
        print("\nDistinct errors (with counts):")
        for msg, count in sorted(error_summary.items(), key=lambda x: -x[1]):
            print(f"  [{count:>4}×] {msg}")

    # Summary table
    con = sqlite3.connect(DB_PATH)
    tables = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    print(f"\nTables in {DB_PATH.name} ({len(tables)} total):")
    for (name,) in tables:
        count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        print(f"  {name:<45} {count:>8} rows")
    con.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/load_mysql_dump.py path/to/dump.sql")
        sys.exit(1)
    dump_path = Path(sys.argv[1])
    if not dump_path.exists():
        print(f"File not found: {dump_path}")
        sys.exit(1)
    load(dump_path)
