"""Raw BluOS /Browse support.

pyblu (https://github.com/LouisChrist/pyblu) covers status, transport,
volume, presets and grouping, but not the /Browse endpoint used for
navigating and searching streaming services (TIDAL, TuneIn, the local
library, etc). This module implements just that part directly against
the BluOS Custom Integration API v1.7.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

import aiohttp


class BluosBrowseError(RuntimeError):
    """Raised when /Browse returns an <error> response, or a search target can't be found."""


@dataclass
class BrowseItem:
    type: str
    text: str | None
    text2: str | None
    browse_key: str | None
    play_url: str | None
    image: str | None


@dataclass
class BrowseResult:
    service_name: str | None
    search_key: str | None
    next_key: str | None
    items: list[BrowseItem]


async def _get_xml(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    path: str,
    params: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> ET.Element:
    url = f"http://{host}:{port}{path}"
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        resp.raise_for_status()
        text = await resp.text()
    return ET.fromstring(text)


def _collect_items(root: ET.Element) -> list[BrowseItem]:
    items: list[BrowseItem] = []

    def walk(el: ET.Element) -> None:
        for child in el:
            if child.tag == "item":
                items.append(
                    BrowseItem(
                        type=child.get("type", ""),
                        text=child.get("text"),
                        text2=child.get("text2"),
                        browse_key=child.get("browseKey"),
                        play_url=child.get("playURL"),
                        image=child.get("image"),
                    )
                )
            elif child.tag == "category":
                # categories are just a grouping wrapper around more items
                walk(child)

    walk(root)
    return items


async def browse(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    key: str | None = None,
    q: str | None = None,
    timeout: float = 10.0,
) -> BrowseResult:
    """GET /Browse. With no key, returns the top-level source list."""
    params: dict[str, str] = {}
    if key is not None:
        params["key"] = key
    if q is not None:
        params["q"] = q

    root = await _get_xml(session, host, port, "/Browse", params=params, timeout=timeout)
    if root.tag == "error":
        message = root.findtext("message") or "Unknown /Browse error"
        raise BluosBrowseError(message)

    return BrowseResult(
        service_name=root.get("serviceName"),
        search_key=root.get("searchKey"),
        next_key=root.get("nextKey"),
        items=_collect_items(root),
    )


async def find_service_search_key(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    service_name: str,
    timeout: float = 10.0,
) -> tuple[str, str] | None:
    """Find a top-level service by (partial, case-insensitive) name and return
    (its browse_key, its searchKey) - or None if no matching service was found."""
    top = await browse(session, host, port, timeout=timeout)
    match = next(
        (
            item
            for item in top.items
            if item.text and service_name.lower() in item.text.lower() and item.browse_key
        ),
        None,
    )
    if match is None:
        return None

    sub = await browse(session, host, port, key=match.browse_key, timeout=timeout)
    if not sub.search_key:
        return None
    return match.browse_key, sub.search_key


async def search_service(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    service_name: str,
    query: str,
    timeout: float = 10.0,
) -> BrowseResult:
    """Search within a top-level service, e.g. search_service(..., "TIDAL", "Miles Davis")."""
    found = await find_service_search_key(session, host, port, service_name, timeout=timeout)
    if found is None:
        top = await browse(session, host, port, timeout=timeout)
        available = ", ".join(i.text for i in top.items if i.text)
        raise BluosBrowseError(
            f"No source matching '{service_name}' found on this player. Available: {available}"
        )
    _, search_key = found
    return await browse(session, host, port, key=search_key, q=query, timeout=timeout)


async def play_item(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    play_path: str,
    timeout: float = 10.0,
) -> str:
    """Invoke a playURL (or autoplayURL) taken verbatim from a BrowseItem."""
    if play_path.startswith(("http://", "https://")):
        url = play_path
    else:
        if not play_path.startswith("/"):
            play_path = "/" + play_path
        url = f"http://{host}:{port}{play_path}"

    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        resp.raise_for_status()
        return await resp.text()
