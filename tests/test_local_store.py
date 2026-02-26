"""Tests for LocalDiscoveryStore."""

from uuid import uuid4

import pytest

from src.local_store import LocalDiscoveryStore


@pytest.fixture
def store() -> LocalDiscoveryStore:
    return LocalDiscoveryStore()


@pytest.fixture
async def store_with_run(store: LocalDiscoveryStore) -> LocalDiscoveryStore:
    await store.create_discovery_run("cosmos")
    return store


@pytest.mark.asyncio
async def test_upsert_host_first_insert_is_new(store: LocalDiscoveryStore) -> None:
    await store.create_discovery_run("cosmos")
    _, is_new = await store.upsert_host("cosmos", "1.2.3.4", 26656)
    assert is_new is True


@pytest.mark.asyncio
async def test_upsert_host_duplicate_ip_port_not_new(store: LocalDiscoveryStore) -> None:
    await store.create_discovery_run("cosmos")
    id1, _ = await store.upsert_host("cosmos", "1.2.3.4", 26656)
    id2, is_new = await store.upsert_host("cosmos", "1.2.3.4", 26656)
    assert is_new is False
    assert id1 == id2


@pytest.mark.asyncio
async def test_upsert_host_dedup_by_hostname_port(store: LocalDiscoveryStore) -> None:
    await store.create_discovery_run("cosmos")
    id1, _ = await store.upsert_host("cosmos", None, 26656, hostname="node.example.com")
    id2, is_new = await store.upsert_host("cosmos", None, 26656, hostname="node.example.com")
    assert is_new is False
    assert id1 == id2


@pytest.mark.asyncio
async def test_upsert_host_different_port_is_new(store: LocalDiscoveryStore) -> None:
    await store.create_discovery_run("cosmos")
    _, is_new1 = await store.upsert_host("cosmos", "1.2.3.4", 26656)
    _, is_new2 = await store.upsert_host("cosmos", "1.2.3.4", 26657)
    assert is_new1 is True
    assert is_new2 is True


@pytest.mark.asyncio
async def test_get_hosts_filter_is_active_excludes_gone(store: LocalDiscoveryStore) -> None:
    await store.create_discovery_run("cosmos")
    host_id, _ = await store.upsert_host("cosmos", "1.2.3.4", 26656)
    await store.upsert_host("cosmos", "5.6.7.8", 26656)
    await store.flag_host_gone(host_id, "timed out")

    active = await store.get_hosts("cosmos", is_active=True)
    assert len(active) == 1
    assert active[0]["ip_address"] == "5.6.7.8"


@pytest.mark.asyncio
async def test_get_hosts_filter_by_service_type(store: LocalDiscoveryStore) -> None:
    await store.create_discovery_run("cosmos")
    await store.upsert_host("cosmos", "1.2.3.4", 26656, service_type="rpc")
    await store.upsert_host("cosmos", "5.6.7.8", 9090, service_type="grpc")

    rpc_hosts = await store.get_hosts("cosmos", service_type="rpc")
    assert len(rpc_hosts) == 1
    assert rpc_hosts[0]["ip_address"] == "1.2.3.4"


@pytest.mark.asyncio
async def test_get_or_create_validator_same_pubkey_same_id(store: LocalDiscoveryStore) -> None:
    await store.create_discovery_run("cosmos")
    pubkey = "cosmosvalconspub1zcjduepq..."
    id1 = await store.get_or_create_validator("cosmos", pubkey)
    id2 = await store.get_or_create_validator("cosmos", pubkey)
    assert id1 == id2


@pytest.mark.asyncio
async def test_get_or_create_validator_different_pubkeys_different_ids(store: LocalDiscoveryStore) -> None:
    await store.create_discovery_run("cosmos")
    id1 = await store.get_or_create_validator("cosmos", "pubkey_a")
    id2 = await store.get_or_create_validator("cosmos", "pubkey_b")
    assert id1 != id2


@pytest.mark.asyncio
async def test_flag_host_gone_marks_inactive(store: LocalDiscoveryStore) -> None:
    await store.create_discovery_run("cosmos")
    host_id, _ = await store.upsert_host("cosmos", "1.2.3.4", 26656)
    result = await store.flag_host_gone(host_id, "unreachable")
    assert result is True

    all_hosts = await store.get_hosts("cosmos")
    gone_host = next(h for h in all_hosts if h["id"] == host_id)
    assert gone_host["is_active"] is False


@pytest.mark.asyncio
async def test_get_results_host_count_and_stats(store: LocalDiscoveryStore) -> None:
    await store.create_discovery_run("cosmos")
    run_id = store._current_run_id
    await store.upsert_host("cosmos", "1.2.3.4", 26656)
    await store.upsert_host("cosmos", "5.6.7.8", 26656)
    await store.update_run_stats(run_id, hosts_new=2, tool_calls=5, tokens=1000)
    await store.complete_discovery_run(run_id, summary="Done", status="completed")

    results = store.get_results()
    assert results["network"] == "cosmos"
    assert len(results["hosts"]) == 2
    assert results["stats"]["hosts_new"] == 2
    assert results["stats"]["tool_calls"] == 5
    assert results["stats"]["tokens"] == 1000
    assert results["status"] == "completed"
    assert results["summary"] == "Done"
    assert results["completed_at"] is not None


@pytest.mark.asyncio
async def test_get_results_excludes_other_networks(store: LocalDiscoveryStore) -> None:
    await store.create_discovery_run("cosmos")
    await store.upsert_host("cosmos", "1.2.3.4", 26656)
    # Manually insert a host for a different network
    store._hosts[__import__("uuid").uuid4()] = {
        "id": __import__("uuid").uuid4(),
        "network_name": "solana",
        "ip_address": "9.9.9.9",
        "port": 8899,
        "is_active": True,
        "confidence": 0.9,
        "service_type": None,
        "hostname": None,
        "protocol": None,
        "discovery_method": None,
        "validator_id": None,
        "metadata": {},
        "created_at": __import__("datetime").datetime.now(),
        "last_seen_at": __import__("datetime").datetime.now(),
    }

    results = store.get_results()
    assert len(results["hosts"]) == 1
    assert results["hosts"][0]["ip_address"] == "1.2.3.4"
