"""Auto-discovery of BluOS players on the local network.

BluOS players advertise themselves over UPnP/SSDP, but SSDP alone doesn't
tell us a responder is actually a BluOS player, or what name you've given
it in the BluOS Controller app. So discovery is two steps: broadcast an
SSDP M-SEARCH to find candidate hosts, then confirm and name each one by
querying its BluOS /SyncStatus endpoint directly - the same endpoint
pyblu uses for status, and always on port 11000 regardless of whatever
port the SSDP response advertised.
"""
from __future__ import annotations

import asyncio
import socket
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
BLUOS_PORT = 11000

_CACHE_TTL = 60.0

_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: ssdp:all\r\n"
    "\r\n"
).encode()


@dataclass(frozen=True)
class DiscoveredPlayer:
    name: str
    host: str
    port: int = BLUOS_PORT
    model: str | None = None


class _SSDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue[tuple[bytes, tuple[str, int]]]) -> None:
        self._queue = queue

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._queue.put_nowait((data, addr))


def _parse_headers(data: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in data.decode(errors="ignore").split("\r\n")[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().upper()] = value.strip()
    return headers


async def _ssdp_hosts(timeout: float) -> set[str]:
    """Multicast an SSDP M-SEARCH and collect the IPs of hosts that respond."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 0))

    transport, _ = await loop.create_datagram_endpoint(lambda: _SSDPProtocol(queue), sock=sock)
    hosts: set[str] = set()
    try:
        transport.sendto(_MSEARCH, (SSDP_ADDR, SSDP_PORT))
        end = loop.time() + timeout
        while True:
            remaining = end - loop.time()
            if remaining <= 0:
                break
            try:
                data, (ip, _port) = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            headers = _parse_headers(data)
            location = headers.get("LOCATION")
            host = (urlparse(location).hostname if location else None) or ip
            hosts.add(host)
    finally:
        transport.close()
    return hosts


async def _probe_bluos(
    session: aiohttp.ClientSession, host: str, timeout: float
) -> DiscoveredPlayer | None:
    """Check whether `host` is a BluOS player and read its configured name."""
    url = f"http://{host}:{BLUOS_PORT}/SyncStatus"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
        root = ET.fromstring(text)
    except (aiohttp.ClientError, asyncio.TimeoutError, ET.ParseError):
        return None

    if root.tag != "SyncStatus":
        return None

    name = root.get("name") or host
    model = root.get("modelName") or root.get("model")
    return DiscoveredPlayer(name=name, host=host, port=BLUOS_PORT, model=model)


async def discover_players(timeout: float = 3.0, probe_timeout: float = 2.0) -> list[DiscoveredPlayer]:
    """Find BluOS players on the local network via SSDP.

    Requires the same subnet/broadcast domain (multicast doesn't cross
    routers or most container network modes without host networking), and
    that UDP multicast isn't firewalled off.
    """
    hosts = await _ssdp_hosts(timeout)
    if not hosts:
        return []

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_probe_bluos(session, host, probe_timeout) for host in hosts))
    return sorted((r for r in results if r is not None), key=lambda p: p.name)


_cache: list[DiscoveredPlayer] | None = None
_cache_time: float = 0.0


async def discover_players_cached(timeout: float = 3.0) -> list[DiscoveredPlayer]:
    """Like discover_players(), but reuses a result from the last _CACHE_TTL
    seconds so that unconfigured setups don't re-scan the network on every
    tool call."""
    global _cache, _cache_time
    now = time.monotonic()
    if _cache is not None and now - _cache_time < _CACHE_TTL:
        return _cache
    _cache = await discover_players(timeout=timeout)
    _cache_time = now
    return _cache
