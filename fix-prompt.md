# Discovery Agent — Fixes

You are working in the `discovery-agent/` directory of the `nr-scan` monorepo. The code is already built and running against live Sui/Solana mainnet. The database schema exists. These are fixes based on a code review and a live run.

TDD — write or update tests for each fix before implementing.

---

## Critical Fixes

### 1. Fix hallucinated summary

The summary is completely fabricated. After a live run that found 3 new hosts, the summary mentioned "val-88", "Host-07", "South America region", "protocol upgrade v2.1" — none of which happened. `_get_summary` asks the LLM to summarise compacted conversation history which has lost most real context.

**Fix:** Track discoveries as they happen in the agent loop. After each successful `report_discovered_host` call, append to a `self._discoveries` list:

```python
{"ip": ..., "port": ..., "service_type": ..., "method": ..., "reasoning": ..., "is_new": ...}
```

In `_get_summary`, pass structured run stats and the discoveries list instead of the conversation:

```python
summary_data = {
    "hosts_new": run_stats.get("hosts_new", 0),
    "hosts_updated": run_stats.get("hosts_updated", 0),
    "hosts_gone": run_stats.get("hosts_gone", 0),
    "new_discoveries": self._discoveries,
}
```

Tell the LLM: "Summarise ONLY from the following data. Do NOT invent details. If nothing notable was found, say so."

### 2. Prevent reporting CDN/website IPs as validator hosts

The agent did DNS lookups on operator websites (kunalabs.io, stakely.io), got Cloudflare IPs (104.21.x.x, 172.67.x.x), and reported them as validator infrastructure. These are false positives.

**Fix:** Add CDN IP detection to `report_discovered_host` in `src/tools/state.py`. Before inserting, check against known CDN ranges:

```python
import ipaddress

_CDN_RANGES = [
    ipaddress.ip_network("104.16.0.0/13"),   # Cloudflare
    ipaddress.ip_network("104.24.0.0/14"),   # Cloudflare
    ipaddress.ip_network("172.64.0.0/13"),   # Cloudflare
    ipaddress.ip_network("103.21.244.0/22"), # Cloudflare
    ipaddress.ip_network("103.22.200.0/22"), # Cloudflare
    ipaddress.ip_network("103.31.4.0/22"),   # Cloudflare
    ipaddress.ip_network("141.101.64.0/18"), # Cloudflare
    ipaddress.ip_network("108.162.192.0/18"),# Cloudflare
    ipaddress.ip_network("190.93.240.0/20"), # Cloudflare
    ipaddress.ip_network("188.114.96.0/20"), # Cloudflare
    ipaddress.ip_network("197.234.240.0/22"),# Cloudflare
    ipaddress.ip_network("198.41.128.0/17"), # Cloudflare
    ipaddress.ip_network("162.158.0.0/15"),  # Cloudflare
    ipaddress.ip_network("151.101.0.0/16"),  # Fastly
    ipaddress.ip_network("23.0.0.0/12"),     # Akamai
]

def _is_cdn_ip(ip_str):
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in _CDN_RANGES)
    except ValueError:
        return False
```

If `_is_cdn_ip(ip_address)` returns True, return an error dict immediately — do not insert:

```python
if ip_address and _is_cdn_ip(ip_address):
    return {"error": f"Rejected: {ip_address} is a CDN/proxy IP. Not validator infrastructure.", "ip_address": ip_address}
```

Also update `src/prompts/discovery.md` — add under Constraints:
```
- Do NOT report website/CDN IPs as validator hosts. Operator domains (omnistake.com,
  kunalabs.io, figment.io) are websites. DNS A records for these point to CDN edge
  nodes, not validators. Only report IPs with evidence of running validator software.
```

### 3. Fix Solana `get_seed_hosts` deduplication

In `src/tools/blockchain/solana.py`, `get_seed_hosts` deduplicates by IP alone (`seen_ips: set[str]`). If a node has gossip on `1.2.3.4:8001` and RPC on `1.2.3.4:8899`, only gossip is recorded.

**Fix:** Change to `seen: set[tuple[str, int]]`, key on `(ip, port)`.

### 4. Remove `solana_get_validators_info`

`getValidatorsInfo` is not a real Solana JSON-RPC method. It errors on every call.

**Fix:** Remove the schema constant, the `get_validators_info` method, and its entries from `schemas()` and `get_tool_map()` in `SolanaTools`.

### 5. Fix `search_hypotheses` SQL in `src/db.py`

Two bugs:
- `having_clause` built with f-string interpolation (SQL injection risk)
- The variable is never used in the query (dead code — feature silently broken)

