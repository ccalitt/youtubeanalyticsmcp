"""Local MCP server: your channel's YouTube Analytics + public YouTube data (read-only).

Run auth.py once first. Dates are YYYY-MM-DD; default range is the last 28 days.
"""
import csv
import io
import json
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import AuthorizedSession, Request
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

HERE = Path(__file__).parent
REAUTH = f"Google sign-in expired or revoked. Fix: cd {HERE} && .venv/bin/python auth.py"
REPORT_CACHE = Path.home() / ".cache" / "youtube-analytics-mcp" / "reports"

mcp = MCPServer("youtube")


def _creds():
    if not TOKEN.exists():
        raise RuntimeError(f"Not signed in. Fix: cd {HERE} && .venv/bin/python auth.py")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise RuntimeError(f"{REAUTH} ({e})") from e
        TOKEN.write_text(creds.to_json())
    return creds


@mcp.tool()
def auth_status() -> dict:
    """Health check: is the Google sign-in working? Call this FIRST in scheduled jobs; if ok is false,
    alert the user with the `fix` command instead of producing a report full of gaps."""
    try:
        ch = _data().channels().list(part="snippet", mine=True).execute()["items"][0]
        return {"ok": True, "channel": ch["snippet"]["title"], "channelId": ch["id"],
                "token_saved": datetime.fromtimestamp(TOKEN.stat().st_mtime).isoformat(timespec="minutes"),
                "api_key": API_KEY.exists()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300],
                "fix": f"cd {HERE} && .venv/bin/python auth.py"}


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


IST = ZoneInfo("Asia/Kolkata")


