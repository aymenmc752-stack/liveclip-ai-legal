```bat
@echo off
title LiveClip AI
cd /d "C:\Users\Administrator\Desktop\liveclip-ai"

echo Starting LiveClip AI...
start "LiveClip AI Server" cmd /k "cd /d C:\Users\Administrator\Desktop\liveclip-ai && py -3.11 -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

timeout /t 5 /nobreak >nul
start "" "http://127.0.0.1:8000"
exit
```