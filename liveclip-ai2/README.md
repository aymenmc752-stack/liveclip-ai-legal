# LiveClip AI — real processing MVP

This version extends the original dashboard with a working FastAPI backend. It can accept a YouTube/Twitch URL (via yt-dlp where supported) or a video upload, transcribe speech with faster-whisper when available, rank candidate moments using transcript cues and audio-energy peaks, and render 9:16 MP4 clips with optional burned-in captions using FFmpeg.

**This is a working processing MVP, not a fully autonomous production service yet.** The schedule preferences are stored by the backend, but automatic YouTube/TikTok publishing is not wired in. The publishing status endpoint explicitly reports that. Real publishing requires authorized OAuth tokens, platform API configuration, and TikTok developer approval. Do not treat a saved schedule as proof that posts will go live.

## Requirements

- Python 3.11 recommended
- FFmpeg + ffprobe installed (the Docker image includes them)
- Deno JavaScript runtime (the Docker image installs it)
- Internet connection for downloading URLs and (on first run) the Whisper model
- Several GB of free disk space; CPU transcription can be slow

## Run locally

### Docker (easiest)

```bash
docker compose up --build --force-recreate
```

Open http://localhost:8000. Docker installs FFmpeg and Python requirements. Add this `docker-compose.yml` if not present:

```yaml
services:
  liveclip:
    build: .
    ports: ["8000:8000"]
    volumes:
      - ./data:/app/data
      - ./output:/app/output
    env_file:
      - .env
```

Copy `.env.example` to `.env` first, or remove `env_file` from the compose file if you don't want environment variables yet.

### Without Docker

Install FFmpeg from https://ffmpeg.org/download.html, ensure `ffmpeg` and `ffprobe` work in a terminal, then:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. The first transcription may download a Whisper model and take a while. If model loading fails, audio-peak detection can still provide candidate clips without transcript captions.


### Windows local executable paths

The downloader now runs `yt-dlp` through the same Python interpreter as the app and uses Deno when available. For direct Windows runs, copy `.env.example` to `.env` and optionally set `FFMPEG_PATH`, `FFPROBE_PATH`, and `DENO_PATH` to the executable paths on your PC. If running with Docker, leave these blank: the container installs FFmpeg and Deno internally.

## Use

1. Open the website.
2. Paste a public supported YouTube/Twitch recording URL or upload a local video.
3. Choose 15, 30, 45, or 60 seconds and whether to burn captions.
4. Submit the job and wait. Jobs update in the queue; generated clips appear as links under the job.
5. Files are saved under `output/<job-id>/` and job records in `data/liveclip.db`.

## Important limitations

- Funny-moment detection is heuristic (speech cues + audio loudness), not a guarantee that every clip is funny. Review output before posting.
- Center crop is used; automatic face/speaker tracking is not implemented.
- yt-dlp support varies by URL, platform changes, login requirements, live status, and local laws/terms. Live streams may need to be recorded while live; this MVP is most reliable with completed VODs. Use only content you own or have permission to clip.
- The default FastAPI background task is not a durable job worker. If the server restarts mid-job, that job may stop. Production should use a persistent worker/queue and durable storage.
- This app has no authentication. Do not expose it publicly as-is; anyone who can reach it may submit jobs and consume bandwidth/storage.
- Free web hosting is often ephemeral, sleeps, or has CPU/time/disk limits. An online 24/7 AI clipper is not guaranteed to be free. Docker is a deployable starting point, not a promise of free always-on compute.
- No automatic publisher is active yet. YouTube uploads require YouTube Data API OAuth setup. TikTok direct publishing requires a developer app, authorized scopes and platform approval; unaudited apps may be restricted to private posts.

## API

- `GET /api/health`
- `GET /api/jobs`
- `GET /api/jobs/{id}`
- `POST /api/jobs/url` JSON `{ "url": "...", "duration": 30, "style": "Funny moments", "captions": true }`
- `POST /api/jobs/upload` multipart form: `file`, `duration`, `style`, `captions`
- `GET /api/schedule`, `PUT /api/schedule`
- `GET /api/publishing/status`

## Next production milestones

1. Add authenticated durable job queue (Redis/Celery or equivalent) and persistent media storage.
2. Implement YouTube OAuth uploader and schedule worker.
3. Implement TikTok Content Posting API OAuth flow and upload/publish status handling.
4. Add account ownership, consent, retention limits, retries, rate limits, and monitoring.

## OAuth publishing setup (new in this build)

The Connections panel now has real OAuth entry points and manual post buttons for generated clips. Platform credentials must be created by you; the app cannot connect accounts without developer credentials, and TikTok public posting depends on TikTok approval/audit.

