"""Auto-discovery of BluOS players on the local network.

BluOS players advertise themselves over UPnP/SSDP, but SSDP alone doesn't
tell us a responder is actually a BluOS player, or what name you've given
it in the BluOS Controller app. So discovery has two parts: find candidate
hosts, then confirm and name each one by querying its BluOS /SyncStatus
endpoint directly - the same endpoint pyblu uses for status, and always on
port 11000 regardless of whatever port the SSDP response advertised.

Candidate hosts come from two sources run in parallel: an SSDP M-SEARCH,
and a direct port-11000 sweep of the local /24. SSDP alone isn't reliable
here - some BluOS players (observed: PULSE 2i, a DALI SOUND HUB acting as
a BluOS receiver) never answer any SSDP query on this network, real or
generic, while other non-BluOS devices happily do. The port sweep is the
fallback that actually finds those; SSDP just gets us there faster for
players that do respond.
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
    "ST: upnp:rootdevice\r\n"
    "\r\n"
).encode()

_PORT_SCAN_TIMEOUT = 0.5
_PORT_SCAN_CONCURRENCY = 100


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


def _local_subnet_hosts() -> list[str]:
    """Return every host IP in this machine's local /24, excluding itself.

    Uses a UDP "connect" to a public IP purely to ask the OS which local
    interface/address would be used for outbound traffic - no packet is
    actually sent.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    except OSError:
        return []
    finally:
        s.close()

    prefix = ".".join(local_ip.split(".")[:3])
    return [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != local_ip]


async def _port_open(host: str, port: int, timeout: float) -> str | None:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    writer.close()
    return host


async def _port_scan_hosts(port: int = BLUOS_PORT) -> set[str]:
    """Sweep the local /24 for hosts with `port` open - the fallback for
    BluOS players that don't answer SSDP at all."""
    hosts = _local_subnet_hosts()
    if not hosts:
        return set()
    sem = asyncio.Semaphore(_PORT_SCAN_CONCURRENCY)

    async def bounded(host: str) -> str | None:
        async with sem:
            return await _port_open(host, port, _PORT_SCAN_TIMEOUT)

    results = await asyncio.gather(*(bounded(h) for h in hosts))
    return {h for h in results if h is not None}


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
    """Find BluOS players on the local network via SSDP plus a port-11000 sweep.

    Requires being on the same /24 as the players - multicast doesn't cross
    routers or most container network modes without host networking, and
    neither approach reaches beyond the local subnet.
    """
    ssdp_hosts, scanned_hosts = await asyncio.gather(
        _ssdp_hosts(timeout),
        _port_scan_hosts(),
    )
    hosts = ssdp_hosts | scanned_hosts
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
