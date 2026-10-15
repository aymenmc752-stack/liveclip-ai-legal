from __future__ import annotations
import json, os, re, shutil, uuid, threading, time, base64, hmac, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from urllib.parse import urlencode
from . import publishing
from .core import ROOT, DATA, OUTPUT, UPLOADS, db, update_job, get_job, list_jobs, process_job, probe, CANCEL_FLAGS, CANCEL_LOCK
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

app=FastAPI(title='LiveClip AI',version='0.2.0',description='Free-first livestream highlight clipper')


def _get_timezone(name: str):
    """Resolve IANA timezones, including common zones on Windows without OS tzdata."""
    import zoneinfo
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:
        # The Watchlist UI offers these two zones; keep them working even if
        # the machine has no IANA timezone database installed (common on Windows).
        from datetime import timedelta
        if name == 'UTC':
            return timezone.utc
        if name == 'Africa/Algiers':
            return timezone(timedelta(hours=1), name='Africa/Algiers')
        raise ValueError(f'Unknown timezone: {name}')

@app.middleware('http')
async def optional_basic_auth(request, call_next):
    """Protect public deployments when APP_USERNAME and APP_PASSWORD are set."""
    username=os.getenv('APP_USERNAME','')
    password=os.getenv('APP_PASSWORD','')
    if username and password and request.url.path != '/api/health':
        header=request.headers.get('authorization','')
        supplied_user=supplied_pass=''
        if header.startswith('Basic '):
            try:
                decoded=base64.b64decode(header[6:], validate=True).decode('utf-8')
                supplied_user, supplied_pass=decoded.split(':',1)
            except Exception:
                pass
        if not (hmac.compare_digest(supplied_user,username) and hmac.compare_digest(supplied_pass,password)):
            from fastapi.responses import Response
            return Response('Authentication required', status_code=401, headers={'WWW-Authenticate':'Basic realm="LiveClip AI"'})
    return await call_next(request)
app.mount('/output',StaticFiles(directory=str(OUTPUT)),name='output')
app.mount('/static',StaticFiles(directory=str(ROOT)),name='static')

class UrlJob(BaseModel):
    url: str
    duration: int | None = Field(default=30, ge=15, le=60)
    style: str = 'Funny moments'
    captions: bool = True
    clips: int = Field(default=10, ge=1, le=30)
    watermark: bool = False
    resolution: str = '1080x1920'

class ScheduleConfig(BaseModel):
    enabled: bool = False
    days: list[int] = [0,1,2,3,4]
    time: str = '18:00'
    timezone: str = 'Africa/Algiers'
    youtube: bool = False
    tiktok: bool = False


def valid_url(url: str):
    from urllib.parse import urlparse
    u=urlparse(url); host=(u.hostname or '').lower()
    return u.scheme in ('http','https') and (host=='youtube.com' or host.endswith('.youtube.com') or host=='youtu.be' or host=='twitch.tv' or host.endswith('.twitch.tv'))


