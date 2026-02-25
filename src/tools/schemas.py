"""
OpenAI-format tool schemas for the discovery agent.

All 14 tool definitions as Python dicts. No logic — pure schema data.
These are passed to /v1/chat/completions as the `tools` parameter.
"""

# ============================================================
# Network intelligence tools (universal across all chains)
# ============================================================

DNS_LOOKUP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "dns_lookup",
        "description": (
            "Perform a DNS lookup for a hostname. Returns records with TTL. "
            "Useful for resolving validator hostnames to IPs and finding service endpoints."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "hostname": {
                    "type": "string",
                    "description": "The hostname to resolve (e.g. 'validator.example.com')",
                },
                "record_type": {
                    "type": "string",
                    "enum": ["A", "AAAA", "PTR", "MX", "TXT", "NS", "SRV", "CNAME"],
                    "description": "DNS record type to query",
                },
            },
            "required": ["hostname", "record_type"],
        },
    },
}

REVERSE_DNS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "reverse_dns",
        "description": (
            "Perform a reverse DNS lookup (PTR record) for an IP address. "
            "Useful for identifying hostnames behind validator IPs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ip_address": {
                    "type": "string",
                    "description": "IPv4 or IPv6 address to reverse-lookup",
                },
            },
            "required": ["ip_address"],
        },
    },
}

ASN_LOOKUP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "asn_lookup",
        "description": (
            "Look up ASN (Autonomous System Number) information for an IP address or ASN number. "
            "Given an IP returns {asn, as_org, prefix, country}. "
            "Given an ASN number returns {as_org, announced_prefixes}. "
            "Use this to identify hosting providers and find co-located infrastructure."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "IP address (e.g. '1.2.3.4') or ASN number (e.g. 'AS13335')",
                },
            },
            "required": ["query"],
        },
    },
}

CERT_TRANSPARENCY_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "cert_transparency_search",
        "description": (
            "Search certificate transparency logs (crt.sh) for TLS certificates matching a specific operator domain. "
            "Only use for operator-owned domains you identified from the cluster summary or OSINT — "
            "e.g. 'validator.myoperator.io' or '%.sui-nodes.net'. "
            "Do NOT search generic hosting/cloud providers (amazonaws.com, digitalocean.com, hetzner.com, "
            "cherryservers.net, vultr.com, etc.) — those return unrelated customer certs, not validator infra. "
            "Results are cached for 24 hours."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Operator-specific domain to search (e.g. '%.myvalidator.io'). NOT hosting provider domains.",
                },
            },
            "required": ["query"],
        },
    },
}

WHOIS_LOOKUP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "whois_lookup",
        "description": (
            "Perform a WHOIS lookup for an operator-owned domain name. "
            "Returns registrant info, creation/expiry dates, and nameservers. "
            "Only use for specific operator domains you identified from OSINT — "
            "NOT for IP addresses (use asn_lookup instead) and NOT for hosting provider "
            "domains (amazonaws.com, hetzner.com, digitalocean.com, etc.)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Operator-owned domain name to look up (e.g. 'myoperator.io'). NOT an IP address or hosting provider domain.",
                },
            },
            "required": ["query"],
        },
    },
}

SUBNET_PROBE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "subnet_probe",
        "description": (
            "Actively probe a subnet CIDR for open ports. "
            "Returns {ip, port, open, banner} for each combination. "
            "IMPORTANT: Maximum /24 (256 hosts). Use only ports from the network allowlist. "
            "Every call is logged. Use sparingly — prefer passive tools first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cidr": {
                    "type": "string",
                    "description": "CIDR to probe, max /24 (e.g. '192.168.1.0/24')",
                },
                "ports": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of ports to check. Must be from the network port allowlist.",
                },
            },
            "required": ["cidr", "ports"],
        },
    },
}

# ============================================================
# State management tools
# ============================================================

