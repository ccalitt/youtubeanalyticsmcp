"""Local MCP server: your channel's YouTube Analytics + public YouTube data (read-only).

Run auth.py once first. Dates are YYYY-MM-DD; default range is the last 28 days.
"""
import json
import re
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from mcp.server.mcpserver import MCPServer

TOKEN = Path.home() / ".config" / "youtube-analytics-mcp" / "token.json"
SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]
CORE_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
    "subscribersGained,subscribersLost,likes,comments,shares"
)

mcp = MCPServer("youtube")


def _creds():
    if not TOKEN.exists():
        raise RuntimeError("Not signed in. Run: .venv/bin/python auth.py")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json())
    return creds


def _analytics():
    return build("youtubeAnalytics", "v2", credentials=_creds(), cache_discovery=False)


def _data():
    return build("youtube", "v3", credentials=_creds(), cache_discovery=False)


def _range(start_date, end_date):
    end = end_date or (date.today() - timedelta(days=1)).isoformat()
    start = start_date or (date.fromisoformat(end) - timedelta(days=27)).isoformat()
    return start, end


def _query(start_date, end_date, metrics, **kw):
    start, end = _range(start_date, end_date)
    resp = _analytics().reports().query(
        ids="channel==MINE", startDate=start, endDate=end, metrics=metrics, **kw
    ).execute()
    cols = [h["name"] for h in resp.get("columnHeaders", [])]
    rows = [dict(zip(cols, r)) for r in resp.get("rows", [])]
    return {"startDate": start, "endDate": end, "rows": rows}


def _titles(ids):
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 50):
        resp = _data().videos().list(part="snippet", id=",".join(ids[i:i + 50])).execute()
        out.update({v["id"]: v["snippet"]["title"] for v in resp.get("items", [])})
    return out


@mcp.tool()
def my_channel() -> dict:
    """Your channel's name, ID, subscriber/view/video totals."""
    resp = _data().channels().list(part="snippet,statistics", mine=True).execute()
    ch = resp["items"][0]
    return {"id": ch["id"], "title": ch["snippet"]["title"], **ch["statistics"]}


@mcp.tool()
def channel_summary(start_date: str = "", end_date: str = "") -> dict:
    """Totals for the period: views, watch time, avg view duration/%, subs gained/lost, likes, comments, shares."""
    return _query(start_date, end_date, CORE_METRICS)


@mcp.tool()
def daily_trend(start_date: str = "", end_date: str = "", video_id: str = "") -> dict:
    """Day-by-day core metrics for the channel, or for one video if video_id is given."""
    kw = {"dimensions": "day", "sort": "day"}
    if video_id:
        kw["filters"] = f"video=={video_id}"
    return _query(start_date, end_date, CORE_METRICS, **kw)


@mcp.tool()
def top_videos(start_date: str = "", end_date: str = "", sort_by: str = "views", limit: int = 10) -> dict:
    """Your best videos in the period. sort_by: views, estimatedMinutesWatched, subscribersGained, likes, comments, shares, averageViewPercentage."""
    res = _query(start_date, end_date, CORE_METRICS, dimensions="video",
                 sort=f"-{sort_by}", maxResults=min(limit, 200))
    titles = _titles(r["video"] for r in res["rows"])
    for r in res["rows"]:
        r["title"] = titles.get(r["video"], "")
    return res


@mcp.tool()
def video_stats(video_id: str, start_date: str = "", end_date: str = "") -> dict:
    """Core metrics for one video (default range: last 28 days; pass an early start_date for lifetime)."""
    res = _query(start_date, end_date, CORE_METRICS, filters=f"video=={video_id}")
    res["title"] = _titles([video_id]).get(video_id, "")
    return res


@mcp.tool()
def retention_curve(video_id: str, start_date: str = "2005-02-14", end_date: str = "") -> dict:
    """Audience retention for one video: share of viewers still watching at each point (0.0-1.0 of the video)."""
    return _query(start_date, end_date, "audienceWatchRatio,relativeRetentionPerformance",
                  dimensions="elapsedVideoTimeRatio", filters=f"video=={video_id}")


@mcp.tool()
def traffic_sources(start_date: str = "", end_date: str = "", video_id: str = "") -> dict:
    """Where views come from (search, suggested, browse, Shorts feed, external...). Channel-wide or per video."""
    kw = {"dimensions": "insightTrafficSourceType", "sort": "-views"}
    if video_id:
        kw["filters"] = f"video=={video_id}"
    return _query(start_date, end_date, "views,estimatedMinutesWatched", **kw)


