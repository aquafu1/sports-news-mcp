"""Sports News MCP server.

Pulls headlines from the sources listed in feeds.json and exposes them to
Claude (or any MCP client) as tools. Runs over streamable HTTP at /mcp so it
can be added as a custom connector.

Source types (the "type" field in feeds.json):
  rss      RSS/Atom feed (default)
  json     JSON news feed, e.g. FotMob's /topnews/feed
  realapp  a realapp.com sport page (e.g. https://www.realapp.com/nba), scraped
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from calendar import timegm
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

FEEDS_FILE = Path(os.environ.get("FEEDS_FILE", Path(__file__).with_name("feeds.json")))
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "60"))
USER_AGENT = "Mozilla/5.0 (compatible; sports-news-mcp/1.1)"

# Matched against the headline. Kept specific on purpose: loose words like
# "return to" or "lineup" pulled in expansion stories and power rankings.
INJURY_PATTERN = re.compile(
    r"\b(injur(y|ies|ed)|ruled out|out for|out indefinitely|questionable|"
    r"doubtful|day-to-day|game-time decision|inactive|scratched|sidelined|"
    r"hamstring|ankle|knee|calf|groin|achilles|acl|mcl|concussion|spine|"
    r"back (spasms|tightness)|illness|sprain(ed)?|strain(ed)?|fracture[ds]?|"
    r"torn|surgery|(injured|IL|IR) list|injured reserve|placed on (the )?(IR|IL)|"
    r"activated|return(s|ing)? from|cleared to (play|return)|load management|"
    r"will not play|won't play|(to|will) miss|miss(es|ing)? (the |\w+ )?"
    r"(game|games|week|weeks|season|match|matches))\b",
    re.IGNORECASE,
)

# The summary only counts for unambiguous phrases; otherwise a story that
# mentions an injury in passing ("...as league battles injury crisis") slips in.
STRONG_INJURY_PATTERN = re.compile(
    r"\b(ruled out|questionable|doubtful|day-to-day|game-time decision|"
    r"injured reserve|(injured|IL|IR) list|placed on (the )?(IR|IL)|out indefinitely|"
    r"will not play|won't play|(to|will) miss)\b",
    re.IGNORECASE,
)


def is_injury(item: dict) -> bool:
    return bool(INJURY_PATTERN.search(item["title"]) or STRONG_INJURY_PATTERN.search(item["summary"]))


# Some feeds mix in other sports (CBS's NBA feed carried a college football
# story). If the link's path names a sport, trust that over the feed's tag.
URL_SPORT_SEGMENTS = {
    "nfl": "nfl", "nba": "nba", "mlb": "mlb", "nhl": "nhl", "wnba": "wnba",
    "soccer": "soccer", "football": "soccer",
    "college-football": "ncaaf", "college-basketball": "ncaab",
}

# Real's pages are full of "X Turns 25" birthday posts. Not news.
REALAPP_SKIP = re.compile(r"\bTurns \d{1,2}\b", re.IGNORECASE)

mcp = MCPServer(
    name="sports-news",
    instructions=(
        "Live sports headlines from RSS feeds, FotMob and Real (realapp.com). Use get_latest_news for what's new, "
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


# When a story was first seen, by link. Used when a source has no real
# timestamp (Real pages) or a broken one (feeds that stamp every item the same).
_first_seen: dict[str, float] = {}


def _seen(link: str, now: float) -> float:
    if len(_first_seen) > 20000:  # keep memory bounded on a long-running server
        _first_seen.clear()
    return _first_seen.setdefault(link, now)


def _item(feed: dict, title: str, summary: str, link: str, ts: float | None, date_only: bool = False) -> dict:
    if ts:
        fmt = "%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M UTC"
        published = time.strftime(fmt, time.gmtime(ts))
    else:
        published = "unknown"
    return {
        "title": _clean(title),
        "summary": _clean(summary)[:400],
        "link": link or "",
        "source": feed.get("name", feed["url"]),
        "sport": _sport_for(link, feed.get("sport", "other")),
        "published_ts": ts or 0,
        "published": published,
    }


def _sport_for(link: str, default: str) -> str:
    try:
        parts = [p for p in urlparse(link).path.lower().split("/") if p][:2]
    except Exception:
        return default
    for p in parts:
        if p in URL_SPORT_SEGMENTS:
            return URL_SPORT_SEGMENTS[p]
    return default


def _fix_flat_timestamps(items: list[dict], now: float) -> None:
    """Some feeds (ESPN Soccer) stamp every item with the feed's build time.

    If most items share one timestamp, it isn't telling us anything, so fall
    back to first-seen time, keeping the feed's own order (newest first).
    """
    if len(items) < 4:
        return
    counts: dict[float, int] = {}
    for it in items:
        counts[it["published_ts"]] = counts.get(it["published_ts"], 0) + 1
    ts, n = max(counts.items(), key=lambda kv: kv[1])
    if not ts or n < len(items) * 0.6:
        return
    for i, it in enumerate(items):
        if it["published_ts"] != ts:
            continue
        seen = _seen(it["link"] or it["title"], now - i)
        new_ts = min(seen, ts - i)
        it["published_ts"] = new_ts
        it["published"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(new_ts)) + " (approx)"


# ---------- RSS / Atom ----------

def parse_rss(feed: dict, body: bytes, now: float) -> list[dict]:
    parsed = feedparser.parse(body)
    items = []
    for e in parsed.entries:
        st = e.get("published_parsed") or e.get("updated_parsed")
        items.append(_item(feed, e.get("title", ""), e.get("summary", ""), e.get("link", ""),
                           timegm(st) if st else None))
    _fix_flat_timestamps(items, now)
    return items


# ---------- JSON feeds (FotMob) ----------

_TITLE_KEYS = ("title", "headline", "name")
_LINK_KEYS = ("url", "link", "external_url", "shareUrl", "articleUrl", "page")
_SUMMARY_KEYS = ("summary", "content_text", "description", "lead", "excerpt", "teaser", "text")
_DATE_KEYS = ("date_published", "published", "publishedAt", "published_at", "gmtTime",
              "date", "pubDate", "createdAt", "created_at", "updated", "date_modified")


def _first(d: dict, keys: tuple[str, ...]):
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):  # e.g. {"page": {"url": "/news/..."}}
            v = v.get("url") or v.get("href")
        if v not in (None, "", [], {}):
            return v
    return None


def _parse_date(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e12 else float(v)
    s = str(v).strip()
    if s.isdigit():
        return _parse_date(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        return None


def _find_article_lists(node, depth: int = 0) -> list[list[dict]]:
    """Find every list of dicts in the JSON that looks like articles."""
    found = []
    if depth > 6:
        return found
    if isinstance(node, list):
        dicts = [x for x in node if isinstance(x, dict)]
        if dicts and sum(1 for x in dicts if _first(x, _TITLE_KEYS)) >= max(1, len(dicts) // 2):
            found.append(dicts)
        for x in node:
            found += _find_article_lists(x, depth + 1)
    elif isinstance(node, dict):
        for v in node.values():
            found += _find_article_lists(v, depth + 1)
    return found


def parse_json_feed(feed: dict, body: bytes, now: float) -> list[dict]:
    data = json.loads(body)
    lists = _find_article_lists(data)
    if not lists:
        raise ValueError("no articles found in JSON (format may have changed)")
    base = feed.get("link_base") or feed["url"]
    items = []
    for art in max(lists, key=len):
        title = _first(art, _TITLE_KEYS)
        if not isinstance(title, str):
            continue
        link = _first(art, _LINK_KEYS) or ""
        link = urljoin(base, link) if isinstance(link, str) else ""
        summary = _first(art, _SUMMARY_KEYS)
        ts = _parse_date(_first(art, _DATE_KEYS))
        if ts is None and link:
            ts = _seen(link, now)
        items.append(_item(feed, title, summary if isinstance(summary, str) else "", link, ts))
    return items


# ---------- realapp.com sport pages ----------

_REAL_ID_PATH = re.compile(r"^/[A-Za-z0-9_-]{8,16}/?$")
_REAL_DATE = r"(?P<date>(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4})"
_REAL_LEAGUES = (r"NBA|NFL|MLB|NHL|WNBA|Soccer|NCAAF|NCAAM|NCAAW|NCAABB|Golf|Tennis|"
                 r"College Football|College Basketball|Premier League|La Liga|Serie A|"
                 r"Bundesliga|Ligue 1|MLS|Champions League|UCL")
# "Title - NBA, Nov 6, 2026": a dash before the league, or a known league name.
_REAL_TAILS = (
    re.compile(r"\s+[-–—]\s+(?P<league>[^-–—,]{2,30}),\s*" + _REAL_DATE + r"\s*$"),
    re.compile(r"\s+(?P<league>" + _REAL_LEAGUES + r"),\s*" + _REAL_DATE + r"\s*$"),
)
_REAL_TAIL_NODATE = re.compile(r"\s+[-–—]\s+(NBA|NFL|MLB|NHL|WNBA|Soccer|NCAAF|NCAAM|NCAAW|Golf|Tennis)\s*$", re.I)


class _AnchorCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._stack: list[list] = []  # [href, text parts]

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._stack.append([dict(attrs).get("href") or "", []])
        elif self._stack and tag in ("div", "p", "span", "br", "time", "h1", "h2", "h3", "h4", "li"):
            self._stack[-1][1].append(" ")

    def handle_endtag(self, tag):
        if tag == "a" and self._stack:
            href, parts = self._stack.pop()
            text = re.sub(r"\s+", " ", "".join(parts)).strip()
            self.anchors.append((href, text))
            if self._stack:
                self._stack[-1][1].append(" " + text + " ")

    def handle_data(self, data):
        if self._stack:
            self._stack[-1][1].append(data)


def parse_realapp(feed: dict, body: bytes, now: float) -> list[dict]:
    p = _AnchorCollector()
    p.feed(body.decode("utf-8", errors="replace"))
    items, seen = [], set()
    for href, text in p.anchors:
        link = urljoin("https://www.realapp.com/", unescape(href))
        u = urlparse(link)
        if not u.netloc.endswith("realapp.com") or not _REAL_ID_PATH.match(u.path):
            continue
        if not text or len(text) < 8 or link in seen:
            continue
        seen.add(link)
        title, ts = text, None
        m = next((m for rx in _REAL_TAILS if (m := rx.search(text))), None)
        if m:
            title = text[: m.start()].strip(" -–—")
            try:
                day = datetime.strptime(m.group("date").replace(",", ""), "%b %d %Y")
            except ValueError:
                day = None
            if day:
                # Pages only give the day. Use first-seen time if it falls on that
                # day (US Central), otherwise the end of that day.
                start = day.replace(tzinfo=timezone.utc).timestamp() + 6 * 3600
                end = start + 86399
                ts = max(start, min(_seen(link, now), end))
        else:
            title = _REAL_TAIL_NODATE.sub("", text).strip()
            ts = _seen(link, now)
        if not title or REALAPP_SKIP.search(title):
            continue
        items.append(_item(feed, title, "", link, ts))
    if not items:
        raise ValueError("no stories found on page (layout may have changed)")
    return items


PARSERS = {"rss": parse_rss, "json": parse_json_feed, "realapp": parse_realapp}


async def _fetch_feed(client: httpx.AsyncClient, feed: dict) -> list[dict]:
    url = feed["url"]
    cache_for = int(feed.get("cache_seconds", CACHE_SECONDS))
    cached = _cache.get(url)
    if cached and time.time() - cached[0] < cache_for:
        return cached[1]
    parser = PARSERS.get(feed.get("type", "rss"))
    if parser is None:
        return [{"error": f"{feed.get('name', url)}: unknown type {feed.get('type')!r}"}]
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        items = parser(feed, resp.content, time.time())
    except Exception as exc:  # one bad feed shouldn't break the rest
        if cached:  # serve stale results rather than nothing
            return cached[1]
        return [{"error": f"{feed.get('name', url)}: {exc}"}]
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
            if sport and it["sport"].lower() != sport.lower():
                continue
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
    hits = [i for i in items if is_injury(i)]
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
    return {"feeds": [{"name": f.get("name"), "sport": f.get("sport"), "type": f.get("type", "rss"), "url": f["url"]} for f in load_feeds()]}


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
