from __future__ import annotations
import json, os, re, shutil, sqlite3, subprocess, threading, time, uuid, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
# Helpful defaults for the Windows setup this project was configured on.
# Explicit .env values always win; these defaults are used only if the files exist.
if os.name == 'nt':
    _default_ffmpeg_dir = Path(r'C:\Users\Administrator\Desktop\ffmpeg-9.0.2-essentials_build\bin')
    _default_deno = Path(r'C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Packages\DenoLand.Deno_Microsoft.Winget.Source_8wekyb3d8bbwe\deno.exe')
    if not os.getenv('FFMPEG_PATH') and (_default_ffmpeg_dir / 'ffmpeg.exe').exists():
        os.environ['FFMPEG_PATH'] = str(_default_ffmpeg_dir / 'ffmpeg.exe')
    if not os.getenv('FFPROBE_PATH') and (_default_ffmpeg_dir / 'ffprobe.exe').exists():
        os.environ['FFPROBE_PATH'] = str(_default_ffmpeg_dir / 'ffprobe.exe')
    if not os.getenv('DENO_PATH') and _default_deno.exists():
        os.environ['DENO_PATH'] = str(_default_deno)
    _tool_dirs = [str(_default_ffmpeg_dir), str(_default_deno.parent)]
    os.environ['PATH'] = os.pathsep.join(_tool_dirs + [os.environ.get('PATH', '')])
DATA = ROOT / 'data'; OUTPUT = ROOT / 'output'; UPLOADS = DATA / 'uploads'
for p in (DATA, OUTPUT, UPLOADS): p.mkdir(parents=True, exist_ok=True)
DB = DATA / 'liveclip.db'
LOCK = threading.Lock()