**Fix:** Simplify. When `min_success_rate` is provided, filter on `validated = true`. Parameterised queries only:

```python
async def search_hypotheses(self, embedding, min_success_rate=None, limit=10):
    conditions = ["embedding IS NOT NULL"]
    args = [embedding, limit]
    if min_success_rate is not None:
        conditions.append("validated = true")
    where = " AND ".join(conditions)
    query = f"""
        SELECT id, hypothesis, method, confidence_before,
               validated, hosts_found, created_at,
               1 - (embedding <=> $1::vector) AS similarity
        FROM discovery.discovery_hypotheses
        WHERE {where}
        ORDER BY embedding <=> $1::vector
        LIMIT $2
    """
    async with self.acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [dict(r) for r in rows]
```

---

## Behavioural Fixes

### 6. Stop the agent re-querying the full inventory

The agent called `get_known_hosts` 6 times without filters — 20% of its tool budget wasted.

**Fix:**

a) Update `src/prompts/discovery.md`, add to "What Has Already Been Done For You":
```
3. **Host inventory** — you already know what's in the database from the seed
   results above. Do NOT call get_known_hosts to review the full inventory.
   Only use get_known_hosts with SPECIFIC filters (operator_name, service_type).
```

b) In the agent loop, after executing `get_known_hosts`, if called unfiltered more than twice, inject a nudge:
```python
if tc.name == "get_known_hosts":
    self._get_hosts_calls = getattr(self, "_get_hosts_calls", 0) + 1
    filters = tc.arguments.get("filters") or {}
    if self._get_hosts_calls > 2 and not any(filters.values() if isinstance(filters, dict) else []):
        messages.append({
            "role": "user",
            "content": (
                "You have queried the full inventory multiple times. Data hasn't changed. "
                "Focus on OSINT — cert_transparency_search, whois_lookup, github_code_search. "
                "Only use get_known_hosts with specific filters."
            ),
        })
```

### 7. Handle crt.sh failures gracefully

crt.sh returned 503 three times but the agent kept trying CT searches.

**Fix:**

a) In `CertTransparencySearchTool.execute` in `src/tools/network.py`, catch `httpx.HTTPStatusError` explicitly (currently caught by bare `except Exception` with ugly Mozilla docs URL in the message):
```python
except httpx.HTTPStatusError as e:
    if e.response.status_code == 503:
        return {"query": query, "results": [], "note": "crt.sh unavailable (503). Skip CT searches and use other tools."}
    return {"query": query, "results": [], "error": f"HTTP {e.response.status_code}"}
```

b) In the agent loop, track CT failures. After 2, inject guidance:
```python
if tc.name == "cert_transparency_search" and _is_error(result):
    self._ct_failures = getattr(self, "_ct_failures", 0) + 1
    if self._ct_failures >= 2:
        messages.append({
            "role": "user",
            "content": "crt.sh is unreliable today (2+ failures). Stop using cert_transparency_search. Use whois_lookup, github_code_search, or web_search instead.",
        })
```

### 8. Reduce token waste from large unfiltered responses

Each `get_known_hosts` returned 136 records. Called 6 times = massive context bloat.

**Fix:** In `StateTools.get_known_hosts`, when result set is large (>20) and no meaningful filters, return a count summary:

```python
has_filters = filters and any(v for v in filters.values() if v is not None)
if total > 20 and not has_filters:
    service_counts = {}
    for h in hosts:
        stype = h.get("service_type", "unknown")
        service_counts[stype] = service_counts.get(stype, 0) + 1
    return {
        "network": network,
        "count": total,
        "by_service_type": service_counts,
        "note": "Large inventory. Use filters (operator_name, service_type) to see individual hosts.",
    }
```

---

## Code Quality Fixes

### 9. Reuse httpx.AsyncClient in gateway client

`src/gateway_client.py` creates a new `httpx.AsyncClient` per request.

**Fix:** Create the client in `__init__`, add `async close()`, update `cli.py` to close it in finally blocks alongside `db.close()`.

### 10. Fix `_build_tool_map` closure pattern

In `src/agent.py`, replace the async closure with `functools.partial`:

```python
from functools import partial
for name, fn in state_map.items():
    tool_map[name] = partial(fn, run_id=run_id)
```

### 11. Exclude `bulk_report_discovered_hosts` from LLM tools

Bulk reporting is Phase 1 code, not LLM. In `src/agent.py`:

```python
_CODE_ONLY_TOOLS = {"asn_lookup", "reverse_dns", "bulk_report_discovered_hosts"}
```

