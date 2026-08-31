"""MCP server for Bluesound/BluOS players.

Wraps pyblu (status, transport, volume, presets, grouping) plus a small
direct-HTTP client for /Browse (search and play from services like TIDAL,
which pyblu doesn't cover). Players are referred to by the names configured
in players.json - see README.md.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp
from pyblu import PairedPlayer, Player

try:  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # mcp >= 2.0 renamed FastMCP -> MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP

from . import bluos_client, config, discovery

logging.basicConfig(level=os.environ.get("BLUESOUND_MCP_LOG_LEVEL", "INFO"))
logger = logging.getLogger("bluesound-mcp")

mcp = FastMCP(
    name="bluesound",
    instructions=(
        "Control Bluesound/BluOS players: transport (play/pause/stop/skip/back), "
        "volume, multi-room grouping, presets, and search/play from streaming "
        "services such as TIDAL. Always call list_players() first if you don't "
        "already know a player's name - tools take player names, not IPs."
    ),
)


async def _address(player: str) -> config.PlayerAddress:
    try:
        players = config.load_players()
    except FileNotFoundError:
        found = await discovery.discover_players_cached()
        players = {p.name: config.PlayerAddress(host=p.host, port=p.port) for p in found}
    return config.resolve(player, players)


def _status_to_dict(status: Any) -> dict[str, Any]:
    return {
        "state": status.state,
        "title": status.name,
        "artist": status.artist,
        "album": status.album,
        "service": status.service,
        "volume": status.volume,
        "mute": status.mute,
        "shuffle": status.shuffle,
        "seconds": status.seconds,
        "total_seconds": status.total_seconds,
        "group_name": status.group_name,
        "group_volume": status.group_volume,
    }


def _sync_status_to_dict(sync: Any) -> dict[str, Any]:
    return {
        "name": sync.name,
        "group": sync.group,
        "volume": sync.volume,
        "is_leader": bool(sync.followers),
        "leader": {"ip": sync.leader.ip, "port": sync.leader.port} if sync.leader else None,
        "followers": [{"ip": f.ip, "port": f.port} for f in (sync.followers or [])],
        "brand": sync.brand,
        "model_name": sync.model_name,
    }


# --- Player registry ---------------------------------------------------

@mcp.tool()
async def list_players() -> list[dict[str, Any]]:
    """List Bluesound players by name, with their host and port. Uses
    players.json if configured; otherwise falls back to a live network scan
    (see discover_players()), so this works with zero setup."""
    try:
        players = config.load_players()
    except FileNotFoundError:
        found = await discovery.discover_players_cached()
        return [
            {"name": p.name, "host": p.host, "port": p.port, "model": p.model}
            for p in found
        ]
    return [
        {"name": name, "host": addr.host, "port": addr.port}
        for name, addr in sorted(players.items())
    ]


@mcp.tool()
async def discover_players(timeout: float = 3.0) -> list[dict[str, Any]]:
    """Scan the local network for BluOS players via SSDP/UPnP, independent of
    players.json. Returns each player's own name (as set in the BluOS
    Controller app), host, port, and model - handy for finding IPs to put in
    players.json, or for checking what's on the network right now. Requires
    being on the same subnet as the players; won't find anything across
    routers/VLANs or most container network setups without host networking."""
    found = await discovery.discover_players(timeout=timeout)
    return [
        {"name": p.name, "host": p.host, "port": p.port, "model": p.model}
        for p in found
    ]


# --- Status --------------------------------------------------------------

@mcp.tool()
async def get_status(player: str) -> dict[str, Any]:
    """Get what a player is currently doing: track, artist, playback state, volume, etc."""
    addr = await _address(player)
    async with Player(addr.host, addr.port) as p:
        status = await p.status()
    return _status_to_dict(status)


@mcp.tool()
async def get_group_status(player: str) -> dict[str, Any]:
    """Get a player's grouping info: whether it's leading or following a group, and who's in it."""
    addr = await _address(player)
    async with Player(addr.host, addr.port) as p:
        sync = await p.sync_status()
    return _sync_status_to_dict(sync)


# --- Transport control -----------------------------------------------

@mcp.tool()
async def play(player: str) -> str:
    """Resume playback on a player. Only works from paused, not from stopped."""
    addr = await _address(player)
    async with Player(addr.host, addr.port) as p:
        return await p.play()


@mcp.tool()
async def pause(player: str) -> str:
    """Pause playback on a player."""
    addr = await _address(player)
    async with Player(addr.host, addr.port) as p:
        return await p.pause()


@mcp.tool()
async def stop(player: str) -> str:
    """Stop playback on a player. Stopped playback can't be resumed with play() - start something new instead."""
    addr = await _address(player)
    async with Player(addr.host, addr.port) as p:
        return await p.stop()


@mcp.tool()
async def skip(player: str) -> None:
    """Skip to the next track in the play queue."""
    addr = await _address(player)
    async with Player(addr.host, addr.port) as p:
        await p.skip()


@mcp.tool()
async def back(player: str) -> None:
    """Go back to the previous track (or restart the current one, if it just started)."""
    addr = await _address(player)
    async with Player(addr.host, addr.port) as p:
        await p.back()


