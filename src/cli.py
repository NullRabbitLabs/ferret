"""
CLI entry point for the discovery agent.

Commands:
  discover  --network <name> [--focus <text>]
  inventory --network <name> [--stale-since <days>]
  runs      --network <name> [--last <n>]
  diff      --network <name> --since <datetime>
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from dotenv import load_dotenv

# Load .env from project root, then parent directory
_here = Path(__file__).parent.parent
load_dotenv(_here / ".env")
load_dotenv(_here.parent / ".env")

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


def _get_config() -> Config:
    return Config.from_env()


async def _setup(config: Config) -> tuple[DiscoveryApiClient, DiscoveryGatewayClient, StateTools]:
    db = DiscoveryApiClient(config.discovery_api_url)

    gateway = DiscoveryGatewayClient(config.llm_gateway_url, model=config.llm_model)

    # Register blockchain tools
    for chain_name, (tools_cls, _, __) in NETWORK_DEFINITIONS.items():
        if chain_name not in NetworkRegistry.registered_chains():
            NetworkRegistry.register(chain_name, tools_cls(rpc_url=config.rpc_urls[chain_name]))

    # Register universal tools (dns, network, osint) — only once per process
    if not NetworkRegistry.get_universal_tool_map():
        dns_tool = DnsLookupTool()
        reverse_dns_tool = ReverseDnsTool()
        asn_tool = AsnLookupTool()
        cert_tool = CertTransparencySearchTool()
        whois_tool = WhoisLookupTool()
        subnet_tool = SubnetProbeTool()
        github_tool = GithubCodeSearchTool(github_token=config.github_token)
        web_tool = WebSearchTool(serp_api_key=config.serp_api_key)
        NetworkRegistry.register_universal_tools({
            "dns_lookup": dns_tool.execute,
            "reverse_dns": reverse_dns_tool.execute,
            "asn_lookup": asn_tool.execute,
            "cert_transparency_search": cert_tool.execute,
            "whois_lookup": whois_tool.execute,
            "subnet_probe": subnet_tool.execute,
            "github_code_search": github_tool.execute,
            "web_search": web_tool.execute,
        })

    state_tools = StateTools(db=db, gateway_client=gateway)
    return db, gateway, state_tools


@click.group()
def cli() -> None:
    """Ferret — autonomous DeFi validator infrastructure discovery."""


@cli.command()
@click.option("--network", required=True, help="Network to discover (e.g. sui, solana)")
@click.option("--focus", default=None, help="Optional discovery focus directive")
def discover(network: str, focus: str | None) -> None:
    """Run an autonomous discovery session for the specified network."""

    async def _run() -> None:
        config = _get_config()
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
            result = await agent.run(network=network, focus=focus, on_event=click.echo)
            click.echo(f"\nDiscovery complete for {network}")
            click.echo(f"  Run ID:        {result.run_id}")
            click.echo(f"  Hosts new:     {result.hosts_new}")
            click.echo(f"  Hosts updated: {result.hosts_updated}")
            click.echo(f"  Hosts gone:    {result.hosts_gone}")
            click.echo(f"  Tool calls:    {result.tool_calls}")
            click.echo(f"  LLM tokens:    {result.llm_tokens_used}")
            if result.summary:
                click.echo(f"\nSummary:\n{result.summary}")
        finally:
            await db.close()
            await gateway.close()

    asyncio.run(_run())


@cli.command()
@click.option("--network", required=True, help="Network name")
@click.option(
    "--stale-since",
    default=None,
    help="Show only hosts not seen in N days (e.g. '7' for 7 days)",
)
def inventory(network: str, stale_since: str | None) -> None:
    """Show current host inventory for a network."""

    async def _run() -> None:
        config = _get_config()
        db, _, _ = await _setup(config)
        try:
            if network not in NETWORK_DEFINITIONS:
                click.echo(f"Unknown network: {network!r}", err=True)
                return

            not_seen_since = None
            if stale_since:
                days = int(stale_since)
                not_seen_since = datetime.now(timezone.utc) - timedelta(days=days)

            hosts = await db.get_hosts(
                network,
                is_active=True,
                not_seen_since=not_seen_since,
            )
            click.echo(f"Inventory for {network}: {len(hosts)} active hosts")
            for h in hosts:
                ip_port = f"{h['ip_address']}:{h['port']}" if h.get("port") else h["ip_address"]
                click.echo(
                    f"  {ip_port:30s}  {h.get('service_type', 'unknown'):10s}  "
                    f"conf={h['confidence']:.2f}  "
                    f"seen={str(h.get('last_seen_at', ''))[:10]}"
                )
        finally:
            await db.close()

    asyncio.run(_run())


@cli.command()
@click.option("--network", required=True, help="Network name")
@click.option("--last", default=5, type=int, help="Number of recent runs to show")
def runs(network: str, last: int) -> None:
    """Show recent discovery runs for a network."""

    async def _run() -> None:
        config = _get_config()
        db, _, _ = await _setup(config)
        try:
            if network not in NETWORK_DEFINITIONS:
                click.echo(f"Unknown network: {network!r}", err=True)
                return

            recent = await db.get_recent_runs(network, limit=last)
            click.echo(f"Recent runs for {network}:")
            for r in recent:
                started = str(r["started_at"])[:16]
                status = r["status"]
                click.echo(
                    f"  [{started}] {status:12s}  "
                    f"new={r['hosts_new']}  updated={r['hosts_updated']}  "
                    f"gone={r['hosts_gone']}  tools={r['tool_calls']}"
                )
                if r.get("summary"):
                    click.echo(f"    Summary: {r['summary'][:120]}...")
        finally:
            await db.close()

    asyncio.run(_run())


@cli.command()
@click.option("--network", required=True, help="Network name")
@click.option("--since", required=True, help="ISO datetime to diff from (e.g. 2026-02-17)")
def diff(network: str, since: str) -> None:
    """Show infrastructure changes since a given datetime."""

    async def _run() -> None:
        config = _get_config()
        db, _, _ = await _setup(config)
        try:
            if network not in NETWORK_DEFINITIONS:
                click.echo(f"Unknown network: {network!r}", err=True)
                return

            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)

            result = await db.get_discovery_diff(network, since_dt)
            click.echo(f"Diff for {network} since {since}:")
            click.echo(f"  New hosts:       {len(result['new_hosts'])}")
            click.echo(f"  Gone hosts:      {len(result['gone_hosts'])}")
            click.echo(f"  Changed hosts:   {len(result['changed_hosts'])}")
            click.echo(f"  New validators:  {len(result['new_validators'])}")
            click.echo(json.dumps(result, indent=2, default=str))
        finally:
            await db.close()

    asyncio.run(_run())


def main() -> None:
    cli()