Rename from `_BATCH_ENRICHMENT_TOOLS`.

### 12. Improve context compaction

`_compact_messages` drops all middle context. The agent re-investigates clusters.

**Fix:** Count discoveries in dropped messages, include in bridge:

```python
def _compact_messages(messages, tail=8):
    if len(messages) <= tail + 2:
        return messages
    dropped = messages[1:-tail]
    report_count = sum(
        1 for msg in dropped
        if msg.get("role") == "tool" and '"is_new"' in (msg.get("content") or "")
    )
    summary = f" Already reported {report_count} hosts." if report_count else ""
    bridge = {
        "role": "user",
        "content": (
            f"[Earlier context compacted.{summary} "
            "Initial directive with ASN clusters is in the first message. "
            "Focus on OSINT: cert_transparency_search, whois_lookup, "
            "github_code_search, report_discovered_host.]"
        ),
    }
    return [messages[0], bridge, *messages[-tail:]]
```

### 13. Add Sui UDP/QUIC multiaddr parsing

In `src/tools/blockchain/sui.py`, `_parse_multiaddr` only handles TCP. Newer Sui validators use UDP/QUIC for P2P.

**Fix:**
```python
def _parse_multiaddr(addr):
    if not addr:
        return None, None
    m = re.search(r"/(?:ip4|ip6|dns4?)/([^/]+)/tcp/(\d+)", addr)
    if m:
        return m.group(1), int(m.group(2))
    m = re.search(r"/(?:ip4|ip6|dns4?)/([^/]+)/udp/(\d+)", addr)
    if m:
        return m.group(1), int(m.group(2))
    return None, None
```

### 14. Deduplicate CT log results

crt.sh returns duplicates. In `CertTransparencySearchTool.execute`, deduplicate before returning:

```python
seen = set()
deduped = []
for entry in results:
    key = (entry["common_name"], entry.get("not_before"))
    if key not in seen:
        seen.add(key)
        deduped.append(entry)
results = deduped
```

### 15. Populate operator_name during Sui seeding

`get_validators` parses the `name` field but `get_seed_hosts` doesn't pass it through.

**Fix:** Include `"operator_name": v.get("name")` in seed host dicts. Update `db.get_or_create_validator` to accept optional `operator_name` and set it via COALESCE on upsert. Update `bulk_report_discovered_hosts` to pass it through.

### 16. Fix gateway port default

In `src/config.py`, change default from `http://localhost:8090` to `http://localhost:8787`.

---

## Tests

Write tests before implementing fixes:

- `tests/test_sui_tools.py` — multiaddr parsing (TCP, UDP/QUIC, IPv6, dns4, None, empty), get_seed_hosts with mock RPC, operator name extraction
- `tests/test_solana_tools.py` — get_seed_hosts dedup (same IP different ports all returned), verify getValidatorsInfo removed from schemas and tool map
- `tests/test_subnet_probe.py` — CIDR > /24 rejected, invalid ports rejected, residential ranges rejected
- `tests/test_state_tools.py` — report_discovered_host run_id tracking, bulk_report counts, flag_host_gone
- `tests/test_cdn_detection.py` — Cloudflare IPs rejected (104.21.x.x, 172.67.x.x), Fastly rejected (151.101.x.x), legitimate IPs accepted (44.198.x.x, 65.108.x.x), report_discovered_host returns error for CDN IPs
- `tests/test_agent.py` — terminates on "stop", terminates on max_tool_calls, terminates on consecutive failures, context compaction fires correctly, discovery tracking captures results, get_known_hosts nudge after 2 unfiltered calls, CT failure nudge after 2 errors
- `tests/test_summary.py` — summary uses structured data, empty discoveries produce honest output, no fabrication possible when structured data passed
- `tests/test_db.py` — search_hypotheses parameterised (no SQL injection), upsert new vs update, get_or_create_validator with operator_name
- `tests/test_schemas.py` — all schemas have required fields, no duplicate tool names

Mock all external I/O. Do NOT mock constraint validation in subnet_probe or CDN detection — test those for real.

## Priority Order

1. **#1** hallucinated summary — fabricating data is the worst possible bug
2. **#2** CDN detection — false positives undermine credibility
3. **#6 + #8** inventory re-querying + token waste — 20% budget burned
4. **#7** crt.sh handling — agent doesn't adapt to failures
5. **#3** Solana dedup — losing real hosts
6. **#4** remove broken tool — errors every call
7. **#5** SQL fix — injection risk
8. Everything else in any order