@mcp.tool()
def search_terms(start_date: str = "", end_date: str = "", limit: int = 25) -> dict:
    """Top YouTube search terms that led viewers to your videos."""
    return _query(start_date, end_date, "views,estimatedMinutesWatched",
                  dimensions="insightTrafficSourceDetail", filters="insightTrafficSourceType==YT_SEARCH",
                  sort="-views", maxResults=min(limit, 25))


@mcp.tool()
def audience(start_date: str = "", end_date: str = "") -> dict:
    """Viewer age/gender split, top countries, and device types."""
    return {
        "demographics": _query(start_date, end_date, "viewerPercentage", dimensions="ageGroup,gender"),
        "countries": _query(start_date, end_date, "views,estimatedMinutesWatched",
                            dimensions="country", sort="-views", maxResults=15),
        "devices": _query(start_date, end_date, "views,estimatedMinutesWatched", dimensions="deviceType"),
    }


@mcp.tool()
def shorts_vs_long(start_date: str = "", end_date: str = "") -> dict:
    """Compare Shorts, long-form videos, and live streams."""
    return _query(start_date, end_date, CORE_METRICS, dimensions="creatorContentType")


@mcp.tool()
def custom_report(metrics: str, dimensions: str = "", filters: str = "", sort: str = "",
                  start_date: str = "", end_date: str = "", max_results: int = 0) -> dict:
    """Any YouTube Analytics API v2 query (see developers.google.com/youtube/analytics/metrics)."""
    kw = {k: v for k, v in {"dimensions": dimensions, "filters": filters, "sort": sort}.items() if v}
    if max_results:
        kw["maxResults"] = max_results
    return _query(start_date, end_date, metrics, **kw)


# ---------- Public YouTube data (any channel/video) — YouTube Data API v3 ----------
# Quota: 10,000 units/day free. search costs 100 units; everything else ~1 unit per call.

API_KEY = TOKEN.parent / "api_key.txt"


def _public():
    """Public data uses the API key (reads comments without extra OAuth scopes); falls back to OAuth."""
    if API_KEY.exists():
        return build("youtube", "v3", developerKey=API_KEY.read_text().strip(), cache_discovery=False)
    return _data()


def _video_id(v):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})", v)
    return m.group(1) if m else v.strip()


def _playlist_id(p):
    m = re.search(r"list=([\w-]+)", p)
    return m.group(1) if m else p.strip()


def _channel_id(c):
    c = c.strip()
    m = re.search(r"channel/(UC[\w-]{22})", c)
    if m:
        return m.group(1)
    if re.fullmatch(r"UC[\w-]{22}", c):
        return c
    m = re.search(r"@([\w.-]+)", c)
    handle = m.group(1) if m else c
    items = _public().channels().list(part="id", forHandle=handle).execute().get("items", [])
    if not items:
        raise ValueError(f"Channel not found: {c}")
    return items[0]["id"]


def _videos(ids):
    """Full details for any number of video IDs, 50 per request (1 unit each)."""
    out = []
    ids = [i for i in ids if i]
    for i in range(0, len(ids), 50):
        resp = _public().videos().list(part="snippet,statistics,contentDetails",
                                     id=",".join(ids[i:i + 50])).execute()
        for v in resp.get("items", []):
            s, st = v["snippet"], v.get("statistics", {})
            out.append({
                "id": v["id"], "title": s["title"], "channel": s["channelTitle"],
                "channelId": s["channelId"], "publishedAt": s["publishedAt"],
                "duration": v["contentDetails"]["duration"],
                "views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)),
                "url": f"https://www.youtube.com/watch?v={v['id']}",
            })
    return out


