"""One-time sign-in: run this yourself, approve access in the browser, done.

Usage:  .venv/bin/python auth.py                     # main channel
        .venv/bin/python auth.py --profile freedom   # another channel (pick it on Google's consent screen)
Needs:  client_secret.json (Google Cloud OAuth "Desktop app" client) in this folder.
Writes: ~/.config/youtube-analytics-mcp/token[-<profile>].json (readable only by you).
"""
import argparse
import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

HERE = Path(__file__).parent
CLIENT_SECRET = HERE / "client_secret.json"
TOKEN_DIR = Path.home() / ".config" / "youtube-analytics-mcp"
SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="", help="name for an extra channel, e.g. freedom")
    profile = ap.parse_args().profile.strip()
    TOKEN = TOKEN_DIR / (f"token-{profile}.json" if profile else "token.json")
    if not CLIENT_SECRET.exists():
        raise SystemExit(f"Missing {CLIENT_SECRET}. Download it from Google Cloud > Credentials.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json())
    os.chmod(TOKEN, 0o600)
    from googleapiclient.discovery import build
    ch = build("youtube", "v3", credentials=creds, cache_discovery=False).channels().list(
        part="snippet", mine=True).execute().get("items", [])
    name = ch[0]["snippet"]["title"] if ch else "NO CHANNEL on this account"
    print(f"Signed in as channel: {name}. Token saved to {TOKEN}")


if __name__ == "__main__":
    main()
