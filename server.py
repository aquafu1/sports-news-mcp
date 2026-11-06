"""Sports News MCP server.

Pulls headlines from the RSS/Atom feeds listed in feeds.json and exposes
them to Claude (or any MCP client) as tools. Runs over streamable HTTP at
/mcp so it can be added as a custom connector.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from calendar import timegm
from pathlib import Path

import feedparser
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

FEEDS_FILE = Path(os.environ.get("FEEDS_FILE", Path(__file__).with_name("feeds.json")))
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "60"))
USER_AGENT = "sports-news-mcp/1.0"

INJURY_PATTERN = re.compile(
    r"\b(injur\w*|ruled out|out for|questionable|doubtful|day-to-day|"
    r"inactive|scratch\w*|sidelined|hamstring|ankle|knee|concussion|"
    r"(injured|IL|IR) list|placed on|activated|return(s|ing)? (to|from)|"
    r"game-time decision|load management|lineup|will not play|won't play|"
    r"miss(es|ing)? (the |\w+ )?(game|week|season|match))\b",
    re.IGNORECASE,
)

mcp = MCPServer(
    name="sports-news",
    instructions=(
        "Live sports headlines from RSS feeds. Use get_latest_news for what's new, "
        "get_injury_news for injury/lineup updates (useful for player props), and "
        "search_news to find a specific player, team or topic."
    ),
)

_cache: dict[str, tuple[float, list[dict]]] = {}


def load_feeds() -> list[dict]:
    data = json.loads(FEEDS_FILE.read_text())
    return [f for f in data.get("feeds", []) if f.get("url")]


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


async def _fetch_feed(client: httpx.AsyncClient, feed: dict) -> list[dict]:
    url = feed["url"]
    cached = _cache.get(url)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:  # one bad feed shouldn't break the rest
        return [{"error": f"{feed.get('name', url)}: {exc}"}]

    items = []
    for e in parsed.entries:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        items.append(
            {
                "title": _clean(e.get("title", "")),
                "summary": _clean(e.get("summary", ""))[:400],
                "link": e.get("link", ""),
                "source": feed.get("name", url),
                "sport": feed.get("sport", "other"),
                "published_ts": timegm(ts) if ts else 0,
                "published": time.strftime("%Y-%m-%d %H:%M UTC", ts) if ts else "unknown",
            }
        )
    _cache[url] = (time.time(), items)
    return items


async def gather_items(sport: str | None = None) -> tuple[list[dict], list[str]]:
    feeds = load_feeds()
    if sport:
        feeds = [f for f in feeds if f.get("sport", "").lower() == sport.lower()]
    async with httpx.AsyncClient(
        timeout=10, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        results = await asyncio.gather(*(_fetch_feed(client, f) for f in feeds))

    items, errors, seen = [], [], set()
    for batch in results:
        for it in batch:
            if "error" in it:
                errors.append(it["error"])
                continue
            key = it["link"] or it["title"]
            if key in seen:
                continue
            seen.add(key)
            items.append(it)
    items.sort(key=lambda x: x["published_ts"], reverse=True)
    return items, errors


def _format(items: list[dict], errors: list[str], limit: int) -> dict:
    out = [{k: v for k, v in it.items() if k != "published_ts"} for it in items[:limit]]
    result: dict = {"count": len(out), "items": out}
    if errors:
        result["feed_errors"] = errors
    return result


@mcp.tool()
async def get_latest_news(sport: str | None = None, limit: int = 15) -> dict:
    """Newest sports headlines, most recent first.

    Args:
        sport: Optional filter: nfl, nba, mlb, nhl, soccer (or any tag in feeds.json).
        limit: Max headlines to return (default 15, max 50).
    """
    items, errors = await gather_items(sport)
    return _format(items, errors, max(1, min(limit, 50)))


@mcp.tool()
async def get_injury_news(
    team_or_player: str | None = None, sport: str | None = None, limit: int = 15
) -> dict:
    """Injury, lineup and availability headlines (ruled out, questionable, IR, returns).

    Args:
        team_or_player: Optional name to narrow results, e.g. "Lakers" or "Mahomes".
        sport: Optional filter: nfl, nba, mlb, nhl, soccer.
        limit: Max headlines to return (default 15, max 50).
    """
    items, errors = await gather_items(sport)
    hits = [i for i in items if INJURY_PATTERN.search(f"{i['title']} {i['summary']}")]
    if team_or_player:
        q = team_or_player.lower()
        hits = [i for i in hits if q in f"{i['title']} {i['summary']}".lower()]
    return _format(hits, errors, max(1, min(limit, 50)))


@mcp.tool()
async def search_news(query: str, sport: str | None = None, limit: int = 15) -> dict:
    """Search current headlines for a player, team or keyword (all words must match).

    Args:
        query: Words to look for, e.g. "Vikings trade" or "Haaland".
        sport: Optional filter: nfl, nba, mlb, nhl, soccer.
        limit: Max headlines to return (default 15, max 50).
    """
    items, errors = await gather_items(sport)
    words = [w.lower() for w in query.split() if w]
    hits = [i for i in items if all(w in f"{i['title']} {i['summary']}".lower() for w in words)]
    return _format(hits, errors, max(1, min(limit, 50)))


@mcp.tool()
async def list_feeds() -> dict:
    """List the news feeds this server pulls from."""
    return {"feeds": [{"name": f.get("name"), "sport": f.get("sport"), "url": f["url"]} for f in load_feeds()]}


@mcp.custom_route("/", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "mcp_endpoint": "/mcp", "feeds": len(load_feeds())})


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        stateless_http=True,
        json_response=True,
        # Hosting platforms put the server behind their own domain, so the
        # localhost-only Host check would reject Claude's requests.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
