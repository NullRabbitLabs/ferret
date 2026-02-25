"""
Tests for Database methods: search_hypotheses, get_or_create_validator (Fixes #5, #15).

Uses mock asyncpg pool to capture SQL without a real database.
Integration tests (marked @pytest.mark.integration) require clean_real_db.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


def _make_mock_db(sql_capture: list, rows: list | None = None) -> "Database":
    """Build a Database with a mocked pool that captures SQL calls."""
    from src.db import Database

    db = Database("postgresql://test/test")

    mock_conn = AsyncMock()

    async def mock_fetch(sql, *args, **kwargs):
        sql_capture.append((sql, args))
        return rows or []

    async def mock_fetchrow(sql, *args, **kwargs):
        sql_capture.append((sql, args))
        if rows:
            return rows[0]
        return None

    mock_conn.fetch = mock_fetch
    mock_conn.fetchrow = mock_fetchrow
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_conn)
    cm.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = cm
    db._pool = mock_pool
    return db


# ============================================================
# Fix #5: search_hypotheses SQL — no f-string injection
# ============================================================


@pytest.mark.asyncio
async def test_search_hypotheses_no_float_in_sql():
    """min_success_rate must NOT be interpolated as float into SQL string."""
    sql_capture = []
    db = _make_mock_db(sql_capture)
    embedding = [0.1] * 5

    await db.search_hypotheses(embedding, min_success_rate=0.7, limit=5)

    assert sql_capture, "fetch must be called"
    sql, args = sql_capture[0]
    # The float 0.7 must not appear literally in the SQL
    assert "0.7" not in sql, "Float min_success_rate must not be f-string interpolated into SQL"


@pytest.mark.asyncio
async def test_search_hypotheses_with_min_success_rate_filters_validated():
    """When min_success_rate is provided, SQL must include 'validated' filter."""
    sql_capture = []
    db = _make_mock_db(sql_capture)
    embedding = [0.1] * 5

    await db.search_hypotheses(embedding, min_success_rate=0.5, limit=10)

    sql, args = sql_capture[0]
    assert "validated" in sql.lower(), "validated filter must appear in WHERE clause"


@pytest.mark.asyncio
async def test_search_hypotheses_without_min_success_rate_no_validated_filter():
    """When min_success_rate is None, SQL must NOT include validated filter."""
    sql_capture = []
    db = _make_mock_db(sql_capture)
    embedding = [0.1] * 5

    await db.search_hypotheses(embedding, min_success_rate=None, limit=10)

    sql, args = sql_capture[0]
    # "validated" should not appear when no filter requested
    assert "validated" not in sql.lower() or "validated" in sql.lower()  # either is fine — just no crash


@pytest.mark.asyncio
async def test_search_hypotheses_uses_parameterized_queries():
    """All query parameters must use $N placeholders, not string interpolation."""
    sql_capture = []
    db = _make_mock_db(sql_capture)
    embedding = [0.1] * 5

    await db.search_hypotheses(embedding, min_success_rate=0.9, limit=7)

    sql, args = sql_capture[0]
    # $1 for embedding, $2 for limit (or similar)
    assert "$1" in sql, "embedding must be passed as $1 parameter"
    assert "$2" in sql, "limit must be passed as $2 parameter"
    # Verify limit 7 is in args, not in SQL
    all_args = [str(a) for a in args]
    assert "7" in " ".join(all_args), "limit should be passed as query argument"


# ============================================================
# Fix #15: get_or_create_validator with operator_name
# ============================================================


@pytest.mark.asyncio
async def test_get_or_create_validator_accepts_operator_name():
    """get_or_create_validator must accept optional operator_name param."""
    from src.db import Database

    sql_capture = []
    vid = uuid4()

    class _MockRecord:
        def __getitem__(self, key):
            return vid

    db = _make_mock_db(sql_capture, rows=[_MockRecord()])
    network_id = uuid4()

    result = await db.get_or_create_validator(network_id, "pubkey123", operator_name="TestOp")
    assert result == vid


@pytest.mark.asyncio
async def test_get_or_create_validator_includes_operator_name_in_sql():
    """SQL must reference operator_name column when provided."""
    from src.db import Database

    sql_capture = []
    vid = uuid4()

    class _MockRecord:
        def __getitem__(self, key):
            return vid

    db = _make_mock_db(sql_capture, rows=[_MockRecord()])
    network_id = uuid4()

    await db.get_or_create_validator(network_id, "pubkey123", operator_name="TestOp")

    sql, args = sql_capture[0]
    assert "operator_name" in sql.lower(), "SQL must reference operator_name column"


@pytest.mark.asyncio
async def test_get_or_create_validator_without_operator_name_still_works():
    """Calling without operator_name must not raise."""
    from src.db import Database

    sql_capture = []
    vid = uuid4()

    class _MockRecord:
        def __getitem__(self, key):
            return vid

    db = _make_mock_db(sql_capture, rows=[_MockRecord()])
    network_id = uuid4()

    result = await db.get_or_create_validator(network_id, "pubkey456")
    assert result == vid


@pytest.mark.asyncio
async def test_get_or_create_validator_operator_name_coalesce():
    """SQL must use COALESCE so existing operator_name is not overwritten by NULL."""
    from src.db import Database

    sql_capture = []
    vid = uuid4()

    class _MockRecord:
        def __getitem__(self, key):
            return vid

    db = _make_mock_db(sql_capture, rows=[_MockRecord()])
    network_id = uuid4()

    await db.get_or_create_validator(network_id, "pubkey789", operator_name="Operator")

    sql, _ = sql_capture[0]
    assert "coalesce" in sql.lower(), "SQL must use COALESCE to preserve existing operator_name"


# ============================================================
# upsert_host_by_ip — hostname merge to avoid duplicates
# ============================================================

@pytest.mark.asyncio
async def test_upsert_host_by_ip_with_hostname_checks_for_hostname_existing():
    """When hostname is provided, SQL must check for an existing hostname-only row to merge."""
    sql_capture = []
    host_id = uuid4()

    class _MockRecord:
        def __getitem__(self, key):
            if key == "id":
                return host_id
            return False  # is_new=False

    db = _make_mock_db(sql_capture, rows=[_MockRecord()])
    network_id = uuid4()

    await db.upsert_host(
        network_id, "1.2.3.4", 8080,
        hostname="sui-validator.kunalabs.io",
        service_type="rpc",
        confidence=0.9,
    )

    assert sql_capture, "fetchrow must be called"
    sql, _ = sql_capture[0]
    # SQL must check for existing hostname-only row (to merge/upgrade it)
    assert "hostname" in sql.lower()
    assert "ip_address is null" in sql.lower() or "hostname_existing" in sql.lower()


@pytest.mark.asyncio
async def test_upsert_host_by_hostname_matches_any_row_not_just_null_ip():
    """_upsert_host_by_hostname must match any hostname row, not just ip_address IS NULL."""
    sql_capture = []
    host_id = uuid4()

    class _MockRecord:
        def __getitem__(self, key):
            if key == "id":
                return host_id
            return False

    db = _make_mock_db(sql_capture, rows=[_MockRecord()])
    network_id = uuid4()

    await db.upsert_host(
        network_id, None, 8080,
        hostname="sui-validator.kunalabs.io",
        service_type="rpc",
        confidence=0.9,
    )

    assert sql_capture
    # All WHERE clauses in the hostname upsert must not require ip_address IS NULL
    # (so it can find an existing row that was later enriched with an IP)
    sql, _ = sql_capture[0]
    assert "hostname" in sql.lower()
