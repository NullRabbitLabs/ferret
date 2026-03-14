"""Tests for the network registry and config integration."""

import os

import pytest
from pydantic import ValidationError

from src.networks import (
    ALL_ALLOWED_PORTS,
    NETWORK_DEFINITIONS,
    NetworkConfig,
    _NETWORK_CONFIGS,
    _build_network_definitions,
)
from src.tools.blockchain.solana import SolanaTools
from src.tools.blockchain.sui import SuiTools
from src.tools.network import DEFAULT_ALLOWED_PORTS


def test_network_definitions_contains_sui_and_solana():
    assert "sui" in NETWORK_DEFINITIONS
    assert "solana" in NETWORK_DEFINITIONS


def test_network_definitions_tuple_structure():
    for name, entry in NETWORK_DEFINITIONS.items():
        tools_cls, env_var, default_url = entry
        assert callable(tools_cls), f"{name}: tools_cls must be callable"
        assert isinstance(env_var, str) and env_var, f"{name}: env_var must be a non-empty string"
        assert isinstance(default_url, str) and default_url.startswith("http"), (
            f"{name}: default_url must be an HTTP(S) URL"
        )


def test_sui_entry_uses_correct_class_and_defaults():
    tools_cls, env_var, default_url = NETWORK_DEFINITIONS["sui"]
    assert tools_cls is SuiTools
    assert env_var == "DISCOVERY_SUI_RPC"
    assert "sui.io" in default_url


def test_solana_entry_uses_correct_class_and_defaults():
    tools_cls, env_var, default_url = NETWORK_DEFINITIONS["solana"]
    assert tools_cls is SolanaTools
    assert env_var == "DISCOVERY_SOLANA_RPC"
    assert "solana.com" in default_url


def test_config_rpc_urls_uses_defaults(monkeypatch):
    """Config.from_env() populates rpc_urls from NETWORK_DEFINITIONS defaults."""
    monkeypatch.delenv("DISCOVERY_SUI_RPC", raising=False)
    monkeypatch.delenv("DISCOVERY_SOLANA_RPC", raising=False)

    from src.config import Config

    config = Config.from_env()
    _, _, sui_default = NETWORK_DEFINITIONS["sui"]
    _, _, solana_default = NETWORK_DEFINITIONS["solana"]
    assert config.rpc_urls["sui"] == sui_default
    assert config.rpc_urls["solana"] == solana_default


def test_config_rpc_urls_overridden_by_env(monkeypatch):
    """Config.from_env() respects environment variable overrides."""
    monkeypatch.setenv("DISCOVERY_SUI_RPC", "https://custom-sui-rpc.example.com")
    monkeypatch.setenv("DISCOVERY_SOLANA_RPC", "https://custom-solana-rpc.example.com")

    from src.config import Config

    config = Config.from_env()
    assert config.rpc_urls["sui"] == "https://custom-sui-rpc.example.com"
    assert config.rpc_urls["solana"] == "https://custom-solana-rpc.example.com"


def test_config_rpc_urls_has_entry_for_every_network():
    """Every network in NETWORK_DEFINITIONS has an entry in config.rpc_urls."""
    from src.config import Config

    config = Config.from_env()
    for name in NETWORK_DEFINITIONS:
        assert name in config.rpc_urls, f"Missing rpc_urls entry for {name}"


# --- New tests for JSON-backed network registry ---


def test_networks_json_loads_and_validates():
    assert len(_NETWORK_CONFIGS) >= 2
    for name, cfg in _NETWORK_CONFIGS.items():
        assert isinstance(cfg, NetworkConfig), f"{name}: expected NetworkConfig"
        assert cfg.env_var
        assert cfg.default_rpc_url.startswith("http")
        assert len(cfg.allowed_ports) > 0


def test_invalid_url_raises_validation_error():
    with pytest.raises(ValidationError):
        NetworkConfig(
            env_var="DISCOVERY_FAKE_RPC",
            default_rpc_url="not-a-url",
            allowed_ports=[8080],
        )


