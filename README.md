# Ferret

Existing tools discover nodes that announce themselves. Ferret discovers infrastructure that doesn't.

LLM-powered validator infrastructure discovery for blockchain networks. Starts with on-chain data and uses OSINT - certificate transparency logs, WHOIS, ASN correlation, GitHub code search - to find related hosts that never appear in gossip or peer tables.

Networks like Sui and Solana expose validator IP addresses on-chain, but operators run more than just the node that votes. RPC endpoints, monitoring servers, backup nodes, sentry layers - this shadow infrastructure is invisible to protocol-native crawlers and often unmonitored. Ferret finds it.

## Features

- **On-chain seeding** - pulls validator addresses directly from chain RPC (Sui, Solana, Cosmos Hub)
- **Batch enrichment** - ASN lookups, reverse DNS, and IP clustering with no LLM required
- **LLM OSINT loop** - autonomous agent uses CT logs, WHOIS, GitHub search to discover related hosts
- **Tool budget controls** - configurable limits on tool calls, idle rounds, and new host caps to prevent runaway spend
- **CDN filtering** - automatic rejection of Cloudflare, Fastly, and Akamai edge IPs
- **Multi-network** - extensible architecture, add a new chain by implementing one class
- **Diff tracking** - compare inventory snapshots across runs to detect infrastructure changes
- **Discovery API integration** - results persist to a REST API for downstream scanning pipelines

## Install

```bash
git clone https://github.com/NullRabbitLabs/ferret.git
cd ferret
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Docker:**

```bash
docker build -t ferret .
docker run --env-file .env ferret discover --network sui
```

## Usage

```bash
# Discover Sui validator infrastructure
ferret discover --network sui

# Discover Solana with a focus directive
ferret discover --network solana --focus "new validators this week"

# View current inventory
ferret inventory --network sui

# Show recent discovery runs
ferret runs --network sui --last 5

# Diff inventory since a date
ferret diff --network sui --since 2026-02-17
```

> [!NOTE]
> Commands assume `PYTHONPATH=.` or package installation. The `ferret` CLI is an alias for `python -m src`.

## How It Works

Ferret runs in two phases:

**Phase 1 - Code.** Seeds on-chain validator addresses and runs batch ASN + reverse DNS enrichment across sampled IPs. No LLM needed. Produces a cluster summary of which ASNs host validators and which IP ranges are interesting.

**Phase 2 - LLM.** Gives an autonomous agent the cluster summary and a small tool budget. The agent performs OSINT - certificate transparency searches, WHOIS lookups, GitHub code search, subnet probing - and reports new hosts it finds. The agent self-terminates when it runs out of budget, hits the idle threshold, or reaches the new host cap.

Results are written to the Discovery API for use by downstream scanning and protection systems.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `DISCOVERY_API_URL` | `http://localhost:8092` | Discovery API base URL |
| `LLM_GATEWAY_URL` | `http://localhost:8090` | OpenAI-compatible LLM gateway |
| `DISCOVERY_LLM_MODEL` | `deepseek-chat` | Model (must support tool calls) |
| `DISCOVERY_SUI_RPC` | `https://fullnode.mainnet.sui.io:443` | Sui RPC endpoint |
| `DISCOVERY_SOLANA_RPC` | `https://api.mainnet-beta.solana.com` | Solana RPC endpoint |
| `DISCOVERY_COSMOS_RPC` | `https://cosmos-rpc.publicnode.com` | Cosmos Hub RPC endpoint |
| `DISCOVERY_MAX_TOOL_CALLS` | `30` | LLM tool call budget per run |
| `DISCOVERY_MAX_NEW_HOSTS` | `10` | Stop after N new hosts |
| `DISCOVERY_MAX_IDLE_CALLS` | `15` | Stop after N calls with no discovery |
| `DISCOVERY_PROBE_RATE_LIMIT` | `50` | Max concurrent TCP connections |
| `GITHUB_TOKEN` | - | GitHub API token (optional) |
| `SERP_API_KEY` | - | SerpAPI key (optional) |

## Adding Networks

Adding a new chain touches **2 files**. Everything else - batch enrichment, LLM OSINT loop, context management, API persistence - is generic.

### 1. Implement `ChainTools`

Create `src/tools/blockchain/<chain>.py`:

```python
from src.tools.blockchain.base import ChainTools

class AptosTools(ChainTools):
    def schemas(self) -> list[dict]: ...
    def primary_tool_name(self) -> str: ...
    def get_tool_map(self) -> dict[str, Callable]: ...
    async def get_seed_hosts(self, network: str) -> list[dict]: ...
```

`get_seed_hosts()` returns host dicts with: `ip_address`, `port`, `service_type`, `confidence`, `discovery_method`, `validator_pubkey`, `reasoning`.

The class is auto-discovered at startup via `__init_subclass__` — no registration step needed.

See `sui.py`, `solana.py`, and `cosmos.py` for reference implementations.

### 2. Add data to `src/networks.json`

```json
"aptos": {
  "env_var": "DISCOVERY_APTOS_RPC",
  "default_rpc_url": "https://fullnode.mainnet.aptoslabs.com/v1",
  "allowed_ports": [6180, 6181, 8080],
  "description": "Aptos mainnet validator nodes"
}
```

That's it. cli.py, server.py, config.py, and the subnet probe allowlist all update automatically.

## Architecture

```
src/
├── agent.py              # LLM discovery loop with tool budget management
├── cli.py                # CLI: discover, inventory, runs, diff
├── config.py             # Environment configuration
├── gateway_client.py     # OpenAI-format LLM gateway client
├── api_client.py         # Discovery API HTTP client
├── db.py                 # Data models
├── networks.json         # Network registry — edit here to add a chain
├── networks.schema.json  # JSON Schema for IDE validation
├── networks.py           # Loads networks.json, exports NETWORK_DEFINITIONS
└── tools/
    ├── base.py           # BaseTool with rate limiting
    ├── schemas.py        # OpenAI tool schemas
    ├── dns.py            # DNS + reverse DNS
    ├── network.py        # ASN, cert transparency, WHOIS, subnet probe
    ├── osint.py          # GitHub code search, web search
    ├── state.py          # Inventory read/write via API
    ├── registry.py       # Network + tool dispatch
    └── blockchain/
        ├── base.py       # ChainTools abstract base
        ├── sui.py        # Sui mainnet
        ├── solana.py     # Solana mainnet
        └── cosmos.py     # Cosmos Hub mainnet
```

## Running Tests

```bash
pytest
pytest -m "not integration"   # skip tests requiring a live API
```

## Why Ferret Exists

Protocol-native crawlers like [Nebula](https://github.com/dennis-tra/nebula) and [ethereum/node-crawler](https://github.com/ethereum/node-crawler) discover peers that participate in gossip. Monitoring tools like [solanamonitoring](https://github.com/stakeconomy/solanamonitoring) track your own node's health. Neither finds the infrastructure that operators run *alongside* their validators - the hosts that are often the most exposed and the least monitored.

Ferret was built by [NullRabbit](https://nullrabbit.ai), where we found that **40% of validators across major networks have critical vulnerabilities their operators don't know exist** - mostly on infrastructure that never appears in any peer table.

## Community

Ferret is open source under the [MIT License](LICENSE).

- 🐛 [Open an issue](https://github.com/NullRabbitLabs/ferret/issues) for bugs or feature requests
- 💬 [Discussions](https://github.com/NullRabbitLabs/ferret/discussions) for questions and ideas
- 🔧 PRs welcome - especially new chain implementations

---

Built by [NullRabbit](https://nullrabbit.ai) · Part of the NullRabbit open-source security toolkit
