"""
Ferret DiscoveryAgent: autonomous LLM agent loop for validator infrastructure discovery.

Calls /v1/chat/completions with tools, executes tool calls, loops until
finish_reason == 'stop' or budget exhausted.

Architecture
------------
Phase 1 (code): Seed on-chain hosts, then run batch enrichment (ASN + reverse DNS
                for unique IPs).  This is mechanical iteration — no LLM needed.

Phase 2 (LLM):  Give the agent the ASN cluster summary and a small tool budget.
                The LLM does OSINT: CT logs, WHOIS, GitHub/web, reporting new hosts.
                ASN/DNS tools are intentionally NOT in the LLM's tool list.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

_MAX_RESULT_CHARS = 3_000
_LIST_KEYS = ("hosts", "validators", "results", "records", "committee")

# Tools excluded from the LLM's tool list:
# - Code-only tools run in code (ASN + DNS) before the LLM loop
# - get_known_hosts: LLM browsing full inventory wastes budget without finding new hosts
# - bulk_report_discovered_hosts: internal batch import, not for LLM use
_CODE_ONLY_TOOLS = {"asn_lookup", "reverse_dns", "get_known_hosts", "bulk_report_discovered_hosts"}

from src.api_client import DiscoveryApiClient as Database
from src.db import DiscoveryRun, DiscoveryRunResult
from src.gateway_client import DiscoveryGatewayClient, GatewayResponse, ToolCall
from src.tools.registry import NetworkRegistry
from src.tools.state import StateTools

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_TEMPLATE = (Path(__file__).parent / "prompts" / "discovery.md").read_text()


class DiscoveryAgent:
    """
    Autonomous discovery agent.

    Phase 1: seed on-chain data, run batch ASN + reverse DNS enrichment in code.
    Phase 2: LLM OSINT loop (CT logs, WHOIS, GitHub, report new hosts).
    """

    def __init__(
        self,
        db: Database,
        gateway: DiscoveryGatewayClient,
        state_tools: StateTools,
        max_tool_calls: int = 30,
        max_consecutive_failures: int = 5,
        max_new_hosts: int = 10,
        max_idle_calls: int = 15,
    ) -> None:
        self._db = db
        self._gateway = gateway
        self._state_tools = state_tools
        self._max_tool_calls = max_tool_calls
        self._max_consecutive_failures = max_consecutive_failures
        self._max_new_hosts = max_new_hosts
        self._max_idle_calls = max_idle_calls
        # Tracking for honest summary and nudge injection
        self._discoveries: list[dict] = []
        self._get_hosts_calls: int = 0
        self._ct_failures: int = 0

    async def run(
        self,
        network: str,
        focus: str | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> DiscoveryRunResult:
        """
        Run a discovery session for the given network.

        Args:
            network: Network name (e.g. 'sui', 'solana')
            focus: Optional directive (e.g. 'new validators since last run')

        Returns:
            DiscoveryRunResult with run stats.
        """
        # Reset per-run tracking
        self._discoveries = []
        self._get_hosts_calls = 0
        self._ct_failures = 0

        network_record = await self._db.get_network(network)
        if not network_record:
            raise ValueError(f"Unknown network: {network!r}")

        chain_type = network_record["chain_type"]
        network_id: UUID = network_record["id"]

        run = await self._db.create_discovery_run(network_id)
        run_id_str = str(run.id)
        self._state_tools.init_run_stats(run_id_str)

        chain_tools = NetworkRegistry.get_chain_tools(chain_type)
        tool_map = self._build_tool_map(chain_type, run_id_str)

        # LLM tools: exclude chain-specific fetch tools (seeded in code)
        # and exclude code-only tools (ASN/DNS run in code, bulk import, etc.).
        chain_schema_names = {s["function"]["name"] for s in chain_tools.schemas()}
        tool_schemas = [
            s for s in NetworkRegistry.get_all_tool_schemas(chain_type)
            if s["function"]["name"] not in chain_schema_names
            and s["function"]["name"] not in _CODE_ONLY_TOOLS
        ]

        def _emit(msg: str) -> None:
            if on_event:
                on_event(msg)

        _emit(f"Starting discovery for {network} (run {run_id_str[:8]}...)")
        _emit(f"Max tool calls: {self._max_tool_calls}  |  {len(tool_schemas)} tools available")

        # ── Phase 1a: Seed on-chain data ──────────────────────────────────────
        _emit("\nSeeding inventory from on-chain data...")
        seed_hosts = await chain_tools.get_seed_hosts(network)
        seed_result: dict = {"total": 0, "new": 0, "updated": 0, "errors": []}
        if seed_hosts:
            seed_result = await self._state_tools.bulk_report_discovered_hosts(
                network, seed_hosts, run_id_str
            )
            _emit(
                f"  Seeded {seed_result['total']} on-chain addresses: "
                f"new={seed_result['new']} updated={seed_result['updated']}"
            )
        else:
            _emit("  No on-chain addresses found to seed.")

        # ── Phase 1b: Batch enrichment (ASN + reverse DNS) ────────────────────
        _emit("\nBatch enrichment (ASN + reverse DNS)...")
        enrich = await self._batch_enrich(network_id)
        _emit(
            f"  Sampled {enrich['sampled']} of {enrich['total_hosts']} IPs"
            f" → {len(enrich['clusters'])} ASN clusters"
        )

        # ── Phase 2: LLM OSINT loop ───────────────────────────────────────────
        system_prompt = await self._build_system_prompt(network, network_id, focus)
        messages: list[dict] = [
            {"role": "user", "content": self._build_initial_directive(network, seed_result, enrich, focus)}
        ]

        tool_call_count = 0
        consecutive_failures = 0
        idle_calls = 0
        new_hosts_found = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        _CONTEXT_COMPACT_EVERY = 10
        _CONTEXT_TAIL = 8

        while tool_call_count < self._max_tool_calls:
            if tool_call_count > 0 and tool_call_count % _CONTEXT_COMPACT_EVERY == 0:
                messages = _compact_messages(messages, tail=_CONTEXT_TAIL)
                _emit(f"  [context compacted to {len(messages)} messages]")

            _emit(f"\n[{tool_call_count}/{self._max_tool_calls}] Thinking...")
            response = await self._gateway.chat_with_tools(
                messages=messages,
                tools=tool_schemas,
                system_prompt=system_prompt,
            )
            total_prompt_tokens += response.prompt_tokens
            total_completion_tokens += response.completion_tokens

            if response.finish_reason == "stop" or not response.tool_calls:
                if response.text:
                    messages.append({"role": "assistant", "content": response.text})
                _emit(f"\nAgent finished. tokens={total_prompt_tokens + total_completion_tokens:,}")
                break

            _emit(f"  {len(response.tool_calls)} tool call(s) requested")
            messages.append(self._build_assistant_message(response))

            for tc in response.tool_calls:
                tool_call_count += 1
                args_preview = _format_args_preview(tc.arguments)
                _emit(f"  [{tool_call_count}] {tc.name}({args_preview})")
                result = await self._execute_tool(tc, tool_map)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _cap_tool_result_content(result),
                    }
                )

                if _is_error(result):
                    consecutive_failures += 1
                    err = result.get("error", "unknown error")
                    _emit(f"      ERROR: {err}  (consecutive={consecutive_failures})")
                    logger.warning(
                        "tool_call_failed tool=%s error=%s consecutive=%d",
                        tc.name,
                        err,
                        consecutive_failures,
                    )
                else:
                    consecutive_failures = 0
                    result_preview = _format_result_preview(result)
                    _emit(f"      -> {result_preview}")

                # Track successful report_discovered_host for honest summary
                if tc.name == "report_discovered_host" and not _is_error(result):
                    self._discoveries.append({
                        "ip": result.get("ip_address"),
                        "port": result.get("port"),
                        "service_type": tc.arguments.get("service_type"),
                        "is_new": result.get("is_new", False),
                    })
                    if result.get("is_new"):
                        new_hosts_found += 1
                        idle_calls = 0
                    else:
                        idle_calls += 1
                else:
                    idle_calls += 1

                # Track unfiltered get_known_hosts calls and inject nudge
                if tc.name == "get_known_hosts":
                    filters = tc.arguments.get("filters") or {}
                    has_filter = bool(
                        filters.get("operator_name")
                        or filters.get("service_type")
                    )
                    if not has_filter:
                        self._get_hosts_calls += 1
                        if self._get_hosts_calls > 2:
                            messages.append({
                                "role": "user",
                                "content": (
                                    "You have reviewed the full inventory multiple times. "
                                    "The inventory won't change. Focus on OSINT: "
                                    "cert_transparency_search, whois_lookup, or github_code_search "
                                    "to find new infrastructure."
                                ),
                            })

                # Track cert_transparency_search failures and inject nudge
                if tc.name == "cert_transparency_search" and (
                    _is_error(result) or result.get("note")
                ):
                    self._ct_failures += 1
                    if self._ct_failures >= 2:
                        messages.append({
                            "role": "user",
                            "content": (
                                "crt.sh is unavailable or failing. "
                                "Switch to whois_lookup or github_code_search instead."
                            ),
                        })

                if consecutive_failures >= self._max_consecutive_failures:
                    _emit(f"\nStopping: {consecutive_failures} consecutive failures")
                    logger.warning("stopping_consecutive_failures count=%d", consecutive_failures)
                    break

                if new_hosts_found >= self._max_new_hosts:
                    _emit(f"\nStopping: found {new_hosts_found} new hosts (target reached)")
                    break

                if idle_calls >= self._max_idle_calls:
                    _emit(f"\nStopping: {idle_calls} calls with no new discovery (diminishing returns)")
                    break

            run_stats = self._state_tools.get_run_stats(run_id_str)
            _emit(
                f"  hosts_new={run_stats.get('hosts_new', 0)}"
                f"  hosts_updated={run_stats.get('hosts_updated', 0)}"
                f"  tokens={total_prompt_tokens + total_completion_tokens:,}"
            )

            await self._db.update_run_stats(
                run.id,
                tool_calls=tool_call_count,
                tokens=total_prompt_tokens + total_completion_tokens,
            )

            if (
                consecutive_failures >= self._max_consecutive_failures
                or new_hosts_found >= self._max_new_hosts
                or idle_calls >= self._max_idle_calls
            ):
                break

        if tool_call_count >= self._max_tool_calls:
            _emit(f"\nStopping: reached tool call budget ({self._max_tool_calls})")

        _emit("\nGenerating summary...")
        run_stats = self._state_tools.get_run_stats(run_id_str)
        summary = await self._get_summary(messages, run_stats, new_discoveries=self._discoveries)

        await self._db.update_run_stats(
            run.id,
            hosts_new=run_stats.get("hosts_new", 0),
            hosts_updated=run_stats.get("hosts_updated", 0),
            hosts_gone=run_stats.get("hosts_gone", 0),
        )
        await self._db.complete_discovery_run(
            run.id,
            summary=summary,
            transcript=messages,
            status="completed",
        )

        logger.info(
            "discovery_run_complete network=%s run_id=%s tool_calls=%d hosts_new=%d hosts_updated=%d",
            network,
            run_id_str,
            tool_call_count,
            run_stats.get("hosts_new", 0),
            run_stats.get("hosts_updated", 0),
        )

        return DiscoveryRunResult(
            run_id=run.id,
            network=network,
            hosts_discovered=run_stats.get("hosts_new", 0) + run_stats.get("hosts_updated", 0),
            hosts_new=run_stats.get("hosts_new", 0),
            hosts_updated=run_stats.get("hosts_updated", 0),
            hosts_gone=run_stats.get("hosts_gone", 0),
            tool_calls=tool_call_count,
            llm_tokens_used=total_prompt_tokens + total_completion_tokens,
            summary=summary,
        )

    async def _batch_enrich(self, network_id: UUID, max_ips: int = 100) -> dict:
        """
        Run ASN + reverse DNS for unique IPs in code (no LLM).

        Caps at max_ips so large networks (Solana: 10k+ hosts) stay fast.
        Returns a cluster summary grouped by ASN for the LLM's initial context.
        """
        hosts = await self._db.get_hosts(network_id, is_active=True)
        total_hosts = len(hosts)

        seen: set[str] = set()
        sampled_ips: list[str] = []
        seen_hostnames: set[str] = set()
        hostname_only: list[str] = []
        for h in hosts:
            ip = str(h["ip_address"]) if h.get("ip_address") else None
            hostname = h.get("hostname")
            if ip and ip not in seen:
                seen.add(ip)
                sampled_ips.append(ip)
                if len(sampled_ips) >= max_ips:
                    break
            elif not ip and hostname and hostname not in seen_hostnames:
                seen_hostnames.add(hostname)
                hostname_only.append(hostname)

        universal = NetworkRegistry.get_universal_tool_map()
        asn_fn = universal.get("asn_lookup")
        rdns_fn = universal.get("reverse_dns")

        if not asn_fn or not rdns_fn:
            return {
                "clusters": [],
                "total_hosts": total_hosts,
                "sampled": len(sampled_ips),
                "hostname_only": hostname_only,
            }

        if not sampled_ips:
            return {"clusters": [], "total_hosts": total_hosts, "sampled": 0, "hostname_only": hostname_only}

        asn_results, rdns_results = await asyncio.gather(
            asyncio.gather(*[asn_fn(query=ip) for ip in sampled_ips], return_exceptions=True),
            asyncio.gather(*[rdns_fn(ip_address=ip) for ip in sampled_ips], return_exceptions=True),
        )

        clusters: dict[str, dict] = {}
        for i, ip in enumerate(sampled_ips):
            asn_r = asn_results[i] if not isinstance(asn_results[i], Exception) else {}
            rdns_r = rdns_results[i] if not isinstance(rdns_results[i], Exception) else {}

            asn_key = str(asn_r.get("asn") or "unknown")
            org = str(asn_r.get("as_org") or "")
            country = str(asn_r.get("country") or "")

            if asn_key not in clusters:
                clusters[asn_key] = {"org": org, "country": country, "ips": [], "domains": []}
            clusters[asn_key]["ips"].append(ip)

            for hostname in rdns_r.get("hostnames", []):
                if hostname not in clusters[asn_key]["domains"]:
                    clusters[asn_key]["domains"].append(hostname)

        sorted_clusters = sorted(clusters.items(), key=lambda x: -len(x[1]["ips"]))
        compact = [
            {
                "asn": asn_key,
                "org": data["org"][:40],
                "country": data["country"],
                "host_count": len(data["ips"]),
                "sample_ips": data["ips"][:2],
                "domains": data["domains"][:5],
            }
            for asn_key, data in sorted_clusters[:20]
        ]

        return {
            "total_hosts": total_hosts,
            "sampled": len(sampled_ips),
            "clusters": compact,
            "hostname_only": hostname_only,
        }

    def _build_tool_map(self, chain_type: str, run_id: str) -> dict:
        """Combine chain-specific, universal, and state tools into a single dispatch map."""
        tool_map: dict = {}

        chain_tools = NetworkRegistry.get_chain_tools(chain_type)
        tool_map.update(chain_tools.get_tool_map())

        tool_map.update(NetworkRegistry.get_universal_tool_map())

        state_map = self._state_tools.get_tool_map()
        # Wrap state tools to inject run_id
        for name, fn in state_map.items():
            async def _wrapper(fn=fn, **kwargs):
                return await fn(run_id=run_id, **kwargs)
            tool_map[name] = _wrapper

        return tool_map

    async def _build_system_prompt(
        self, network: str, network_id: UUID, focus: str | None
    ) -> str:
        validators = await self._db.get_validators(network_id)
        hosts = await self._db.get_hosts(network_id, is_active=True)
        runs = await self._db.get_recent_runs(network_id, limit=1)
        last_run = str(runs[0]["started_at"])[:19] if runs else "Never"

        return _SYSTEM_PROMPT_TEMPLATE.format(
            network=network,
            validator_count=len(validators),
            host_count=len(hosts),
            last_run_timestamp=last_run,
            focus=focus or "General discovery — update the full inventory.",
        )

    @staticmethod
    def _build_initial_directive(
        network: str, seed_result: dict, enrich: dict, focus: str | None
    ) -> str:
        new_count = seed_result.get("new", 0)
        updated_count = seed_result.get("updated", 0)
        total_count = seed_result.get("total", 0)
        clusters = enrich.get("clusters", [])
        sampled = enrich.get("sampled", 0)
        total_hosts = enrich.get("total_hosts", 0)
        hostname_only = enrich.get("hostname_only", [])

        directive = (
            f"Inventory seeded: {new_count} new, {updated_count} updated ({total_count} total).\n"
            f"ASN enrichment complete: sampled {sampled} of {total_hosts} IPs.\n"
            f"Do NOT call asn_lookup or reverse_dns — that work is done.\n\n"
        )

        if clusters:
            directive += "ASN CLUSTER SUMMARY:\n"
            for c in clusters[:15]:
                domains = ", ".join(c["domains"][:3]) or "none found"
                directive += (
                    f"  {c['asn']:12s} {c['org'][:35]:35s} ({c['country']})"
                    f"  {c['host_count']:3d} hosts  domains: {domains}\n"
                )
            directive += "\n"

        if hostname_only:
            directive += f"HOSTNAME-ONLY VALIDATORS ({len(hostname_only)} total, sample):\n"
            for h in hostname_only[:20]:
                directive += f"  {h}\n"
            if len(hostname_only) > 20:
                directive += f"  ... and {len(hostname_only) - 20} more\n"
            directive += (
                "These are stored by hostname only (dns4 multiaddr). "
                "Use their domains for cert_transparency_search or whois_lookup.\n\n"
            )

        directive += (
            "Your task is OSINT enrichment of these clusters. "
            "Pick 2-3 interesting clusters and:\n"
            "  1. cert_transparency_search for their operator-owned domains\n"
            "  2. whois_lookup on interesting registrars\n"
            "  3. github_code_search / web_search for operator configs if needed\n"
            "  4. report_discovered_host for any new IPs you find\n"
            "Stop when leads are exhausted. Do not re-check known hosts.\n"
        )
        if focus:
            directive += f"\nFocus: {focus}"
        return directive

    @staticmethod
    def _build_assistant_message(response: GatewayResponse) -> dict:
        msg: dict = {"role": "assistant", "content": response.text}
        if response.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in response.tool_calls
            ]
        return msg

    async def _execute_tool(self, tc: ToolCall, tool_map: dict) -> dict:
        """Dispatch a tool call and return the result dict."""
        fn = tool_map.get(tc.name)
        if not fn:
            return {"error": f"Unknown tool: {tc.name!r}"}

        try:
            result = await fn(**tc.arguments)
            return result if isinstance(result, dict) else {"result": result}
        except ValueError as e:
            return {"error": str(e), "tool": tc.name}
        except Exception as e:
            logger.error(
                "tool_execution_error tool=%s error_type=%s error=%s",
                tc.name,
                type(e).__name__,
                str(e),
            )
            return {"error": f"Tool error: {e}", "tool": tc.name}

    async def _get_summary(
        self,
        messages: list[dict],
        run_stats: dict,
        new_discoveries: list[dict] | None = None,
    ) -> str | None:
        """Ask the model to summarise what was discovered.

        Injects verified run stats and structured discovery list so the model
        cannot fabricate specific host names or counts.
        """
        hosts_new = run_stats.get("hosts_new", 0)
        hosts_updated = run_stats.get("hosts_updated", 0)
        hosts_gone = run_stats.get("hosts_gone", 0)

        discoveries = new_discoveries or []
        if discoveries:
            disc_lines = "\n".join(
                f"  - {d['ip']}:{d['port']} ({d['service_type']}) new={d['is_new']}"
                for d in discoveries
            )
            discoveries_section = f"\nDiscovered hosts:\n{disc_lines}"
        else:
            discoveries_section = ""

        summary_messages = messages + [
            {
                "role": "user",
                "content": (
                    f"Session complete. Verified stats: {hosts_new} new hosts, "
                    f"{hosts_updated} updated, {hosts_gone} marked gone."
                    f"{discoveries_section}\n\n"
                    "Summarise ONLY from the following data. Do NOT invent details. "
                    "Provide a concise summary (under 200 words) of what you discovered: "
                    "which clusters were investigated, what OSINT sources were used, and any "
                    "notable findings. Use only the verified stats above — do not invent numbers."
                ),
            }
        ]
        try:
            response = await self._gateway.chat_with_tools(
                messages=summary_messages,
                tools=[],  # No tools for summary
            )
            return response.text
        except Exception as e:
            logger.warning("summary_failed error=%s", str(e))
            return None


def _cap_tool_result_content(result: dict, max_chars: int = _MAX_RESULT_CHARS) -> str:
    """Serialize a tool result, truncating large list fields to stay within max_chars."""
    raw = json.dumps(result, default=str)
    if len(raw) <= max_chars:
        return raw
    for key in _LIST_KEYS:
        items = result.get(key)
        if not isinstance(items, list) or not items:
            continue
        # Proportional estimate: how many items fit in the budget
        keep = max(1, int(max_chars * len(items) / len(raw)))
        trimmed = {**result, key: items[:keep], "_truncated": f"{len(items) - keep} more items omitted"}
        return json.dumps(trimmed, default=str)
    return raw[:max_chars] + " [truncated]"


def _compact_messages(messages: list[dict], tail: int = 8) -> list[dict]:
    """Keep the first message and the last `tail` messages.

    Replaces the middle with a single reminder to focus on OSINT.
    Counts report_discovered_host results in dropped messages and includes
    the count in the bridge message so the agent knows how many it reported.
    """
    if len(messages) <= tail + 2:
        return messages
    first = messages[0]
    dropped = messages[1:-tail]
    recent = messages[-tail:]

    report_count = sum(
        1 for msg in dropped
        if msg.get("role") == "tool" and '"is_new"' in (msg.get("content") or "")
    )
    discovery_note = f" Already reported {report_count} hosts." if report_count else ""

    bridge = {
        "role": "user",
        "content": (
            f"[Earlier context compacted.{discovery_note} Your initial directive (with ASN cluster summary) "
            "is in the first message. Focus on OSINT: cert_transparency_search, "
            "whois_lookup, github_code_search, report_discovered_host.]"
        ),
    }
    return [first, bridge, *recent]


def _is_error(result: dict) -> bool:
    """Return True if the tool result indicates a failure."""
    return "error" in result


def _format_args_preview(args: dict) -> str:
    """Compact single-line preview of tool arguments (max ~60 chars)."""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        v_str = str(v)
        if len(v_str) > 30:
            v_str = v_str[:27] + "..."
        parts.append(f"{k}={v_str!r}")
    preview = ", ".join(parts)
    if len(preview) > 80:
        preview = preview[:77] + "..."
    return preview


def _format_result_preview(result: dict) -> str:
    """One-line summary of a successful tool result."""
    if "count" in result:
        return f"count={result['count']}"
    if "records" in result:
        return f"{len(result['records'])} record(s)"
    if "results" in result:
        return f"{len(result['results'])} result(s)"
    if "hosts" in result:
        return f"{len(result['hosts'])} host(s)"
    if "validators" in result:
        return f"{len(result['validators'])} validator(s)"
    if "open_count" in result:
        return f"open={result['open_count']}/{result.get('total_probed', '?')}"
    if "asn" in result:
        return f"asn={result['asn']} org={result.get('as_org', '?')}"
    if "ip" in result:
        return f"ip={result['ip']}"
    if "host_id" in result:
        is_new = result.get("is_new", False)
        return f"host_id={str(result['host_id'])[:8]} new={is_new}"
    # Fallback: show first key=value
    first = next(iter(result.items()), None)
    if first:
        v = str(first[1])
        return f"{first[0]}={v[:40]}"
    return "ok"
