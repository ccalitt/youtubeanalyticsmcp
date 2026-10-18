# youtubeanalyticsmcp

A local, read-only MCP server that gives Claude your own YouTube channel's analytics
(YouTube Analytics API) plus public YouTube data (Data API v3, transcripts, search suggestions).

## Setup

1. Google Cloud project with **YouTube Analytics API** and **YouTube Data API v3** enabled.
2. OAuth client of type **Desktop app** → save as `client_secret.json` in this folder.
3. Optional: an API key restricted to YouTube Data API v3 in `~/.config/youtube-analytics-mcp/api_key.txt` (used for public data and comments).
4. Install and sign in:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   .venv/bin/python auth.py
   ```
5. Register with Claude Code:
   ```bash
   claude mcp add --scope user youtube-analytics -- "$PWD/.venv/bin/python" "$PWD/server.py"
   ```

Secrets (`client_secret.json`, `token.json`, `api_key.txt`) are git-ignored and never leave your machine.

[Privacy policy](https://ccalitt.github.io/youtubeanalyticsmcp/privacy.html)
