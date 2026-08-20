"""Unit tests for Stage 12b Text-to-SQL retrieval (SQLRetriever).

Focus on the SQL-vs-document routing failure modes:
  * aggregate over no rows (all-NULL row) must fall back, not answer "None";
  * LLM output wrapped in fences/prose must still parse (not fall back to docs);
  * NO_SQL abstention is recognised even when decorated;
  * a real SQL result becomes a pinned RESULT chunk.

The SQLRetriever runs against a REAL temporary SQLite database; only the LLM
(router.chat for task="reasoning") is faked so the SQL string is deterministic.
"""

import sqlite3
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

from src.models.schemas import ChunkType
from src.stages.s12b_sql_retrieval import (
    SQLRetriever,
    _is_all_null,
    _load_relationships,
    _unwrap_sql,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_is_all_null_single_all_null_row():
    assert _is_all_null([{"total": None}]) is True
    assert _is_all_null([{"m": None, "lo": None}]) is True


def test_is_all_null_preserves_count_zero_and_real_data():
    # COUNT(*) over no matches returns 0, not NULL — a legitimate answer.
    assert _is_all_null([{"n": 0}]) is False
    assert _is_all_null([{"total": 100}]) is False
    # Multi-row results are always real data.
    assert _is_all_null([{"x": None}, {"x": None}]) is False
    assert _is_all_null([]) is False


def test_relationships_join_map_disambiguates_columns():
    """The shipped join map must place product_color_id on the line-item tables
    and NOT on `product` — the exact confusion behind the observed
    'Unknown column p.product_color_id' join hallucination."""
    rel = _load_relationships()
    if not rel:
        pytest.skip("config/sql_relationships.json not present in this deployment")
    lines = {ln.split(":", 1)[0].lstrip("- ").strip(): ln for ln in rel.splitlines()}
    # product has no product_color_id
    assert "product_color_id" not in lines.get("product", "")
    # sales_order_products does, joining to product_color
    assert "product_color_id->product_color.id" in lines.get("sales_order_products", "")


def test_relationships_injected_into_prompt():
    """When relationships are configured, the SQL-generation prompt must carry the
    join map so the model doesn't guess joins."""
    from unittest.mock import AsyncMock
    r = SQLRetriever(AsyncMock())
    r._relationships = "- sales_order_products: product_color_id->product_color.id"

    captured = {}

    async def chat(task=None, messages=None, **kw):
        captured["system"] = messages[0]["content"]
        return "SELECT 1"

    r._router.chat = chat

    import asyncio
    asyncio.run(r._generate_sql("q", "TABLE product (...)"))
    assert "Table relationships" in captured["system"]
    assert "product_color_id->product_color.id" in captured["system"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("SELECT 1", "SELECT 1"),
        ("```sql\nSELECT 1\n```", "SELECT 1"),
        ("```\nSELECT 1\n```", "SELECT 1"),
        ("Here is the query: SELECT 1 FROM t", "SELECT 1 FROM t"),
        ("WITH c AS (SELECT 1) SELECT * FROM c", "WITH c AS (SELECT 1) SELECT * FROM c"),
    ],
)
def test_unwrap_sql(raw, expected):
    assert _unwrap_sql(raw) == expected


# ---------------------------------------------------------------------------
# Retriever against a real SQLite DB
# ---------------------------------------------------------------------------

