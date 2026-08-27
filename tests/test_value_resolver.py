"""Tests for EntityValueResolver — the real-data grounding layer for NL2SQL.

These tests exercise the pure/local pieces (column-selection heuristic and
in-memory question matching against a pre-seeded cache) rather than hitting a
real database, mirroring how test_sql_column_validation.py tests
ColumnRegistry without a live connection.
"""

import pytest

from src.core.sql_column_registry import ColumnRegistry
from src.core.sql_dialects import get_dialect_profile
from src.core.value_resolver import EntityValueResolver, _is_entity_column


@pytest.fixture(autouse=True)
def _reset_cache():
    """Every test starts with a clean class-level value cache."""
    EntityValueResolver.clear_cache()
    yield
    EntityValueResolver.clear_cache()


def test_is_entity_column_accepts_name_and_category_like_columns():
    assert _is_entity_column("category") is True
    assert _is_entity_column("customer_name") is True
    assert _is_entity_column("status") is True
    assert _is_entity_column("state") is True


def test_is_entity_column_rejects_ids_amounts_and_dates():
    assert _is_entity_column("customer_id") is False
    assert _is_entity_column("category_id") is False  # hint + exclude both match
    assert _is_entity_column("total_amount") is False
    assert _is_entity_column("created_at") is False
    assert _is_entity_column("email") is False


@pytest.fixture
def registry() -> ColumnRegistry:
    schema = """
    CREATE TABLE customers (
        id INTEGER PRIMARY KEY,
        name TEXT,
        state TEXT,
        created_at DATETIME
    );
    CREATE TABLE products (
        id INTEGER PRIMARY KEY,
        category TEXT,
        price REAL
    );
    """
    return ColumnRegistry(schema, "sqlite")


@pytest.mark.asyncio
async def test_resolve_returns_only_values_matching_the_question(registry):
    dialect = get_dialect_profile("sqlite")
    resolver = EntityValueResolver(registry, dialect)

    # Seed the cache directly instead of hitting a live DB, exactly like
    # SQLRetriever._full_schema_cache would be pre-populated by
    # _fetch_full_schema() in production.
    EntityValueResolver._value_cache = {
        ("customers", "name"): ["Acme Corp", "Reliance Digital", "Wipro"],
        ("customers", "state"): ["Gujarat", "Maharashtra"],
        ("products", "category"): ["Electronics", "Furniture"],
    }

    result = await resolver.resolve("what did reliance digital buy last month")
    assert "Reliance Digital" in result
    assert "Acme Corp" not in result
    assert "customers.name" in result


@pytest.mark.asyncio
async def test_resolve_returns_empty_string_when_nothing_matches(registry):
    dialect = get_dialect_profile("sqlite")
    resolver = EntityValueResolver(registry, dialect)
    EntityValueResolver._value_cache = {
        ("customers", "name"): ["Acme Corp"],
    }

    result = await resolver.resolve("show me total revenue this year")
    assert result == ""


@pytest.mark.asyncio
async def test_resolve_returns_empty_string_with_no_registry():
    dialect = get_dialect_profile("sqlite")
    resolver = EntityValueResolver(None, dialect)
    result = await resolver.resolve("anything")
    assert result == ""
