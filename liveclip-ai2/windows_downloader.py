"""
Low-CPU Windows downloader helper for LiveClip AI.
Install: py -m pip install -U yt-dlp requests
Configure LIVECLIP_URL and LOCAL_DOWNLOADER_TOKEN as environment variables.
By default, reads fresh cookies from Chrome instead of a stale exported cookie file.
Set YTDLP_COOKIES_FROM_BROWSER=firefox (or another yt-dlp-supported browser) to change browser.
Set YTDLP_COOKIES_FROM_BROWSER=none to disable browser cookies.
Optionally set YTDLP_COOKIES_FILE to use an exported cookies.txt file instead.
This downloads one video at a time at below-normal process priority; clipping stays on Render.
"""
import os
import sys
import time
import tempfile
import subprocess
from pathlib import Path
import requests

BASE_URL = os.getenv("LIVECLIP_URL", "").strip().rstrip("/")
TOKEN = os.getenv("LOCAL_DOWNLOADER_TOKEN", "").strip()
POLL_SECONDS = max(3, int(os.getenv("POLL_SECONDS", "8")))
MAX_RATE = os.getenv("DOWNLOAD_RATE_LIMIT", "5M")
COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "").strip()
COOKIES_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "chrome").strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def download(url: str, folder: Path) -> Path:
    output = folder / "source.%(ext)s"
    command = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", "--no-warnings",
        # Keep useful error messages visible instead of hiding all yt-dlp output.
        "--js-runtimes", "deno",
        "--remote-components", "ejs:github",
        "--format", "bv*+ba/b",
        "--limit-rate", MAX_RATE,
        "--retries", "5", "--fragment-retries", "10",
        "--extractor-retries", "5",
        "--output", str(output),
    ]
    if COOKIES_FILE:
        cookie_path = Path(COOKIES_FILE).expanduser()
        if not cookie_path.is_file():
            raise RuntimeError(f"YTDLP_COOKIES_FILE does not exist: {cookie_path}")
        command.extend(["--cookies", str(cookie_path)])
        print(f"Using exported cookies file: {cookie_path}")
    elif COOKIES_BROWSER and COOKIES_BROWSER.lower() not in {"none", "off", "false"}:
        command.extend(["--cookies-from-browser", COOKIES_BROWSER])
        print(
            f"Using cookies from {COOKIES_BROWSER}. Make sure that browser is "
            "signed in to YouTube; do not share its cookie/session data."
        )
    command.append(url)

    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
    subprocess.run(command, check=True, cwd=str(folder), **kwargs)
    supported = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
    files = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in supported and not p.name.endswith(".part")
    ]
    if not files:
        raise RuntimeError("yt-dlp finished but no video file was created.")
    return max(files, key=lambda p: p.stat().st_size)


def main():
    if not BASE_URL or not TOKEN:
        raise SystemExit("Set LIVECLIP_URL and LOCAL_DOWNLOADER_TOKEN first.")
    session = requests.Session()
    print("LiveClip low-CPU downloader running. One job at a time; Ctrl+C to stop.")
    if not COOKIES_FILE and COOKIES_BROWSER and COOKIES_BROWSER.lower() not in {"none", "off", "false"}:
        print(f"YouTube cookies will be read from {COOKIES_BROWSER}; log in to YouTube in that browser.")
    connection_backoff = 10
    while True:
        try:
            response = session.get(BASE_URL + "/api/local-downloader/next", headers=HEADERS, timeout=30)
            response.raise_for_status()
            connection_backoff = 10
            job = response.json().get("job")
            if not job:
                time.sleep(POLL_SECONDS)
                continue
            job_id, url = job["id"], job["url"]
            print(f"Downloading job {job_id} at below-normal priority…")
            try:
                with tempfile.TemporaryDirectory(prefix="liveclip-") as temp:
                    source = download(url, Path(temp))
                    with source.open("rb") as stream:
                        result = session.post(
                            f"{BASE_URL}/api/local-downloader/{job_id}/upload",
                            headers=HEADERS,
                            files={"file": (source.name, stream, "application/octet-stream")},
                            timeout=(30, 1800),
                        )
                    result.raise_for_status()
                    print(f"Job {job_id}: uploaded. Cloud clipping is now running.")
            except Exception as exc:
                print(f"Job {job_id} failed: {type(exc).__name__}: {exc}")
                print("Fix the download issue, then retry this job from the website.")
                time.sleep(3)
        except KeyboardInterrupt:
            print("Downloader stopped.")
            return
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retry_after = getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After")
            if status == 429:
                wait = max(30, int(retry_after)) if retry_after and retry_after.isdigit() else max(30, connection_backoff)
                connection_backoff = min(max(connection_backoff * 2, 30), 180)
                print(f"Server rate-limited the downloader (429). Waiting {wait}s before retrying.")
            elif status in {502, 503, 504}:
                wait = connection_backoff
                connection_backoff = min(connection_backoff * 2, 180)
                print(f"Server gateway issue ({status}). Waiting {wait}s before retrying.")
            else:
                wait = connection_backoff
                connection_backoff = min(connection_backoff * 2, 180)
                print(f"Connection issue: {type(exc).__name__}: {exc}. Retrying in {wait}s.")
            time.sleep(wait)


if __name__ == "__main__":
    main()
