"""
Tests for state tools (StateTools).

Uses mock_db for unit tests.
Integration tests (marked pytest.mark.integration) use real_db.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


@pytest.fixture
def state_tools(mock_db):
    from src.tools.state import StateTools
    return StateTools(db=mock_db)


@pytest.mark.asyncio
async def test_get_known_hosts_returns_hosts(state_tools, mock_db):
    host_id = uuid4()
    mock_db.get_hosts.return_value = [
        {
            "id": host_id,
            "ip_address": "1.2.3.4",
            "port": 8080,
            "service_type": "rpc",
            "confidence": 0.9,
            "last_seen_at": datetime.now(timezone.utc),
        }
    ]

    result = await state_tools.get_known_hosts(network="sui")

    assert result["network"] == "sui"
    assert result["count"] == 1
    assert result["hosts"][0]["ip_address"] == "1.2.3.4"


@pytest.mark.asyncio
async def test_get_known_hosts_large_unfiltered_returns_count_summary(state_tools, mock_db):
    """75 hosts with no filter → count summary (not paginated list) to save context budget."""
    hosts = [
        {"id": uuid4(), "ip_address": f"10.0.0.{i}", "port": 8080,
         "service_type": "rpc", "confidence": 0.9, "last_seen_at": datetime.now(timezone.utc),
         "hostname": None}
        for i in range(75)
    ]
    mock_db.get_hosts.return_value = hosts

    result = await state_tools.get_known_hosts(network="sui")

    assert result["count"] == 75, "count reflects total"
    assert "hosts" not in result, "large unfiltered result must return count summary not full list"
    assert "by_service_type" in result
    assert "note" in result


@pytest.mark.asyncio
async def test_get_known_hosts_explicit_limit_filtered(state_tools, mock_db):
    """Filtered call with limit respected — 20 hosts, filter applied, limit=5."""
    hosts = [
        {"id": uuid4(), "ip_address": f"10.0.0.{i}", "port": 8080,
         "service_type": "rpc", "confidence": 0.9, "last_seen_at": datetime.now(timezone.utc),
         "hostname": None}
        for i in range(20)
    ]
    mock_db.get_hosts.return_value = hosts

    result = await state_tools.get_known_hosts(
        network="sui", filters={"service_type": "rpc"}, limit=5
    )

    assert "hosts" in result
    assert len(result["hosts"]) == 5
    assert result["count"] == 20
    assert "note" in result


@pytest.mark.asyncio
async def test_get_known_hosts_no_note_when_under_limit(state_tools, mock_db):
    hosts = [
        {"id": uuid4(), "ip_address": f"10.0.0.{i}", "port": 8080,
         "service_type": "rpc", "confidence": 0.9, "last_seen_at": datetime.now(timezone.utc)}
        for i in range(5)
    ]
    mock_db.get_hosts.return_value = hosts

    result = await state_tools.get_known_hosts(network="sui")

    assert len(result["hosts"]) == 5
    assert "note" not in result, "no truncation note when results fit within limit"



@pytest.mark.asyncio
async def test_get_known_validators_returns_list(state_tools, mock_db):
    mock_db.get_validators.return_value = [
        {"id": uuid4(), "pubkey": "0xabc", "operator_name": "Operator1", "host_count": 3},
    ]

    result = await state_tools.get_known_validators(network="sui")

    assert result["count"] == 1
    # pubkeys are truncated to 16 chars + "..." in the compact output
    assert result["validators"][0]["pubkey"].startswith("0xabc")
    assert result["validators"][0]["operator_name"] == "Operator1"
    assert result["validators"][0]["host_count"] == 3


@pytest.mark.asyncio
async def test_report_discovered_host_is_new(state_tools, mock_db):
    host_id = uuid4()
    mock_db.upsert_host.return_value = (host_id, True)

    state_tools.init_run_stats("run-1")
    result = await state_tools.report_discovered_host(
        network="sui",
        ip_address="5.6.7.8",
        service_type="rpc",
        confidence=0.9,
        discovery_method="on_chain",
        reasoning="Found in suix_getLatestSuiSystemState",
        run_id="run-1",
    )

    assert result["is_new"] is True
    assert result["ip_address"] == "5.6.7.8"
    assert state_tools.get_run_stats("run-1")["hosts_new"] == 1
    assert state_tools.get_run_stats("run-1")["hosts_updated"] == 0


@pytest.mark.asyncio
async def test_report_discovered_host_is_update(state_tools, mock_db):
    host_id = uuid4()
    mock_db.upsert_host.return_value = (host_id, False)

    state_tools.init_run_stats("run-2")
    result = await state_tools.report_discovered_host(
        network="sui",
        ip_address="5.6.7.8",
        service_type="rpc",
        confidence=0.8,
        discovery_method="on_chain",
        reasoning="Update",
        run_id="run-2",
    )

    assert result["is_new"] is False
    assert state_tools.get_run_stats("run-2")["hosts_updated"] == 1


@pytest.mark.asyncio
async def test_report_discovered_host_with_validator_pubkey(state_tools, mock_db):
    host_id = uuid4()
    validator_id = uuid4()
    mock_db.upsert_host.return_value = (host_id, True)
    mock_db.get_or_create_validator.return_value = validator_id

    await state_tools.report_discovered_host(
        network="sui",
        ip_address="9.9.9.9",
        service_type="p2p",
        confidence=0.95,
        discovery_method="on_chain",
        reasoning="Directly from on-chain data",
        validator_pubkey="0xvalidator123",
    )

    mock_db.get_or_create_validator.assert_called_once()
    call_args = mock_db.upsert_host.call_args
    assert call_args[1]["validator_id"] == validator_id


@pytest.mark.asyncio
async def test_flag_host_gone_updates_stats(state_tools, mock_db):
    mock_db.flag_host_gone.return_value = True

    state_tools.init_run_stats("run-3")
    result = await state_tools.flag_host_gone(
        host_id=str(uuid4()),
        reason="No longer in validator set",
        run_id="run-3",
    )

    assert result["updated"] is True
    assert state_tools.get_run_stats("run-3")["hosts_gone"] == 1


@pytest.mark.asyncio
async def test_flag_host_gone_invalid_uuid(state_tools):
    result = await state_tools.flag_host_gone(host_id="not-a-uuid", reason="test")
    assert "error" in result


@pytest.mark.asyncio
async def test_get_discovery_diff_returns_diff(state_tools, mock_db):
    mock_db.get_discovery_diff.return_value = {
        "new_hosts": [{"ip_address": "1.2.3.4", "port": 8080}],
        "gone_hosts": [],
        "changed_hosts": [],
        "new_validators": [],
        "since": "2026-02-17T00:00:00+00:00",
    }

    result = await state_tools.get_discovery_diff(network="sui", since="2026-02-17T00:00:00Z")

    assert len(result["new_hosts"]) == 1
    assert result["new_hosts"][0]["ip_address"] == "1.2.3.4"


@pytest.mark.asyncio
async def test_get_discovery_diff_invalid_since(state_tools):
    result = await state_tools.get_discovery_diff(network="sui", since="not-a-date")
    assert "error" in result


@pytest.mark.asyncio
async def test_search_past_hypotheses_no_gateway(mock_db):
    from src.tools.state import StateTools

    tools = StateTools(db=mock_db, gateway_client=None)
    result = await tools.search_past_hypotheses(query="same ASN co-location")

    assert result["results"] == []
    assert "note" in result


@pytest.mark.asyncio
async def test_search_past_hypotheses_calls_gateway(state_tools, mock_db):
    mock_gateway = AsyncMock()
    mock_gateway.get_embedding.return_value = [0.1] * 1536
    mock_db.search_hypotheses.return_value = [
        {"hypothesis": "Validators share ASN", "validated": True, "similarity": 0.9}
    ]

    from src.tools.state import StateTools
    tools = StateTools(db=mock_db, gateway_client=mock_gateway)

    result = await tools.search_past_hypotheses(query="co-located validators")

    mock_gateway.get_embedding.assert_called_once_with("co-located validators")
    assert len(result["results"]) == 1


@pytest.mark.asyncio
async def test_bulk_report_discovered_hosts_returns_counts(state_tools, mock_db):
    id1, id2 = uuid4(), uuid4()
    mock_db.upsert_host.side_effect = [(id1, True), (id2, False)]

    state_tools.init_run_stats("run-bulk")
    result = await state_tools.bulk_report_discovered_hosts(
        network="sui",
        hosts=[
            {
                "ip_address": "1.2.3.4",
                "port": 8080,
                "service_type": "rpc",
                "confidence": 0.95,
                "discovery_method": "on_chain",
                "reasoning": "From on-chain netAddress",
            },
            {
                "ip_address": "5.6.7.8",
                "port": 8084,
                "service_type": "p2p",
                "confidence": 0.95,
                "discovery_method": "on_chain",
                "reasoning": "From on-chain p2pAddress",
            },
        ],
        run_id="run-bulk",
    )

    assert result["total"] == 2
    assert result["new"] == 1
    assert result["updated"] == 1
    assert result["errors"] == []
    assert state_tools.get_run_stats("run-bulk")["hosts_new"] == 1
    assert state_tools.get_run_stats("run-bulk")["hosts_updated"] == 1


@pytest.mark.asyncio
async def test_bulk_report_skips_entries_missing_ip(state_tools, mock_db):
    mock_db.upsert_host.return_value = (uuid4(), True)

    result = await state_tools.bulk_report_discovered_hosts(
        network="sui",
        hosts=[
            {"service_type": "rpc", "confidence": 0.9, "discovery_method": "on_chain"},  # no ip
            {"ip_address": "1.2.3.4", "service_type": "rpc", "confidence": 0.9, "discovery_method": "on_chain"},
        ],
    )

    assert result["total"] == 2
    assert result["new"] == 1  # only the valid one was inserted


@pytest.mark.asyncio
async def test_bulk_report_with_validator_pubkeys(state_tools, mock_db):
    vid = uuid4()
    mock_db.upsert_host.return_value = (uuid4(), True)
    mock_db.get_or_create_validator.return_value = vid

    await state_tools.bulk_report_discovered_hosts(
        network="sui",
        hosts=[
            {
                "ip_address": "1.2.3.4",
                "port": 8080,
                "service_type": "rpc",
                "confidence": 0.95,
                "discovery_method": "on_chain",
                "validator_pubkey": "0xabc123",
            }
        ],
    )

    mock_db.get_or_create_validator.assert_called_once()
    call_args = mock_db.upsert_host.call_args
    assert call_args[1]["validator_id"] == vid


# ============================================================
# Fix #8: Count summary for large unfiltered get_known_hosts
# ============================================================

@pytest.mark.asyncio
async def test_get_known_hosts_large_no_filter_returns_count_summary(state_tools, mock_db):
    """When total > 20 and no meaningful filters, return count summary instead of full list."""
    from datetime import datetime, timezone

    hosts = [
        {
            "id": uuid4(), "ip_address": f"10.0.0.{i}", "port": 8080,
            "service_type": "rpc", "confidence": 0.9,
            "last_seen_at": datetime.now(timezone.utc),
            "hostname": None,
        }
        for i in range(25)
    ]
    mock_db.get_hosts.return_value = hosts

    result = await state_tools.get_known_hosts(network="sui")

    assert "hosts" not in result, "Large unfiltered result must not return full host list"
    assert result["count"] == 25
    assert "by_service_type" in result
    assert "note" in result


@pytest.mark.asyncio
async def test_get_known_hosts_small_no_filter_returns_full_list(state_tools, mock_db):
    """When total <= 20, return full list as usual."""
    from datetime import datetime, timezone

    hosts = [
        {
            "id": uuid4(), "ip_address": f"10.0.0.{i}", "port": 8080,
            "service_type": "rpc", "confidence": 0.9,
            "last_seen_at": datetime.now(timezone.utc),
            "hostname": None,
        }
        for i in range(10)
    ]
    mock_db.get_hosts.return_value = hosts

    result = await state_tools.get_known_hosts(network="sui")

    assert "hosts" in result
    assert len(result["hosts"]) == 10


@pytest.mark.asyncio
async def test_get_known_hosts_filtered_large_returns_full_list(state_tools, mock_db):
    """When filtered (e.g. operator_name), return full list even if > 20."""
    from datetime import datetime, timezone

    hosts = [
        {
            "id": uuid4(), "ip_address": f"10.0.0.{i}", "port": 8080,
            "service_type": "rpc", "confidence": 0.9,
            "last_seen_at": datetime.now(timezone.utc),
            "hostname": None,
        }
        for i in range(25)
    ]
    mock_db.get_hosts.return_value = hosts

    result = await state_tools.get_known_hosts(
        network="sui", filters={"operator_name": "SomeOp"}
    )

    assert "hosts" in result, "Filtered result must return full host list"


@pytest.mark.asyncio
async def test_get_known_hosts_service_type_filter_returns_full_list(state_tools, mock_db):
    """service_type filter also bypasses the count summary."""
    from datetime import datetime, timezone

    hosts = [
        {
            "id": uuid4(), "ip_address": f"10.0.0.{i}", "port": 8080,
            "service_type": "rpc", "confidence": 0.9,
            "last_seen_at": datetime.now(timezone.utc),
            "hostname": None,
        }
        for i in range(25)
    ]
    mock_db.get_hosts.return_value = hosts

    result = await state_tools.get_known_hosts(
        network="sui", filters={"service_type": "rpc"}
    )

    assert "hosts" in result, "service_type filtered result must return full host list"
