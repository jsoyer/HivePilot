"""HP-58: allowlisted HTTPS fetch that refuses private and metadata hops.

Loopback HTTP stays allowed (same hosts as MCP probe). Remote fetch is
HTTPS only. Redirects are refused so a public name cannot bounce onto
link-local. Callers never pass this a user-controlled scheme/host without
going through ``assert_url_allowed``.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
_MAX_BYTES = 1_000_000
_TIMEOUT = 8.0

_BLOCKED = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "2001:db8::/32",
    )
)


class SsrfError(ValueError):
    """The URL is not safe to fetch from this process."""


class _RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        raise SsrfError("redirects are not followed")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return True
    return any(ip in net for net in _BLOCKED)


def _is_loopback_host(host: str) -> bool:
    return host.lower() in _LOOPBACK_HOSTS


def assert_url_allowed(url: str) -> None:
    parsed = urlparse((url or "").strip())
    if parsed.username or parsed.password:
        raise SsrfError("userinfo is not allowed in the URL")
    host = (parsed.hostname or "").strip()
    if not host:
        raise SsrfError("URL must include a host")
    if _is_loopback_host(host):
        if parsed.scheme not in {"http", "https"}:
            raise SsrfError("loopback URL must be http(s)")
        return
    if parsed.scheme != "https":
        raise SsrfError("remote fetch must use https")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SsrfError(f"could not resolve {host!r}") from exc
    if not infos:
        raise SsrfError(f"could not resolve {host!r}")
    for info in infos:
        sockaddr = info[4]
        ip = ipaddress.ip_address(sockaddr[0])
        if _is_blocked_ip(ip):
            raise SsrfError(f"refusing to fetch private or metadata address {ip}")


def fetch_allowed(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = _TIMEOUT,
    max_bytes: int = _MAX_BYTES,
) -> bytes:
    """GET/POST ``url`` after the SSRF checks. Never follows redirects."""
    assert_url_allowed(url)
    req = Request(url, data=body, method=method.upper(), headers=headers or {})
    opener = build_opener(_RefuseRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
    except SsrfError:
        raise
    except HTTPError as exc:
        raise SsrfError(f"upstream HTTP {exc.code}") from exc
    except URLError as exc:
        raise SsrfError(f"fetch failed: {exc.reason}") from exc
    if len(data) > max_bytes:
        raise SsrfError(f"response exceeds {max_bytes} bytes")
    return data