def _playlist_video_ids(playlist_id, max_results):
    ids, token = [], None
    while len(ids) < max_results:
        resp = _public().playlistItems().list(part="contentDetails", playlistId=playlist_id,
                                            maxResults=50, pageToken=token).execute()
        ids += [i["contentDetails"]["videoId"] for i in resp.get("items", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids[:max_results]


@mcp.tool()
def search_videos(query: str, max_results: int = 10, order: str = "relevance",
                  published_after: str = "", duration: str = "any", region: str = "") -> list:
    """Search YouTube (costs 100 quota units). order: relevance, date, viewCount, rating.
    published_after: YYYY-MM-DD. duration: any, short (<4 min), medium (4-20), long (>20). region: 2-letter code, e.g. IN."""
    kw = {"q": query, "part": "id", "type": "video", "maxResults": min(max_results, 50),
          "order": order, "videoDuration": duration}
    if published_after:
        kw["publishedAfter"] = f"{published_after}T00:00:00Z"
    if region:
        kw["regionCode"] = region
    resp = _public().search().list(**kw).execute()
    return _videos([i["id"]["videoId"] for i in resp.get("items", [])])


@mcp.tool()
def video_details(videos: str) -> list:
    """Details + stats for one or more videos (comma-separated IDs or URLs)."""
    return _videos([_video_id(v) for v in videos.split(",")])


@mcp.tool()
def channel_info(channel: str) -> dict:
    """Any channel's name, description, subscribers, views, video count. Accepts @handle, channel ID, or URL."""
    ch = _public().channels().list(part="snippet,statistics", id=_channel_id(channel)).execute()["items"][0]
    s, st = ch["snippet"], ch["statistics"]
    return {"id": ch["id"], "title": s["title"], "handle": s.get("customUrl"), "country": s.get("country"),
            "createdAt": s["publishedAt"], "description": s["description"],
            "subscribers": st.get("subscriberCount"), "views": st.get("viewCount"), "videos": st.get("videoCount")}


@mcp.tool()
def channel_videos(channel: str, max_results: int = 30) -> list:
    """A channel's latest uploads (Shorts, videos and streams), newest first, with stats."""
    uploads = "UU" + _channel_id(channel)[2:]
    return _videos(_playlist_video_ids(uploads, max_results))


@mcp.tool()
def channel_playlists(channel: str, max_results: int = 50) -> list:
    """A channel's public playlists."""
    resp = _public().playlists().list(part="snippet,contentDetails", channelId=_channel_id(channel),
                                    maxResults=min(max_results, 50)).execute()
    return [{"id": p["id"], "title": p["snippet"]["title"], "videos": p["contentDetails"]["itemCount"],
             "url": f"https://www.youtube.com/playlist?list={p['id']}"} for p in resp.get("items", [])]


@mcp.tool()
def playlist_videos(playlist: str, max_results: int = 200) -> list:
    """Every video in a playlist (ID or URL), in order, with stats."""
    return _videos(_playlist_video_ids(_playlist_id(playlist), max_results))


@mcp.tool()
def video_comments(video: str, max_results: int = 100, order: str = "relevance") -> list:
    """Top-level comments on a video with like counts and up to 5 replies each. order: relevance or time."""
    out, token = [], None
    while len(out) < max_results:
        resp = _public().commentThreads().list(part="snippet,replies", videoId=_video_id(video),
                                             maxResults=100, order=order, textFormat="plainText",
                                             pageToken=token).execute()
        for t in resp.get("items", []):
            c = t["snippet"]["topLevelComment"]["snippet"]
            out.append({"author": c["authorDisplayName"], "text": c["textDisplay"], "likes": c["likeCount"],
                        "publishedAt": c["publishedAt"], "replyCount": t["snippet"]["totalReplyCount"],
                        "replies": [r["snippet"]["textDisplay"] for r in t.get("replies", {}).get("comments", [])]})
        token = resp.get("nextPageToken")
        if not token:
            break
    return out[:max_results]


@mcp.tool()
def trending(region: str = "US", category_id: str = "", max_results: int = 25) -> list:
    """Most popular videos in a country (region: 2-letter code, e.g. IN, US). category_id 20 = Gaming."""
    kw = {"part": "id", "chart": "mostPopular", "regionCode": region, "maxResults": min(max_results, 50)}
    if category_id:
        kw["videoCategoryId"] = category_id
    resp = _public().videos().list(**kw).execute()
    return _videos([v["id"] for v in resp.get("items", [])])


@mcp.tool()
def video_transcript(video: str, languages: str = "en") -> dict:
    """Transcript/captions for any video with captions (no quota). languages: preference order, e.g. "en,hi"."""
    from youtube_transcript_api import YouTubeTranscriptApi
    t = YouTubeTranscriptApi().fetch(_video_id(video), languages=[l.strip() for l in languages.split(",")])
    return {"language": t.language_code, "auto_generated": t.is_generated,
            "text": " ".join(s.text for s in t.snippets),
            "segments": [{"start": round(s.start, 1), "text": s.text} for s in t.snippets]}


@mcp.tool()
def search_suggestions(query: str) -> list:
    """YouTube search autocomplete for a phrase — what people are typing (no quota). Good for title/topic ideas."""
    url = "https://suggestqueries.google.com/complete/search?" + urllib.parse.urlencode(
        {"client": "firefox", "ds": "yt", "q": query})
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8", "replace"))[1]


if __name__ == "__main__":
    mcp.run()
