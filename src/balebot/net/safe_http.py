"""Outbound HTTP for user-chosen hosts: public https only, DNS pinned, no redirects.

The user (or the AI that read a docs page) decides which host receives an API key, so every
request is checked against SSRF: the host must resolve only to public addresses, and the TCP
connection goes to the address we checked (not a second lookup). The URL keeps the real
hostname, so TLS SNI and certificate verification use the name, not the IP.
"""

import asyncio
import ipaddress
import socket
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

MAX_BODY = 2_000_000

# (hostname, checked ip) for the request running in this task
_pin: ContextVar[tuple[str, str] | None] = ContextVar("balebot_pin", default=None)


class UnsafeURL(Exception):
    """The URL is not an allowed public https endpoint."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes


def _public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped  # ::ffff:127.0.0.1 must not slip through
    return addr.is_global and not addr.is_multicast


async def resolve_public(host: str) -> list[str]:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise UnsafeURL("IP literals are not allowed")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise UnsafeURL("host does not resolve") from None
    ips = sorted({str(i[4][0]) for i in infos})
    if not ips or not all(_public(ip) for ip in ips):
        raise UnsafeURL("host resolves to a non-public address")
    return ips


def check_url(url: str) -> tuple[str, str]:
    """-> (host, path+query). Raises UnsafeURL for anything but plain public https."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise UnsafeURL("only https URLs are allowed")
    if parts.username or parts.password:
        raise UnsafeURL("credentials in URL are not allowed")
    if parts.port not in (None, 443):
        raise UnsafeURL("only port 443 is allowed")
    path = parts.path or "/"
    return parts.hostname, path + (f"?{parts.query}" if parts.query else "")


class _PinnedBackend(httpcore.AnyIOBackend):
    """Connects only to the address that passed the public-IP check for this request."""

    async def connect_tcp(  # type: ignore[no-untyped-def,override]
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        pin = _pin.get()
        if pin is None or pin[0] != host:
            raise httpcore.ConnectError("refusing to connect to an unchecked host")
        return await super().connect_tcp(
            pin[1],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


def _pinned_transport() -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport()
    pool: Any = transport._pool  # noqa: SLF001
    if not hasattr(pool, "_network_backend"):  # fail closed if httpx internals change
        raise RuntimeError("httpx/httpcore layout changed: cannot pin connections")
    pool._network_backend = _PinnedBackend()  # noqa: SLF001
    return transport


class SafeHttp:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # trust_env=False: an environment proxy would bypass the pinning
        self._http = httpx.AsyncClient(
            transport=transport or _pinned_transport(), follow_redirects=False, trust_env=False
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: object = None,
        timeout: float = 20.0,
        max_bytes: int = MAX_BODY,
    ) -> HttpResult:
        host, _ = check_url(url)
        ip = (await resolve_public(host))[0]
        token = _pin.set((host, ip))
        try:
            async with self._http.stream(
                method, url, headers=headers, json=json, timeout=timeout
            ) as resp:
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise UnsafeURL("response too large")
                return HttpResult(resp.status_code, bytes(body))
        except httpx.HTTPError as exc:
            # type only: the message can contain the URL
            raise UnsafeURL(f"request failed: {type(exc).__name__}") from None
        finally:
            _pin.reset(token)

    async def aclose(self) -> None:
        await self._http.aclose()
