# Discovery Agent — Developer Reference

Autonomous LLM agent that maintains a live inventory of validator infrastructure for DeFi networks (Sui, Solana, …). Runs in two phases: code-side on-chain seeding + batch ASN/DNS enrichment, then a small LLM OSINT loop (CT logs, WHOIS, GitHub). Writes to `discovery.hosts`; the scan pipeline reads from `discovery.scan_targets`.

```bash
PYTHONPATH=. venv/bin/python -m src discover --network sui
PYTHONPATH=. venv/bin/python -m src discover --network solana --focus "new validators this week"
PYTHONPATH=. venv/bin/python -m src inventory --network sui
PYTHONPATH=. venv/bin/python -m src runs --network sui --last 5
PYTHONPATH=. venv/bin/python -m src diff --network sui --since 2026-02-17
```

## Environment Variables

```
DATABASE_URL=postgresql://nr_scan:nr_scan_dev@localhost:5433/nr_scan
LLM_GATEWAY_URL=http://localhost:8090
DISCOVERY_LLM_MODEL=deepseek-chat
DISCOVERY_SUI_RPC=https://fullnode.mainnet.sui.io:443
DISCOVERY_SOLANA_RPC=https://api.mainnet-beta.solana.com
DISCOVERY_MAX_TOOL_CALLS=30
DISCOVERY_PROBE_RATE_LIMIT=50
GITHUB_TOKEN=ghp_...        # optional
SERP_API_KEY=...             # optional
```

## Adding New Networks

> Sui and Solana are implemented. This is how to add Aptos, Ethereum, Avalanche, etc.

Adding a new network touches **4 files**. Everything else (batch enrichment, LLM OSINT loop, context management, DB persistence) is generic and requires no changes.

### File 1: `src/tools/blockchain/<chain>.py`

Subclass `ChainTools` (from `src/tools/blockchain/base.py`) and implement four methods:

```python
class AptosTools(ChainTools):
    def __init__(self, rpc_url: str, cache_ttl: int = 3600) -> None: ...

    def schemas(self) -> list[dict]:
        """OpenAI-format tool schemas for this chain's LLM tools (kept for audit trail)."""
        return [APTOS_GET_VALIDATORS_SCHEMA]

    def primary_tool_name(self) -> str:
        return "aptos_get_validators"

    def get_tool_map(self) -> dict[str, Callable]:
        return {"aptos_get_validators": self.get_validators}

    async def get_seed_hosts(self, network: str) -> list[dict]:
        """
        Called ONCE in code before the LLM loop. Fetch on-chain validator data
        and return a list of host dicts for bulk_report_discovered_hosts.

        Each dict must include:
          ip_address (str|None), hostname (str|None), port (int|None),
          service_type (str), confidence (float), discovery_method (str),
          validator_pubkey (str), reasoning (str)
        """
        result = await self.get_validators()
        hosts = []
        for v in result.get("validators", []):
            ip = v.get("network_address")
            if not ip:
                continue
            hosts.append({
                "ip_address": ip,
                "port": 6180,
                "service_type": "p2p",
                "confidence": 0.95,
                "discovery_method": "on_chain",
                "validator_pubkey": v.get("address"),
                "reasoning": f"On-chain address for {v.get('address','')[:10]}",
            })
        return hosts
```

The chain tools are **excluded from the LLM tool list** at runtime — they only run in `get_seed_hosts()`. The LLM never calls them directly.

See `src/tools/blockchain/sui.py` (Sui multiaddr parsing) and `src/tools/blockchain/solana.py` (Solana `getClusterNodes` JSON-RPC) as reference implementations.

### File 2: `src/cli.py`

Register the new chain inside `_setup()`:

```python
from src.tools.blockchain.aptos import AptosTools

if "aptos" not in NetworkRegistry.registered_chains():
    NetworkRegistry.register("aptos", AptosTools(rpc_url=config.aptos_rpc_url))
```

### File 3: `src/config.py`

Add the RPC URL:

```python
# dataclass field:
aptos_rpc_url: str

# in from_env():
aptos_rpc_url=os.environ.get("DISCOVERY_APTOS_RPC", "https://fullnode.mainnet.aptoslabs.com"),
```

### File 4: DB migration

Next migration number is tracked in `MEMORY.md`. Migrations live in the repo root `migrations/`:

```sql
-- migrate:up
INSERT INTO discovery.networks (name, chain_type, description)
VALUES ('aptos', 'aptos', 'Aptos mainnet validator discovery')
ON CONFLICT (name) DO NOTHING;

-- migrate:down
DELETE FROM discovery.networks WHERE name = 'aptos';
```

Run: `dbmate up`

### What you get for free

Once `get_seed_hosts()` is implemented:

- **Batch ASN + reverse DNS enrichment** — runs in code for up to 100 sampled IPs, produces ASN cluster table
- **LLM OSINT loop** — receives the cluster table, runs CT log / WHOIS / GitHub searches
- **Token management** — context compaction, result capping, 30-call budget
- **DB persistence** — host upserts, discovery run audit trail, diff view
- **CLI** — `discover`, `inventory`, `runs`, `diff` commands all work for the new network

### Network-specific port allowlist

`subnet_probe` enforces an allowlist. Add the new chain's ports to `DEFAULT_ALLOWED_PORTS` in `src/tools/network.py`:

```python
DEFAULT_ALLOWED_PORTS: set[int] = {
    # Sui
    8080, 8081, 8082, 8083, 8084, 9184, 1337,
    # Solana
    8000, 8001, 8002, 8003, 8899, 8900, 9900, 8328,
    # Aptos
    6180, 6181, 6182, 9101,
}
```

### Ethereum note

Ethereum has ~1 million validator records. `get_seed_hosts()` must cap or filter (e.g. only validators with an ENR containing an IP, or only the top N by balance). The `_batch_enrich` step uses `max_ips=100` so large networks are handled automatically. Everything else is identical.