def create_job(url, source_type, duration, style, captions, clips=10, watermark=False, resolution='1080x1920'):
    if resolution not in {'720x1280','1080x1920'}: raise HTTPException(400, 'Choose a supported 9:16 resolution.')
    jid=uuid.uuid4().hex[:12]
    conn=db(); conn.execute('INSERT INTO jobs (id,source_url,source_type,duration,style,captions,clips,watermark,resolution,status,progress,message,created_at,result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (jid,url,source_type,duration,style,int(captions),int(clips),int(watermark),resolution,'queued',0,'Waiting for worker…',datetime.now(timezone.utc).isoformat(),'[]')); conn.commit(); conn.close(); return jid

_scheduler_stop = threading.Event()
_scheduler_thread = None
_watchlist_thread = None
_watchlist_lock = threading.Lock()
_watchlist_thread = None
_watchlist_lock = threading.Lock()

def _run_due_scheduled_posts():
    """Claim due posts in SQLite before publishing, preventing duplicate attempts."""
    now = datetime.now(timezone.utc).isoformat()
    conn = db()
    rows = conn.execute("SELECT * FROM scheduled_posts WHERE status='pending' AND publish_at<=? ORDER BY publish_at LIMIT 5", (now,)).fetchall()
    for row in rows:
        changed = conn.execute("UPDATE scheduled_posts SET status='publishing', updated_at=? WHERE id=? AND status='pending'", (now, row['id'])).rowcount
        if not changed:
            continue
        conn.commit()
        try:
            clip = (OUTPUT / row['job_id'] / row['clip_name']).resolve()
            if not clip.is_relative_to(OUTPUT.resolve()) or not clip.is_file():
                raise RuntimeError('Scheduled clip file no longer exists.')
            if row['platform'] == 'youtube':
                result = publishing.upload_youtube(clip, row['title'], row['description'] or '')
            elif row['platform'] == 'tiktok':
                result = publishing.publish_tiktok(clip, row['title'], row['privacy_level'] or 'SELF_ONLY')
            else:
                raise RuntimeError('Unsupported publishing platform.')
            conn.execute("UPDATE scheduled_posts SET status='published', result_json=?, error=NULL, updated_at=? WHERE id=?", (json.dumps(result), datetime.now(timezone.utc).isoformat(), row['id']))
        except Exception as exc:
            conn.execute("UPDATE scheduled_posts SET status='failed', error=?, updated_at=? WHERE id=?", (str(exc)[:2000], datetime.now(timezone.utc).isoformat(), row['id']))
        conn.commit()
    conn.close()

def _scheduler_loop():
    while not _scheduler_stop.wait(20):
        try:
            _run_due_scheduled_posts()
        except Exception:
            # Keep the worker alive if one polling pass fails.
            pass

@app.on_event('startup')
def startup():
    global _scheduler_thread, _watchlist_thread
    public_base=os.getenv('PUBLIC_BASE_URL','http://localhost:8000').lower()
    if not any(host in public_base for host in ('localhost','127.0.0.1')) and not (os.getenv('APP_USERNAME') and os.getenv('APP_PASSWORD')):
        raise RuntimeError('Public deployment requires APP_USERNAME and APP_PASSWORD to protect the web app.')
    db().close()
    if _scheduler_thread is None or not _scheduler_thread.is_alive():
        _scheduler_stop.clear()
        _scheduler_thread = threading.Thread(target=_scheduler_loop, name='liveclip-scheduler', daemon=True)
        _scheduler_thread.start()
    if _watchlist_thread is None or not _watchlist_thread.is_alive():
        _watchlist_thread = threading.Thread(target=_watchlist_loop, name='liveclip-watchlist', daemon=True)
        _watchlist_thread.start()

@app.on_event('shutdown')
def shutdown():
    _scheduler_stop.set()

@app.get('/',response_class=HTMLResponse)
def home():
    return (ROOT/'index.html').read_text(encoding='utf-8')

@app.get('/api/health')
def health():
    from .core import tool_path
    try:
        import yt_dlp  # noqa: F401
        ytdlp_ok = True
    except Exception:
        ytdlp_ok = False
    ffmpeg = tool_path('FFMPEG_PATH', 'ffmpeg')
    ffprobe = tool_path('FFPROBE_PATH', 'ffprobe')
    return {'ok':True,'ffmpeg':Path(ffmpeg).exists() if os.path.isabs(ffmpeg) else shutil.which(ffmpeg) is not None,'ffprobe':Path(ffprobe).exists() if os.path.isabs(ffprobe) else shutil.which(ffprobe) is not None,'yt_dlp':ytdlp_ok,'deno':bool(os.getenv('DENO_PATH') and Path(os.getenv('DENO_PATH').strip().strip('\"')).exists()) or shutil.which('deno') is not None,'whisper':'optional: installed on first processing run if dependencies are present','note':'Background scheduled-post worker checks due posts every 20 seconds while this server process is running. The host must remain online and persistent storage is required.'}

@app.get('/api/jobs')
def jobs(): return list_jobs()

@app.get('/api/jobs/{job_id}')
def job(job_id: str):
    j=get_job(job_id)
    if not j: raise HTTPException(404,'Job not found')
    return j

@app.delete('/api/jobs/{job_id}')
def delete_job(job_id: str):
    """Permanently remove a finished/failed job and its local clips/uploads."""
    j=get_job(job_id)
    if not j: raise HTTPException(404, 'Job not found')
    if j['status'] in ('queued', 'processing'):
        raise HTTPException(409, 'Cancel the job and wait for it to stop before deleting it.')
    # Only delete paths contained within this app's own output/upload folders.
    out_dir=(OUTPUT/job_id).resolve()
    if out_dir.is_relative_to(OUTPUT.resolve()) and out_dir.exists():
        shutil.rmtree(out_dir)
    for p in UPLOADS.glob(job_id+'.*'):
        resolved=p.resolve()
        if resolved.is_relative_to(UPLOADS.resolve()) and resolved.is_file():
            resolved.unlink(missing_ok=True)
    conn=db(); conn.execute('DELETE FROM jobs WHERE id=?',(job_id,)); conn.commit(); conn.close()
    return {'deleted': True, 'id': job_id, 'permanent': True}

@app.post('/api/jobs/{job_id}/cancel')
def cancel_job(job_id: str):
    j=get_job(job_id)
    if not j: raise HTTPException(404, 'Job not found')
    if j['status'] in ('completed', 'failed', 'cancelled'):
        raise HTTPException(409, f"Job is already {j['status']}.")
    with CANCEL_LOCK:
        flag = CANCEL_FLAGS.get(job_id)
        if flag:
            flag.set()
        else:
            # BackgroundTasks may not have started the job yet.
            update_job(job_id, status='cancelled', progress=100, message='Cancelled before processing started', error=None)
    return {'id': job_id, 'status': 'cancelling' if flag else 'cancelled'}

@app.post('/api/jobs/{job_id}/retry', status_code=202)
def retry_job(job_id: str, background: BackgroundTasks):
    j=get_job(job_id)
    if not j: raise HTTPException(404, 'Job not found')
    if j['source_type'] == 'upload':
        raise HTTPException(400, 'Retrying uploaded files is not available yet. Please upload the video again.')
    if j['status'] not in ('failed', 'cancelled'):
        raise HTTPException(409, 'Only failed or cancelled jobs can be retried.')
    jid=create_job(j['source_url'], j['source_type'], j['duration'], j['style'], bool(j['captions']), int(j.get('clips') or 10), bool(j.get('watermark', 0)), j.get('resolution', '1080x1920'))
    background.add_task(process_job, jid)
    return {'id': jid, 'status': 'queued', 'message': 'Retry queued as a new job.'}

@app.post('/api/jobs/url',status_code=202)
def create_from_url(body: UrlJob, background: BackgroundTasks):
    if not valid_url(body.url): raise HTTPException(400,'Use a valid YouTube or Twitch URL.')
    host=body.url.lower(); source='twitch' if 'twitch.tv' in host else 'youtube'
    jid=create_job(body.url,source,body.duration,body.style,body.captions,body.clips,body.watermark,body.resolution)
    background.add_task(process_job,jid)
    return {'id':jid,'status':'queued','message':'Job accepted. Processing runs in the server process.'}

@app.post('/api/jobs/upload',status_code=202)
def create_from_upload(background: BackgroundTasks, file: UploadFile=File(...), duration: int=Form(30), style: str=Form('Funny moments'), captions: bool=Form(True), clips: int=Form(10), watermark: bool=Form(False), resolution: str=Form('1080x1920')):
    if duration<15 or duration>60: raise HTTPException(400,'Duration must be 15–60 seconds.')
    if clips < 1 or clips > 30: raise HTTPException(400,'Choose between 1 and 30 clips.')
    if resolution not in {'720x1280','1080x1920'}: raise HTTPException(400,'Choose a supported 9:16 resolution.')
    ext=Path(file.filename or 'video.mp4').suffix.lower()
    if ext not in {'.mp4','.mkv','.webm','.mov','.m4v','.avi'}: raise HTTPException(400,'Unsupported video extension.')
    jid=uuid.uuid4().hex[:12]; target=UPLOADS/f'{jid}{ext}'
    size=0
    with target.open('wb') as out:
        while True:
            chunk=file.file.read(1024*1024)
            if not chunk: break
            size+=len(chunk)
            if size>8*1024*1024*1024:
                target.unlink(missing_ok=True); raise HTTPException(413,'File exceeds the configured 8 GB upload limit.')
            out.write(chunk)
    conn=db(); conn.execute('INSERT INTO jobs (id,source_url,source_type,duration,style,captions,clips,watermark,resolution,status,progress,message,created_at,result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (jid,'uploaded file','upload',duration,style,int(captions),clips,int(watermark),resolution,'queued',0,'Upload received',datetime.now(timezone.utc).isoformat(),'[]')); conn.commit(); conn.close()
    background.add_task(process_job,jid,target)
    return {'id':jid,'status':'queued','message':'Upload accepted.'}

@app.get('/api/schedule')
def get_schedule():
    conn=db(); row=conn.execute('SELECT * FROM schedule WHERE id=1').fetchone(); conn.close()
    if not row: return {'enabled':False,'days':[0,1,2,3,4],'time':'18:00','timezone':'Africa/Algiers','youtube':False,'tiktok':False}
    return {'enabled':bool(row['enabled']),'days':json.loads(row['days_json']),'time':row['time_local'],'timezone':row['timezone'],'youtube':bool(row['youtube']),'tiktok':bool(row['tiktok'])}

@app.put('/api/schedule')
def save_schedule(config: ScheduleConfig):
    try: _get_timezone(config.timezone)
    except Exception: raise HTTPException(400,'Unknown timezone. Choose Africa/Algiers or UTC.')
    if not re.fullmatch(r'\d{2}:\d{2}',config.time): raise HTTPException(400,'Time must use HH:MM.')
    conn=db(); conn.execute('INSERT INTO schedule (id,enabled,days_json,time_local,timezone,youtube,tiktok,updated_at) VALUES (1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,days_json=excluded.days_json,time_local=excluded.time_local,timezone=excluded.timezone,youtube=excluded.youtube,tiktok=excluded.tiktok,updated_at=excluded.updated_at',(int(config.enabled),json.dumps(config.days),config.time,config.timezone,int(config.youtube),int(config.tiktok),datetime.now(timezone.utc).isoformat())); conn.commit(); conn.close()
    return {'saved':True,'config':config.model_dump(),'note':'Posting preferences saved. Use Schedule on an individual generated clip to add it to the persistent automatic publishing queue.'}


class WatchlistAdd(BaseModel):
    url: str
    authorized: bool = False

class WatchlistSettings(BaseModel):
    enabled: bool = False
    platform: str = 'youtube'
    daily_time: str = '18:00'
    timezone: str = 'Africa/Algiers'
    resolution: str = '1080x1920'
    watermark: bool = True


def _normalize_creator_url(url: str):
    from urllib.parse import urlparse
    u=urlparse(url.strip()); host=(u.hostname or '').lower()
    if u.scheme not in ('http','https') or not (host in ('youtube.com','www.youtube.com','m.youtube.com','twitch.tv','www.twitch.tv') or host.endswith('.youtube.com') or host.endswith('.twitch.tv')):
        raise HTTPException(400,'Paste a valid YouTube or Twitch channel URL.')
    if 'youtube.com' in host:
        if not any(x in u.path for x in ('/@','/channel/','/c/','/user/')):
            raise HTTPException(400,'Use a YouTube channel URL, not an individual video URL.')
        platform='youtube'; label=u.path.strip('/').split('/')[-1] or 'YouTube creator'
        clean=f'https://www.youtube.com/{u.path.strip("/")}'
        feed=clean.rstrip('/')+'/videos'
    else:
        slug=u.path.strip('/').split('/')[0]
        if not slug or slug.lower() in ('videos','clip','directory','downloads'):
            raise HTTPException(400,'Use a Twitch channel URL such as https://www.twitch.tv/channelname')
        platform='twitch'; label=slug; clean=f'https://www.twitch.tv/{slug}'; feed=clean.rstrip('/')+'/videos'
    return clean, feed, platform, label

@app.get('/api/watchlist')
def watchlist_list():
    conn=db(); rows=[dict(r) for r in conn.execute('SELECT * FROM watchlist ORDER BY id DESC').fetchall()]; settings=conn.execute('SELECT * FROM watchlist_settings WHERE id=1').fetchone(); conn.close()
    return {'items':rows,'settings':dict(settings) if settings else {}}

@app.post('/api/watchlist',status_code=201)
def watchlist_add(body: WatchlistAdd):
    if not body.authorized:
        raise HTTPException(400,'Confirm you own this content or have explicit permission to clip and republish it.')
    clean,feed,platform,label=_normalize_creator_url(body.url)
    conn=db()
    try:
        cur=conn.execute('INSERT INTO watchlist (url,platform,label,authorized,enabled,status,created_at) VALUES (?,?,?,?,1,?,?)',(feed,platform,label,1,'Added; waiting for next check',datetime.now(timezone.utc).isoformat()))
        conn.commit(); item_id=cur.lastrowid
    except Exception as e:
        conn.close(); raise HTTPException(409,'That creator is already in your Watchlist.')
    conn.close(); return {'id':item_id,'label':label,'platform':platform,'status':'Added'}

@app.delete('/api/watchlist/{item_id}')
def watchlist_delete(item_id:int):
    conn=db(); conn.execute('DELETE FROM watchlist WHERE id=?',(item_id,)); conn.execute('DELETE FROM watchlist_seen WHERE watchlist_id=?',(item_id,)); conn.commit(); conn.close(); return {'deleted':True}

@app.put('/api/watchlist/settings')
def watchlist_settings(body: WatchlistSettings):
    if body.platform not in ('youtube','tiktok'): raise HTTPException(400,'Choose YouTube or TikTok.')
    if body.resolution not in ('720x1280','1080x1920'): raise HTTPException(400,'Choose HD or Full HD.')
    if not re.fullmatch(r'\d{2}:\d{2}',body.daily_time): raise HTTPException(400,'Time must use HH:MM.')
    try: _get_timezone(body.timezone)
    except Exception: raise HTTPException(400,'Unknown timezone. Choose Africa/Algiers or UTC.')
    conn=db(); conn.execute('UPDATE watchlist_settings SET enabled=?,platform=?,daily_time=?,timezone=?,resolution=?,watermark=?,updated_at=? WHERE id=1',(int(body.enabled),body.platform,body.daily_time,body.timezone,body.resolution,int(body.watermark),datetime.now(timezone.utc).isoformat())); conn.commit(); conn.close()
    return {'saved':True,'settings':body.model_dump(),'note':'Monitoring runs while LiveClip AI is running. Only authorized sources are processed; automatic publishing requires the selected platform to be connected and API access approved.'}

@app.post('/api/watchlist/{item_id}/toggle')
def watchlist_toggle(item_id:int):
    conn=db(); row=conn.execute('SELECT enabled FROM watchlist WHERE id=?',(item_id,)).fetchone()
    if not row: conn.close(); raise HTTPException(404,'Creator not found.')
    enabled=0 if row['enabled'] else 1; conn.execute('UPDATE watchlist SET enabled=?,status=? WHERE id=?',(enabled,'Monitoring enabled' if enabled else 'Paused',item_id)); conn.commit(); conn.close(); return {'enabled':bool(enabled)}

@app.get('/api/watchlist/logs')
def watchlist_logs():
    conn=db(); rows=[dict(r) for r in conn.execute('SELECT s.*,w.label,w.platform FROM watchlist_seen s LEFT JOIN watchlist w ON w.id=s.watchlist_id ORDER BY s.seen_at DESC LIMIT 50').fetchall()]; conn.close(); return rows

@app.get('/api/watchlist/clips')
def watchlist_clips():
    conn=db()
    rows=conn.execute('''SELECT s.video_id,s.job_id,s.seen_at,s.status AS watch_status,s.error,w.label,w.platform,j.status AS job_status,j.result_json,j.error AS job_error
        FROM watchlist_seen s LEFT JOIN watchlist w ON w.id=s.watchlist_id LEFT JOIN jobs j ON j.id=s.job_id
        ORDER BY s.seen_at DESC LIMIT 50''').fetchall()
    conn.close(); out=[]
    for row in rows:
        item=dict(row); vid=str(item.get('video_id') or '')
        item['source_url']=(f"https://www.youtube.com/watch?v={vid}" if item.get('platform')=='youtube' else (f"https://www.twitch.tv/videos/{vid}" if vid.isdigit() else f"https://www.twitch.tv/{item.get('label') or ''}"))
        try: results=json.loads(item.pop('result_json') or '[]')
        except Exception: results=[]
        item['clips']=[{'title':c.get('title') or Path(c.get('file','clip')).name,'file':Path(c.get('file','')).name,'url':c.get('url'),'start':c.get('start',0),'end':c.get('end',0),'score':c.get('score')} for c in results]
        out.append(item)
    return out

def _next_daily_publish(settings):
    now=datetime.now(timezone.utc); tz=_get_timezone(settings['timezone']); local=now.astimezone(tz)
    hh,mm=map(int,settings['daily_time'].split(':')); target=local.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if target<=local: target=target + __import__('datetime').timedelta(days=1)
    # Keep only one automatic watchlist post per local calendar day.
    conn=db(); existing=conn.execute("SELECT publish_at FROM scheduled_posts WHERE status IN ('pending','publishing','published') AND title LIKE '[Watchlist]%' ORDER BY publish_at DESC LIMIT 1").fetchone(); conn.close()
    if existing:
        last=datetime.fromisoformat(existing['publish_at'].replace('Z','+00:00')).astimezone(tz)
        if last.date()>=target.date(): target=last.replace(hour=hh,minute=mm,second=0,microsecond=0)+__import__('datetime').timedelta(days=1)
    return target.astimezone(timezone.utc).isoformat()

def _process_watchlist_entry(item, video):
    vid=video.get('id')
    if not vid: return
    conn=db()
    if conn.execute('SELECT 1 FROM watchlist_seen WHERE video_id=?',(vid,)).fetchone(): conn.close(); return
    cur=conn.execute("INSERT INTO watchlist_seen (video_id,watchlist_id,seen_at,status) VALUES (?,?,?,'processing')",(vid,item['id'],datetime.now(timezone.utc).isoformat())); conn.commit(); conn.close()
    url=(f"https://www.youtube.com/watch?v={vid}" if item['platform']=='youtube' else (f"https://www.twitch.tv/videos/{vid}" if str(vid).isdigit() else f"https://www.twitch.tv/{item['label']}"))
    try:
        conn=db(); settings=dict(conn.execute('SELECT * FROM watchlist_settings WHERE id=1').fetchone()); conn.close()
        jid=create_job(url,item['platform'],30,'High-energy highlights',True,5,bool(settings['watermark']),settings['resolution'])
        conn=db(); conn.execute('UPDATE watchlist_seen SET job_id=?,status=? WHERE video_id=?',(jid,'clipping',vid)); conn.commit(); conn.close()
        process_job(jid)
        job=get_job(jid)
        if not job or job['status']!='completed' or not job.get('results'):
            raise RuntimeError((job or {}).get('error') or 'No clips generated')
        # Prefer highest scored clip and schedule one daily post; no unsupported claim of semantic AI understanding.
        best=max(job['results'],key=lambda r:float(r.get('score',0)))
        conn=db(); already=conn.execute("SELECT 1 FROM scheduled_posts WHERE status IN ('pending','publishing','published') AND title LIKE '[Watchlist]%' AND date(publish_at)=date(?) LIMIT 1",(_next_daily_publish(settings),)).fetchone()
        # Convert chosen clip's URL to a safe relative output filename.
        clip_name=Path(best.get('file','')).name
        if not clip_name: raise RuntimeError('Generated clip filename missing')
        publish_at=_next_daily_publish(settings)
        title='[Watchlist] '+(best.get('title') or 'Best moment #Shorts')
        conn.execute('INSERT INTO scheduled_posts (job_id,clip_name,platform,title,description,privacy_level,publish_at,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',(jid,clip_name,settings['platform'],title,'Automatically selected from an authorized Watchlist source.','SELF_ONLY' if settings['platform']=='tiktok' else 'PUBLIC_TO_EVERYONE',publish_at,'pending',datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat()))
        conn.execute("UPDATE watchlist_seen SET status=? WHERE video_id=?",('scheduled for '+publish_at,vid)); conn.execute('UPDATE watchlist SET status=?,last_video_id=?,error=NULL WHERE id=?',('Latest replay clipped and best clip scheduled',vid,item['id'])); conn.commit(); conn.close()
    except Exception as e:
        conn=db(); conn.execute('UPDATE watchlist_seen SET status=?,error=? WHERE video_id=?',('failed',str(e)[:1000],vid)); conn.execute('UPDATE watchlist SET status=?,error=? WHERE id=?',('Check failed',str(e)[:1000],item['id'])); conn.commit(); conn.close()

def _watchlist_poll_once():
    import yt_dlp
    conn=db(); settings=dict(conn.execute('SELECT * FROM watchlist_settings WHERE id=1').fetchone()); rows=[dict(r) for r in conn.execute('SELECT * FROM watchlist WHERE enabled=1 AND authorized=1').fetchall()]; conn.close()
    if not settings.get('enabled'): return
    for item in rows:
        try:
            opts={'quiet':True,'no_warnings':True,'extract_flat':'in_playlist','playlistend':5,'skip_download':True,'ignoreerrors':True,'socket_timeout':15}
            with yt_dlp.YoutubeDL(opts) as ydl: info=ydl.extract_info(item['url'],download=False)
            entries=(info or {}).get('entries') or []
            # Entries are newest-first for channel videos/replays; skip active livestream entries.
            candidates=[]
            for v in entries:
                if not v or not v.get('id'): continue
                if v.get('is_live') or v.get('live_status') in ('is_live','is_upcoming'): continue
                candidates.append(v)
            if candidates:
                if candidates: _process_watchlist_entry(item,candidates[0])
            conn=db(); conn.execute('UPDATE watchlist SET last_checked=?,error=NULL WHERE id=?',(datetime.now(timezone.utc).isoformat(),item['id'])); conn.commit(); conn.close()
        except Exception as e:
            conn=db(); conn.execute('UPDATE watchlist SET last_checked=?,error=?,status=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),str(e)[:1000],'Monitoring error',item['id'])); conn.commit(); conn.close()

def _watchlist_loop():
    while not _scheduler_stop.is_set():
        try:
            if _watchlist_lock.acquire(blocking=False):
                try: _watchlist_poll_once()
                finally: _watchlist_lock.release()
        except Exception:
            pass
        if _scheduler_stop.wait(300):
            break






def _oauth_db():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA / "oauth_states.sqlite", timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            expires_at REAL NOT NULL,
            code_verifier TEXT
        )
    """)
    # Upgrade an existing database created by the previous version.
    columns = {
        row[1] for row in
        conn.execute("PRAGMA table_info(oauth_states)").fetchall()
    }
    if "code_verifier" not in columns:
        conn.execute(
            "ALTER TABLE oauth_states ADD COLUMN code_verifier TEXT"
        )
        conn.commit()
    return conn


def _save_oauth_state(
    state, platform, redirect_uri, code_verifier=None
):
    conn = _oauth_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO oauth_states "
            "(state, platform, redirect_uri, expires_at, code_verifier) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                state,
                platform,
                redirect_uri,
                time.time() + 600,
                code_verifier
            )
        )
        conn.commit()
    finally:
        conn.close()


def _consume_oauth_state(state, platform):
    conn = _oauth_db()
    try:
        row = conn.execute(
            "SELECT redirect_uri, expires_at, code_verifier "
            "FROM oauth_states WHERE state=? AND platform=?",
            (state, platform)
        ).fetchone()

        conn.execute(
            "DELETE FROM oauth_states WHERE state=? AND platform=?",
            (state, platform)
        )
        conn.commit()
    finally:
        conn.close()

    if not row or row[1] < time.time():
        return None

    return {
        "redirect_uri": row[0],
        "code_verifier": row[2]
    }


@app.get('/oauth/youtube/start')
def youtube_oauth_start():
    from google_auth_oauthlib.flow import Flow
    from fastapi.responses import RedirectResponse

    secrets_file = os.getenv(
        'YOUTUBE_CLIENT_SECRETS_FILE', ''
    ).strip()

    if not secrets_file:
        raise HTTPException(
            400,
            'Set YOUTUBE_CLIENT_SECRETS_FILE in your environment.'
        )

    path = Path(secrets_file)
    if not path.is_absolute():
        path = ROOT / path

    if not path.exists():
        raise HTTPException(
            400,
            f'YouTube OAuth JSON file not found: {path}'
        )

    redirect = (
        os.getenv(
            'PUBLIC_BASE_URL',
            'http://localhost:8000'
        ).rstrip('/')
        + '/oauth/youtube/callback'
    )

    flow = Flow.from_client_secrets_file(
        str(path),
        scopes=['https://www.googleapis.com/auth/youtube.upload'],
        redirect_uri=redirect,
        autogenerate_code_verifier=True
    )

    url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )

    _save_oauth_state(
        state,
        'youtube',
        redirect,
        flow.code_verifier
    )

    return RedirectResponse(url)


@app.get('/oauth/youtube/callback')
def youtube_oauth_callback(code: str, state: str):
    from google_auth_oauthlib.flow import Flow
    from fastapi.responses import RedirectResponse

    item = _consume_oauth_state(state, 'youtube')
    if not item:
        raise HTTPException(
            400,
            'OAuth state expired. Please try connecting again.'
        )

    secrets_file = os.getenv(
        'YOUTUBE_CLIENT_SECRETS_FILE', ''
    ).strip()

    if not secrets_file:
        raise HTTPException(
            400,
            'Set YOUTUBE_CLIENT_SECRETS_FILE in your environment.'
        )

    path = Path(secrets_file)
    if not path.is_absolute():
        path = ROOT / path

    if not path.exists():
        raise HTTPException(
            400,
            f'YouTube OAuth JSON file not found: {path}'
        )

    verifier = item.get('code_verifier')
    if not verifier:
        raise HTTPException(
            400,
            'Missing saved OAuth code verifier. Please reconnect YouTube.'
        )

    flow = Flow.from_client_secrets_file(
        str(path),
        scopes=['https://www.googleapis.com/auth/youtube.upload'],
        redirect_uri=item['redirect_uri'],
        autogenerate_code_verifier=True
    )

    flow.code_verifier = verifier

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials

        token_path = ROOT / 'data' / 'youtube-token.json'
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(
            json.dumps(
                {'credentials': json.loads(creds.to_json())},
                indent=2
            ),
            encoding='utf-8'
        )
    except Exception as e:
        raise HTTPException(
            400,
            f'YouTube OAuth failed: {e}'
        )

    return RedirectResponse('/#connections?youtube=connected')

@app.get('/oauth/tiktok/start')
def tiktok_oauth_start():
    import secrets
    from fastapi.responses import RedirectResponse

    key = os.getenv('TIKTOK_CLIENT_KEY', '').strip()
    secret = os.getenv('TIKTOK_CLIENT_SECRET', '').strip()

    if not key or not secret:
        raise HTTPException(
            400,
            'Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in your environment.'
        )

    redirect = (
        os.getenv('PUBLIC_BASE_URL', 'http://localhost:8000').rstrip('/')
        + '/oauth/tiktok/callback'
    )

    state = secrets.token_urlsafe(24)
    _save_oauth_state(state, 'tiktok', redirect)

    params = {
        'client_key': key,
        'response_type': 'code',
        'scope': 'user.info.basic,video.publish,video.upload',
        'redirect_uri': redirect,
        'state': state
    }

    return RedirectResponse(
        'https://www.tiktok.com/v2/auth/authorize/?'
        + urlencode(params)
    )


@app.get('/oauth/tiktok/callback')
def tiktok_oauth_callback(code: str, state: str):
    from fastapi.responses import RedirectResponse

    item = _consume_oauth_state(state, 'tiktok')
    if not item:
        raise HTTPException(
            400,
            'OAuth state expired. Please try connecting again.'
        )

    try:
        publishing.exchange_tiktok_code(
            code,
            item['redirect_uri']
        )
    except Exception as e:
        raise HTTPException(
            400,
            f'TikTok OAuth failed: {e}'
        )

    return RedirectResponse('/#connections?tiktok=connected')

class PublishRequest(BaseModel):
    title: str = 'Funny moment #shorts'
    description: str = ''
    privacy_level: str = 'PUBLIC_TO_EVERYONE'

class ScheduledPostRequest(BaseModel):
    job_id: str
    clip_name: str
    platform: str
    title: str = Field(min_length=1, max_length=100)
    description: str = ''
    privacy_level: str = 'PUBLIC_TO_EVERYONE'
    publish_at: datetime

@app.get('/api/scheduled-posts')
def list_scheduled_posts():
    conn=db(); rows=conn.execute('SELECT * FROM scheduled_posts ORDER BY publish_at DESC LIMIT 200').fetchall(); conn.close()
    out=[]
    for row in rows:
        item=dict(row)
        if item.get('result_json'):
            try: item['result']=json.loads(item.pop('result_json'))
            except Exception: item['result']=None
        else: item.pop('result_json',None)
        out.append(item)
    return out

@app.post('/api/scheduled-posts', status_code=201)
def create_scheduled_post(body: ScheduledPostRequest):
    if body.platform not in ('youtube','tiktok'):
        raise HTTPException(400, 'Platform must be youtube or tiktok.')
    if not re.fullmatch(r'clip_\d{2}\.mp4', body.clip_name):
        raise HTTPException(400, 'Invalid clip name.')
    if body.publish_at.tzinfo is None:
        raise HTTPException(400, 'Choose a date and time with a timezone offset.')
    if body.publish_at <= datetime.now(timezone.utc):
        raise HTTPException(400, 'Scheduled time must be in the future.')
    job=get_job(body.job_id)
    if not job or job.get('status') != 'completed':
        raise HTTPException(400, 'The source job must be completed before scheduling a clip.')
    path=(OUTPUT/body.job_id/body.clip_name).resolve()
    if not path.is_relative_to(OUTPUT.resolve()) or not path.is_file():
        raise HTTPException(404, 'Generated clip not found.')
    if body.platform == 'youtube' and not publishing.youtube_connected():
        raise HTTPException(400, 'Connect YouTube before scheduling posts.')
    if body.platform == 'tiktok' and not publishing.tiktok_connected():
        raise HTTPException(400, 'Connect TikTok before scheduling posts.')
    now=datetime.now(timezone.utc).isoformat()
    conn=db(); cur=conn.execute('INSERT INTO scheduled_posts (job_id,clip_name,platform,title,description,privacy_level,publish_at,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)', (body.job_id,body.clip_name,body.platform,body.title.strip(),body.description,body.privacy_level,body.publish_at.astimezone(timezone.utc).isoformat(),'pending',now,now)); conn.commit(); pid=cur.lastrowid; conn.close()
    return {'id':pid,'status':'pending','publish_at':body.publish_at.astimezone(timezone.utc).isoformat(),'message':'Saved. The background worker will publish it when due while this server is running.'}

@app.delete('/api/scheduled-posts/{post_id}')
def cancel_scheduled_post(post_id: int):
    conn=db(); row=conn.execute('SELECT status FROM scheduled_posts WHERE id=?',(post_id,)).fetchone()
    if not row:
        conn.close(); raise HTTPException(404,'Scheduled post not found.')
    if row['status'] != 'pending':
        conn.close(); raise HTTPException(409,'Only pending posts can be cancelled.')
    conn.execute("UPDATE scheduled_posts SET status='cancelled', updated_at=? WHERE id=?",(datetime.now(timezone.utc).isoformat(),post_id)); conn.commit(); conn.close()
    return {'id':post_id,'status':'cancelled'}

@app.post('/api/publish/{platform}/{job_id}/{clip_name}')
def publish_clip(platform: str, job_id: str, clip_name: str, body: PublishRequest):
    if platform not in ('youtube','tiktok'): raise HTTPException(400,'Platform must be youtube or tiktok.')
    # Prevent path traversal; only serve generated clip files under this job output directory.
    if not re.fullmatch(r'clip_\d{2}\.mp4',clip_name): raise HTTPException(400,'Invalid clip name.')
    path=(OUTPUT/job_id/clip_name).resolve()
    if not path.is_relative_to(OUTPUT.resolve()) or not path.exists(): raise HTTPException(404,'Generated clip not found.')
    try:
        if platform=='youtube': result=publishing.upload_youtube(path,body.title,body.description)
        else: result=publishing.publish_tiktok(path,body.title,body.privacy_level)
        return result
    except Exception as e: raise HTTPException(400,str(e))

@app.get('/api/publishing/status')
def publishing_status():
    yt=publishing.youtube_connected(); tt=publishing.tiktok_connected()
    return {'youtube':{'configured':bool(os.getenv('YOUTUBE_CLIENT_SECRETS_FILE')),'ready':yt,'reason':'Connected' if yt else 'Create a Google OAuth client and use Connect YouTube.'},'tiktok':{'configured':bool(os.getenv('TIKTOK_CLIENT_KEY') and os.getenv('TIKTOK_CLIENT_SECRET')),'ready':tt,'reason':'Connected' if tt else 'Configure an approved TikTok developer app and use Connect TikTok.'},'automatic_publishing_enabled':True,'note':'A background worker processes due scheduled posts while the server is running. TikTok public posting still requires approved Content Posting API access and audit.'}