@pytest.fixture
def live_db(tmp_path, monkeypatch):
    """A real SQLite DB wired into db_client via settings + DB_PATH override."""
    db_path = tmp_path / "live_data.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER,
                             amount REAL, order_date TEXT);
        INSERT INTO customers (id,name,city) VALUES (1,'Acme','NYC'),(2,'Globex','LA');
        INSERT INTO orders (id,customer_id,amount,order_date) VALUES
            (1,1,2140000,'2025-03-01'),(2,2,1890500,'2025-05-11');
        """
    )
    con.commit()
    con.close()

    from src.core import config, db_client

    monkeypatch.setattr(config.settings, "db_engine", "sqlite", raising=False)
    monkeypatch.setattr(db_client, "DB_PATH", db_path, raising=False)
    return db_path


def _router_returning(sql: str) -> AsyncMock:
    router = AsyncMock()

    async def chat(task=None, messages=None, **kw):
        return sql

    router.chat = chat
    return router


@pytest.mark.asyncio
async def test_real_sql_result_becomes_pinned_chunk(live_db):
    sql = ("SELECT c.name AS customer, SUM(o.amount) AS total "
           "FROM orders o JOIN customers c ON o.customer_id=c.id "
           "GROUP BY c.name ORDER BY total DESC")
    retriever = SQLRetriever(_router_returning(sql))

    chunks = await retriever.retrieve("top customers by revenue")

    assert len(chunks) == 1
    assert chunks[0].chunk.chunk_type == ChunkType.SQL_RESULT
    assert "Acme" in chunks[0].chunk.content
    assert "SQL Query Executed" in chunks[0].chunk.content


@pytest.mark.asyncio
async def test_aggregate_over_no_rows_falls_back_not_null(live_db):
    """SUM over a period with no data returns [{'total': None}]; the retriever
    must treat that as empty (→ document fallback), never surface 'None'."""
    sql = "SELECT SUM(amount) AS total FROM orders WHERE order_date LIKE '1998%'"
    retriever = SQLRetriever(_router_returning(sql))

    chunks = await retriever.retrieve("total revenue in 1998")

    assert chunks == []  # abstains → pipeline falls back to documents


@pytest.mark.asyncio
async def test_count_zero_is_a_real_answer(live_db):
    """COUNT(*) = 0 is a legitimate answer and must NOT be collapsed to empty."""
    sql = "SELECT COUNT(*) AS n FROM orders WHERE order_date LIKE '1998%'"
    retriever = SQLRetriever(_router_returning(sql))

    chunks = await retriever.retrieve("how many orders in 1998")

    assert len(chunks) == 1
    assert "0" in chunks[0].chunk.content


@pytest.mark.asyncio
async def test_fenced_sql_still_executes(live_db):
    """A query wrapped in a markdown fence must run, not fall back to docs."""
    sql = "```sql\nSELECT name FROM customers ORDER BY name\n```"
    retriever = SQLRetriever(_router_returning(sql))

    chunks = await retriever.retrieve("list customer names")

    assert len(chunks) == 1
    assert "Acme" in chunks[0].chunk.content


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["NO_SQL", "NO_SQL.", "NO_SQL - schema has no such data"])
async def test_no_sql_abstention(live_db, reply):
    retriever = SQLRetriever(_router_returning(reply))
    chunks = await retriever.retrieve("what is the meaning of life")
    assert chunks == []


# ---------------------------------------------------------------------------
# Safety / malformed — every one must abstain (→ []), never raise or mutate data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE customers",                          # not a SELECT
        "DELETE FROM orders",                            # not a SELECT
        "SELECT 1; DROP TABLE customers",                # stacked statement
        "SELECT name FROM customers INTO OUTFILE '/tmp/x'",  # disk write
        "SELECT LOAD_FILE('/etc/passwd') AS x",          # file read
        "UPDATE orders SET amount = 0",                  # not a SELECT
    ],
)
async def test_unsafe_queries_are_blocked(live_db, sql):
    retriever = SQLRetriever(_router_returning(sql))
    chunks = await retriever.retrieve("something")
    assert chunks == []
    # The data is untouched — the read-only path never executed a write.
    con = sqlite3.connect(live_db)
    assert con.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2
    con.close()


@pytest.mark.asyncio
async def test_malformed_sql_falls_back(live_db):
    """A syntactically broken query is retried once, then abstains (→ docs)."""
    retriever = SQLRetriever(_router_returning("SELCT nope FROM"))
    chunks = await retriever.retrieve("broken")
    assert chunks == []


@pytest.mark.asyncio
async def test_nonexistent_column_falls_back(live_db):
    """Valid syntax but a hallucinated column errors at execution → abstains."""
    retriever = SQLRetriever(_router_returning("SELECT made_up_col FROM customers"))
    chunks = await retriever.retrieve("hallucinated column")
    assert chunks == []
