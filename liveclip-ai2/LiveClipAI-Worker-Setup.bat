@echo off
setlocal
title LiveClip AI - YouTube Downloader Worker

rem IMPORTANT: Replace the placeholder below in your LOCAL copy only.
rem Never commit your real token to GitHub or share this file.
set "LOCAL_DOWNLOADER_TOKEN=PASTE_YOUR_RENDER_LOCAL_DOWNLOADER_TOKEN_HERE"
set "LIVECLIP_URL=https://liveclip-ai-legal-2.onrender.com"
set "YTDLP_COOKIES_FROM_BROWSER=chrome"

if "%LOCAL_DOWNLOADER_TOKEN%"=="PASTE_YOUR_RENDER_LOCAL_DOWNLOADER_TOKEN_HERE" (
  echo ERROR: Open this BAT file and paste your Render LOCAL_DOWNLOADER_TOKEN into the variable near the top.
  pause
  exit /b 1
)

echo ==========================================
echo     LiveClip AI - Downloader Worker
echo ==========================================
echo [1/3] Installing/updating required packages...
py -m pip install --upgrade yt-dlp requests
if errorlevel 1 (
  echo ERROR: Package installation failed.
  pause
  exit /b 1
)

echo [2/3] Downloading the latest worker from GitHub...
set "WORKER=%USERPROFILE%\Downloads\LiveClipWorker.py"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/aymenmc752-stack/liveclip-ai-legal/main/liveclip-ai2/windows_downloader.py' -OutFile $env:USERPROFILE\Downloads\LiveClipWorker.py"
if errorlevel 1 (
  echo ERROR: Could not download the worker from GitHub.
  pause
  exit /b 1
)
if not exist "%WORKER%" (
  echo ERROR: Worker file was not created.
  pause
  exit /b 1
)

echo [3/3] Starting LiveClip AI worker...
echo Make sure you are signed in to YouTube in Chrome.
echo Keep this window open while the worker runs.
py "%WORKER%"
echo.
echo Worker has stopped.
pause
endlocal
