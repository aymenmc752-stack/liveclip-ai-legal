"""Local OAuth and publishing helpers for LiveClip AI.

Tokens are stored locally under data/. Keep this app bound to localhost unless
proper authentication and HTTPS are added.
"""
from __future__ import annotations
import json, os, math, mimetypes
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
YT_TOKEN = DATA / 'youtube-token.json'
TT_TOKEN = DATA / 'tiktok-token.json'


def _read(path):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except Exception: return {}

def _write(path, data):
    DATA.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding='utf-8')

def youtube_connected():
    d = _read(YT_TOKEN)
    return bool(d.get('credentials'))

def tiktok_connected():
    d = _read(TT_TOKEN)
    return bool(d.get('access_token'))

def youtube_credentials(): return _read(YT_TOKEN).get('credentials')
def tiktok_token(): return _read(TT_TOKEN).get('access_token')

def upload_youtube(video_path: Path, title: str, description: str = '', publish_at: str | None = None):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    data = _read(YT_TOKEN)
    raw = data.get('credentials')
    if not raw: raise RuntimeError('YouTube is not connected. Use Connect YouTube first.')
    creds = Credentials.from_authorized_user_info(raw, ['https://www.googleapis.com/auth/youtube.upload'])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        data['credentials'] = json.loads(creds.to_json()); _write(YT_TOKEN, data)
    youtube = build('youtube', 'v3', credentials=creds, cache_discovery=False)
    # Immediate uploads should be public. Scheduled queue uploads only when the scheduled time is due.
    status = {'privacyStatus': 'public'}
    if publish_at: status['publishAt'] = publish_at
    body = {'snippet': {'title': title[:100], 'description': description[:5000], 'categoryId': '24'}, 'status': status}
    req = youtube.videos().insert(part='snippet,status', body=body, media_body=MediaFileUpload(str(video_path), chunksize=8*1024*1024, resumable=True))
    response = None
    while response is None:
        _, response = req.next_chunk()
    return {'platform':'youtube','id':response.get('id'),'url':'https://www.youtube.com/watch?v='+response.get('id',''),'status':'uploaded_private' if publish_at is None else 'scheduled'}

def exchange_tiktok_code(code: str, redirect_uri: str):
    key=os.getenv('TIKTOK_CLIENT_KEY',''); secret=os.getenv('TIKTOK_CLIENT_SECRET','')
    if not key or not secret: raise RuntimeError('Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env first.')
    r=requests.post('https://open.tiktokapis.com/v2/oauth/token/', data={'client_key':key,'client_secret':secret,'code':code,'grant_type':'authorization_code','redirect_uri':redirect_uri}, timeout=30)
    r.raise_for_status(); payload=r.json()
    if payload.get('error'): raise RuntimeError(str(payload['error']))
    _write(TT_TOKEN, payload)
    return payload

def publish_tiktok(video_path: Path, title: str, privacy_level: str = 'PUBLIC_TO_EVERYONE'):
    token=tiktok_token()
    if not token: raise RuntimeError('TikTok is not connected. Use Connect TikTok first.')
    # TikTok requires querying creator info and using a privacy option returned by that endpoint.
    headers={'Authorization':f'Bearer {token}','Content-Type':'application/json; charset=UTF-8'}
    info=requests.post('https://open.tiktokapis.com/v2/post/publish/creator_info/query/',headers=headers,json={},timeout=30)
    info.raise_for_status(); infoj=info.json()
    if infoj.get('error',{}).get('code') not in ('ok',None): raise RuntimeError(str(infoj['error']))
    options=infoj.get('data',{}).get('privacy_level_options',[])
    if privacy_level not in options:
        privacy_level=options[-1] if options else 'SELF_ONLY'
    size=video_path.stat().st_size
    chunk_size=min(size, 10*1024*1024)
    total=max(1, math.ceil(size/chunk_size))
    init=requests.post('https://open.tiktokapis.com/v2/post/publish/video/init/',headers=headers,json={
        'post_info':{'title':title[:2200],'privacy_level':privacy_level,'disable_duet':False,'disable_comment':False,'disable_stitch':False},
        'source_info':{'source':'FILE_UPLOAD','video_size':size,'chunk_size':chunk_size,'total_chunk_count':total}
    },timeout=30)
    init.raise_for_status(); j=init.json()
    if j.get('error',{}).get('code')!='ok': raise RuntimeError(str(j.get('error')))
    upload_url=j['data']['upload_url']; start=0
    with video_path.open('rb') as f:
        while start<size:
            chunk=f.read(chunk_size); end=start+len(chunk)-1
            rr=requests.put(upload_url, data=chunk, headers={'Content-Type':'video/mp4','Content-Length':str(len(chunk)),'Content-Range':f'bytes {start}-{end}/{size}'}, timeout=180)
            rr.raise_for_status(); start=end+1
    return {'platform':'tiktok','publish_id':j['data'].get('publish_id'),'status':'submitted','note':'TikTok processes posts asynchronously; use the returned publish ID to check status.'}