GET_KNOWN_HOSTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_known_hosts",
        "description": (
            "Retrieve known hosts from the discovery database for a network. "
            "Returns at most `limit` hosts (default 50). Use filters to focus on a specific "
            "operator or service type. For large networks (Solana, etc.) always filter rather "
            "than fetching the full inventory — there may be thousands of hosts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "network": {
                    "type": "string",
                    "description": "Network name (e.g. 'sui', 'solana')",
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "Optional filters: operator_name, service_type, "
                        "min_confidence (float), is_active (bool), not_seen_since (ISO datetime)"
                    ),
                    "properties": {
                        "operator_name": {"type": "string"},
                        "service_type": {"type": "string"},
                        "min_confidence": {"type": "number"},
                        "is_active": {"type": "boolean"},
                        "not_seen_since": {"type": "string"},
                    },
                },
                "limit": {
                    "type": "integer",
                    "description": "Max hosts to return (default 50, max 200).",
                },
            },
            "required": ["network"],
        },
    },
}

GET_KNOWN_VALIDATORS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_known_validators",
        "description": (
            "Retrieve the list of known validators for a network with host counts. "
            "Use this to understand what validators we already track."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "network": {
                    "type": "string",
                    "description": "Network name (e.g. 'sui', 'solana')",
                },
            },
            "required": ["network"],
        },
    },
}

REPORT_DISCOVERED_HOST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "report_discovered_host",
        "description": (
            "Report a discovered host to the inventory database. "
            "Will UPSERT — if (network, ip, port) already exists, updates last_seen_at. "
            "Returns {id, is_new} to tell you if this is a new discovery."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "network": {
                    "type": "string",
                    "description": "Network name (e.g. 'sui', 'solana')",
                },
                "ip_address": {
                    "type": "string",
                    "description": "IP address of the discovered host",
                },
                "port": {
                    "type": "integer",
                    "description": "Port number (null if not port-specific)",
                },
                "protocol": {
                    "type": "string",
                    "enum": ["tcp", "udp"],
                    "description": "Network protocol",
                },
                "service_type": {
                    "type": "string",
                    "enum": ["rpc", "gossip", "p2p", "metrics", "admin", "sentry", "unknown"],
                    "description": "Type of service running on this host/port",
                },
                "validator_pubkey": {
                    "type": "string",
                    "description": "On-chain pubkey of the associated validator (if known)",
                },
                "hostname": {
                    "type": "string",
                    "description": "Hostname if resolved via DNS",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence score 0.0-1.0 that this is validator infrastructure",
                },
                "discovery_method": {
                    "type": "string",
                    "enum": ["on_chain", "gossip", "dns", "asn_expansion", "ct_log", "osint"],
                    "description": "How this host was discovered",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Your reasoning for why this is validator infrastructure",
                },
            },
            "required": [
                "network",
                "ip_address",
                "service_type",
                "confidence",
                "discovery_method",
                "reasoning",
            ],
        },
    },
}

FLAG_HOST_GONE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "flag_host_gone",
        "description": (
            "Mark a previously active host as no longer active (is_active=false). "
            "Use when a host that was previously in our inventory is no longer reachable "
            "or no longer appears in on-chain data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "host_id": {
                    "type": "string",
                    "description": "UUID of the host record to flag",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason why this host is being marked as gone",
                },
            },
            "required": ["host_id", "reason"],
        },
    },
}

SEARCH_PAST_HYPOTHESES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_past_hypotheses",
        "description": (
            "Search past discovery hypotheses by semantic similarity. "
            "Useful to avoid repeating failed approaches and to find similar successful ones. "
            "Returns past hypotheses ordered by embedding similarity."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language description of the hypothesis to search for",
                },
                "min_success_rate": {
                    "type": "number",
                    "description": "Minimum fraction of validated=true results (0.0-1.0, null for all)",
                },
            },
            "required": ["query"],
        },
    },
}

