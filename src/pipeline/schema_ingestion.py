"""Schema Ingestion — sync the live database schema into the vector store.

Fetches the full database schema, splits it into one chunk per table,
embeds each chunk, and upserts them into Qdrant under
document_id="live_db_schema" with chunk_type=SQL_SCHEMA.

This powers Schema RAG: instead of pasting the entire (potentially huge)
schema into the NL→SQL prompt, the pipeline retrieves only the 5–10 most
relevant tables for the user's question.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from src.core.config import settings
from src.core.db_client import run_readonly_query
from src.core.sql_dialects import get_dialect_profile, SQLDialectProfile
from src.models.schemas import Chunk, ChunkType, DocumentType
from src.stages.s10_embeddings import EmbeddingService
from src.stages.s11_vector_store import QdrantStore
from src.stages.s12b_sql_retrieval import format_schema_rows, format_fk_rows

logger = logging.getLogger(__name__)

SCHEMA_DOCUMENT_ID = "live_db_schema"


def _split_mysql_tables(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Group MySQL information_schema rows into per-table CREATE-TABLE-like text."""
    tables: dict[str, list[str]] = {}
    for row in rows:
        comment = row.get("column_comment") or ""
        suffix = f"  -- {comment}" if comment else ""
        tables.setdefault(row["table_name"], []).append(
            f"  {row['column_name']} {row['data_type']}{suffix}"
        )
    return {
        name: f"TABLE {name} (\n" + ",\n".join(cols) + "\n)"
        for name, cols in tables.items()
    }


def _split_sqlite_tables(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Split SQLite sqlite_master rows into per-table CREATE statements."""
    return {
        row["name"]: row["sql"]
        for row in rows
        if row.get("name") != "sqlite_sequence" and row.get("sql")
    }


def _split_schema_by_table(
    dialect: SQLDialectProfile, rows: list[dict[str, Any]]
) -> dict[str, str]:
    """Return {table_name: schema_text} for each table in the database."""
    if dialect.key == "mysql":
        return _split_mysql_tables(rows)
    if dialect.key == "sqlite":
        return _split_sqlite_tables(rows)
    raise ValueError(f"Unsupported dialect key {dialect.key!r}")


async def sync_live_schema(
    embedding_service: EmbeddingService | None = None,
    vector_store: QdrantStore | None = None,
) -> dict[str, Any]:
    """Fetch the live DB schema, chunk per table, embed, and upsert to Qdrant.

    Returns a summary dict with table count and status.
    """
    from src.core.rate_limiter import get_shared_rate_limiter

    rate_limiter = get_shared_rate_limiter()
    embeddings = embedding_service or EmbeddingService(rate_limiter)
    store = vector_store or QdrantStore(embedding_service=embeddings)

    dialect = get_dialect_profile(settings.db_engine)

    # 1. Fetch schema rows from the live database
    schema_rows = await run_readonly_query(dialect.schema_query, max_rows=20000)
    if not schema_rows:
        return {"status": "error", "message": "No schema rows returned from database"}

    # 2. Split into per-table chunks
    table_schemas = _split_schema_by_table(dialect, schema_rows)

    # 3. Fetch FK info and attach to relevant tables
    fk_map: dict[str, list[str]] = {}
    try:
        if dialect.key == "mysql" and dialect.fk_query:
            fk_rows = await run_readonly_query(dialect.fk_query, max_rows=20000)
        elif dialect.key == "sqlite":
            from src.stages.s12b_sql_retrieval import fetch_sqlite_foreign_keys
            fk_rows = await fetch_sqlite_foreign_keys()
        else:
            fk_rows = []

        for fk_row in fk_rows:
            from_table = fk_row.get("table_name") or fk_row.get("TABLE_NAME", "")
            from_col = fk_row.get("column_name") or fk_row.get("COLUMN_NAME", "")
            to_table = fk_row.get("referenced_table_name") or fk_row.get("REFERENCED_TABLE_NAME", "")
            to_col = fk_row.get("referenced_column_name") or fk_row.get("REFERENCED_COLUMN_NAME", "")
            if from_table and to_table:
                fk_line = f"  FOREIGN KEY ({from_col}) REFERENCES {to_table}({to_col})"
                fk_map.setdefault(from_table, []).append(fk_line)
    except Exception as e:
        logger.warning("Could not fetch FK info for schema sync: %s", e)

    # 4. Build Chunk objects — one per table
    chunks: list[Chunk] = []
    for table_name, schema_text in table_schemas.items():
        # Append FK info to the table's schema text
        if table_name in fk_map:
            schema_text += "\n" + "\n".join(fk_map[table_name])

        chunk_id = f"schema_{table_name}_{hashlib.md5(schema_text.encode()).hexdigest()[:8]}"
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                document_id=SCHEMA_DOCUMENT_ID,
                chunk_type=ChunkType.SQL_SCHEMA,
                content=schema_text,
                token_count=len(schema_text) // 4,  # rough estimate
                document_type=DocumentType.DATABASE,
                source_file=f"live_database/{table_name}",
            )
        )

    if not chunks:
        return {"status": "error", "message": "No tables found in schema"}

    # 5. Delete old schema chunks from Qdrant
    try:
        await store.delete_document(SCHEMA_DOCUMENT_ID)
        logger.info("Deleted old schema chunks from Qdrant")
    except Exception as e:
        logger.warning("Could not delete old schema chunks (may not exist yet): %s", e)

    # 6. Embed all table chunks
    vectors, sparse_vectors = await embeddings.embed_chunks(chunks)

    # 7. Upsert into Qdrant
    await store.upsert(chunks, vectors, sparse_vectors)

    logger.info(
        "Schema sync complete: %d tables embedded and upserted to Qdrant",
        len(chunks),
    )

    return {
        "status": "ok",
        "tables_synced": len(chunks),
        "table_names": sorted(table_schemas.keys()),
    }
