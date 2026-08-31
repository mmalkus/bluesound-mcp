# bluesound-mcp

An MCP server for Bluesound/BluOS players — transport control, volume,
multi-room grouping, presets, and search/play from streaming services
(TIDAL, TuneIn, Deezer, ...). Built for wiring into Hermes the same way
you've wired up `ah-mcp`.

Built on:
- [`pyblu`](https://github.com/LouisChrist/pyblu) — the same library Home
  Assistant's own Bluesound integration uses — for status, transport,
  volume, presets, and grouping.
- A small direct client (`bluos_client.py`) for the `/Browse` endpoint,
  which `pyblu` doesn't cover, used for search and playing search results.
  Implemented against the official [BluOS Custom Integration API v1.7](https://bluos.io/wp-content/uploads/2025/06/BluOS-Custom-Integration-API_v1.7.pdf).

## Install

```bash
cd bluesound-mcp
pip install -e .
```

## Configure your players

Players are referred to by name everywhere (never by IP), so Hermes can
say "woonkamer" instead of an address.

### Zero config: auto-discovery

If no `players.json` is set up, tools fall back to scanning the local
network via SSDP/UPnP and confirming/naming each responder through its
BluOS `/SyncStatus` endpoint — so `list_players()` and every other tool
work out of the box, using whatever name you've already given the player
in the BluOS Controller app. Call `discover_players()` any time to run
this scan directly, e.g. to find IPs to pin in `players.json`.

This requires being on the same subnet/broadcast domain as the players —
it won't find anything across routers/VLANs, or from most container
network setups without host networking, and depends on UDP multicast not
being firewalled off. If that's your situation, use `players.json`
instead.

### Pinning players explicitly

Create `~/.config/bluesound-mcp/players.json`:

```json
{
  "players": {
    "woonkamer": "192.168.1.50",
    "keuken": "192.168.1.51",
    "slaapkamer": "192.168.1.52:11000"
  }
}
```

Port defaults to `11000` (standard for all BluOS players) if omitted.

Alternatives, if you'd rather not use a file (e.g. containerized):
- `BLUESOUND_MCP_CONFIG=/path/to/players.json`
- `BLUESOUND_MCP_PLAYERS='{"woonkamer": "192.168.1.50"}'` (inline JSON, takes priority)

You already have these IPs in Home Assistant's Bluesound integration
device list if you need to look them up again.

## Run

Stdio (default — for local subprocess-style MCP clients):

```bash
bluesound-mcp
```

SSE or streamable-http (for a networked setup, matching however you
ended up running `ah-mcp`):

```bash
BLUESOUND_MCP_TRANSPORT=sse BLUESOUND_MCP_HOST=0.0.0.0 BLUESOUND_MCP_PORT=8765 bluesound-mcp
# or
BLUESOUND_MCP_TRANSPORT=streamable-http BLUESOUND_MCP_HOST=0.0.0.0 BLUESOUND_MCP_PORT=8765 bluesound-mcp
```

`BLUESOUND_MCP_HOST` defaults to `127.0.0.1` — set it to `0.0.0.0` if
Hermes reaches this over the network rather than as a local subprocess
(same reasoning as `API_SERVER_HOST=0.0.0.0` for the Hermes API server
itself).

Wire it into Hermes's MCP config the same way as `ah-mcp` — point it at
the command (stdio) or URL (sse/streamable-http) above.

## Tools

| Tool | What it does |
|---|---|
| `list_players()` | List player names + host/port (configured, or auto-discovered if no config) |
| `discover_players(timeout=3.0)` | Scan the network for BluOS players directly, regardless of config |
| `get_status(player)` | Current track, artist, state, volume, etc. |
| `get_group_status(player)` | Grouping info: leader/follower, who's grouped |
| `play` / `pause` / `stop` / `skip` / `back(player)` | Transport control |
| `set_shuffle(player, enabled)` | Toggle shuffle |
| `set_volume(player, level, mute, tell_followers)` | Get/set volume (0-100) |
| `group_players(leader, followers)` | Group players for synced multi-room playback |
| `ungroup_players(leader, followers)` | Remove specific followers from a group |
| `ungroup_player(player)` | Remove a player from its group, leader or follower |
| `list_presets(player)` / `load_preset(player, id)` | BluOS presets |
| `browse(player, key=None)` | Navigate sources (top-level if no key) |
| `search_service(player, service, query)` | Search a service, e.g. `search_service("woonkamer", "TIDAL", "Miles Davis")` |
| `play_item(player, play_url)` | Play a `play_url` from `browse()`/`search_service()` results |

### A typical "play something from TIDAL" flow

1. `search_service("woonkamer", "TIDAL", "Kind of Blue")`
2. Pick a result — an album/track item will have a `play_url`; some
   items (an artist, say) only have a `browse_key` and need another
   `browse()` call to get to something playable.
3. `play_item("woonkamer", <that play_url>)`

TIDAL (or any other service) has to already be set up and signed in on
the player via the BluOS Controller app — this only searches/plays
within what's already configured, it doesn't handle auth.

### Notes on grouping

BluOS uses leader/follower ("primary"/"secondary") terminology. Only the
leader accepts source-selection and most control commands are proxied to
it, so if you group `keuken` under `woonkamer`, you're steering the
group through `woonkamer`. `ungroup_player` looks this up for you so you
can just say "take the keuken out of the group" without knowing which
side it's on.

## Errors

`pyblu` raises `PlayerUnreachableError` (offline/timeout) and
`PlayerUnexpectedResponseError` (unexpected response — likely a `pyblu`
bug) — these surface as tool call failures with their message intact,
which is enough for Hermes to explain to you what went wrong. Unknown
player names raise a `PlayerNotFoundError` listing what *is* configured.
