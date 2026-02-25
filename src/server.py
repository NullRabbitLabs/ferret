"""FastAPI HTTP server for ferret (discovery agent).

Exposes:
  GET  /health   → liveness probe
  POST /discover → runs the LLM agent loop for a given network
"""

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from src.agent import DiscoveryAgent
from src.api_client import DiscoveryApiClient
from src.config import Config
from src.gateway_client import DiscoveryGatewayClient
from src.tools.blockchain.solana import SolanaTools
from src.tools.blockchain.sui import SuiTools
from src.tools.dns import DnsLookupTool, ReverseDnsTool
from src.tools.network import (
    AsnLookupTool,
    CertTransparencySearchTool,
    SubnetProbeTool,
    WhoisLookupTool,
)
from src.tools.osint import GithubCodeSearchTool, WebSearchTool
from src.tools.registry import NetworkRegistry
from src.tools.state import StateTools

app = FastAPI(title="ferret")


class DiscoverRequest(BaseModel):
    network: str
    focus: str | None = None


async def _setup(
    config: Config,
) -> tuple[DiscoveryApiClient, DiscoveryGatewayClient, StateTools]:
    """Initialise API client, LLM gateway, and register all tools."""
    db = DiscoveryApiClient(config.discovery_api_url)
    gateway = DiscoveryGatewayClient(config.llm_gateway_url, model=config.llm_model)

    if "sui" not in NetworkRegistry.registered_chains():
        NetworkRegistry.register("sui", SuiTools(rpc_url=config.sui_rpc_url))
    if "solana" not in NetworkRegistry.registered_chains():
        NetworkRegistry.register("solana", SolanaTools(rpc_url=config.solana_rpc_url))

    if not NetworkRegistry.get_universal_tool_map():
        NetworkRegistry.register_universal_tools({
            "dns_lookup": DnsLookupTool().execute,
            "reverse_dns": ReverseDnsTool().execute,
            "asn_lookup": AsnLookupTool().execute,
            "cert_transparency_search": CertTransparencySearchTool().execute,
            "whois_lookup": WhoisLookupTool().execute,
            "subnet_probe": SubnetProbeTool().execute,
            "github_code_search": GithubCodeSearchTool(github_token=config.github_token).execute,
            "web_search": WebSearchTool(serp_api_key=config.serp_api_key).execute,
        })

    state_tools = StateTools(db=db, gateway_client=gateway)
    return db, gateway, state_tools


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/discover")
async def discover(body: DiscoverRequest) -> dict:
    config = Config.from_env()
    db, gateway, state_tools = await _setup(config)
    try:
        network = await db.get_network(body.network)
        if network is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown network: {body.network}"
            )
        agent = DiscoveryAgent(
            db=db,
            gateway=gateway,
            state_tools=state_tools,
            max_tool_calls=config.max_tool_calls,
            max_new_hosts=config.max_new_hosts,
            max_idle_calls=config.max_idle_calls,
        )
        result = await agent.run(network=body.network, focus=body.focus)
        return jsonable_encoder(result)
    finally:
        await db.close()
        await gateway.close()
