"""Canonical network-client identity for abuse controls (Phase 2, finding F6).

`X-Forwarded-For` is caller-controlled unless a trusted proxy in front of us overwrote or
appended to it. Deployment facts: on Render every request reaches the service through Render's
edge proxy, which APPENDS the real client address to X-Forwarded-For (render.yaml therefore sets
TRUSTED_PROXY_HOPS=1). Locally there is no proxy (TRUSTED_PROXY_HOPS=0) and the socket peer is
the client.

Rule: with N trusted hops, the client address is the N-th entry from the RIGHT of
X-Forwarded-For (entries to its left were supplied by untrusted parties and are ignored). With 0
hops the header is ignored entirely. A malformed or missing address maps to a shared "unknown"
bucket, which is deliberately the strictest outcome, never a bypass.
"""

import ipaddress

from starlette.requests import Request

from app.config import settings

UNKNOWN_CLIENT = "unknown"


def _valid_ip(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    hops = settings.trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        entries = [e for e in forwarded.split(",") if e.strip()]
        if len(entries) >= hops:
            ip = _valid_ip(entries[-hops])
            return ip or UNKNOWN_CLIENT
        # Fewer entries than trusted hops: the request did not come through the expected
        # proxies. Fall back to the socket peer rather than trusting anything in the header.
    peer = request.client.host if request.client else None
    return _valid_ip(peer or "") or UNKNOWN_CLIENT
