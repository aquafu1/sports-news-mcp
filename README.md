# Sports News MCP

A small MCP server that pulls sports headlines from RSS feeds and gives Claude 4 tools:

- `get_latest_news(sport, limit)` returns the newest headlines first.
- `get_injury_news(team_or_player, sport, limit)` returns only injury, lineup and availability news.
- `search_news(query, sport, limit)` finds a player, team or keyword.
- `list_feeds()` shows which feeds the server is using.

Feeds are cached for 60 seconds, so Claude never sees news more than about a minute older than the feed itself.

## Add your own feeds

Edit `feeds.json`. Every entry needs a `name`, a `sport` tag and a `url`. The optional `type` field says how to read it:

| type | what it reads | example |
|---|---|---|
| `rss` (default) | RSS or Atom feeds | ESPN, CBS, BBC |
| `json` | JSON news feeds | `https://www.fotmob.com/topnews/feed` |
| `realapp` | a realapp.com sport page (scraped) | `https://www.realapp.com/nba` |

```json
{"name": "Real NBA", "sport": "nba", "type": "realapp", "url": "https://www.realapp.com/nba", "cache_seconds": 180}
```

The Real pages have no feed, so the server scrapes their HTML. If Real changes its layout, that source will show up under `feed_errors` with "layout may have changed" while the other sources keep working. Real's birthday posts ("X Turns 25") are skipped.

## Deploy on Render (free)

1. Make a GitHub repo and upload these files: `server.py`, `feeds.json`, `requirements.txt`, `render.yaml`.
2. Go to render.com and sign in with GitHub. Click **New → Blueprint** and pick your repo. Render reads `render.yaml` and sets everything up.
3. When it finishes, open your URL (for example `https://sports-news-mcp.onrender.com`). You should see `{"status":"ok", ...}`.
4. In Claude, go to **Customize → Connectors → Add → Custom → Web**. Paste your URL **with `/mcp` at the end**, choose **No sign in**, then click Add.

Free Render apps go to sleep after about 15 minutes without traffic, so the first request after a break takes around 30–50 seconds. The $7/month tier stays awake.

## Run locally

```bash
pip install -r requirements.txt
python server.py        # serves http://localhost:8000/mcp
```

## Notes

- Anyone who has the URL can use the server. It only reads public feeds, so the risk is low, but don't post the URL publicly.
- If a feed breaks, the other feeds still return results. The broken one is listed under `feed_errors`.
