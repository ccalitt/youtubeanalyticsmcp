"""One-time sign-in: run this yourself, approve access in the browser, done.

Usage:  .venv/bin/python auth.py
Needs:  client_secret.json (Google Cloud OAuth "Desktop app" client) in this folder.
Writes: ~/.config/youtube-analytics-mcp/token.json (readable only by you).
"""
import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

HERE = Path(__file__).parent
CLIENT_SECRET = HERE / "client_secret.json"
TOKEN_DIR = Path.home() / ".config" / "youtube-analytics-mcp"
TOKEN = TOKEN_DIR / "token.json"
SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def main():
    if not CLIENT_SECRET.exists():
        raise SystemExit(f"Missing {CLIENT_SECRET}. Download it from Google Cloud > Credentials.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json())
    os.chmod(TOKEN, 0o600)
    print(f"Signed in. Token saved to {TOKEN}")


if __name__ == "__main__":
    main()