@mcp.tool()
async def set_shuffle(player: str, enabled: bool) -> dict[str, Any]:
    """Turn shuffle on or off for the current play queue."""
    addr = await _address(player)
    async with Player(addr.host, addr.port) as p:
        queue = await p.shuffle(enabled)
    return {"shuffle": queue.shuffle, "length": queue.length}


# --- Volume ------------------------------------------------------------

@mcp.tool()
async def set_volume(
    player: str,
    level: int | None = None,
    mute: bool | None = None,
    tell_followers: bool | None = None,
) -> dict[str, Any]:
    """Get or set a player's volume (0-100). Call with no arguments to just read
    the current volume. Set tell_followers=True to also change grouped players' volume."""
    addr = await _address(player)
    async with Player(addr.host, addr.port) as p:
        vol = await p.volume(level=level, mute=mute, tell_followers=tell_followers)
    return {"volume": vol.volume, "db": vol.db, "mute": vol.mute}


# --- Grouping ------------------------------------------------------------

@mcp.tool()
async def group_players(leader: str, followers: list[str]) -> list[dict[str, Any]]:
    """Group one or more players under a leader, for synchronized multi-room playback.
    leader and followers are player names as returned by list_players()."""
    leader_addr = await _address(leader)
    follower_addrs = [await _address(name) for name in followers]
    paired = [PairedPlayer(ip=addr.host, port=addr.port) for addr in follower_addrs]
    async with Player(leader_addr.host, leader_addr.port) as p:
        result = await p.add_followers(paired)
    return [{"ip": f.ip, "port": f.port} for f in result]


@mcp.tool()
async def ungroup_players(leader: str, followers: list[str]) -> dict[str, Any]:
    """Remove one or more followers from a leader's group. Leader and followers remain reachable individually."""
    leader_addr = await _address(leader)
    follower_addrs = [await _address(name) for name in followers]
    paired = [PairedPlayer(ip=addr.host, port=addr.port) for addr in follower_addrs]
    async with Player(leader_addr.host, leader_addr.port) as p:
        sync = await p.remove_followers(paired)
    return _sync_status_to_dict(sync)


@mcp.tool()
async def ungroup_player(player: str) -> dict[str, Any]:
    """Fully remove a player from whatever group it's in, whether it's the leader
    (this disbands the group) or a follower (this just detaches it)."""
    addr = await _address(player)
    async with Player(addr.host, addr.port) as p:
        sync = await p.sync_status()

    if sync.followers:
        async with Player(addr.host, addr.port) as p:
            result = await p.remove_followers(list(sync.followers))
        return _sync_status_to_dict(result)

    if sync.leader:
        async with Player(sync.leader.ip, sync.leader.port) as leader_p:
            result = await leader_p.remove_follower(addr.host, addr.port)
        return _sync_status_to_dict(result)

    return {"message": f"{player} is not currently in a group."}


# --- Presets -------------------------------------------------------------

@mcp.tool()
async def list_presets(player: str) -> list[dict[str, Any]]:
    """List the saved presets (radio stations, playlists, inputs) available on a player."""
    addr = await _address(player)
    async with Player(addr.host, addr.port) as p:
        presets = await p.presets()
    return [{"id": pr.id, "name": pr.name, "url": pr.url} for pr in presets]


@mcp.tool()
async def load_preset(player: str, preset_id: int) -> None:
    """Start playing a preset by its numeric id (see list_presets())."""
    addr = await _address(player)
    async with Player(addr.host, addr.port) as p:
        await p.load_preset(preset_id)


# --- Browse / search / play from a service --------------------------------

@mcp.tool()
async def browse(player: str, key: str | None = None) -> dict[str, Any]:
    """Browse a player's music sources. With no key, lists top-level sources
    (TIDAL, TuneIn, Library, Playlists, inputs, ...). Pass a browse_key from a
    previous browse() or search_service() result to descend into it."""
    addr = await _address(player)
    async with aiohttp.ClientSession() as session:
        result = await bluos_client.browse(session, addr.host, addr.port, key=key)
    return {
        "service_name": result.service_name,
        "search_key": result.search_key,
        "next_key": result.next_key,
        "items": [item.__dict__ for item in result.items],
    }


@mcp.tool()
async def search_service(player: str, service: str, query: str) -> dict[str, Any]:
    """Search within a streaming service configured on the player, e.g.
    search_service(player, "TIDAL", "Miles Davis"). service is matched
    (case-insensitive, partial) against the top-level source names from browse().
    Results carry a play_url (pass to play_item()) or a browse_key (pass to
    browse(), e.g. to see an album's tracks) depending on the item type."""
    addr = await _address(player)
    async with aiohttp.ClientSession() as session:
        result = await bluos_client.search_service(session, addr.host, addr.port, service, query)
    return {
        "service_name": result.service_name,
        "items": [item.__dict__ for item in result.items],
    }


@mcp.tool()
async def play_item(player: str, play_url: str) -> str:
    """Start playing an item found via browse() or search_service(), using its
    play_url. This clears the current queue and starts playing immediately."""
    addr = await _address(player)
    async with aiohttp.ClientSession() as session:
        return await bluos_client.play_item(session, addr.host, addr.port, play_url)


def main() -> None:
    transport = os.environ.get("BLUESOUND_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    host = os.environ.get("BLUESOUND_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("BLUESOUND_MCP_PORT", "8765"))
    mcp.run(transport=transport, host=host, port=port)


if __name__ == "__main__":
    main()