GET_DISCOVERY_DIFF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_discovery_diff",
        "description": (
            "Get a diff of discovered infrastructure changes since a given timestamp. "
            "Returns new_hosts, gone_hosts, changed_hosts, new_validators, gone_validators."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "network": {
                    "type": "string",
                    "description": "Network name (e.g. 'sui', 'solana')",
                },
                "since": {
                    "type": "string",
                    "description": "ISO 8601 datetime to diff from (e.g. '2026-02-17T00:00:00Z')",
                },
            },
            "required": ["network", "since"],
        },
    },
}

# ============================================================
# OSINT tools
# ============================================================

GITHUB_CODE_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "github_code_search",
        "description": (
            "Search GitHub code for validator configuration files, IP addresses, or operator patterns. "
            "Returns up to 5 results with file paths and matched content. "
            "Requires GITHUB_TOKEN to be configured."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "GitHub code search query (e.g. 'sui validator config 1.2.3.4')",
                },
                "language": {
                    "type": "string",
                    "description": "Filter by programming language (e.g. 'yaml', 'toml', 'json')",
                },
            },
            "required": ["query"],
        },
    },
}

WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for information about validator operators and infrastructure. "
            "Use for finding forum posts, blog posts, or documentation referencing specific operators. "
            "Returns title, URL, and snippet for each result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Web search query",
                },
            },
            "required": ["query"],
        },
    },
}

# ============================================================
# Universal tool schemas list (chain-agnostic tools)
# ============================================================

BULK_REPORT_DISCOVERED_HOSTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bulk_report_discovered_hosts",
        "description": (
            "Report multiple discovered hosts in a single call. "
            "Use this immediately after getting on-chain validator data to import all addresses at once — "
            "do NOT call report_discovered_host one at a time for on-chain data. "
            "Returns {total, new, updated, errors}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "network": {
                    "type": "string",
                    "description": "Network name (e.g. 'sui', 'solana')",
                },
                "hosts": {
                    "type": "array",
                    "description": "List of hosts to import",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ip_address": {
                                "type": "string",
                                "description": "IPv4 or IPv6 address",
                            },
                            "port": {
                                "type": "integer",
                                "description": "Port number (optional)",
                            },
                            "service_type": {
                                "type": "string",
                                "description": "Service type: rpc, p2p, consensus, metrics, admin",
                            },
                            "confidence": {
                                "type": "number",
                                "description": "Confidence score 0.0-1.0",
                            },
                            "discovery_method": {
                                "type": "string",
                                "description": "How it was found: on_chain, dns, ct_log, asn, osint",
                            },
                            "validator_pubkey": {
                                "type": "string",
                                "description": "Validator public key this host belongs to (optional)",
                            },
                            "hostname": {
                                "type": "string",
                                "description": "Hostname if known (optional)",
                            },
                            "reasoning": {
                                "type": "string",
                                "description": "Why this host was reported",
                            },
                        },
                        "required": ["ip_address", "service_type", "confidence", "discovery_method"],
                    },
                },
            },
            "required": ["network", "hosts"],
        },
    },
}

UNIVERSAL_TOOL_SCHEMAS = [
    DNS_LOOKUP_SCHEMA,
    REVERSE_DNS_SCHEMA,
    ASN_LOOKUP_SCHEMA,
    CERT_TRANSPARENCY_SEARCH_SCHEMA,
    WHOIS_LOOKUP_SCHEMA,
    SUBNET_PROBE_SCHEMA,
    GET_KNOWN_HOSTS_SCHEMA,
    GET_KNOWN_VALIDATORS_SCHEMA,
    BULK_REPORT_DISCOVERED_HOSTS_SCHEMA,
    REPORT_DISCOVERED_HOST_SCHEMA,
    FLAG_HOST_GONE_SCHEMA,
    SEARCH_PAST_HYPOTHESES_SCHEMA,
    GET_DISCOVERY_DIFF_SCHEMA,
    GITHUB_CODE_SEARCH_SCHEMA,
    WEB_SEARCH_SCHEMA,
]
