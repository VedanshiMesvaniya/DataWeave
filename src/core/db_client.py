"""Database Client — thin read-only adapter for live data retrieval.

Supports SQLite (default, local file) and MySQL, selected via settings.db_engine.
Query validation/LIMIT-enforcement and the timeout/row-cap wrapper are shared;
only connection setup and row fetching branch by engine, since the aiosqlite
and aiomysql driver APIs differ in shape (context-managed connection vs.
explicit cursor) enough that unifying them would just be boilerplate.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiosqlite

from src.core.config import DATA_DIR, settings
from src.core.sql_dialects import get_dialect_profile

logger = logging.getLogger(__name__)

# Path to the local SQLite database (only used when db_engine == "sqlite")
DB_PATH = DATA_DIR / "live_data.db"

# Safety limits
QUERY_TIMEOUT_SECONDS = settings.db_query_timeout_seconds
MAX_ROWS = 500


async def run_readonly_query(
    sql: str, params: dict | None = None, max_rows: int | None = None
) -> list[dict[str, Any]]:
    """Execute a read-only SQL query with hard timeouts and row caps.

    Args:
        sql: The SQL SELECT statement.
        params: Optional dictionary/tuple of parameters.

    Returns:
        List of rows as dictionaries.

    Raises:
        asyncio.TimeoutError: If the query exceeds the timeout limit.
        Exception: If the database throws a SQL error (e.g. syntax, missing table).
    """
    engine = settings.db_engine
    profile = get_dialect_profile(engine)
    row_cap = max_rows if max_rows is not None else MAX_ROWS

    if engine == "sqlite" and not DB_PATH.exists():
        logger.warning(f"Database file not found at {DB_PATH}")
        return []

    import sqlglot
    from sqlglot import exp

    try:
        # Enforce hard row cap safely via AST
        tree = sqlglot.parse_one(sql, read=profile.sqlglot_dialect)

        # Only inject LIMIT for SELECT and UNION statements
        if isinstance(tree, (exp.Select, exp.Union)):
            existing = tree.args.get("limit")
            if existing is None:
                tree.set("limit", exp.Limit(expression=exp.Literal.number(row_cap)))
            else:
                try:
                    # If there's already a limit > row_cap, clamp it down
                    if int(existing.expression.this) > row_cap:
                        existing.set("expression", exp.Literal.number(row_cap))
                except (TypeError, ValueError, AttributeError):
                    pass

            # Serialize back to string without trailing semicolons
            sql = tree.sql(dialect=profile.sqlglot_dialect)
    except Exception as e:
        logger.error(f"Failed to parse SQL for limit enforcement: {e}")
        raise ValueError(f"Invalid SQL: {e}")

    try:
        async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
            results = await _execute(engine, sql, params)

            if len(results) >= row_cap:
                logger.warning(f"Query results capped at {row_cap} rows to protect context window.")

            return results

    except asyncio.TimeoutError:
        logger.error(f"SQL query timed out after {QUERY_TIMEOUT_SECONDS}s: {sql}")
        raise
    except Exception as e:
        logger.error(f"SQL execution failed: {e}")
        raise


async def _execute(engine: str, sql: str, params: dict | None) -> list[dict[str, Any]]:
    """Open a read-only connection for the configured engine and run the query.

    This is the one place engine-specific connection/driver differences live.
    """
    if engine == "sqlite":
        # Layer 1 defense: SQLite engine-level Read-Only mode via the URI trick.
        db_uri = f"file:{DB_PATH.resolve()}?mode=ro"
        async with aiosqlite.connect(db_uri, uri=True) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    elif engine == "mysql":
        import aiomysql

        # Layer 1 defense here is NOT a connection-string trick (MySQL has no
        # equivalent) — it's connecting as a dedicated read-only DB user with
        # only SELECT granted (GRANT SELECT ON db.* TO 'readonly_user'@'%';
        # no INSERT/UPDATE/DELETE grants). That's a deployment/DBA step, not
        # application code, and it's enforced server-side even if the AST
        # validation above were ever bypassed.
        conn = await aiomysql.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_readonly_user,
            password=settings.db_readonly_password,
            db=settings.db_name,
            cursorclass=aiomysql.cursors.DictCursor,
        )
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, params)
                rows = await cursor.fetchall()
                return list(rows)
        finally:
            conn.close()

    raise ValueError(f"Unsupported db_engine {engine!r}")