def test_missing_required_field_raises_validation_error():
    with pytest.raises(ValidationError):
        NetworkConfig(
            env_var="DISCOVERY_FAKE_RPC",
            allowed_ports=[8080],
        )


def test_empty_allowed_ports_raises_validation_error():
    with pytest.raises(ValidationError):
        NetworkConfig(
            env_var="DISCOVERY_FAKE_RPC",
            default_rpc_url="https://example.com",
            allowed_ports=[],
        )


def test_network_missing_from_registry_raises_key_error():
    fake_cfg = NetworkConfig(
        env_var="DISCOVERY_FAKE_RPC",
        default_rpc_url="https://example.com",
        allowed_ports=[1234],
    )
    with pytest.raises(KeyError, match="fake"):
        _build_network_definitions({"fake": fake_cfg}, class_registry={})


def test_all_allowed_ports_is_union_of_all_networks():
    expected = {port for cfg in _NETWORK_CONFIGS.values() for port in cfg.allowed_ports}
    assert ALL_ALLOWED_PORTS == expected


def test_default_allowed_ports_matches_all_allowed_ports():
    assert DEFAULT_ALLOWED_PORTS == ALL_ALLOWED_PORTS


# --- chain_type indirection (testnet/devnet support) ---


def test_chain_type_resolves_to_correct_class():
    """Network with chain_type='sui' resolves to SuiTools even without a sui-testnet.py."""
    cfg = NetworkConfig(
        env_var="DISCOVERY_SUI_TESTNET_RPC",
        default_rpc_url="https://fullnode.testnet.sui.io:443",
        allowed_ports=[8080],
        chain_type="sui",
    )
    result = _build_network_definitions(
        {"sui-testnet": cfg},
        class_registry={"sui": SuiTools},
    )
    tools_cls, env_var, default_url = result["sui-testnet"]
    assert tools_cls is SuiTools
    assert env_var == "DISCOVERY_SUI_TESTNET_RPC"
    assert "testnet" in default_url


def test_chain_type_omitted_defaults_to_name():
    """When chain_type is not set, class is looked up by network name (existing behavior)."""
    cfg = NetworkConfig(
        env_var="DISCOVERY_SUI_RPC",
        default_rpc_url="https://fullnode.mainnet.sui.io:443",
        allowed_ports=[8080],
    )
    result = _build_network_definitions(
        {"sui": cfg},
        class_registry={"sui": SuiTools},
    )
    assert result["sui"][0] is SuiTools


def test_invalid_chain_type_raises_key_error():
    """chain_type pointing to a non-existent class raises KeyError."""
    cfg = NetworkConfig(
        env_var="DISCOVERY_FAKE_RPC",
        default_rpc_url="https://example.com",
        allowed_ports=[8080],
        chain_type="nonexistent",
    )
    with pytest.raises(KeyError, match="chain_type='nonexistent'"):
        _build_network_definitions({"fake-net": cfg}, class_registry={"sui": SuiTools})


def test_sui_testnet_in_network_definitions():
    """sui-testnet should be in NETWORK_DEFINITIONS after networks.json is updated."""
    assert "sui-testnet" in NETWORK_DEFINITIONS
    tools_cls, env_var, default_url = NETWORK_DEFINITIONS["sui-testnet"]
    assert tools_cls is SuiTools
    assert env_var == "DISCOVERY_SUI_TESTNET_RPC"
    assert "testnet" in default_url


def test_sui_devnet_in_network_definitions():
    """sui-devnet should be in NETWORK_DEFINITIONS after networks.json is updated."""
    assert "sui-devnet" in NETWORK_DEFINITIONS
    tools_cls, env_var, default_url = NETWORK_DEFINITIONS["sui-devnet"]
    assert tools_cls is SuiTools
    assert env_var == "DISCOVERY_SUI_DEVNET_RPC"
    assert "devnet" in default_url
