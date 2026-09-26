# Clip Extractor

A self-hosted web app that extracts clips from long-form videos, edits them, generates subtitles, and uploads them to YouTube, Instagram, and Facebook — all driven by a CSV/Excel schedule.

## Features

- **Clip Extractor** — trim segments from a source video using a CSV/Excel file with timestamps, titles, and upload schedules
- **Auto Edit** — auto-cut silences, add zoom effects, loudness normalization, and transitions
- **Subtitles** — generate timestamped SRT subtitles via OpenAI Whisper (standard or HyperFrames kinetic captions)
- **Upload** — direct API upload to YouTube and Instagram/Facebook, or browser-based upload via automation
- **YouTube Tools** — download any YouTube video/audio, or generate an SRT subtitle file from a YouTube URL
- **Instagram Reel Downloader** — download public Instagram reels

## Prerequisites

Make sure these are installed and available in your `PATH`:

| Tool | Purpose |
|------|---------|
| [Node.js](https://nodejs.org/) ≥ 18 | Runs the server |
| [Python](https://python.org/) ≥ 3.10 | Runs clip processing scripts |
| [FFmpeg](https://ffmpeg.org/) | Video encoding / editing |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | YouTube and Instagram downloads |

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/your-username/clip-extractor.git
cd clip-extractor
```

### 2. Install Node.js dependencies

```bash
npm install
```

### 3. Set up a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> The server automatically uses `.venv/bin/python3` if the virtual environment exists, otherwise it falls back to the system `python3`.

### 4. Configure environment variables (optional)

Copy the example file and fill in your credentials if you want to pre-seed them. You can also enter credentials directly through the web UI.

```bash
cp .env.example .env
# edit .env with your editor
```

### 5. Start the server

```bash
npm start
# or for development with auto-reload:
npm run dev
```

Open **http://localhost:3001** in your browser.

## YouTube API Setup

To upload to YouTube, you need a Google Cloud OAuth 2.0 client:

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials
2. Create an **OAuth 2.0 Client ID** (type: Web application)
3. Add `http://localhost:3001/youtube/callback` as an **Authorized Redirect URI**
4. In the app, click **Connect YouTube Channel**, enter your Client ID and Client Secret, and sign in

Credentials are stored locally in `youtube_credentials.json` and `youtube_oauth_config.json` (both git-ignored).

## Facebook / Instagram API Setup

To upload to Instagram or Facebook Pages, you need a Meta App:

1. Create an app at [developers.facebook.com](https://developers.facebook.com/)
2. Add the **Instagram Graph API** and **Facebook Pages** products
3. Add `http://localhost:3001/facebook/callback` as a valid OAuth redirect URI
4. In the app, click **Connect Instagram / Facebook**, enter your App ID and App Secret, and sign in

Credentials are stored locally in `facebook_credentials.json` and `facebook_oauth_config.json` (both git-ignored).

## CSV / Excel Format

The Clip Extractor tab requires a CSV or Excel file with the following columns:

| Column | Required | Format | Description |
|--------|----------|--------|-------------|
| `timestamp_start` | Yes | `HH:MM:SS` or `MM:SS` | Start of the clip |
| `timestamp_end` | Yes | `HH:MM:SS` or `MM:SS` | End of the clip |
| `title` | Yes | text | Video title |
| `description` | Yes | text | Video description |
| `upload_date` | Yes | `YYYY-MM-DD` | Scheduled upload date |
| `upload_time` | Yes | `HH:MM` | Upload time (IST) |
| `video_path` | No | file path | Per-row source video (enables "Use video path from CSV") |
| `youtube_channel` | No | channel ID | Per-row platform (enables "Use platforms from CSV") |
| `instagram_page` | No | account ID | Per-row platform |
| `facebook_page` | No | page ID | Per-row platform |
| `watermark_text` | No | text | Per-row watermark (enables "Use watermark text from CSV") |

## Browser-Based Upload (Optional)

The **Browser Posts** section lets you upload via browser automation (useful when API quota is exhausted). This requires additional setup specific to your automation scripts — edit the `clipout_shorts.py` file to configure which browser profiles correspond to `beahead_yt`, `bewise_yt`, and `wise_reel` account identifiers shown in the UI.

## Directory Structure

```
clip-extractor/
├── server.js              # Express server + all API routes
├── public/
│   └── index.html         # Single-page web UI
├── clipout_shorts.py      # Main clip processing script
├── automation.py          # Upload automation helpers
├── srtgen.py              # Whisper-based SRT generator
├── database.py            # Optional PostgreSQL deduplication tracking
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
├── pictures/              # Background images/videos for Style 2 (git-ignored)
├── uploads/               # Temporary upload storage (git-ignored)
└── output/                # Processed clip output (git-ignored)
```

## Security Notes

- All credential files (`*_credentials.json`, `*_oauth_config.json`) are git-ignored — they are never committed
- The `uploads/` and `output/` directories are git-ignored
- The server binds to `0.0.0.0:3001` — if exposing beyond localhost, add authentication or restrict with a firewall
- OAuth tokens are stored in plain JSON locally; do not run this on a shared machine without additional access controls

## License

MIT — see [LICENSE](LICENSE)
