"""
DNS tools: dns_lookup and reverse_dns.

Uses dnspython for all lookups.
Rate limit: 10 queries/second.
"""

import dns.exception
import dns.rdatatype
import dns.resolver
import dns.reversename

from src.tools.base import BaseTool
from src.tools.schemas import DNS_LOOKUP_SCHEMA, REVERSE_DNS_SCHEMA


class DnsLookupTool(BaseTool):
    """Perform forward DNS lookups."""

    rate_limit = 10.0

    @property
    def schema(self) -> dict:
        return DNS_LOOKUP_SCHEMA

    async def execute(self, hostname: str, record_type: str, **kwargs) -> dict:
        await self._rate_limit()
        try:
            resolver = dns.resolver.Resolver()
            resolver.lifetime = 5.0
            answers = resolver.resolve(hostname, record_type)
            records = [
                {"value": str(rdata), "ttl": answers.ttl}
                for rdata in answers
            ]
            return {"hostname": hostname, "record_type": record_type, "records": records}
        except dns.resolver.NXDOMAIN:
            return {"hostname": hostname, "record_type": record_type, "records": [], "error": "NXDOMAIN"}
        except dns.resolver.NoAnswer:
            return {"hostname": hostname, "record_type": record_type, "records": [], "error": "NoAnswer"}
        except dns.exception.Timeout:
            return {"hostname": hostname, "record_type": record_type, "records": [], "note": "DNS timeout — no records"}
        except Exception as e:
            return {"hostname": hostname, "record_type": record_type, "records": [], "error": str(e)}


class ReverseDnsTool(BaseTool):
    """Perform reverse DNS lookups (PTR records)."""

    rate_limit = 10.0

    @property
    def schema(self) -> dict:
        return REVERSE_DNS_SCHEMA

    async def execute(self, ip_address: str, **kwargs) -> dict:
        await self._rate_limit()
        try:
            resolver = dns.resolver.Resolver()
            resolver.lifetime = 5.0
            rev_name = dns.reversename.from_address(ip_address)
            answers = resolver.resolve(rev_name, "PTR")
            hostnames = [str(rdata) for rdata in answers]
            return {"ip_address": ip_address, "hostnames": hostnames}
        except dns.resolver.NXDOMAIN:
            return {"ip_address": ip_address, "hostnames": [], "error": "NXDOMAIN"}
        except dns.resolver.NoAnswer:
            return {"ip_address": ip_address, "hostnames": [], "error": "NoAnswer"}
        except dns.exception.Timeout:
            return {"ip_address": ip_address, "hostnames": [], "note": "DNS timeout — no PTR record"}
        except Exception as e:
            return {"ip_address": ip_address, "hostnames": [], "error": str(e)}