def _seconds(iso):
    """ISO 8601 duration (PT1H5M36S, P0D, P1DT2H) -> seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", iso or "P0D")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _ist(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(IST).strftime("%a %d %b %H:%M IST") if ts else None


def _videos(ids, client=None):
    """Full details for any number of video IDs, 50 per request (1 unit each).
    kind: live (was/is a stream or premiere), short (<=3 min, approximate), video."""
    out = []
    ids = [i for i in ids if i]
    for i in range(0, len(ids), 50):
        resp = (client or _public()).videos().list(
            part="snippet,statistics,contentDetails,liveStreamingDetails,status",
            id=",".join(ids[i:i + 50])).execute()
        for v in resp.get("items", []):
            s, st = v["snippet"], v.get("statistics", {})
            live = v.get("liveStreamingDetails")
            secs = _seconds(v["contentDetails"]["duration"])
            row = {
                "id": v["id"], "title": s["title"], "channel": s["channelTitle"],
                "channelId": s["channelId"], "publishedAt": s["publishedAt"],
                "publishedIST": _ist(s["publishedAt"]),
                "duration": v["contentDetails"]["duration"], "seconds": secs,
                "kind": "live" if live else ("short" if 0 < secs <= 180 else "video"),
                "privacy": v.get("status", {}).get("privacyStatus"),
                "views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)),
                "url": f"https://www.youtube.com/watch?v={v['id']}",
            }
            if live:
                row["live"] = {"status": s.get("liveBroadcastContent"),  # upcoming / live / none (ended)
                               "scheduledStart": live.get("scheduledStartTime"),
                               "actualStart": live.get("actualStartTime"),
                               "actualEnd": live.get("actualEndTime")}
            out.append(row)
    return out


def _playlist_items(playlist_id, max_results, client=None, after=None):
    """[(videoId, videoPublishedAt)] newest first for uploads playlists. Stops paging once a whole
    page is older than `after` (UTC datetime)."""
    items, token = [], None
    while len(items) < max_results:
        resp = (client or _public()).playlistItems().list(
            part="contentDetails", playlistId=playlist_id, maxResults=50, pageToken=token).execute()
        page = [(i["contentDetails"]["videoId"], i["contentDetails"].get("videoPublishedAt"))
                for i in resp.get("items", [])]
        items += page
        token = resp.get("nextPageToken")
        if not token or (after and page and all(p and _utc(p) < after for _, p in page)):
            break
    return items[:max_results]


def _playlist_video_ids(playlist_id, max_results, client=None):
    return [vid for vid, _ in _playlist_items(playlist_id, max_results, client)]


def _utc(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _window(start_date, end_date, tz):
    """Inclusive local-date window -> (start, end) UTC datetimes."""
    z = ZoneInfo(tz)
    start = datetime.fromisoformat(start_date).replace(tzinfo=z) if start_date else None
    end = (datetime.fromisoformat(end_date) + timedelta(days=1)).replace(tzinfo=z) if end_date else None
    return start, end


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
def channel_videos(channel: str = "", max_results: int = 50, published_after: str = "",
                   published_before: str = "", tz: str = "UTC") -> list:
    """A channel's uploads, newest first, with stats, kind (live/short/video), IST publish time and live
    status (scheduled/actual start/end — an 'upcoming' stream with a past schedule never ran).
    channel: @handle, ID or URL; empty = your own channel (includes private/unlisted).
    published_after/before: inclusive YYYY-MM-DD dates in timezone tz (e.g. UTC, Asia/Kolkata)."""
    own = not channel
    client = _data() if own else _public()
    cid = client.channels().list(part="id", mine=True).execute()["items"][0]["id"] if own else _channel_id(channel)
    start, end = _window(published_after, published_before, tz)
    limit = max_results if not (start or end) else max(max_results, 500)
    items = _playlist_items("UU" + cid[2:], limit, client, after=start)
    ids = [v for v, p in items if (not start or (p and _utc(p) >= start)) and (not end or (p and _utc(p) < end))]
    return _videos(ids[:max_results], client)


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


# ---------- Enhanced own-channel analytics ----------

LIFETIME_START = "2005-02-14"


def _ids(videos):
    return [_video_id(v) for v in (videos.split(",") if isinstance(videos, str) else videos) if v.strip()]


@mcp.tool()
def video_analytics(videos: str, start_date: str = LIFETIME_START, end_date: str = "") -> list:
    """Core analytics for many of YOUR videos in one call (comma-separated IDs/URLs). Default = lifetime.
    Videos with no Analytics data yet (Analytics lags ~2-3 days) come back with zeros."""
    ids = _ids(videos)
    end_date = end_date or date.today().isoformat()
    rows = {}
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        res = _query(start_date, end_date, CORE_METRICS, dimensions="video",
                     filters="video==" + ",".join(chunk), sort="-views", maxResults=200)
        rows.update({r["video"]: r for r in res["rows"]})
    zero = dict.fromkeys(CORE_METRICS.split(","), 0)
    return [{"video": v, **rows.get(v, zero)} for v in ids]


@mcp.tool()
def live_stats(videos: str) -> list:
    """Live-stream stats for your streams: scheduled/actual start and end (IST), stream length,
    peak concurrent viewers."""
    out = []
    for v in _videos(_ids(videos), _data()):
        live = v.get("live") or {}
        row = {"id": v["id"], "title": v["title"], "status": live.get("status"),
               "scheduledStart": _ist(live.get("scheduledStart")), "actualStart": _ist(live.get("actualStart")),
               "actualEnd": _ist(live.get("actualEnd")), "length_min": round(v["seconds"] / 60, 1),
               "views": v["views"]}
        if live.get("actualStart"):
            start = live["actualStart"][:10]
            # averageConcurrentViewers is documented but Google returns internalError for it; peak works.
            try:
                r = _query(start, date.today().isoformat(), "peakConcurrentViewers", filters=f"video=={v['id']}")["rows"]
                row["peakConcurrentViewers"] = r[0]["peakConcurrentViewers"] if r else None
            except Exception:
                row["peakConcurrentViewers"] = None
        out.append(row)
    return out


def _never_ran(v, now=None):
    """An 'upcoming' broadcast whose scheduled time has passed, or that has no schedule and is >1 day old."""
    live = v.get("live") or {}
    if live.get("status") != "upcoming" or live.get("actualStart"):
        return False
    now = now or datetime.now(timezone.utc)
    ref = live.get("scheduledStart") or v["publishedAt"]
    return _utc(ref) < now - (timedelta(0) if live.get("scheduledStart") else timedelta(days=1))


@mcp.tool()
def channel_audit(max_videos: int = 300) -> dict:
    """Housekeeping check on YOUR channel (incl. private/unlisted): duplicate titles, tiny stream stubs,
    scheduled streams that never ran, 0-second videos, public long-form videos missing from every playlist,
    and all playlist titles (check them for typos)."""
    yt = _data()
    cid = yt.channels().list(part="id", mine=True).execute()["items"][0]["id"]
    vids = _videos(_playlist_video_ids("UU" + cid[2:], max_videos, yt), yt)
    now = datetime.now(timezone.utc)

    by_title = {}
    for v in vids:
        by_title.setdefault(re.sub(r"\s+", " ", v["title"].strip().lower()), []).append(v)
    brief = lambda v: {k: v.get(k) for k in ("id", "title", "kind", "seconds", "privacy", "views", "publishedIST")}

    playlists, in_playlist, token = [], set(), None
    while True:
        resp = yt.playlists().list(part="snippet,contentDetails,status", mine=True, maxResults=50,
                                   pageToken=token).execute()
        playlists += resp.get("items", [])
        token = resp.get("nextPageToken")
        if not token:
            break
    for p in playlists:
        in_playlist.update(_playlist_video_ids(p["id"], 1000, yt))

    never_ran = [v for v in vids if _never_ran(v, now)]
    return {
        "videos_scanned": len(vids),
        "by_privacy": {p: sum(v["privacy"] == p for v in vids) for p in {v["privacy"] for v in vids}},
        "duplicate_titles": [[brief(v) for v in g] for g in by_title.values() if len(g) > 1],
        "stream_stubs_under_5min": [brief(v) for v in vids
                                    if v["kind"] == "live" and 0 < v["seconds"] < 300 and v["privacy"] == "public"],
        "scheduled_never_ran": [brief(v) for v in never_ran],
        "zero_duration": [brief(v) for v in vids if v["seconds"] == 0 and v not in never_ran],
        "public_longform_not_in_any_playlist": [brief(v) for v in vids if v["privacy"] == "public"
                                                and v["kind"] != "short" and v["id"] not in in_playlist],
        "playlists": [{"title": p["snippet"]["title"], "videos": p["contentDetails"]["itemCount"],
                       "privacy": p["status"]["privacyStatus"]} for p in playlists],
    }


# ---------- Impressions & CTR (YouTube Reporting API: daily bulk "reach" reports) ----------

def _reporting():
    return build("youtubereporting", "v1", credentials=_creds(), cache_discovery=False)


def _reach_type(rp):
    types = [t["id"] for t in rp.reportTypes().list().execute().get("reportTypes", [])]
    for want in ("channel_reach_basic_a1", "channel_reach_combined_a1"):
        if want in types:
            return want
    reach = [t for t in types if t.startswith("channel_reach")]
    if not reach:
        raise RuntimeError(f"No reach report type available. Types: {types}")
    return reach[0]


@mcp.tool()
def reach_setup() -> dict:
    """One-time: start Google's daily impressions/CTR report for your channel. Needs the
    'YouTube Reporting API' enabled in your Cloud project. First data appears ~48 hours later."""
    rp = _reporting()
    rtype = _reach_type(rp)
    jobs = rp.jobs().list().execute().get("jobs", [])
    job = next((j for j in jobs if j["reportTypeId"] == rtype), None)
    created = False
    if not job:
        job = rp.jobs().create(body={"reportTypeId": rtype, "name": "Local Analytics reach"}).execute()
        created = True
    return {"reportType": rtype, "jobId": job["id"], "created": created,
            "note": "Reports arrive daily, ~2 days behind. Use reach_report to read them."}


def _reach_rows(start_date, end_date):
    rp = _reporting()
    rtype = _reach_type(rp)
    job = next((j for j in rp.jobs().list().execute().get("jobs", []) if j["reportTypeId"] == rtype), None)
    if not job:
        raise RuntimeError("No reach report job yet. Run reach_setup first.")
    reports, token = [], None
    while True:
        resp = rp.jobs().reports().list(jobId=job["id"], pageToken=token,
                                        startTimeAtOrAfter=f"{start_date}T00:00:00Z").execute()
        reports += resp.get("reports", [])
        token = resp.get("nextPageToken")
        if not token:
            break
    end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
    reports = [r for r in reports if _utc(r["startTime"]) < end]
    # Google may re-issue a day's report; keep the newest per day.
    latest = {}
    for r in reports:
        if r["startTime"] not in latest or r["createTime"] > latest[r["startTime"]]["createTime"]:
            latest[r["startTime"]] = r
    REPORT_CACHE.mkdir(parents=True, exist_ok=True)
    session = AuthorizedSession(_creds())
    rows, days = [], []
    for r in sorted(latest.values(), key=lambda r: r["startTime"]):
        f = REPORT_CACHE / f"{rtype}_{r['id']}.csv"
        if not f.exists():
            resp = session.get(r["downloadUrl"], timeout=60)
            resp.raise_for_status()
            f.write_bytes(resp.content)
        days.append(r["startTime"][:10])
        rows += list(csv.DictReader(io.StringIO(f.read_text())))
    return rows, days


@mcp.tool()
def reach_report(start_date: str = "", end_date: str = "", limit: int = 25) -> dict:
    """Impressions and impressions click-through rate (CTR) for your channel and top videos,
    from the daily reach reports (run reach_setup once first). Default: last 7 days of available data."""
    end_date = end_date or (date.today() - timedelta(days=1)).isoformat()
    start_date = start_date or (date.fromisoformat(end_date) - timedelta(days=6)).isoformat()
    rows, days = _reach_rows(start_date, end_date)
    imp_col = next((c for c in (rows[0] if rows else {}) if c.endswith("impressions")), "video_thumbnail_impressions")
    ctr_col = next((c for c in (rows[0] if rows else {}) if c.endswith("impressions_ctr")), "video_thumbnail_impressions_ctr")
    per = {}
    for r in rows:
        imp = float(r.get(imp_col) or 0)
        clicks = imp * float(r.get(ctr_col) or 0)
        p = per.setdefault(r.get("video_id", ""), [0.0, 0.0])
        p[0] += imp
        p[1] += clicks
    total_imp = sum(p[0] for p in per.values())
    total_clicks = sum(p[1] for p in per.values())
    top = sorted(per.items(), key=lambda kv: -kv[1][0])[:limit]
    titles = _titles(v for v, _ in top if v)
    return {
        "days_with_data": days, "requested": [start_date, end_date],
        "impressions": int(total_imp), "ctr_pct": round(100 * total_clicks / total_imp, 2) if total_imp else None,
        "videos": [{"video": v, "title": titles.get(v, ""), "impressions": int(i),
                    "ctr_pct": round(100 * c / i, 2) if i else None} for v, (i, c) in top],
    }


# ---------- One-call weekly report ----------

def _latest_analytics_day():
    """Last day the channel has any Analytics rows (Analytics usually lags 2-3 days)."""
    today = date.today()
    res = _query((today - timedelta(days=10)).isoformat(), today.isoformat(), "views", dimensions="day", sort="day")
    days = [r["day"] for r in res["rows"] if r["views"]]
    return days[-1] if days else None


@mcp.tool()
def weekly_report(start_date: str, end_date: str, tz: str = "UTC") -> dict:
    """Everything a weekly review needs in ONE call, for an inclusive window (YYYY-MM-DD in timezone tz):
    sign-in health, Analytics data freshness, totals vs the previous window, Shorts vs long vs live
    (incl. subscribers by type), uploads in the window with lifetime analytics, live peak viewers,
    traffic sources, search terms, cumulative watch hours, public subscriber count, retention for the
    window's best Short, and impressions/CTR if reach reports exist. Sections that fail are listed in
    `errors` instead of breaking the report."""
    out, errors = {"window": {"start": start_date, "end": end_date, "tz": tz}}, {}

    def section(name, fn):
        try:
            out[name] = fn()
        except Exception as e:
            errors[name] = str(e)[:300]

    out["auth"] = auth_status()
    if not out["auth"]["ok"]:
        out["errors"] = {"auth": out["auth"]["error"]}
        return out

    span = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
    prev_end = (date.fromisoformat(start_date) - timedelta(days=1)).isoformat()
    prev_start = (date.fromisoformat(prev_end) - timedelta(days=span - 1)).isoformat()

    def freshness():
        last = _latest_analytics_day()
        return {"analytics_through": last,
                "window_complete": bool(last and last >= end_date),
                "note": None if last and last >= end_date else
                f"Analytics has data only through {last}; later days are missing or partial. "
                "Public counts below are current."}
    section("freshness", freshness)
    section("totals", lambda: {"this": _query(start_date, end_date, CORE_METRICS)["rows"],
                               "previous": {"window": [prev_start, prev_end],
                                            "rows": _query(prev_start, prev_end, CORE_METRICS)["rows"]}})
    section("by_content_type", lambda: _query(start_date, end_date, CORE_METRICS,
                                              dimensions="creatorContentType")["rows"])

    def uploads():
        vids = channel_videos("", 200, start_date, end_date, tz)
        ghosts = [{"id": v["id"], "title": v["title"]} for v in vids if _never_ran(v)]
        vids = [v for v in vids if not _never_ran(v)]
        stats = {r["video"]: r for r in video_analytics(",".join(v["id"] for v in vids))} if vids else {}
        for v in vids:
            a = stats.get(v["id"], {})
            v["analytics_lifetime"] = {k: a.get(k) for k in ("views", "estimatedMinutesWatched",
                                                             "averageViewDuration", "averageViewPercentage",
                                                             "subscribersGained")}
        counts = {k: sum(v["kind"] == k for v in vids) for k in ("live", "video", "short")}
        return {"counts": counts, "hours_published": round(sum(v["seconds"] for v in vids
                                                                if v["kind"] != "short") / 3600, 1),
                "never_ran_excluded": ghosts, "videos": vids}
    section("uploads", uploads)

    def live():
        ids = [v["id"] for v in out.get("uploads", {}).get("videos", []) if v["kind"] == "live"]
        return live_stats(",".join(ids)) if ids else []
    section("live", live)
    section("traffic_sources", lambda: traffic_sources(start_date, end_date)["rows"])
    section("search_terms", lambda: search_terms(start_date, end_date, 10)["rows"])
    section("cumulative", lambda: {
        "watch_hours": round(_query(LIFETIME_START, end_date, "estimatedMinutesWatched")["rows"][0]
                             ["estimatedMinutesWatched"] / 60, 1),
        "public": my_channel()})

    def best_short():
        shorts = [v for v in out.get("uploads", {}).get("videos", []) if v["kind"] == "short"]
        if not shorts:
            return None
        top = max(shorts, key=lambda v: v["views"])
        curve = retention_curve(top["id"])["rows"]
        step = max(1, len(curve) // 10)
        return {"id": top["id"], "title": top["title"], "views": top["views"],
                "retention": [{"at": r["elapsedVideoTimeRatio"], "watching": round(r["audienceWatchRatio"], 2)}
                              for r in curve[::step]]}
    section("best_short", best_short)
    section("reach", lambda: {k: v for k, v in reach_report(start_date, end_date, 10).items()})

    out["errors"] = errors
    return out


if __name__ == "__main__":
    mcp.run()