### YouTube
1. Open Google Cloud Console and create/select a project: https://console.cloud.google.com/
2. Enable **YouTube Data API v3** and configure the OAuth consent screen.
3. Create an OAuth client ID of type **Web application**. Add this exact authorized redirect URI: `http://localhost:8000/oauth/youtube/callback`.
4. Download its JSON and place it at `liveclip-ai/client_secret.json`.
5. Copy `.env.example` to `.env` (PowerShell: `Copy-Item .env.example .env`) and set `YOUTUBE_CLIENT_SECRETS_FILE=client_secret.json`.
6. Restart the app and click **Connect YouTube**. Google may show a warning while your OAuth app is in testing; add your Google account as a test user if required.

### TikTok
1. Create an app at https://developers.tiktok.com/ and add the Content Posting API product.
2. Configure the exact redirect URI `http://localhost:8000/oauth/tiktok/callback` and request `user.info.basic`, `video.publish`, and `video.upload` scopes as available for your app.
3. Set `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` in `.env`, restart the app, then click **Connect TikTok**.
4. TikTok may require review/audit and limits unaudited apps to private visibility. The app queries creator privacy options and requests `PUBLIC_TO_EVERYONE` by default when the account/app offers it; TikTok can restrict unaudited apps to private visibility.

### Posting and limitations
- Once a channel is connected and a clip has completed, use the **Post YouTube** or **Post TikTok** button next to that clip. This is manual posting, not automatic scheduled publishing.
- YouTube uploads are set to private by default for safety. Scheduling/public visibility needs additional user-selected settings and YouTube policy compliance.
- TikTok posting is asynchronous and its API can reject requests if scopes/app approval are missing. Review the result shown in Connections.
- OAuth tokens are stored under `data/`. Do not share this folder or expose this local app to the internet. No account passwords are collected by LiveClip AI.
- This local OAuth flow expects the server to run on the same PC at port 8000. If you use a different port, update both redirect URIs in code and in the platform developer consoles.


## Improvements in this build

- Live download progress is reported from yt-dlp when the source exposes download totals, rather than leaving URL jobs at 5% with no explanation.
- Job cards refresh every 2 seconds and show a progress bar and the current backend message.
- Cancel active/queued jobs and retry failed or cancelled URL jobs from the job list. Retrying uploaded-file jobs requires re-uploading the file.
- Preview generated clips in the browser and download them from the job list.
- Health diagnostics check the configured FFmpeg/FFprobe executable paths and whether yt-dlp can be imported.

### Important limitations

- Downloading and analyzing a long source video can still take a long time; progress is only as precise as the source downloader reports. Transcription and video rendering may also take time on CPU-only computers.
- Schedule preferences are saved, but this build does **not** run a persistent scheduler that automatically publishes clips at those times. Publishing buttons send clips manually. Real automatic publishing requires a reliable background scheduler, valid OAuth/API setup, and platform permission.
- Cancellation is cooperative. A running FFmpeg render may finish its current operation before cancellation is reflected.
- Keep the app bound to localhost unless you add authentication and secure deployment.


## Faster processing and clip count

The clip form lets you request 5, 10, 15, 20, or 30 clips. Actual results can be fewer if the source has too few distinct candidate moments. The faster defaults use up-to-720p source downloads, the `tiny` Whisper model on CPU, and 1080x1920 (9:16) `ultrafast` rendering. This improves speed and disk usage but reduces visual resolution and transcription accuracy versus larger models and 1080p rendering. Set `WHISPER_MODEL=base` or `small` in `.env` for better transcription at the cost of speed.


## Permanent clip deletion
Completed, failed, and cancelled jobs now show **Delete permanently**. Confirming removes the job record and the local generated output folder/upload for that job. Running jobs must be cancelled and stopped first. This does not delete videos already uploaded to YouTube or TikTok.

## YouTube connection and scheduling notes
This build includes the YouTube OAuth connection flow and manual publishing endpoint. To connect, configure a Google OAuth client JSON and set `YOUTUBE_CLIENT_SECRETS_FILE` in `.env`, with the authorized redirect URI `http://localhost:8000/oauth/youtube/callback` for local use. Scheduled preferences in this build are saved settings only; a background scheduler that actually publishes clips at scheduled times is not yet implemented. Do not rely on the saved schedule to publish automatically.


## Automatic clip titles

