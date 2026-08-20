"""Tests for the dialect-aware SQL retrieval layer.

SQLite tests run against a real temp file — no mocking needed, since it's
just local file I/O. MySQL tests are marked and skipped unless a live
MySQL/MariaDB instance is configured via env vars (see MYSQL_TEST_* below),
since standing one up isn't something CI should require by default; run
them locally with a real server (or MariaDB, which is wire/SQL compatible)
to validate the read-only grant, schema query, and LIMIT handling for real.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from src.core.sql_dialects import DIALECTS, SQLDialectProfile, get_dialect_profile
from src.stages.s12b_sql_retrieval import SQLRetriever, format_schema_rows


def _retriever(engine: str) -> SQLRetriever:
    """Build a retriever whose safety check runs against ``engine``'s dialect.

    The router is unused by ``_is_safe_read_query``, so a bare stand-in is fine.
    """
    retriever = SQLRetriever(router=object())
    retriever._dialect = get_dialect_profile(engine)
    return retriever


class TestSafeReadQuery:
    """`_is_safe_read_query` must accept only single, side-effect-free SELECTs."""

    def test_allows_plain_select(self) -> None:
        assert _retriever("mysql")._is_safe_read_query(
            "SELECT model, price FROM gpu_sales WHERE price < 500"
        )
        assert _retriever("sqlite")._is_safe_read_query(
            "SELECT COUNT(*) FROM gpu_sales"
        )

    def test_blocks_non_select(self) -> None:
        r = _retriever("mysql")
        assert not r._is_safe_read_query("DELETE FROM gpu_sales")
        assert not r._is_safe_read_query("UPDATE gpu_sales SET price = 0")

    def test_blocks_stacked_statements(self) -> None:
        # parse_one used to silently keep only the first statement.
        assert not _retriever("mysql")._is_safe_read_query(
            "SELECT 1; DROP TABLE gpu_sales"
        )

    def test_blocks_load_file(self) -> None:
        assert not _retriever("mysql")._is_safe_read_query(
            "SELECT LOAD_FILE('/etc/passwd')"
        )

    def test_blocks_select_into_variable(self) -> None:
        assert not _retriever("mysql")._is_safe_read_query(
            "SELECT price INTO @v FROM gpu_sales"
        )

    def test_blocks_into_outfile(self) -> None:
        # Rejected either by the INTO guard or by sqlglot failing to parse it.
        assert not _retriever("mysql")._is_safe_read_query(
            "SELECT * FROM gpu_sales INTO OUTFILE '/tmp/x.csv'"
        )

    def test_blocks_garbage(self) -> None:
        assert not _retriever("mysql")._is_safe_read_query("not a query at all;;;")

# --- MySQL integration tests are opt-in, not run by default in CI ---
MYSQL_HOST = os.environ.get("MYSQL_TEST_HOST")
MYSQL_AVAILABLE = bool(MYSQL_HOST)

requires_mysql = pytest.mark.skipif(
    not MYSQL_AVAILABLE,
    reason="Set MYSQL_TEST_HOST (and MYSQL_TEST_USER/PASSWORD/DB) to run against a live MySQL/MariaDB instance.",
)


class TestDialectProfiles:
    """DIALECTS registry and lookup — pure data, no I/O."""

    def test_sqlite_profile_fields(self) -> None:
        profile = get_dialect_profile("sqlite")
        assert profile.key == "sqlite"
        assert profile.sqlglot_dialect == "sqlite"
        assert "sqlite_master" in profile.schema_query

    def test_mysql_profile_fields(self) -> None:
        profile = get_dialect_profile("mysql")
        assert profile.key == "mysql"
        assert profile.sqlglot_dialect == "mysql"
        assert "information_schema.columns" in profile.schema_query

    def test_unknown_engine_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported db_engine"):
            get_dialect_profile("oracle")

    def test_every_profile_key_matches_its_registry_key(self) -> None:
        # Guards against copy-paste drift if a new dialect is added later.
        for registry_key, profile in DIALECTS.items():
            assert profile.key == registry_key


class TestFormatSchemaRows:
    """format_schema_rows is a pure function of (profile, rows) — no DB needed."""

    def test_sqlite_passes_through_create_table_text(self) -> None:
        rows = [
            {"name": "gpu_sales", "sql": "CREATE TABLE gpu_sales (id INTEGER)"},
            {"name": "users", "sql": "CREATE TABLE users (id INTEGER)"},
        ]
        result = format_schema_rows(get_dialect_profile("sqlite"), rows)
        assert "CREATE TABLE gpu_sales (id INTEGER)" in result
        assert "CREATE TABLE users (id INTEGER)" in result

    def test_sqlite_filters_out_sqlite_sequence(self) -> None:
        rows = [
            {"name": "gpu_sales", "sql": "CREATE TABLE gpu_sales (id INTEGER)"},
            {"name": "sqlite_sequence", "sql": "CREATE TABLE sqlite_sequence(name,seq)"},
        ]
        result = format_schema_rows(get_dialect_profile("sqlite"), rows)
        assert "sqlite_sequence" not in result

    def test_mysql_groups_columns_by_table(self) -> None:
        rows = [
            {"table_name": "gpu_sales", "column_name": "id", "data_type": "int"},
            {"table_name": "gpu_sales", "column_name": "model", "data_type": "varchar"},
            {"table_name": "regions", "column_name": "id", "data_type": "int"},
        ]
        result = format_schema_rows(get_dialect_profile("mysql"), rows)
        # Compact "TABLE name\n  col type" format (no parens/commas) — trims
        # fixed per-call schema token cost. See format_schema_rows docstring.
        assert "TABLE gpu_sales" in result
        assert "id int" in result
        assert "model varchar" in result
        assert "TABLE regions" in result

    def test_mysql_empty_rows_produces_empty_string(self) -> None:
        assert format_schema_rows(get_dialect_profile("mysql"), []) == ""

    def test_unsupported_dialect_key_raises(self) -> None:
        bogus = SQLDialectProfile(
            key="oracle", name="Oracle", sqlglot_dialect="oracle", schema_query="", date_functions="", fk_query="",
        )
        with pytest.raises(ValueError, match="Unsupported dialect key"):
            format_schema_rows(bogus, [])


class TestSQLiteQueryPath:
    """Exercises the real db_client code path against a temp SQLite file."""

    @pytest.fixture
    def sqlite_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        db_path = tmp_path / "live_data.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE gpu_sales (id INTEGER PRIMARY KEY, model TEXT, revenue REAL)")
        conn.execute("INSERT INTO gpu_sales VALUES (1, 'RTX 5090', 1999000.0)")
        conn.execute("INSERT INTO gpu_sales VALUES (2, 'RTX 5080', 2497500.0)")
        conn.commit()
        conn.close()

        import src.core.db_client as db_client_mod
        monkeypatch.setattr(db_client_mod, "DB_PATH", db_path)
        return db_path

    @pytest.mark.asyncio
    async def test_select_returns_rows(self, sqlite_db: Path) -> None:
        from src.core.db_client import run_readonly_query
        rows = await run_readonly_query("SELECT * FROM gpu_sales ORDER BY id")
        assert len(rows) == 2
        assert rows[0]["model"] == "RTX 5090"

    @pytest.mark.asyncio
    async def test_limit_is_injected_when_absent(self, sqlite_db: Path) -> None:
        from src.core.db_client import run_readonly_query
        # Can't easily inspect the rewritten SQL string from here, but a
        # LIMIT-free query against a 2-row table returning exactly 2 rows
        # (not erroring, not truncating oddly) confirms injection didn't
        # break the query.
        rows = await run_readonly_query("SELECT * FROM gpu_sales")
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_oversized_limit_is_clamped(self, sqlite_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.core.db_client as db_client_mod
        monkeypatch.setattr(db_client_mod, "MAX_ROWS", 1)
        rows = await db_client_mod.run_readonly_query("SELECT * FROM gpu_sales LIMIT 999999")
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_missing_db_file_returns_empty_list(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.core.db_client as db_client_mod
        monkeypatch.setattr(db_client_mod, "DB_PATH", tmp_path / "does_not_exist.db")
        rows = await db_client_mod.run_readonly_query("SELECT * FROM gpu_sales")
        assert rows == []


@requires_mysql
class TestMySQLQueryPath:
    """Integration tests against a real MySQL/MariaDB instance.

    Requires env vars:
        MYSQL_TEST_HOST, MYSQL_TEST_PORT (default 3306), MYSQL_TEST_DB,
        MYSQL_TEST_READONLY_USER, MYSQL_TEST_READONLY_PASSWORD

    The referenced user must be a real read-only grant (GRANT SELECT only) —
    that's the actual thing worth validating here, not just that a SELECT
    round-trips.
    """

    @pytest.fixture(autouse=True)
    def mysql_settings(self, monkeypatch: pytest.MonkeyPatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "db_engine", "mysql")
        monkeypatch.setattr(settings, "db_host", MYSQL_HOST)
        monkeypatch.setattr(settings, "db_port", int(os.environ.get("MYSQL_TEST_PORT", "3306")))
        monkeypatch.setattr(settings, "db_name", os.environ["MYSQL_TEST_DB"])
        monkeypatch.setattr(settings, "db_readonly_user", os.environ["MYSQL_TEST_READONLY_USER"])
        monkeypatch.setattr(settings, "db_readonly_password", os.environ["MYSQL_TEST_READONLY_PASSWORD"])

    @pytest.mark.asyncio
    async def test_select_returns_rows(self) -> None:
        from src.core.db_client import run_readonly_query
        rows = await run_readonly_query("SELECT * FROM gpu_sales")
        assert isinstance(rows, list)

    @pytest.mark.asyncio
    async def test_schema_query_returns_column_rows(self) -> None:
        from src.core.db_client import run_readonly_query
        profile = get_dialect_profile("mysql")
        rows = await run_readonly_query(profile.schema_query)
        assert all({"table_name", "column_name", "data_type"} <= row.keys() for row in rows)

    @pytest.mark.asyncio
    async def test_write_statement_is_rejected_by_readonly_grant(self) -> None:
        """The real safety net for non-SELECT statements: the DB grant itself.

        run_readonly_query only special-cases SELECT for LIMIT injection —
        it does not itself reject other statement types (that check lives in
        SQLRetriever._is_safe_read_query, one layer up). So this test exists
        specifically to confirm the read-only DB user is configured correctly
        as the actual last line of defense.
        """
        from src.core.db_client import run_readonly_query
        with pytest.raises(Exception, match="denied"):
            await run_readonly_query("DELETE FROM gpu_sales WHERE id = 1")
