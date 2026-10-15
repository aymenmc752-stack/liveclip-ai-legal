"""
Low-CPU Windows downloader helper for LiveClip AI.
Install: py -m pip install -U yt-dlp requests
Configure LIVECLIP_URL and LOCAL_DOWNLOADER_TOKEN as environment variables.
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
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

def download(url: str, folder: Path) -> Path:
    output = folder / "source.%(ext)s"
    command = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", "--no-warnings", "--quiet",
        # Prefer a combined stream so the PC does not need to merge separate tracks.
        "--format", "best[height<=720][acodec!=none][vcodec!=none]/best[acodec!=none][vcodec!=none]",
        "--limit-rate", MAX_RATE,
        "--retries", "3", "--extractor-retries", "3",
        "--output", str(output), url,
    ]
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
    subprocess.run(command, check=True, cwd=str(folder), **kwargs)
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"} and not p.name.endswith(".part")]
    if not files:
        raise RuntimeError("yt-dlp finished but no video file was created.")
    return max(files, key=lambda p: p.stat().st_size)

def main():
    if not BASE_URL or not TOKEN:
        raise SystemExit("Set LIVECLIP_URL and LOCAL_DOWNLOADER_TOKEN first.")
    session = requests.Session()
    print("LiveClip low-CPU downloader running. One job at a time; Ctrl+C to stop.")
    while True:
        try:
            response = session.get(BASE_URL + "/api/local-downloader/next", headers=HEADERS, timeout=30)
            response.raise_for_status()
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
                print("This job may need to be retried from the website.")
                time.sleep(3)
        except KeyboardInterrupt:
            print("Downloader stopped.")
            return
        except Exception as exc:
            print(f"Connection issue: {type(exc).__name__}: {exc}")
            time.sleep(10)

if __name__ == "__main__":
    main()
