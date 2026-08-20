"""Tests for the Schema-Aware Column Registry."""

import pytest
from src.core.sql_column_registry import ColumnRegistry


@pytest.fixture
def sqlite_schema() -> str:
    return """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        name TEXT,
        email TEXT UNIQUE,
        created_at DATETIME
    );
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        total_amount REAL,
        status TEXT
    );
    """


@pytest.fixture
def mysql_schema() -> str:
    # Compact "TABLE name\n  col type" format (no parens/commas) — matches
    # what format_schema_rows actually emits for MySQL.
    return """
TABLE users
  id int(11)
  name varchar(255)
  email varchar(255)
  created_at datetime

TABLE orders
  id int(11)
  user_id int(11)
  total_amount decimal(10,2)
  status varchar(50)
"""


def test_sqlite_parsing(sqlite_schema: str) -> None:
    registry = ColumnRegistry(sqlite_schema, "sqlite")
    assert "users" in registry._tables
    assert "orders" in registry._tables
    assert registry._tables["users"] == {"id", "name", "email", "created_at"}
    assert registry._tables["orders"] == {"id", "user_id", "total_amount", "status"}


def test_mysql_parsing(mysql_schema: str) -> None:
    registry = ColumnRegistry(mysql_schema, "mysql")
    assert "users" in registry._tables
    assert registry._tables["users"] == {"id", "name", "email", "created_at"}


def test_valid_sql_passes_validation(sqlite_schema: str) -> None:
    registry = ColumnRegistry(sqlite_schema, "sqlite")
    sql = "SELECT id, name FROM users WHERE email = 'test@example.com'"
    result = registry.validate_columns(sql)
    assert result.is_valid
    assert not result.errors


def test_valid_sql_with_aliases_passes(sqlite_schema: str) -> None:
    registry = ColumnRegistry(sqlite_schema, "sqlite")
    sql = "SELECT u.name, o.total_amount FROM users u JOIN orders o ON u.id = o.user_id"
    result = registry.validate_columns(sql)
    assert result.is_valid


def test_hallucinated_column_caught(sqlite_schema: str) -> None:
    registry = ColumnRegistry(sqlite_schema, "sqlite")
    sql = "SELECT id, astrological_sign FROM users"
    result = registry.validate_columns(sql)
    assert not result.is_valid
    assert len(result.errors) == 1
    assert "astrological_sign" in result.errors[0]
    assert "users" in result.errors[0]
    assert result.hallucinated_columns == ["astrological_sign"]


def test_hallucinated_qualified_column_caught(sqlite_schema: str) -> None:
    registry = ColumnRegistry(sqlite_schema, "sqlite")
    sql = "SELECT u.name, o.gpu_model FROM users u JOIN orders o ON u.id = o.user_id"
    result = registry.validate_columns(sql)
    assert not result.is_valid
    assert "gpu_model" in result.errors[0]
    assert "orders" in result.errors[0]
    assert result.hallucinated_columns == ["orders.gpu_model"]


def test_unqualified_column_resolved_via_from(sqlite_schema: str) -> None:
    registry = ColumnRegistry(sqlite_schema, "sqlite")
    sql = "SELECT total_amount FROM orders"
    result = registry.validate_columns(sql)
    assert result.is_valid


def test_cte_columns_validated(sqlite_schema: str) -> None:
    registry = ColumnRegistry(sqlite_schema, "sqlite")
    sql = """
    WITH recent_orders AS (
        SELECT user_id, fake_column FROM orders
    )
    SELECT u.name FROM users u JOIN recent_orders r ON u.id = r.user_id
    """
    result = registry.validate_columns(sql)
    assert not result.is_valid
    assert "fake_column" in result.errors[0]
    assert result.hallucinated_columns == ["fake_column"]


def test_alias_from_question_flagged(sqlite_schema: str) -> None:
    registry = ColumnRegistry(sqlite_schema, "sqlite")
    sql = "SELECT status AS technology_used FROM orders"
    question = "What technology is used the most?"
    warnings = registry.validate_aliases(sql, question)
    assert len(warnings) == 1
    assert "technology_used" in warnings[0]


def test_alias_from_schema_not_flagged(sqlite_schema: str) -> None:
    registry = ColumnRegistry(sqlite_schema, "sqlite")
    # 'name' is a real column, even if it's in the question.
    sql = "SELECT name AS name FROM users"
    question = "What is the name of the user?"
    warnings = registry.validate_aliases(sql, question)
    assert len(warnings) == 0


def test_alias_aggregate_flagged(sqlite_schema: str) -> None:
    registry = ColumnRegistry(sqlite_schema, "sqlite")
    sql = "SELECT SUM(total_amount) AS technology_used FROM orders"
    question = "What technology is used the most?"
    warnings = registry.validate_aliases(sql, question)
    assert len(warnings) == 1
    assert "technology_used" in warnings[0]
    assert "total_amount" in warnings[0]
