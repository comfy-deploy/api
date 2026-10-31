"""SSRF guard for outbound requests to user-controlled URLs.

Shadow mode is the default: unless SSRF_GUARD_ENFORCE is truthy, `guard()` only
logs a "would block" WARNING and the caller proceeds exactly as before. With
SSRF_GUARD_ENFORCE=true, `guard()` returns the reason and the caller refuses.

Known limitation (DNS rebinding): we resolve and check the host here, then the HTTP
client resolves it again when it connects. An attacker-controlled DNS server with a
short TTL can answer "public" to us and "private" to the client. Closing that race
needs the checked IP pinned into the connection (custom aiohttp resolver / httpx
transport); deliberately not done here.
"""

import asyncio
import ipaddress
import logging
import os
import socket
from typing import Optional
from urllib.parse import urljoin, urlsplit

import aiohttp

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 5
DNS_TIMEOUT = 5  # seconds; a hung resolver must not stall the request in shadow mode

_BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal"}
_BLOCKED_SUFFIXES = (".localhost", ".internal")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class SSRFBlockedError(Exception):
    pass


def enforcing() -> bool:
    return os.getenv("SSRF_GUARD_ENFORCE", "false").strip().lower() in ("1", "true", "yes", "on")


def _ip_reason(ip) -> Optional[str]:
    candidates = [ip]
    if ip.version == 6:
        # IPv4 embedded in IPv6 (::ffff:a.b.c.d, 6to4, NAT64) is checked as IPv4 too.
        candidates += [v4 for v4 in (ip.ipv4_mapped, ip.sixtofour) if v4]
        if ip in _NAT64:
            candidates.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    for a in candidates:
        # is_global already excludes private, loopback, link-local (incl. 169.254.169.254),
        # CGNAT 100.64/10, ULA fc00::/7, fe80::/10; the rest are belt and braces
        # (multicast is "global" per ipaddress, so it must be checked explicitly).
        if (
            not a.is_global
            or a.is_private
            or a.is_loopback
            or a.is_link_local
            or a.is_multicast
            or a.is_reserved
            or a.is_unspecified
        ):
            return f"non-public address {a}"
    return None


async def check_url(url: str) -> Optional[str]:
    """Return None if `url` is safe to fetch, else a reason string (for logs, not clients)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname  # strips userinfo and [], lowercases
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except (ValueError, TypeError, AttributeError):
        return "invalid url"
    if parts.scheme not in ("http", "https"):
        return f"scheme {parts.scheme!r} not allowed"
    if not host:
        return "missing host"
    host = host.rstrip(".")
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        return f"blocked hostname {host}"
    # Resolve with the system resolver (also normalises tricks like 2130706433, 0x7f.1)
    # and require EVERY address to be public.
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
            DNS_TIMEOUT,
        )
    except (OSError, UnicodeError, asyncio.TimeoutError) as e:
        return f"dns resolution failed ({type(e).__name__})"
    if not infos:
        return "dns returned no addresses"
    for *_, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return f"unparseable address {sockaddr[0]}"
        if reason := _ip_reason(ip):
            return reason
    return None


def _host(url) -> str:
    try:
        return urlsplit(url).hostname or "?"
    except Exception:
        return "?"


async def guard(url: str, sink: str) -> Optional[str]:
    """Check `url` and log. Returns a reason only when the caller must refuse (enforce mode).

    Logs the host only, never the path, query string or credentials.
    """
    try:
        reason = await check_url(url)
    except Exception as e:  # the guard itself must never break a request in shadow mode
        reason = f"guard error ({type(e).__name__})"
    if reason is None:
        return None
    enforce = enforcing()
    logger.warning(
        "SSRF guard %s %s request: host=%r reason=%s",
        "blocked" if enforce else "would block",
        sink,
        _host(url),
        reason,
    )
    return reason if enforce else None


def redirect_trace_config(sink: str) -> aiohttp.TraceConfig:
    """Enforce mode for aiohttp: check each redirect target before it is followed."""

    async def on_redirect(session, ctx, params):
        ctx.hops = getattr(ctx, "hops", 0) + 1
        if ctx.hops > MAX_REDIRECTS:
            logger.warning("SSRF guard blocked %s request: more than %d redirects", sink, MAX_REDIRECTS)
            raise SSRFBlockedError(f"more than {MAX_REDIRECTS} redirects")
        headers = params.response.headers
        location = headers.get("Location") or headers.get("URI")
        if location and (reason := await guard(urljoin(str(params.url), location), sink)):
            raise SSRFBlockedError(reason)

    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(on_redirect)
    return trace


async def log_redirect_hops(response, sink: str) -> None:
    """Shadow mode for aiohttp: after redirects were followed, log hops enforce mode would refuse."""
    if len(response.history) > MAX_REDIRECTS:
        logger.warning("SSRF guard would block %s request: %d redirects", sink, len(response.history))
    for hop in [h.url for h in response.history[1:]] + [response.url]:
        await guard(str(hop), sink)
