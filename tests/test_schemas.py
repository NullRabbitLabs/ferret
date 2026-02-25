"""
Tests for tool schema definitions.

Every schema must have type, function.name, function.description,
function.parameters with at least one property (or empty required=[]).
"""

import pytest

from src.tools.schemas import UNIVERSAL_TOOL_SCHEMAS
from src.tools.blockchain.sui import SUI_GET_VALIDATORS_SCHEMA, SUI_GET_COMMITTEE_SCHEMA
from src.tools.blockchain.solana import (
    SOLANA_GET_CLUSTER_NODES_SCHEMA,
    SOLANA_GET_VOTE_ACCOUNTS_SCHEMA,
)

ALL_SCHEMAS = UNIVERSAL_TOOL_SCHEMAS + [
    SUI_GET_VALIDATORS_SCHEMA,
    SUI_GET_COMMITTEE_SCHEMA,
    SOLANA_GET_CLUSTER_NODES_SCHEMA,
    SOLANA_GET_VOTE_ACCOUNTS_SCHEMA,
]

SCHEMA_NAMES = [s["function"]["name"] for s in ALL_SCHEMAS]


@pytest.mark.parametrize("schema", ALL_SCHEMAS, ids=SCHEMA_NAMES)
def test_schema_has_type(schema):
    assert schema["type"] == "function"


@pytest.mark.parametrize("schema", ALL_SCHEMAS, ids=SCHEMA_NAMES)
def test_schema_has_function_key(schema):
    assert "function" in schema


@pytest.mark.parametrize("schema", ALL_SCHEMAS, ids=SCHEMA_NAMES)
def test_schema_has_name(schema):
    fn = schema["function"]
    assert "name" in fn
    assert isinstance(fn["name"], str)
    assert len(fn["name"]) > 0


@pytest.mark.parametrize("schema", ALL_SCHEMAS, ids=SCHEMA_NAMES)
def test_schema_has_description(schema):
    fn = schema["function"]
    assert "description" in fn
    assert isinstance(fn["description"], str)
    assert len(fn["description"]) > 10


@pytest.mark.parametrize("schema", ALL_SCHEMAS, ids=SCHEMA_NAMES)
def test_schema_has_parameters(schema):
    fn = schema["function"]
    assert "parameters" in fn
    params = fn["parameters"]
    assert params["type"] == "object"
    assert "properties" in params


@pytest.mark.parametrize("schema", ALL_SCHEMAS, ids=SCHEMA_NAMES)
def test_schema_required_is_list(schema):
    params = schema["function"]["parameters"]
    # required is optional but if present must be a list
    if "required" in params:
        assert isinstance(params["required"], list)


def test_universal_schema_count():
    """Verify we have all 15 universal tool schemas."""
    assert len(UNIVERSAL_TOOL_SCHEMAS) == 15


def test_no_duplicate_names():
    """All tool names must be unique."""
    assert len(SCHEMA_NAMES) == len(set(SCHEMA_NAMES)), "Duplicate schema names found"


def test_required_fields_in_properties():
    """Every field listed in required must exist in properties."""
    for schema in ALL_SCHEMAS:
        params = schema["function"]["parameters"]
        required = params.get("required", [])
        properties = params.get("properties", {})
        for field in required:
            assert field in properties, (
                f"Schema {schema['function']['name']!r}: required field {field!r} "
                f"not in properties"
            )
