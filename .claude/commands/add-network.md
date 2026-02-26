# Add Discovery Network

Add a new blockchain network to the discovery agent.

## Usage

- `/add-network aptos` — add Aptos mainnet
- `/add-network ethereum` — add Ethereum beacon chain
- `/add-network $ARGUMENTS` — add the named network

## What this does

Creates all the boilerplate needed to run the discovery agent against a new network:
1. `src/tools/blockchain/<network>.py` — ChainTools subclass (auto-discovered at startup)
2. Entry in `src/networks.json` — pure data (env var, default RPC URL, allowed ports)

Everything else (networks.py, cli.py, server.py, config.py, DEFAULT_ALLOWED_PORTS) is driven automatically.

## Architecture (read before coding)

The discovery agent runs in two phases:

**Phase 1 (code, no LLM):** `get_seed_hosts()` is called on the ChainTools instance. It fetches on-chain validator/node data and returns a list of host dicts for bulk import. Batch ASN + reverse DNS enrichment then runs concurrently for up to 100 IPs.

**Phase 2 (LLM OSINT):** The agent receives an ASN cluster summary and has a 30-call budget for CT log searches, WHOIS, GitHub, etc. Chain tools are **not available to the LLM** — they only run in phase 1.

## Behaviour

### Step 1 — Research the chain's API

Before writing any code, look up how to query the validator/node set for `$ARGUMENTS`:

- What JSON-RPC or REST endpoint exposes the active validator set?
- What fields contain IP addresses or hostnames? What format (multiaddr? host:port? plain IP)?
- What are the standard ports for this chain's validator services?
- What's the appropriate RPC URL for mainnet?

Check the existing implementations for reference:
- `src/tools/blockchain/sui.py` — uses `suix_getLatestSuiSystemState`, parses multiaddr strings
- `src/tools/blockchain/solana.py` — uses `getClusterNodes`, parses `host:port` strings

### Step 2 — Write tests first

Write `tests/test_<network>_tools.py` following the pattern in `tests/test_sui_tools.py` and `tests/test_solana_tools.py`. At minimum:

- Mock the RPC HTTP call and verify `get_seed_hosts()` returns correctly shaped host dicts
- Test that `get_seed_hosts()` returns empty list (not an error) when the RPC returns no validators
- Test caching behaviour

Run the tests — they must fail (red) before you write the implementation.

### Step 3 — Write `src/tools/blockchain/<network>.py`

Implement `ChainTools` with these four methods:

```python
class <Network>Tools(ChainTools):
    def schemas(self) -> list[dict]:
        # One or more OpenAI-format tool schemas. Kept for audit trail only.

    def primary_tool_name(self) -> str:
        # Name of the main validator-fetch schema.

    def get_tool_map(self) -> dict[str, Callable]:
        # Maps tool name -> async callable.

    async def get_seed_hosts(self, network: str) -> list[dict]:
        # Fetch on-chain data. Return list of dicts, each with:
        #   ip_address (str|None), hostname (str|None), port (int|None),
        #   service_type (str), confidence (float 0-1),
        #   discovery_method ("on_chain"), validator_pubkey (str), reasoning (str)
```

Cache the RPC response for 1 hour (validator sets change slowly). Use `httpx.AsyncClient(timeout=30.0)`.

**Ethereum note:** The beacon chain has ~1 million validators. `get_seed_hosts()` must filter to those with a populated ENR/IP — do not return all 1M records.

### Step 4 — Add data to `src/networks.json`

```json
"<network>": {
  "env_var": "DISCOVERY_<NETWORK>_RPC",
  "default_rpc_url": "<mainnet_default_url>",
  "allowed_ports": [<port1>, <port2>, ...],
  "description": "<Network> mainnet nodes"
}
```

That's it. `networks.py` auto-discovers the new `ChainTools` subclass at startup via `__init_subclass__`, and `DEFAULT_ALLOWED_PORTS` in `src/tools/network.py` is derived from the JSON — no other file changes needed.

### Step 5 — Update README.md


Update the following sections in `README.md`:

1. **Features** — add the new chain to the "On-chain seeding" bullet:
   ```
   - **On-chain seeding** — pulls validator addresses directly from chain RPC (Sui, Solana, <Network>)
   ```

2. **Configuration table** — add the new RPC env var row:
   ```
   | `DISCOVERY_<NETWORK>_RPC` | `<mainnet_default_url>` | <Network> RPC endpoint |
   ```

3. **Architecture tree** — add the new file under `blockchain/`:
   ```
   └── <network>.py     # <Network> mainnet
   ```

### Step 6 — Verify


```bash
source venv/bin/activate
python -m pytest tests/ -x -q
PYTHONPATH=. python -m src discover --network <network>
```

## Rules

- TDD: write the test before the implementation
- Do not modify existing blockchain files
- Cache RPC responses — do not hammer the public RPC on every run
- `get_seed_hosts()` must never raise — return `[]` on RPC errors, log a warning
- Keep `schemas()` even though the LLM doesn't call them (used in audit trail)
- Module in `src/tools/blockchain/`, data in `src/networks.json` — that's all; cli.py, server.py, config.py, and DEFAULT_ALLOWED_PORTS update automatically
