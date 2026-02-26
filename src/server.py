"""FastAPI HTTP server for ferret (discovery agent).

Exposes:
  GET  /health   → liveness probe
  POST /discover → starts a background discovery run; returns run_id immediately
"""

import asyncio
import logging
from uuid import UUID

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.agent import DiscoveryAgent
from src.api_client import DiscoveryApiClient
from src.config import Config
from src.gateway_client import DiscoveryGatewayClient
from src.networks import NETWORK_DEFINITIONS
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

logger = logging.getLogger(__name__)

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

    for chain_name, (tools_cls, _, __) in NETWORK_DEFINITIONS.items():
        if chain_name not in NetworkRegistry.registered_chains():
            NetworkRegistry.register(chain_name, tools_cls(rpc_url=config.rpc_urls[chain_name]))

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


async def _run_agent(network: str, focus: str | None, run_id: UUID, config: Config) -> None:
    """Background task: run the full discovery agent loop."""
    db, gateway, state_tools = await _setup(config)
    try:
        agent = DiscoveryAgent(
            db=db,
            gateway=gateway,
            state_tools=state_tools,
            max_tool_calls=config.max_tool_calls,
            max_new_hosts=config.max_new_hosts,
            max_idle_calls=config.max_idle_calls,
        )
        await agent.run(network=network, focus=focus, existing_run_id=run_id)
    except Exception:
        logger.exception("background agent run failed network=%s run_id=%s", network, run_id)
        try:
            await db.complete_discovery_run(run_id, status="failed")
        except Exception:
            pass
    finally:
        await db.close()
        await gateway.close()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/discover")
async def discover(body: DiscoverRequest) -> dict:
    config = Config.from_env()
    logger.info("discover request received network=%s focus=%s", body.network, body.focus)
    if body.network not in NETWORK_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"Unknown network: {body.network!r}")
    db = DiscoveryApiClient(config.discovery_api_url)
    try:
        run = await db.create_discovery_run(body.network)
        asyncio.create_task(_run_agent(body.network, body.focus, run.id, config))
        logger.info("discovery run started network=%s run_id=%s", body.network, run.id)
        return {"run_id": str(run.id), "status": "running"}
    finally:
        await db.close()