LiveClip AI now creates a suggested title for each rendered clip from the words spoken in that clip. If `OPENAI_API_KEY` is set in `.env`, it asks the configured OpenAI model (default `gpt-4o-mini`) for a concise, transcript-grounded title. If no key is configured or the API request fails, it falls back to a local transcript-based title and does not require a paid AI service. The suggested title is prefilled when you choose to publish a clip and can be edited before posting. Never commit your `.env` or OAuth JSON credentials to a public repository.


## Automatic scheduled publishing (background worker)

This build has a persistent SQLite scheduled-post queue. From a completed clip, choose **Schedule YouTube** or **Schedule TikTok**, enter a future local date/time, and the background worker checks for due posts every 20 seconds while the server process is alive. Queue state is stored in `data/liveclip.db`; keep `data/`, `output/`, and OAuth token files on persistent storage when hosting. A post that errors is marked failed for review; it is not blindly retried to avoid accidental duplicate uploads.

For a public cloud deployment, set `APP_USERNAME` and `APP_PASSWORD` to strong values and set `PUBLIC_BASE_URL` to the public HTTPS URL. The app refuses to start on a non-local public base URL without these credentials. Update the authorized redirect URIs in Google Cloud and TikTok developer settings to the matching `https://YOUR-DOMAIN/oauth/youtube/callback` and `https://YOUR-DOMAIN/oauth/tiktok/callback`. Reconnect the platforms after deploying so token files are written to the persistent `data/` directory.

**Hosting notes:** use a VM/container that stays running and provides persistent disk. A free web host that sleeps or deletes its filesystem will miss scheduled posts or lose OAuth tokens. Free VM availability, signup, and quotas vary. Run a single app worker/process for this SQLite-backed scheduler; multiple workers could attempt the same due posts concurrently. Keep the app behind HTTPS, protect it with the configured login, and never commit `.env` or OAuth token JSON files. YouTube API quotas and TikTok Content Posting API approval/audit still apply; TikTok public posting may be restricted until the app is audited.


## Time zones, public publishing, and Shorts format update

- The schedule time-zone dropdown uses the browser's built-in IANA time-zone database and detects the computer's configured time zone. No separate time-zone API key is required. Individual scheduled post dates are converted to UTC by the browser before being stored, so daylight-saving changes are handled by the browser's time-zone database.
- Immediate YouTube uploads request `public`; the scheduled worker uploads when the scheduled time is due and requests public visibility.
- TikTok requests `PUBLIC_TO_EVERYONE` when available in the creator's allowed privacy options. TikTok app audit/approval and account options can still limit visibility; the code cannot bypass platform restrictions.
- Generated clips are rendered at 1080x1920, 9:16 vertical. Landscape input is scaled to fill and center-cropped, so edges may be cropped; face/speaker tracking is not included.
- The weekly schedule settings panel stores preferences only; scheduling a particular clip with its Schedule button creates the actual persistent queue entry.


### Vertical video and watermark

All generated clips render as 1080×1920 (9:16) vertical video. The clipping form includes an enabled-by-default “TizShort” watermark option; uncheck it to omit the text watermark. The watermark is burned into the bottom-right corner during FFmpeg rendering.


### Shorts output resolution
Every generated clip is rendered in exact 9:16 dimensions. Choose HD (720×1280), Full HD (1080×1920, recommended), QHD (1440×2560), or 4K UHD (2160×3840) in the Create Clips form. Higher resolutions require more CPU time, storage, and upload bandwidth; upscaling cannot restore detail absent from the original source. Framing uses a centered crop, not automatic speaker tracking.


## Streamer Watchlist (authorized sources only)

The Streamer Watchlist page can store YouTube channel and Twitch channel URLs, pause/resume sources, and poll available replay/VOD listings about every five minutes while the local app is running. It deduplicates discovered video IDs, generates five 30-second highlight candidates in the selected HD (720×1280) or Full HD (1080×1920) 9:16 output, then schedules the highest-scoring generated clip for the next available daily posting time. The selected destination platform must already be connected and have the required API access; failed publishing attempts are shown in the publishing queue.

**Important limits:** only add content you own or have explicit permission to download, clip, and republish. A channel URL is not permission. This version checks available VOD/replay lists; it does not reliably record a live broadcast as it happens, and a replay may not be available. The clip ranker uses local audio/activity and transcript heuristics, not a guarantee of semantic understanding or virality. Keep the computer and app running for monitoring and scheduled publishing.

### Timezone behavior
The Posting Schedule and Streamer Watchlist detect the computer/browser's IANA timezone automatically (via `Intl.DateTimeFormat().resolvedOptions().timeZone`) and use it by default. You can still select another timezone manually. The first save persists the detected timezone; later saves preserve the selected timezone.
