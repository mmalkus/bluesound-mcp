"""Player name -> host/port registry for the Bluesound MCP server.

Hermes (and you) refer to players by name ("woonkamer", "keuken", ...),
never by IP, so the registry is the one place that mapping lives.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "bluesound-mcp" / "players.json"


@dataclass(frozen=True)
class PlayerAddress:
    host: str
    port: int = 11000


class PlayerNotFoundError(KeyError):
    """Raised when a tool is called with a player name that isn't configured."""


def _parse_address(value: str) -> PlayerAddress:
    if ":" in value:
        host, port_str = value.rsplit(":", 1)
        return PlayerAddress(host=host, port=int(port_str))
    return PlayerAddress(host=value)


def load_players() -> dict[str, PlayerAddress]:
    """Load the player registry.

    Resolution order:
      1. BLUESOUND_MCP_PLAYERS env var - inline JSON, e.g.
         '{"woonkamer": "192.168.1.50", "keuken": "192.168.1.51:11000"}'
      2. BLUESOUND_MCP_CONFIG env var - path to a JSON config file
      3. ~/.config/bluesound-mcp/players.json
    """
    inline = os.environ.get("BLUESOUND_MCP_PLAYERS")
    if inline:
        raw = json.loads(inline)
        return {name: _parse_address(v) for name, v in raw.items()}

    config_path = Path(os.environ.get("BLUESOUND_MCP_CONFIG", str(DEFAULT_CONFIG_PATH)))
    if not config_path.exists():
        raise FileNotFoundError(
            f"No Bluesound player config found at {config_path}. "
            "Create it (see README.md) or set BLUESOUND_MCP_PLAYERS."
        )
    data = json.loads(config_path.read_text())
    players = data.get("players", {})
    if not players:
        raise ValueError(f"{config_path} has no 'players' entries.")
    return {name: _parse_address(v) for name, v in players.items()}


def resolve(name: str, players: dict[str, PlayerAddress] | None = None) -> PlayerAddress:
    """Look up a configured player by name, raising a helpful error if unknown."""
    players = players if players is not None else load_players()
    try:
        return players[name]
    except KeyError as exc:
        available = ", ".join(sorted(players)) or "(none configured)"
        raise PlayerNotFoundError(
            f"Unknown player '{name}'. Configured players: {available}"
        ) from exc