def db():
    conn = sqlite3.connect(DB, timeout=30); conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, source_url TEXT, source_type TEXT, duration INTEGER, style TEXT, captions INTEGER, clips INTEGER DEFAULT 10, status TEXT, progress INTEGER DEFAULT 0, message TEXT, created_at TEXT, result_json TEXT, error TEXT)''')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(jobs)').fetchall()}
    if 'clips' not in columns:
        conn.execute('ALTER TABLE jobs ADD COLUMN clips INTEGER DEFAULT 10')
    if 'watermark' not in columns:
        conn.execute('ALTER TABLE jobs ADD COLUMN watermark INTEGER DEFAULT 0')
    if 'resolution' not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN resolution TEXT DEFAULT '1080x1920'")
    conn.execute('''CREATE TABLE IF NOT EXISTS schedule (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER, days_json TEXT, time_local TEXT, timezone TEXT, youtube INTEGER, tiktok INTEGER, updated_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS scheduled_posts (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, clip_name TEXT NOT NULL, platform TEXT NOT NULL, title TEXT NOT NULL, description TEXT DEFAULT '', privacy_level TEXT DEFAULT 'SELF_ONLY', publish_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_json TEXT, error TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_scheduled_posts_due ON scheduled_posts(status, publish_at)')
    conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, platform TEXT NOT NULL, label TEXT NOT NULL, authorized INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, last_checked TEXT, last_video_id TEXT, status TEXT DEFAULT 'Added', error TEXT, created_at TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS watchlist_seen (video_id TEXT PRIMARY KEY, watchlist_id INTEGER NOT NULL, job_id TEXT, seen_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', error TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS watchlist_settings (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0, platform TEXT NOT NULL DEFAULT 'youtube', daily_time TEXT NOT NULL DEFAULT '18:00', timezone TEXT NOT NULL DEFAULT 'Africa/Algiers', resolution TEXT NOT NULL DEFAULT '1080x1920', watermark INTEGER NOT NULL DEFAULT 1, updated_at TEXT)''')
    conn.execute("INSERT OR IGNORE INTO watchlist_settings (id,enabled,platform,daily_time,timezone,resolution,watermark,updated_at) VALUES (1,0,'youtube','18:00','Africa/Algiers','1080x1920',1,'')")
    conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, platform TEXT NOT NULL, label TEXT NOT NULL, authorized INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, last_checked TEXT, last_video_id TEXT, status TEXT DEFAULT 'Added', error TEXT, created_at TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS watchlist_seen (video_id TEXT PRIMARY KEY, watchlist_id INTEGER NOT NULL, job_id TEXT, seen_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', error TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS watchlist_settings (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0, platform TEXT NOT NULL DEFAULT 'youtube', daily_time TEXT NOT NULL DEFAULT '18:00', timezone TEXT NOT NULL DEFAULT 'Africa/Algiers', resolution TEXT NOT NULL DEFAULT '1080x1920', watermark INTEGER NOT NULL DEFAULT 1, updated_at TEXT)''')
    conn.execute("INSERT OR IGNORE INTO watchlist_settings (id,enabled,platform,daily_time,timezone,resolution,watermark,updated_at) VALUES (1,0,'youtube','18:00','Africa/Algiers','1080x1920',1,'')")
    conn.commit(); return conn


def update_job(job_id: str, **fields):
    if not fields: return
    conn = db(); cols=', '.join(f'{k}=?' for k in fields)
    conn.execute(f'UPDATE jobs SET {cols} WHERE id=?', (*fields.values(), job_id)); conn.commit(); conn.close()


def get_job(job_id: str):
    conn=db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone(); conn.close()
    if not row: return None
    d=dict(row); d['results']=json.loads(d.pop('result_json') or '[]'); return d


def list_jobs():
    conn=db(); rows=conn.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100').fetchall(); conn.close()
    out=[]
    for row in rows:
        d=dict(row); d['results']=json.loads(d.pop('result_json') or '[]'); out.append(d)
    return out


def tool_path(env_name: str, command_name: str) -> str:
    """Resolve a media executable from an explicit env path or PATH."""
    configured = os.getenv(env_name, '').strip().strip('"')
    if configured:
        # Accept either a full executable path or a directory containing the executable.
        candidate = Path(configured)
        if candidate.is_dir():
            candidate = candidate / (command_name + ('.exe' if os.name == 'nt' else ''))
        if candidate.exists():
            return str(candidate)
        # Keep the configured value so subprocess produces a useful error.
        return str(candidate)
    return shutil.which(command_name) or command_name


def run(cmd: list[str], timeout: int | None = None):
    # Resolve FFmpeg tools explicitly; this works on Windows and in Docker/Linux.
    if cmd and cmd[0] == 'ffmpeg':
        cmd = [tool_path('FFMPEG_PATH', 'ffmpeg'), *cmd[1:]]
    elif cmd and cmd[0] == 'ffprobe':
        cmd = [tool_path('FFPROBE_PATH', 'ffprobe'), *cmd[1:]]
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, check=True)
    except subprocess.CalledProcessError as e:
        detail=(e.stderr or e.stdout or '').strip()
        if detail:
            raise RuntimeError(f"Command failed ({e.returncode}): {' '.join(cmd[:3])}\n{detail[-2500:]}") from e
        raise


def probe(path: Path) -> dict[str, Any]:
    r=run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_type,width,height','-of','json',str(path)], 60)
    data=json.loads(r.stdout); duration=float(data.get('format',{}).get('duration',0) or 0)
    video=next((s for s in data.get('streams',[]) if s.get('codec_type')=='video'),{})
    if duration <= 0 or not video: raise ValueError('Could not read a video stream from this file.')
    return {'duration':duration,'width':int(video.get('width',0)),'height':int(video.get('height',0))}


# Cancellation flags are process-local; restart clears stale flags.
CANCEL_FLAGS: dict[str, threading.Event] = {}
CANCEL_LOCK = threading.Lock()


def cancel_requested(job_id: str) -> bool:
    with CANCEL_LOCK:
        flag = CANCEL_FLAGS.get(job_id)
    return bool(flag and flag.is_set())


def check_cancelled(job_id: str):
    if cancel_requested(job_id):
        raise RuntimeError('Job cancelled by user.')


def download_url(url: str, job_id: str) -> Path:
    """Download with yt-dlp's progress hooks so the UI doesn't sit at 5%."""
    dest = UPLOADS / job_id
    dest.mkdir(parents=True, exist_ok=True)
    template = str(dest / 'source.%(ext)s')
    deno = os.getenv('DENO_PATH', '').strip().strip('"') or shutil.which('deno')
    ffmpeg_dir = str(Path(tool_path('FFMPEG_PATH', 'ffmpeg')).parent)
    # Optional YouTube cookies for hosted environments that receive sign-in/bot challenges.
    # Configure YOUTUBE_COOKIES_FILE to a private mounted secret-file path; never commit cookies.
    cookies_file = os.getenv('YOUTUBE_COOKIES_FILE', '').strip().strip('"')

    def progress_hook(data):
        check_cancelled(job_id)
        status = data.get('status')
        if status == 'downloading':
            downloaded = int(data.get('downloaded_bytes') or 0)
            total = int(data.get('total_bytes') or data.get('total_bytes_estimate') or 0)
            if total > 0:
                pct = max(0, min(100, int(downloaded * 100 / total)))
                update_job(job_id, progress=min(12, 5 + int(pct * 0.07)),
                           message=f'Downloading source video… {pct}% ({downloaded // (1024*1024)} MB' + (f' / {total // (1024*1024)} MB)' if total else ' MB)'))
            else:
                update_job(job_id, progress=6, message=f'Downloading source video… {downloaded // (1024*1024)} MB received (size unknown)')
        elif status == 'finished':
            update_job(job_id, progress=12, message='Download finished. Preparing video file…')

    opts = {
        'noplaylist': True,
        'restrictfilenames': True,
        'format': 'bv*[height<=720]+ba/b[height<=720]/b',
        'merge_output_format': 'mp4',
        'outtmpl': template,
        'ffmpeg_location': ffmpeg_dir,
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [progress_hook],
        'continuedl': True,
    }
    if deno and os.path.isfile(deno):
        opts['js_runtimes'] = {'deno': {'path': deno}}
    if cookies_file and os.path.isfile(cookies_file):
        opts['cookiefile'] = cookies_file
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        if cancel_requested(job_id):
            raise RuntimeError('Job cancelled by user.') from e
        raise RuntimeError(f'Video download failed: {type(e).__name__}: {e}') from e

    check_cancelled(job_id)
    candidates = [p for p in dest.iterdir() if p.is_file() and p.suffix.lower() in {'.mp4', '.mkv', '.webm', '.mov', '.m4v'} and not p.name.endswith('.part')]
    if not candidates:
        raise ValueError('Downloader finished but no video file was found. Check the URL and try a public VOD/recording.')
    return max(candidates, key=lambda p: p.stat().st_size)


def transcribe(path: Path):
    try:
        from faster_whisper import WhisperModel
        model_name=os.getenv('WHISPER_MODEL','tiny')
        model=WhisperModel(model_name, device=os.getenv('WHISPER_DEVICE','cpu'), compute_type=os.getenv('WHISPER_COMPUTE_TYPE','int8'))
        segments, info=model.transcribe(str(path), vad_filter=True, word_timestamps=False, beam_size=1)
        rows=[]
        for s in segments:
            text=(s.text or '').strip()
            if text: rows.append({'start':float(s.start),'end':float(s.end),'text':text})
        return rows, None
    except Exception as e:
        return [], f'Transcription unavailable: {type(e).__name__}: {e}'


def audio_peaks(path: Path, total_duration: float):
    # Low-resolution RMS envelope. Peaks suggest excitement/laughter but are not guaranteed funny.
    import wave, math
    from array import array
    wav=path.with_suffix('.mono.wav')
    run(['ffmpeg','-y','-i',str(path),'-vn','-ac','1','-ar','8000','-f','wav',str(wav)], timeout=60*30)
    peaks=[]
    try:
        with wave.open(str(wav),'rb') as w:
            rate=w.getframerate(); width=w.getsampwidth(); chunk=rate*2
            i=0
            while True:
                data=w.readframes(chunk)
                if not data: break
                samples=array('h'); samples.frombytes(data[:len(data)-(len(data)%2)])
                rms=int(math.sqrt(sum(x*x for x in samples)/len(samples))) if samples else 0
                peaks.append((i*2, rms)); i+=1
    finally:
        wav.unlink(missing_ok=True)
    if not peaks: return []
    vals=sorted(v for _,v in peaks); baseline=vals[len(vals)//2] or 1
    candidates=[]
    for t,v in peaks:
        if v >= max(900, baseline*1.65) and t < total_duration-15:
            candidates.append({'start':max(0,t-8),'end':min(total_duration,t+22),'score':round(v/max(1,baseline),2),'reason':'Audio-energy peak'})
    # Merge overlapping candidates.
    merged=[]
    for c in sorted(candidates,key=lambda x:x['start']):
        if merged and c['start'] <= merged[-1]['end']:
            merged[-1]['end']=max(merged[-1]['end'],c['end']); merged[-1]['score']=max(merged[-1]['score'],c['score'])
        else: merged.append(c)
    return merged


def choose_candidates(duration: float, transcript: list[dict], peaks: list[dict], clip_len: int, max_clips: int, style: str):
    # Mix speech windows with loud-audio candidates; speech windows make useful fallback clips.
    candidates=[]
    for p in peaks:
        center=(p['start']+p['end'])/2; start=max(0,min(duration-clip_len,center-clip_len/2))
        candidates.append({'start':start,'end':min(duration,start+clip_len),'score':p['score'],'reason':p['reason']})
    if transcript:
        # Divide transcript into candidate windows; score based on punctuation/reaction vocabulary and speech density.
        words_re=re.compile(r'\b(ha+|lol|lmao|no way|what|bro|oh my|crazy|insane|shut up|help|wow|bruh|damn|laugh|scream|really)\b',re.I)
        step=max(6,clip_len//3)
        for start in range(0,max(1,int(duration-clip_len)+1),step):
            end=min(duration,start+clip_len)
            ss=[s for s in transcript if s['end']>start and s['start']<end]
            text=' '.join(s['text'] for s in ss)
            if not text.strip(): continue
            hits=len(words_re.findall(text)); density=min(1,len(text)/180)
            score=1.0 + hits*1.5 + density
            if style=='Big reactions': score += hits*0.4
            candidates.append({'start':float(start),'end':float(end),'score':score,'reason':'Speech/reaction candidate','transcript':text})
    if not candidates:
        start=0
        while start < duration-1:
            end=min(duration,start+clip_len)
            if end-start >= 10: candidates.append({'start':start,'end':end,'score':1,'reason':'Fallback segment'})
            start += clip_len
    # Greedy diversity: avoid clips with heavy overlap.
    selected=[]
    for c in sorted(candidates,key=lambda x:x['score'],reverse=True):
        if any(abs((c['start']+c['end'])/2-(s['start']+s['end'])/2)<clip_len*0.55 for s in selected): continue
        c['start']=max(0,float(c['start'])); c['end']=min(duration,c['start']+clip_len)
        if c['end']-c['start'] >= 10: selected.append(c)
        if len(selected)>=max_clips: break
    return sorted(selected,key=lambda x:x['start'])


def srt_time(seconds: float):
    ms=int(seconds*1000); h=ms//3600000; ms%=3600000; m=ms//60000; ms%=60000; s=ms//1000; ms%=1000
    return f'{h:02}:{m:02}:{s:02},{ms:03}'


def make_srt(transcript, start, end, output):
    lines=[]; n=1
    for seg in transcript:
        a=max(start,seg['start']); b=min(end,seg['end'])
        if b<=a: continue
        txt=seg['text'].replace('-->','→').strip()
        if not txt: continue
        # Keep subtitles readable in short lines.
        words=txt.split(); chunks=[]; current=''
        for word in words:
            if len(current)+len(word)+1>38 and current: chunks.append(current); current=word
            else: current=(current+' '+word).strip()
        if current: chunks.append(current)
        duration=max(.4,(b-a)/len(chunks))
        for i,ch in enumerate(chunks):
            aa=max(0,a-start+i*duration); bb=min(end-start,aa+duration)
            lines += [str(n),f'{srt_time(aa)} --> {srt_time(bb)}',ch,'']; n+=1
    output.write_text('\n'.join(lines),encoding='utf-8')


def render_clip(source: Path, candidate: dict, transcript: list, job_id: str, index: int, captions: bool, meta: dict, watermark: bool = False, resolution: str = '1080x1920'):
    start=float(candidate['start']); end=float(candidate['end']); duration=end-start
    outdir=OUTPUT/job_id; outdir.mkdir(parents=True,exist_ok=True)
    out=outdir/f'clip_{index:02}.mp4'; srt=outdir/f'clip_{index:02}.srt'
    if captions and transcript: make_srt(transcript,start,end,srt)
    # All supported output sizes are exact 9:16. Scale-to-fill + center crop avoids bars;
    # it is a center crop rather than AI speaker tracking.
    dimensions = {'720x1280': (720,1280), '1080x1920': (1080,1920), '1440x2560': (1440,2560), '2160x3840': (2160,3840)}
    width,height = dimensions.get(resolution, dimensions['1080x1920'])
    vf=f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=30"
    if captions and transcript:
        escaped=str(srt.resolve()).replace('\\','/').replace(':','\\:').replace("'","\\'")
        vf += f",subtitles='{escaped}':force_style='FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,Alignment=2,MarginV=160'"
    if watermark:
        # A subtle text watermark, burned into every rendered frame.
        vf += ",drawtext=text='TizShort':fontcolor=white@0.82:fontsize=42:borderw=3:bordercolor=black@0.65:x=w-tw-42:y=h-th-70"
    run(['ffmpeg','-y','-ss',str(start),'-i',str(source),'-t',str(duration),'-vf',vf,'-c:v','libx264','-preset','ultrafast','-crf','24','-c:a','aac','-b:a','128k','-movflags','+faststart',str(out)],timeout=60*20)
    return {'file':str(out.relative_to(ROOT)),'url':f'/output/{job_id}/{out.name}','duration':round(duration,1),'start':round(start,1),'end':round(end,1),'score':candidate['score'],'reason':candidate['reason'],'transcript':candidate.get('transcript','')[:300], 'resolution':f'{width}x{height}'}



def generate_clip_title(transcript_text: str, style: str = 'Funny moments') -> str:
    """Suggest a short, relevant title from the clip transcript. Uses an optional AI API key,
    with a transcript-based local fallback so the feature still works without paid services."""
    text = re.sub(r'\s+', ' ', (transcript_text or '')).strip()
    if not text:
        return 'Best Moment #Shorts'
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    if api_key:
        try:
            import requests
            response = requests.post(
                'https://api.openai.com/v1/chat/completions',
                headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                json={'model': os.getenv('TITLE_AI_MODEL', 'gpt-4o-mini'), 'temperature': 0.7,
                      'messages': [
                          {'role': 'system', 'content': 'Write one compelling but accurate YouTube Shorts title based only on the clip transcript. Keep it under 80 characters, natural, specific, no fabricated claims, no quotation marks. Add #Shorts if it fits. Return only the title.'},
                          {'role': 'user', 'content': f'Clip style: {style}\nTranscript: {text[:2500]}'}]},
                timeout=18)
            response.raise_for_status()
            title = response.json()['choices'][0]['message']['content'].strip().strip('\"\'')
            title = re.sub(r'\s+', ' ', title).strip()
            if title:
                return title[:100]
        except Exception:
            pass
    # No API key/network? Build a relevant, transcript-grounded title locally.
    clean = re.sub(r'\[[^]]*\]', ' ', text)
    clean = re.sub(r"[^\w\s!?.,'-]", " ", clean)
    words = clean.split()
    if len(words) > 11:
        clean = ' '.join(words[:11]).rstrip('.,!?') + '…'
    clean = clean.strip(' .,!?:;')
    if not clean:
        clean = 'The Moment Everyone Reacted To'
    title = clean[0].upper() + clean[1:]
    if len(title) <= 88:
        title += ' #Shorts'
    return title[:100]

def process_job(job_id: str, source_path: Path | None = None):
    job=get_job(job_id)
    if not job: return
    try:
        with CANCEL_LOCK:
            current = get_job(job_id)
            if not current or current['status'] == 'cancelled':
                return
            CANCEL_FLAGS[job_id] = threading.Event()
            update_job(job_id,status='processing',progress=5,message='Preparing video…')
        if source_path is None:
            source_path=download_url(job['source_url'],job_id)
        check_cancelled(job_id)
        info=probe(source_path)
        if info['duration'] < 15: raise ValueError('Video must be at least 15 seconds long.')
        update_job(job_id,progress=15,message='Transcribing speech (first run may download a Whisper model)…')
        transcript, transcribe_warning=transcribe(source_path)
        check_cancelled(job_id)
        update_job(job_id,progress=45,message='Analyzing audio peaks and ranking candidate moments…')
        peaks=audio_peaks(source_path,info['duration'])
        check_cancelled(job_id)
        requested=job['duration']
        clip_len=min(60,max(15,int(requested if requested else 30)))
        candidates=choose_candidates(info['duration'],transcript,peaks,clip_len,int(job.get('clips') or os.getenv('CLIPS_PER_JOB','10')),job['style'])
        if not candidates: raise ValueError('No usable clip segments found.')
        results=[]
        for i,c in enumerate(candidates,1):
            check_cancelled(job_id)
            update_job(job_id,progress=45+int(45*(i-1)/len(candidates)),message=f'Rendering clip {i} of {len(candidates)}…')
            rendered = render_clip(source_path,c,transcript,job_id,i,bool(job['captions']),info,bool(job.get('watermark', 0)), job.get('resolution', '1080x1920'))
            clip_words = [seg.get('text', '') for seg in transcript if seg.get('end', 0) > c['start'] and seg.get('start', 0) < c['end']]
            title_source = ' '.join(clip_words).strip() or c.get('transcript', '')
            rendered['title'] = generate_clip_title(title_source, job.get('style', 'Funny moments'))
            results.append(rendered)
        message='Finished. Review the clips before publishing.'
        if transcribe_warning: message += ' '+transcribe_warning
        update_job(job_id,status='completed',progress=100,message=message,result_json=json.dumps(results))
    except Exception as e:
        cancelled = cancel_requested(job_id) or 'cancelled by user' in str(e).lower()
        update_job(job_id,status='cancelled' if cancelled else 'failed',progress=100,message='Cancelled' if cancelled else 'Processing failed',error=None if cancelled else f'{type(e).__name__}: {e}')
    finally:
        with CANCEL_LOCK:
            CANCEL_FLAGS.pop(job_id, None)
